import os
import sys
import time
import copy
import subprocess
import swsscommon as swsscommon_package
from sonic_py_common import device_info
from swsscommon import swsscommon

from parameterized import parameterized
from sonic_py_common.general import load_module_from_source
from unittest import TestCase, mock

from .test_vectors import FEATURED_TEST_VECTOR, FEATURE_DAEMON_CFG_DB
from tests.common.mock_configdb import MockConfigDb, MockDBConnector, MockSubscriberStateTable, MockSelect
from tests.common.mock_restart_waiter import MockRestartWaiter

from pyfakefs.fake_filesystem_unittest import patchfs, Patcher
from deepdiff import DeepDiff
from unittest.mock import call

test_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
modules_path = os.path.dirname(test_path)
scripts_path = os.path.join(modules_path, 'scripts')
sys.path.insert(0, modules_path)

# Load the file under test
featured_path = os.path.join(scripts_path, 'featured')
featured = load_module_from_source('featured', featured_path)
featured.ConfigDBConnector = MockConfigDb
featured.DBConnector = MockDBConnector
featured.Table = mock.Mock()
swsscommon.Select = MockSelect
swsscommon.SubscriberStateTable = MockSubscriberStateTable
swsscommon.RestartWaiter = MockRestartWaiter

def syslog_side_effect(pri, msg): 
    print(f"{pri}: {msg}")

class TestFeatureHandler(TestCase):
    """Test methods of `FeatureHandler` class.
    """
    def checks_config_table(self, feature_table, expected_table):
        """Compares `FEATURE` table in `CONFIG_DB` with expected output table.

        Args:
            feature_table: A dictionary indicates current `FEATURE` table in `CONFIG_DB`.
            expected_table A dictionary indicates the expected `FEATURE` table in `CONFIG_DB`.

        Returns:
            Returns True if `FEATURE` table in `CONFIG_DB` was not modified unexpectedly;
            otherwise, returns False.
        """
        ddiff = DeepDiff(feature_table, expected_table, ignore_order=True)

        return True if not ddiff else False

    def checks_systemd_config_file(self, device_type, feature_table, feature_systemd_name_map=None):
        """Checks whether the systemd configuration file of each feature was created or not
        and whether the `Restart=` field in the file is set correctly or not.

        Args:
            feature_table: A dictionary indicates `Feature` table in `CONFIG_DB`.

        Returns: Boolean value indicates whether test passed or not.
        """

        truth_table = {'enabled': 'always',
                       'disabled': 'no'}

        systemd_config_file_path = os.path.join(featured.FeatureHandler.SYSTEMD_SERVICE_CONF_DIR,
                                                'auto_restart.conf')

        for feature_name in feature_table:
            is_dependent_feature = True if feature_name in ['syncd', 'gbsyncd'] else False
            auto_restart_status = feature_table[feature_name].get('auto_restart', 'disabled')
            if "enabled" in auto_restart_status:
                auto_restart_status = "enabled"
            elif "disabled" in auto_restart_status:
                auto_restart_status = "disabled"

            feature_systemd_list = feature_systemd_name_map[feature_name] if feature_systemd_name_map else [feature_name]

            for feature_systemd in feature_systemd_list:
                feature_systemd_config_file_path = systemd_config_file_path.format(feature_systemd)
                is_config_file_existing = os.path.exists(feature_systemd_config_file_path)
                assert is_config_file_existing, "Systemd configuration file of feature '{}' does not exist!".format(feature_systemd)

                with open(feature_systemd_config_file_path) as systemd_config_file:
                    status = systemd_config_file.read().strip()
                    if device_type == 'SpineRouter' and is_dependent_feature:
                        assert status == '[Service]\nRestart=no'
                    else:
                        assert status == '[Service]\nRestart={}'.format(truth_table[auto_restart_status])

    def get_state_db_set_calls(self, feature_table):
        """Returns a Mock call objects which recorded the `set` calls to `FEATURE` table in `STATE_DB`.

        Args:
            feature_table: A dictionary indicates `FEATURE` table in `CONFIG_DB`.

        Returns:
            set_call_list: A list indicates Mock call objects.
        """
        set_call_list = []

        for feature_name in feature_table.keys():
            feature_state = ""
            if "enabled" in feature_table[feature_name]["state"]:
                feature_state = "enabled"
            elif "disabled" in feature_table[feature_name]["state"]:
                feature_state = "disabled"
            else:
                feature_state = feature_table[feature_name]["state"]

            set_call_list.append(mock.call(feature_name, [("state", feature_state)]))

        return set_call_list

    @parameterized.expand(FEATURED_TEST_VECTOR)
    @patchfs
    def test_sync_state_field(self, test_scenario_name, config_data, fs):
        """Tests the method `sync_state_field(...)` of `FeatureHandler` class.

        Args:
            test_secnario_name: A string indicates different testing scenario.
            config_data: A dictionary contains initial `CONFIG_DB` tables and expected results.

        Returns:
            Boolean value indicates whether test will pass or not.
        """
        # add real path of sesscommon for database_config.json
        fs.add_real_paths(swsscommon_package.__path__)
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)

        MockConfigDb.set_config_db(config_data['config_db'])
        feature_state_table_mock = mock.Mock()
        with mock.patch('featured.subprocess') as mocked_subprocess:
            with mock.patch("sonic_py_common.device_info.get_device_runtime_metadata", return_value=config_data['device_runtime_metadata']):
                with mock.patch("sonic_py_common.device_info.is_multi_npu", return_value=True if 'num_npu' in config_data else False):
                    with mock.patch("sonic_py_common.device_info.get_num_npus", return_value=config_data['num_npu'] if 'num_npu' in config_data else 1):
                        with mock.patch("sonic_py_common.device_info.get_namespaces", return_value=["asic{}".format(a) for a in  range(config_data['num_npu'])] if 'num_npu' in config_data else []):
                            popen_mock = mock.Mock()
                            attrs = config_data['popen_attributes']
                            popen_mock.configure_mock(**attrs)
                            mocked_subprocess.Popen.return_value = popen_mock

                            device_config = {}
                            device_config['DEVICE_METADATA'] = MockConfigDb.CONFIG_DB['DEVICE_METADATA']
                            device_config.update(config_data['device_runtime_metadata'])
                            device_type = MockConfigDb.CONFIG_DB['DEVICE_METADATA']['localhost']['type']

                            feature_handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock,
                                                                      device_config, False)
                            feature_handler.is_delayed_enabled = True
                            feature_table = MockConfigDb.CONFIG_DB['FEATURE']
                            feature_handler.sync_state_field(feature_table)

                            feature_systemd_name_map = {}
                            for feature_name in feature_table.keys():
                                feature = featured.Feature(feature_name, feature_table[feature_name], device_config)
                                feature_names, _ = feature_handler.get_multiasic_feature_instances(feature)
                                feature_systemd_name_map[feature_name] = feature_names

                            is_any_difference = self.checks_config_table(MockConfigDb.get_config_db()['FEATURE'],
                                                                         config_data['expected_config_db']['FEATURE'])
                            assert is_any_difference, "'FEATURE' table in 'CONFIG_DB' is modified unexpectedly!"

                            if 'num_npu' in config_data:
                                for ns in range(config_data['num_npu']):
                                    namespace = "asic{}".format(ns)
                                    is_any_difference = self.checks_config_table(feature_handler.ns_cfg_db[namespace].get_config_db()['FEATURE'],
                                                                                 config_data['expected_config_db']['FEATURE'])
                                    assert is_any_difference, "'FEATURE' table in 'CONFIG_DB' in namespace {} is modified unexpectedly!".format(namespace)

                            feature_table_state_db_calls = self.get_state_db_set_calls(feature_table)

                            self.checks_systemd_config_file(device_type, config_data['config_db']['FEATURE'], feature_systemd_name_map)
                            mocked_subprocess.run.assert_has_calls(config_data['enable_feature_subprocess_calls'],
                                                                          any_order=True)
                            mocked_subprocess.run.assert_has_calls(config_data['daemon_reload_subprocess_call'],
                                                                          any_order=True)
                            feature_state_table_mock.set.assert_has_calls(feature_table_state_db_calls)
                            self.checks_systemd_config_file(device_type, config_data['config_db']['FEATURE'], feature_systemd_name_map)

    @parameterized.expand(FEATURED_TEST_VECTOR)
    @patchfs
    def test_handler(self, test_scenario_name, config_data, fs):
        """Tests the method `handle(...)` of `FeatureHandler` class.

        Args:
            test_secnario_name: A string indicates different testing scenario.
            config_data: A dictionary contains initial `CONFIG_DB` tables and expected results.

        Returns:
            Boolean value indicates whether test will pass or not.
        """
        # add real path of sesscommon for database_config.json
        fs.add_real_paths(swsscommon_package.__path__)
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)

        MockConfigDb.set_config_db(config_data['config_db'])
        feature_state_table_mock = mock.Mock()
        with mock.patch('featured.subprocess') as mocked_subprocess:
            with mock.patch("sonic_py_common.device_info.get_device_runtime_metadata", return_value=config_data['device_runtime_metadata']):
                with mock.patch("sonic_py_common.device_info.is_multi_npu", return_value=True if 'num_npu' in config_data else False):
                    with mock.patch("sonic_py_common.device_info.get_num_npus", return_value=config_data['num_npu'] if 'num_npu' in config_data else 1):
                        popen_mock = mock.Mock()
                        attrs = config_data['popen_attributes']
                        popen_mock.configure_mock(**attrs)
                        mocked_subprocess.Popen.return_value = popen_mock

                        device_config = {}
                        device_config['DEVICE_METADATA'] = MockConfigDb.CONFIG_DB['DEVICE_METADATA']
                        device_config.update(config_data['device_runtime_metadata'])
                        device_type = MockConfigDb.CONFIG_DB['DEVICE_METADATA']['localhost']['type']
                        feature_handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock,
                                                                  device_config, False)
                        feature_handler.is_delayed_enabled = True

                        feature_table = MockConfigDb.CONFIG_DB['FEATURE']

                        feature_systemd_name_map = {}
                        for feature_name, feature_config in feature_table.items():
                            feature_handler.handler(feature_name, 'SET', feature_config)
                            feature = featured.Feature(feature_name, feature_table[feature_name], device_config)
                            feature_names, _ = feature_handler.get_multiasic_feature_instances(feature)
                            feature_systemd_name_map[feature_name] = feature_names

                        self.checks_systemd_config_file(device_type, config_data['config_db']['FEATURE'], feature_systemd_name_map)
                        mocked_subprocess.run.assert_has_calls(config_data['enable_feature_subprocess_calls'],
                                                                      any_order=True)
                        mocked_subprocess.run.assert_has_calls(config_data['daemon_reload_subprocess_call'],
                                                                      any_order=True)

    def test_feature_config_parsing(self):
        swss_feature = featured.Feature('swss', {
            'state': 'enabled',
            'auto_restart': 'enabled',
            'delayed': 'True',
            'has_global_scope': 'False',
            'has_per_asic_scope': 'True',
        })

        assert swss_feature.name == 'swss'
        assert swss_feature.state == 'enabled'
        assert swss_feature.auto_restart == 'enabled'
        assert swss_feature.delayed
        assert not swss_feature.has_global_scope
        assert swss_feature.has_per_asic_scope

    def test_feature_config_parsing_defaults(self):
        swss_feature = featured.Feature('swss', {
            'state': 'enabled',
        })

        assert swss_feature.name == 'swss'
        assert swss_feature.state == 'enabled'
        assert swss_feature.auto_restart == 'disabled'
        assert not swss_feature.delayed
        assert swss_feature.has_global_scope
        assert not swss_feature.has_per_asic_scope
    
    @mock.patch('featured.FeatureHandler.update_systemd_config', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.update_feature_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.sync_feature_scope', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.sync_feature_delay_state', mock.MagicMock())
    def test_feature_resync(self):
        mock_db = mock.MagicMock()
        mock_db.get_entry = mock.MagicMock()
        mock_db.mod_entry = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature_table = {
            'sflow': {
                'state': 'enabled',
                'auto_restart': 'enabled',
                'delayed': 'True',
                'has_global_scope': 'False',
                'has_per_asic_scope': 'True',
            }
        }
        mock_db.get_entry.return_value = None
        feature_handler.sync_state_field(feature_table)
        # Missing FEATURE row: resync_feature_state skips mod_entry (avoid recreating a row removed concurrently).
        mock_db.mod_entry.assert_not_called()
        mock_db.mod_entry.reset_mock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        mock_db.get_entry.return_value = {
            'state': 'disabled',
        }
        feature_handler.sync_state_field(feature_table)
        mock_db.mod_entry.assert_not_called()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature_table = {
            'sflow': {
                'state': 'always_enabled',
                'auto_restart': 'enabled',
                'delayed': 'True',
                'has_global_scope': 'False',
                'has_per_asic_scope': 'True',
            }
        }
        feature_handler.sync_state_field(feature_table)
        mock_db.mod_entry.assert_called_with('FEATURE', 'sflow', {'state': 'always_enabled'})
        mock_db.mod_entry.reset_mock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        mock_db.get_entry.return_value = {
            'state': 'some template',
        }
        feature_table = {
            'sflow': {
                'state': 'enabled',
                'auto_restart': 'enabled',
                'delayed': 'True',
                'has_global_scope': 'False',
                'has_per_asic_scope': 'True',
            }
        }
        feature_handler.sync_state_field(feature_table)
        mock_db.mod_entry.assert_called_with('FEATURE', 'sflow', {'state': 'enabled'})
        mock_db.mod_entry.reset_mock()

        # Partial entry (no 'state' key): left behind by sync_feature_scope racing with
        # deregister.  resync_feature_state must delete it rather than completing it.
        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        mock_db.get_entry.return_value = {
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
        }
        mock_db.set_entry = mock.MagicMock()
        feature_handler.sync_state_field(feature_table)
        mock_db.set_entry.assert_called_with('FEATURE', 'sflow', None)
        mock_db.mod_entry.assert_not_called()

    def test_sync_feature_scope_toctou(self):
        """sync_feature_scope must delete a partial entry it accidentally creates when
        the FEATURE row is deleted by a concurrent deregister between the mod_entry and
        the post-write existence check (TOCTOU)."""
        mock_db = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature = featured.Feature('sflow', {
            'state': 'disabled',
            'has_global_scope': 'True',
            'has_per_asic_scope': 'False',
            'delayed': 'False',
        })

        # Post-write check sees a partial entry (no 'state' key): the FEATURE row was
        # deleted by deregister and then recreated by mod_entry during the race
        # window, leaving only the scope fields.
        mock_db.get_entry.return_value = {
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
        }

        feature_handler.sync_feature_scope(feature)
        mock_db.set_entry.assert_called_with(featured.FEATURE_TBL, 'sflow', None)

    def test_port_init_done_twice(self):
        """There could be multiple "PortInitDone" event in case of swss
        restart(either due to crash or due to manual operation). swss
        restarting would cause all services that depend on it to be stopped.
        Those stopped services which have delayed=True will not be auto
        restarted by systemd, featured is responsible for enabling those services
        when swss is ready. This test case covers it.
        """
        feature_handler = featured.FeatureHandler(None, None, {}, False)
        assert not feature_handler.is_delayed_enabled
        feature_handler.port_listener(key='PortInitDone', op='SET', data=None)
        assert feature_handler.is_delayed_enabled
        
        feature_handler.enable_delayed_services = mock.MagicMock()
        feature_handler.port_listener(key='PortInitDone', op='SET', data=None)
        feature_handler.enable_delayed_services.assert_called_once()

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_enable_and_disable_feature_ExclusionList_skips_actions(self, mock_syslog):
        """Verify that ExclusionList feature 'frr_bmp' is skipped in both enable and disable."""
        feature_state_table_mock = mock.Mock()
        device_cfg = {"DEVICE_METADATA": {"localhost": {"type": "FixedSwitch"}}}
        handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock, device_cfg, False)

        feat_cfg = {"state": "enabled", "auto_restart": "enabled"}
        feature = featured.Feature("frr_bmp", feat_cfg, device_cfg)

        with mock.patch.object(handler, "get_multiasic_feature_instances",
                            return_value=(["frr_bmp"], ["service"])), \
            mock.patch.object(handler, "get_systemd_unit_state", return_value="disabled"), \
            mock.patch("featured.run_cmd") as mocked_run_cmd, \
            mock.patch.object(handler, "set_feature_state") as mocked_set_state:

            # --- enable_feature() ---
            handler.enable_feature(feature)

            mocked_run_cmd.assert_not_called()
            mocked_set_state.assert_not_called()
            assert any("ExclusionList: skip enabling 'frr_bmp'" in str(c.args[1]) for c in mock_syslog.call_args_list)

        mock_syslog.reset_mock()
        with mock.patch.object(handler, "get_multiasic_feature_instances",
                            return_value=(["frr_bmp"], ["service"])), \
            mock.patch.object(handler, "get_systemd_unit_state", return_value="enabled"), \
            mock.patch("featured.run_cmd") as mocked_run_cmd, \
            mock.patch.object(handler, "set_feature_state") as mocked_set_state:

            # --- disable_feature() ---
            handler.disable_feature(feature)

            mocked_run_cmd.assert_not_called()
            mocked_set_state.assert_not_called()
            assert any("ExclusionList: skip disabling 'frr_bmp'" in str(c.args[1]) for c in mock_syslog.call_args_list)

    @mock.patch('featured.FeatureHandler.update_systemd_config', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.update_feature_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.sync_feature_delay_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.get_systemd_unit_state', mock.MagicMock(return_value=""))
    def test_sync_feature_scope_conditional_write(self):
        """Verify sync_feature_scope only writes when scope values differ or entry is missing."""
        mock_db = mock.MagicMock()
        mock_db.get_entry = mock.MagicMock()
        mock_db.mod_entry = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)

        feature = featured.Feature('sflow', {
            'state': 'enabled',
            'has_global_scope': 'True',
            'has_per_asic_scope': 'False',
        })

        # Values already match -> no write
        mock_db.get_entry.return_value = {
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
        }
        feature_handler.sync_feature_scope(feature)
        mock_db.mod_entry.assert_not_called()
        mock_db.mod_entry.reset_mock()

        # Only has_per_asic_scope differs -> write only changed field in single call
        mock_db.get_entry.return_value = {
            'has_per_asic_scope': 'True',
            'has_global_scope': 'True',
        }
        feature_handler.sync_feature_scope(feature)
        mock_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {'has_per_asic_scope': 'False'})
        mock_db.mod_entry.reset_mock()

        # Entry missing (None) -> write both fields in single call
        mock_db.get_entry.return_value = None
        feature_handler.sync_feature_scope(feature)
        mock_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {
            'has_per_asic_scope': 'False',
            'has_global_scope': 'True',
        })

    @mock.patch('featured.FeatureHandler.update_systemd_config', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.update_feature_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.sync_feature_delay_state', mock.MagicMock())
    @mock.patch('featured.FeatureHandler.get_systemd_unit_state', mock.MagicMock(return_value=""))
    def test_sync_feature_scope_namespace_dbs(self):
        """Verify sync_feature_scope propagates writes to per-namespace DBs on multi-ASIC."""
        mock_db = mock.MagicMock()
        mock_db.get_entry = mock.MagicMock()
        mock_db.mod_entry = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        mock_ns_db_0 = mock.MagicMock()
        mock_ns_db_1 = mock.MagicMock()
        feature_handler.ns_cfg_db = {'asic0': mock_ns_db_0, 'asic1': mock_ns_db_1}

        feature = featured.Feature('sflow', {
            'state': 'enabled',
            'has_global_scope': 'True',
            'has_per_asic_scope': 'True',
        })

        # Host differs -> both host and namespaces should be written
        mock_db.get_entry.return_value = {
            'state': 'enabled',
            'has_per_asic_scope': 'False',
            'has_global_scope': 'False',
        }
        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.get_entry.return_value = {
                'has_per_asic_scope': 'False',
                'has_global_scope': 'False',
            }
        feature_handler.sync_feature_scope(feature)

        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {
                'has_per_asic_scope': 'True',
                'has_global_scope': 'True',
            })

        # Reset for next scenario
        mock_db.mod_entry.reset_mock()
        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.mod_entry.reset_mock()

        # Host matches but namespaces are stale -> namespaces should still be written
        mock_db.get_entry.return_value = {
            'state': 'enabled',
            'has_per_asic_scope': 'True',
            'has_global_scope': 'True',
        }
        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.get_entry.return_value = {
                'has_per_asic_scope': 'False',
                'has_global_scope': 'False',
            }
        feature_handler.sync_feature_scope(feature)

        mock_db.mod_entry.assert_not_called()
        for mock_ns_db in [mock_ns_db_0, mock_ns_db_1]:
            mock_ns_db.mod_entry.assert_called_once_with(featured.FEATURE_TBL, 'sflow', {
                'has_per_asic_scope': 'True',
                'has_global_scope': 'True',
            })


@mock.patch("syslog.syslog", side_effect=syslog_side_effect)
@mock.patch('sonic_py_common.device_info.get_device_runtime_metadata')
class TestFeatureDaemon(TestCase):

    def setUp(self):
        print("Running Setup")
        self.patcher = Patcher()
        self.patcher.setUp()
        self.patcher.fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        MockConfigDb.CONFIG_DB = copy.deepcopy(FEATURE_DAEMON_CFG_DB)
        MockRestartWaiter.advancedReboot = False
        MockSelect.NUM_TIMEOUT_TRIES = 0

    def tearDown(self):
        print("Running TearDown")
        self.patcher.tearDown()
        MockConfigDb.CONFIG_DB.clear()
        MockSelect.reset_event_queue()

    def test_feature_events(self, mock_syslog, get_runtime):
        MockSelect.set_event_queue([('FEATURE', 'dhcp_relay'),
                                    ('FEATURE', 'mux')])
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.render_all_feature_states()
            daemon.register_callbacks()
            try:
                daemon.start(time.time())
            except TimeoutError as e:
                pass
            expected = [call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True)]
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

            # Change the state to disabled
            MockSelect.reset_event_queue()
            MockConfigDb.CONFIG_DB['FEATURE']['dhcp_relay']['state'] = 'disabled'
            MockSelect.set_event_queue([('FEATURE', 'dhcp_relay')])
            try:
                daemon.start(time.time())
            except TimeoutError:
                pass
            expected = [call(['sudo', 'systemctl', 'stop', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'disable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'mask', 'dhcp_relay.service'], capture_output=True, check=True, text=True)]
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_delayed_service(self, mock_syslog, get_runtime):
        MockSelect.set_event_queue([('FEATURE', 'dhcp_relay'),
                                    ('FEATURE', 'mux'),
                                    ('PORT_TABLE', 'PortInitDone')])
        # Note: To simplify testing, subscriberstatetable only read from CONFIG_DB
        MockConfigDb.CONFIG_DB['PORT_TABLE'] = {'PortInitDone': {'lanes': '0'}, 'PortConfigDone': {'val': 'true'}}
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.register_callbacks()
            daemon.render_all_feature_states()
            try:
                daemon.start(time.time())
            except TimeoutError:
                pass
            expected = [call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True)]

            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_advanced_reboot(self, mock_syslog, get_runtime):
        MockRestartWaiter.advancedReboot = True
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.render_all_feature_states()
            daemon.register_callbacks()
            try:            
                daemon.start(time.time())
            except TimeoutError:
                pass        
            expected = [
                call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True)]               
        
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_portinit_timeout(self, mock_syslog, get_runtime):
        print(MockConfigDb.CONFIG_DB)
        MockSelect.NUM_TIMEOUT_TRIES = 1
        MockSelect.set_event_queue([('FEATURE', 'dhcp_relay'),
                                    ('FEATURE', 'mux')])
        with mock.patch('featured.subprocess') as mocked_subprocess:
            popen_mock = mock.Mock()
            attrs = {'communicate.return_value': ('output', 'error')}
            popen_mock.configure_mock(**attrs)
            mocked_subprocess.Popen.return_value = popen_mock
            daemon = featured.FeatureDaemon()
            daemon.render_all_feature_states()
            daemon.register_callbacks()
            try:
                daemon.start(0.0)
            except TimeoutError:
                pass
            expected = [call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'dhcp_relay.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'daemon-reload'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'unmask', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'enable', 'mux.service'], capture_output=True, check=True, text=True),
                        call(['sudo', 'systemctl', 'start', 'mux.service'], capture_output=True, check=True, text=True)]
            mocked_subprocess.run.assert_has_calls(expected, any_order=True)

    def test_systemctl_command_failure(self, mock_syslog, get_runtime):
        """Test that when systemctl commands fail:
        1. The feature state is not cached
        2. The feature state is set to FAILED
        3. The update_feature_state returns False
        """
        mock_db = mock.MagicMock()
        mock_feature_state_table = mock.MagicMock()

        feature_handler = featured.FeatureHandler(mock_db, mock_feature_state_table, {}, False)
        feature_handler.is_delayed_enabled = True

        # Create a feature that should be enabled
        feature_name = 'test_feature'
        feature_cfg = {
            'state': 'enabled',
            'auto_restart': 'enabled',
            'delayed': 'False',
            'has_global_scope': 'True',
            'has_per_asic_scope': 'False'
        }

        # Initialize the feature in cached_config using the same pattern as in featured
        feature = featured.Feature(feature_name, feature_cfg)
        feature_handler._cached_config.setdefault(feature_name, featured.Feature(feature_name, {}))

        # Mock subprocess.run and Popen to simulate command failure
        with mock.patch('featured.subprocess') as mocked_subprocess:
            # Mock Popen for get_systemd_unit_state
            popen_mock = mock.Mock()
            popen_mock.communicate.return_value = ('enabled', '')
            popen_mock.returncode = 1
            mocked_subprocess.Popen.return_value = popen_mock

            # Mock run_cmd to raise an exception
            with mock.patch('featured.run_cmd') as mocked_run_cmd:
                mocked_run_cmd.side_effect = Exception("Command failed")

                # Try to update feature state
                result = feature_handler.update_feature_state(feature)

                # Verify the result is False
                assert result is False

                # Verify the feature state was set to FAILED
                mock_feature_state_table.set.assert_called_with('test_feature', [('state', 'failed')])

                # Verify the feature state was not enabled in the cache
                assert feature_handler._cached_config[feature.name].state != 'enabled'


class TestWaitForServiceStable(TestCase):
    """Tests for wait_for_service_stable method that prevents orphaned containers."""

    def _create_handler(self):
        feature_state_table_mock = mock.Mock()
        device_cfg = {"DEVICE_METADATA": {"localhost": {"type": "ToRRouter"}}}
        handler = featured.FeatureHandler(MockConfigDb(), feature_state_table_mock, device_cfg, False)
        return handler

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    def test_service_already_active(self, mock_sleep, mock_subprocess):
        """Service is already 'active' — should return immediately without polling."""
        handler = self._create_handler()
        popen_mock = mock.Mock()
        popen_mock.communicate.return_value = (b"active\n", b"")
        popen_mock.returncode = 0
        mock_subprocess.Popen.return_value = popen_mock

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "active"
        mock_sleep.assert_not_called()

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    def test_service_inactive(self, mock_sleep, mock_subprocess):
        """Service is 'inactive' — should return immediately."""
        handler = self._create_handler()
        popen_mock = mock.Mock()
        popen_mock.communicate.return_value = (b"inactive\n", b"")
        popen_mock.returncode = 0
        mock_subprocess.Popen.return_value = popen_mock

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "inactive"
        mock_sleep.assert_not_called()

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    def test_service_transitions_from_activating_to_active(self, mock_sleep, mock_subprocess):
        """Service starts in 'activating' then transitions to 'active' — should poll and return."""
        handler = self._create_handler()

        popen_mocks = []
        for state in [b"activating\n", b"activating\n", b"active\n"]:
            m = mock.Mock()
            m.communicate.return_value = (state, b"")
            m.returncode = 0
            popen_mocks.append(m)

        mock_subprocess.Popen.side_effect = popen_mocks

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "active"
        assert mock_sleep.call_count == 2

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    @mock.patch("featured.time.time")
    def test_service_activating_timeout(self, mock_time, mock_sleep, mock_subprocess):
        """Service stays 'activating' past timeout — should return 'activating'."""
        handler = self._create_handler()

        # Simulate time progressing past the timeout
        mock_time.side_effect = [0.0, 1.0, 2.0, 61.0]

        popen_mock = mock.Mock()
        popen_mock.communicate.return_value = (b"activating\n", b"")
        popen_mock.returncode = 0
        mock_subprocess.Popen.return_value = popen_mock

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "activating"

    @mock.patch("featured.subprocess")
    @mock.patch("featured.time.sleep")
    def test_service_failed(self, mock_sleep, mock_subprocess):
        """Service is 'failed' — should return immediately."""
        handler = self._create_handler()
        popen_mock = mock.Mock()
        popen_mock.communicate.return_value = (b"failed\n", b"")
        popen_mock.returncode = 0
        mock_subprocess.Popen.return_value = popen_mock

        result = handler.wait_for_service_stable("bgp@3.service")

        assert result == "failed"
        mock_sleep.assert_not_called()

    @mock.patch("syslog.syslog", side_effect=syslog_side_effect)
    def test_disable_feature_calls_wait_for_service_stable(self, mock_syslog):
        """Verify disable_feature calls wait_for_service_stable before systemctl stop."""
        handler = self._create_handler()

        feat_cfg = {"state": "disabled", "auto_restart": "enabled"}
        feature = featured.Feature("bgp", feat_cfg)

        call_order = []

        def track_wait(unit):
            call_order.append(("wait", unit))
            return "active"

        def track_run_cmd(cmd, **kwargs):
            call_order.append(("cmd", cmd))

        with mock.patch.object(handler, "get_multiasic_feature_instances",
                               return_value=(["bgp@3"], ["service"])), \
             mock.patch.object(handler, "get_systemd_unit_state", return_value="enabled"), \
             mock.patch.object(handler, "wait_for_service_stable", side_effect=track_wait), \
             mock.patch("featured.run_cmd", side_effect=track_run_cmd), \
             mock.patch.object(handler, "set_feature_state"):

            handler.disable_feature(feature)

            # Verify wait was called before stop
            assert len(call_order) >= 2
            assert call_order[0] == ("wait", "bgp@3.service")
            assert call_order[1][0] == "cmd"
            assert "stop" in call_order[1][1]


class TestAbsentFeature(TestCase):
    """Disabled features may be absent regardless of platform or scope defaults."""

    def setUp(self):
        self.patch('sonic_py_common.device_info.is_multi_npu', return_value=False)
        self.patch('sonic_py_common.device_info.get_num_dpus', return_value=0)
        self.patch('sonic_py_common.device_info.get_device_runtime_metadata', return_value={})
        self.is_bmc = self.patch('sonic_py_common.device_info.is_switch_bmc', return_value=False)
        self.log = self.patch('featured.syslog.syslog')
        self.run = self.patch('featured.subprocess.run')
        self.popen = self.patch('featured.subprocess.Popen', side_effect=self.systemd_show)
        self.wait = self.patch('featured.FeatureHandler.wait_for_service_stable')
        self.config = {
            'state': 'disabled',
            'auto_restart': 'enabled',
        }
        self.load_state = 'not-found'
        self.load_rc = 0
        self.db = mock.Mock()
        self.db.get_entry.side_effect = lambda *args: self.config.copy()
        self.state_table = mock.Mock()
        self.handler = featured.FeatureHandler(self.db, self.state_table, {}, False)

    def patch(self, target, **kwargs):
        patcher = mock.patch(target, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def systemd_show(self, cmd, **kwargs):
        prop = cmd[-1]
        self.assertIn(prop, ('LoadState', 'UnitFileState'))
        value = self.load_state if prop == 'LoadState' else ''
        return mock.Mock(
            returncode=self.load_rc if prop == 'LoadState' else 0,
            communicate=mock.Mock(return_value=(
                '{}={}\n'.format(prop, value).encode(), b'')))

    @parameterized.expand([
        ('startup', 'swss'),
        ('startup', 'radv'),
        ('startup', 'restapi'),
        ('startup', 'sflow'),
        ('startup', 'syncd'),
        ('startup', 'teamd'),
        ('startup', 'snmp'),
        ('startup', 'eventd'),
        ('startup', 'bmp'),
        ('change', 'swss'),
        ('change', 'restapi'),
        ('change', 'snmp'),
        ('change', 'teamd'),
        ('change', 'eventd'),
        ('change', 'radv'),
        ('change', 'bmp'),
        ('auto_restart', 'swss'),
        ('auto_restart', 'bmp'),
        ('delayed', 'snmp'),
        ('delayed', 'bmp'),
        ('always_disabled', 'swss'),
        ('always_disabled', 'bmp'),
    ])
    @patchfs
    def test_absent_unit_reconciliation(self, path, name, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        self.assertTrue(featured.Feature(name, self.config).has_global_scope)
        if path == 'always_disabled':
            self.config['state'] = 'always_disabled'
        if path == 'delayed':
            self.config['delayed'] = 'True'
        if path == 'change':
            self.handler._cached_config[name] = featured.Feature(name, {'state': 'enabled'})
            self.handler.handler(name, 'SET', self.config)
        elif path == 'auto_restart':
            self.handler._cached_config[name] = featured.Feature(
                name, dict(self.config, auto_restart='disabled'))
            self.handler.handler(name, 'SET', self.config)
        else:
            self.handler.sync_state_field({name: self.config})
        if path == 'delayed':
            self.state_table.set.assert_not_called()
            self.handler.handle_port_table_timeout()

        if path == 'auto_restart':
            self.state_table.set.assert_not_called()
        else:
            self.state_table.set.assert_called_with(name, [('state', 'disabled')])
        self.wait.assert_not_called()
        self.assertFalse(os.path.exists(
            self.handler.SYSTEMD_SERVICE_CONF_DIR.format(name)))
        self.popen.assert_any_call(
            ['sudo', 'systemctl', 'show', '{}.service'.format(name), '--property', 'LoadState'],
            stdout=featured.subprocess.PIPE, stderr=featured.subprocess.PIPE)
        self.assertTrue(self.run.called)  # The normal daemon-reload still runs.
        for invocation in self.run.call_args_list:
            self.assertEqual(invocation.args[0], ['sudo', 'systemctl', 'daemon-reload'])
        self.assertFalse(any(c.args[0] == featured.syslog.LOG_ERR
                             for c in self.log.call_args_list))

    @parameterized.expand([
        ('no_scope', {'has_global_scope': 'False'}),
        ('global_scope', {'has_global_scope': 'True'}),
        ('asic_scope', {'has_per_asic_scope': 'True'}),
        ('dpu_scope', {'has_per_dpu_scope': 'True'}),
    ])
    def test_explicit_scope_does_not_prevent_absent_unit_skip(self, name, overrides):
        self.config.update(overrides)

        self.assertTrue(self.handler.disable_feature(featured.Feature('swss', self.config)))

        self.run.assert_not_called()
        self.wait.assert_not_called()
        self.state_table.set.assert_called_once_with('swss', [('state', 'disabled')])

    @parameterized.expand([
        ('switch_disabled', False, 'disabled'),
        ('switch_always_disabled', False, 'always_disabled'),
        ('bmc_disabled', True, 'disabled'),
        ('bmc_always_disabled', True, 'always_disabled'),
    ])
    def test_absent_disabled_unit_skipped_on_any_platform(self, name, is_bmc, state):
        self.is_bmc.return_value = is_bmc
        self.config['state'] = state

        self.assertTrue(self.handler.disable_feature(featured.Feature('swss', self.config)))

        self.run.assert_not_called()
        self.wait.assert_not_called()
        self.is_bmc.assert_not_called()
        self.state_table.set.assert_called_once_with('swss', [('state', 'disabled')])

    @parameterized.expand([
        ('loaded', 'loaded', 0),
        ('bad_setting', 'bad-setting', 0),
        ('query_failure', 'not-found', 1),
        ('empty_load_state', '', 0),
    ])
    def test_systemd_failures_are_not_skipped(self, name, load_state, rc):
        self.load_state, self.load_rc = load_state, rc
        stop = ['sudo', 'systemctl', 'stop', 'swss.service']
        self.run.side_effect = subprocess.CalledProcessError(5, stop, output='stop failed')

        result = self.handler.disable_feature(featured.Feature('swss', self.config))

        self.assertFalse(result)
        self.wait.assert_called_once_with('swss.service')
        self.run.assert_called_once_with(stop, capture_output=True, check=True, text=True)
        self.state_table.set.assert_called_once_with('swss', [('state', 'failed')])
        self.assertTrue(any('return code - 5' in c.args[1]
                            for c in self.log.call_args_list))
        self.assertTrue(any('failed to be stopped and disabled' in c.args[1]
                            for c in self.log.call_args_list))

    @parameterized.expand([('enabled',), ('always_enabled',)])
    def test_enabled_absent_unit_still_fails(self, state):
        self.config['state'] = state
        self.run.side_effect = subprocess.CalledProcessError(
            5, ['sudo', 'systemctl', 'unmask', 'swss.service'])

        self.assertFalse(self.handler.enable_feature(featured.Feature('swss', self.config)))

        self.state_table.set.assert_called_once_with('swss', [('state', 'failed')])
        self.assertTrue(any('failed to be enabled and started' in c.args[1]
                            for c in self.log.call_args_list))

    @parameterized.expand([('swss',), ('bmp',)])
    @patchfs
    def test_loaded_unit_keeps_config_and_stop_actions(self, name, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        self.load_state = 'loaded'

        self.handler.sync_state_field({name: self.config})

        self.assertTrue(os.path.exists(os.path.join(
            self.handler.SYSTEMD_SERVICE_CONF_DIR.format(name), 'auto_restart.conf')))
        self.wait.assert_called_once_with('{}.service'.format(name))
        self.run.assert_any_call(['sudo', 'systemctl', 'stop', '{}.service'.format(name)],
                                 capture_output=True, check=True, text=True)
        self.state_table.set.assert_called_with(name, [('state', 'disabled')])

    @parameterized.expand([('enabled',), ('always_enabled',)])
    @patchfs
    def test_packaged_bmp_still_starts(self, state, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        self.load_state = 'loaded'
        self.config['state'] = state

        self.handler.sync_state_field({'bmp': self.config})

        config_path = os.path.join(
            self.handler.SYSTEMD_SERVICE_CONF_DIR.format('bmp'), 'auto_restart.conf')
        with open(config_path) as config_file:
            self.assertEqual(config_file.read(), '[Service]\nRestart=always\n')
        self.assertEqual([c.args[0] for c in self.run.call_args_list], [
            ['sudo', 'systemctl', 'daemon-reload'],
            ['sudo', 'systemctl', 'unmask', 'bmp.service'],
            ['sudo', 'systemctl', 'enable', 'bmp.service'],
            ['sudo', 'systemctl', 'start', 'bmp.service'],
        ])
        self.state_table.set.assert_called_with('bmp', [('state', 'enabled')])
        self.assertNotIn('bmp', self.handler._features_pending_config)

    @parameterized.expand([('disabled',), ('always_disabled',)])
    @patchfs
    def test_absent_bmp_repeated_sync_does_not_create_unit(self, state, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        self.config['state'] = state

        for _ in range(2):
            self.handler.sync_state_field({'bmp': self.config})

        self.assertEqual(os.listdir(self.handler.SYSTEMD_SYSTEM_DIR), [])
        self.assertEqual([c.args[0] for c in self.run.call_args_list], [
            ['sudo', 'systemctl', 'daemon-reload'],
            ['sudo', 'systemctl', 'daemon-reload'],
        ])
        self.wait.assert_not_called()
        self.state_table.set.assert_called_with('bmp', [('state', 'disabled')])
        self.assertIn('bmp', self.handler._features_pending_config)

    def test_missing_load_property_is_not_absence(self):
        self.popen.side_effect = None
        self.popen.return_value = mock.Mock(
            returncode=0, communicate=mock.Mock(return_value=(b'', b'')))

        self.assertFalse(self.handler.is_intentionally_absent_unit(
            featured.Feature('swss', self.config), 'swss.service'))

        self.log.assert_called_once_with(
            featured.syslog.LOG_ERR, 'Missing LoadState in status of swss.service')

    def test_absent_disable_retries_uncached_transition(self):
        feature = featured.Feature('swss', dict(self.config, state='enabled'))
        self.handler._cached_config['swss'] = feature

        self.handler.handler('swss', 'SET', self.config)

        self.assertEqual(self.handler._cached_config['swss'].state, 'disabled')
        self.state_table.set.assert_called_once_with('swss', [('state', 'disabled')])
        self.run.assert_not_called()
        self.wait.assert_not_called()

    @parameterized.expand([
        (path, auto_restart)
        for path in ('startup', 'auto_restart', 'delayed', 'retry')
        for auto_restart in ('enabled', 'disabled')
    ])
    @patchfs
    def test_skipped_config_applied_before_enable(self, path, auto_restart, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        self.config['auto_restart'] = auto_restart
        if path == 'delayed':
            self.config['delayed'] = 'True'
        if path == 'auto_restart':
            previous = 'disabled' if auto_restart == 'enabled' else 'enabled'
            self.handler._cached_config['swss'] = featured.Feature(
                'swss', dict(self.config, auto_restart=previous))
            self.handler.handler('swss', 'SET', self.config)
        else:
            self.handler.sync_state_field({'swss': self.config})
        config_path = os.path.join(
            self.handler.SYSTEMD_SERVICE_CONF_DIR.format('swss'), 'auto_restart.conf')
        self.assertFalse(os.path.exists(config_path))
        self.assertEqual(self.handler._cached_config['swss'].auto_restart, auto_restart)
        self.run.reset_mock()
        observed_configs = []
        fail_start = path == 'retry'

        def run_systemctl(cmd, **kwargs):
            nonlocal fail_start
            if cmd[2] in ('daemon-reload', 'start'):
                content = None
                if os.path.exists(config_path):
                    with open(config_path) as config_file:
                        content = config_file.read()
                observed_configs.append((cmd[2], content))
            if fail_start and cmd[2] == 'start':
                fail_start = False
                raise subprocess.CalledProcessError(1, cmd, stderr='start failed')
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        self.run.side_effect = run_systemctl
        self.load_state = 'loaded'
        self.config['state'] = 'enabled'
        self.handler.handler('swss', 'SET', self.config)
        if path == 'delayed':
            self.run.assert_not_called()
            self.handler.handle_port_table_timeout()
        if path == 'retry':
            self.assertEqual(self.handler._cached_config['swss'].state, 'disabled')
            self.handler.handler('swss', 'SET', self.config)

        expected_config = '[Service]\nRestart={}\n'.format(
            'always' if auto_restart == 'enabled' else 'no')
        expected = [('daemon-reload', expected_config), ('start', expected_config)]
        if path == 'retry':
            expected.append(('start', expected_config))
        self.assertEqual(observed_configs, expected)
        self.assertEqual(self.handler._cached_config['swss'].state, 'enabled')
        self.assertNotIn('swss', self.handler._features_pending_config)
        self.state_table.set.assert_called_with('swss', [('state', 'enabled')])

    @parameterized.expand([('write',), ('reload',)])
    @patchfs
    def test_skipped_config_failure_blocks_start_until_retry(self, failure, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        self.handler.sync_state_field({'swss': self.config})
        self.run.reset_mock()
        self.load_state = 'loaded'
        self.config['state'] = 'enabled'
        if failure == 'write':
            with mock.patch('featured.open', side_effect=OSError('write failed')):
                self.handler.handler('swss', 'SET', self.config)
            self.run.assert_not_called()
        else:
            self.run.side_effect = subprocess.CalledProcessError(
                1, ['sudo', 'systemctl', 'daemon-reload'], stderr='reload failed')
            self.handler.handler('swss', 'SET', self.config)
            self.run.assert_called_once_with(
                ['sudo', 'systemctl', 'daemon-reload'],
                capture_output=True, check=True, text=True)

        self.assertIn('swss', self.handler._features_pending_config)
        self.assertEqual(self.handler._cached_config['swss'].state, 'disabled')
        self.state_table.set.assert_called_with('swss', [('state', 'failed')])
        self.assertTrue(any(c.args[0] == featured.syslog.LOG_ERR
                            for c in self.log.call_args_list))

        self.run.side_effect = None
        self.run.reset_mock()
        self.handler.handler('swss', 'SET', self.config)

        commands = [invocation.args[0][2] for invocation in self.run.call_args_list]
        self.assertEqual(commands, ['daemon-reload', 'unmask', 'enable', 'start'])
        self.assertNotIn('swss', self.handler._features_pending_config)
        self.assertEqual(self.handler._cached_config['swss'].state, 'enabled')
        self.state_table.set.assert_called_with('swss', [('state', 'enabled')])

    @parameterized.expand([('success', False), ('failure', True)])
    @patchfs
    def test_skipped_config_cleared_only_after_reload(self, name, fail_reload, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        self.handler.sync_state_field({'swss': self.config})
        self.assertIn('swss', self.handler._features_pending_config)
        self.load_state = 'loaded'
        self.run.reset_mock()

        def run_systemctl(cmd, **kwargs):
            if fail_reload and cmd[2] == 'daemon-reload':
                raise subprocess.CalledProcessError(1, cmd, stderr='reload failed')
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        self.run.side_effect = run_systemctl
        self.handler.sync_state_field({'swss': self.config})

        self.assertEqual('swss' in self.handler._features_pending_config, fail_reload)
        reloads = [c for c in self.run.call_args_list if c.args[0][2] == 'daemon-reload']
        self.assertEqual(len(reloads), 1)

    @patchfs
    def test_partial_instance_config_is_applied_before_enable(self, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        units = ['swss@0', 'swss@1']
        config_paths = [os.path.join(self.handler.SYSTEMD_SERVICE_CONF_DIR.format(unit),
                                    'auto_restart.conf') for unit in units]
        systemd_show = self.systemd_show

        def show_instance(cmd, **kwargs):
            self.load_state = 'not-found' if cmd[3] == 'swss@0.service' else 'loaded'
            return systemd_show(cmd, **kwargs)

        self.popen.side_effect = show_instance
        observed_configs = []

        def run_systemctl(cmd, **kwargs):
            if cmd[2] in ('daemon-reload', 'start'):
                observed_configs.append((cmd[2], [os.path.exists(p) for p in config_paths]))
            return subprocess.CompletedProcess(cmd, 0, stdout='', stderr='')

        with mock.patch.object(self.handler, 'get_multiasic_feature_instances',
                               return_value=(units, ['service'])):
            self.handler.sync_state_field({'swss': self.config})
            self.assertFalse(os.path.exists(config_paths[0]))
            self.assertTrue(os.path.exists(config_paths[1]))
            self.assertIn('swss', self.handler._features_pending_config)
            self.popen.side_effect = systemd_show
            self.load_state = 'loaded'
            self.config['state'] = 'enabled'
            self.run.side_effect = run_systemctl

            self.handler.handler('swss', 'SET', self.config)

        self.assertEqual(observed_configs, [
            ('daemon-reload', [True, True]),
            ('start', [True, True]),
            ('start', [True, True]),
        ])
        self.assertNotIn('swss', self.handler._features_pending_config)

    @patchfs
    def test_deregister_clears_skipped_config(self, fs):
        fs.create_dir(featured.FeatureHandler.SYSTEMD_SYSTEM_DIR)
        self.handler.sync_state_field({'swss': self.config})
        self.assertIn('swss', self.handler._features_pending_config)

        self.handler.handler('swss', 'DEL', {})

        self.assertNotIn('swss', self.handler._features_pending_config)
