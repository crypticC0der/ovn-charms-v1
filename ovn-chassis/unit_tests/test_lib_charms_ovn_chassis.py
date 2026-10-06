# Copyright 2026 Canonical Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import unittest.mock as mock

import charms_openstack.test_utils as test_utils

import charm.openstack.ovn_chassis as ovn_chassis


class Helper(test_utils.PatchHelper):

    def setUp(self):
        super().setUp()
        self.target = ovn_chassis.OVNChassisCharm()
        self.target.options = mock.MagicMock()
        self.target.options.install_doca = False
        self.target.options.enable_dpdk = False

    def patch_ovsdb(self, **tables):
        """Patch SimpleOVSDB to present ``tables`` as the named tables.

        :param tables: Map of table name to the rows to present.
        :type tables: Dict[str,List[Dict[str,any]]]
        :returns: Map of table name to the mocked table object.
        :rtype: Dict[str,mock.MagicMock]
        """
        self.patch_object(ovn_chassis, 'ch_ovsdb')
        mocks = {}
        for name, rows in tables.items():
            table = mock.MagicMock()
            # SimpleOVSDB returns a fresh generator from __iter__ and from
            # find() on every call, so the mock must be re-iterable too.
            table.__iter__ = mock.MagicMock(
                side_effect=lambda rows=rows: iter(rows))
            table.find = mock.MagicMock(
                side_effect=lambda *_, rows=rows: iter(rows))
            setattr(
                ovn_chassis.ch_ovsdb.SimpleOVSDB.return_value, name, table)
            mocks[name] = table
        return mocks

    def patch_open_vswitch(self, rows):
        """Patch SimpleOVSDB to present ``rows`` as the Open_vSwitch table.

        :param rows: Rows to present.
        :type rows: List[Dict[str,any]]
        :returns: The mocked ``open_vswitch`` table object.
        :rtype: mock.MagicMock
        """
        return self.patch_ovsdb(open_vswitch=rows)['open_vswitch']

    def patch_doca_datapath(self):
        """Make the unit resolve its datapath type to ``doca``."""
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'doca'})
        self.target.options.install_doca = True


class TestOvsDatapathTypes(Helper):

    def test_multiple_types(self):
        self.patch_open_vswitch(
            [{'datapath_types': ['doca', 'netdev', 'system']}])
        self.assertEqual({'doca', 'netdev', 'system'},
                         self.target.ovs_datapath_types())

    def test_single_type_presented_as_string(self):
        # SimpleOVSDB has no schema knowledge and collapses a single valued
        # set column to a plain string.
        self.patch_open_vswitch([{'datapath_types': 'system'}])
        self.assertEqual({'system'}, self.target.ovs_datapath_types())

    def test_column_absent(self):
        self.patch_open_vswitch([{}])
        self.assertEqual(set(), self.target.ovs_datapath_types())

    def test_empty_table(self):
        self.patch_open_vswitch([])
        self.assertEqual(set(), self.target.ovs_datapath_types())


class TestDatapathType(Helper):

    def test_default_is_system(self):
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'netdev'})
        self.assertEqual('system', self.target.datapath_type)

    def test_dpdk_is_netdev(self):
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'netdev'})
        self.target.options.enable_dpdk = True
        self.assertEqual('netdev', self.target.datapath_type)

    def test_doca_when_supported(self):
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'netdev', 'doca'})
        self.target.options.install_doca = True
        self.assertEqual('doca', self.target.datapath_type)

    def test_doca_takes_precedence_over_dpdk(self):
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'netdev', 'doca'})
        self.target.options.install_doca = True
        self.target.options.enable_dpdk = True
        self.assertEqual('doca', self.target.datapath_type)

    def test_falls_back_when_doca_unsupported(self):
        # Writing a datapath type the local ovs-vswitchd does not support
        # would leave br-int unable to instantiate.
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'netdev'})
        self.target.options.install_doca = True
        self.assertEqual('system', self.target.datapath_type)

    def test_falls_back_to_netdev_when_doca_unsupported(self):
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'netdev'})
        self.target.options.install_doca = True
        self.target.options.enable_dpdk = True
        self.assertEqual('netdev', self.target.datapath_type)


class TestSetOpenVswitch(Helper):

    def test_sets_missing_key(self):
        opvs = self.patch_open_vswitch([{'other_config': {}}])
        self.assertTrue(
            self.target._set_open_vswitch(
                {'other_config': (('doca-init', 'true'),)}))
        opvs.set.assert_called_once_with(
            '.', 'other_config:doca-init', 'true')

    def test_sets_differing_key(self):
        opvs = self.patch_open_vswitch(
            [{'other_config': {'doca-init': 'false'}}])
        self.assertTrue(
            self.target._set_open_vswitch(
                {'other_config': (('doca-init', 'true'),)}))
        opvs.set.assert_called_once_with(
            '.', 'other_config:doca-init', 'true')

    def test_idempotent_when_already_set(self):
        opvs = self.patch_open_vswitch(
            [{'other_config': {'doca-init': 'true'}}])
        self.assertFalse(
            self.target._set_open_vswitch(
                {'other_config': (('doca-init', 'true'),)}))
        opvs.set.assert_not_called()

    def test_absent_column(self):
        opvs = self.patch_open_vswitch([{}])
        self.assertTrue(
            self.target._set_open_vswitch(
                {'external_ids': (
                    ('ovn-bridge-datapath-type', 'doca'),)}))
        opvs.set.assert_called_once_with(
            '.', 'external_ids:ovn-bridge-datapath-type', 'doca')


class TestConfigureOvsDocaInit(Helper):

    def test_enabled(self):
        opvs = self.patch_open_vswitch([{'other_config': {}}])
        self.target.options.install_doca = True
        self.assertTrue(self.target.configure_ovs_doca_init())
        opvs.set.assert_called_once_with(
            '.', 'other_config:doca-init', 'true')

    def test_disabled_is_written_explicitly(self):
        # Written rather than removed so that disabling install-doca
        # converges instead of leaving the previous value in place.
        opvs = self.patch_open_vswitch(
            [{'other_config': {'doca-init': 'true'}}])
        self.assertTrue(self.target.configure_ovs_doca_init())
        opvs.set.assert_called_once_with(
            '.', 'other_config:doca-init', 'false')


class TestConfigureOvsDatapath(Helper):

    def test_sets_both_keys(self):
        opvs = self.patch_open_vswitch(
            [{'external_ids': {}, 'other_config': {}}])
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'doca'})
        self.target.options.install_doca = True
        self.assertTrue(self.target.configure_ovs_datapath())
        opvs.set.assert_has_calls([
            mock.call('.', 'external_ids:ovn-bridge-datapath-type', 'doca'),
            mock.call('.', 'other_config:default-datapath-type', 'doca'),
        ])

    def test_idempotent(self):
        opvs = self.patch_open_vswitch([{
            'external_ids': {'ovn-bridge-datapath-type': 'system'},
            'other_config': {'default-datapath-type': 'system'},
        }])
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system'})
        self.assertFalse(self.target.configure_ovs_datapath())
        opvs.set.assert_not_called()


class TestCharmManagedResources(Helper):

    def test_bridges_filtered_on_external_id(self):
        tables = self.patch_ovsdb(bridge=[{'name': 'br-int'}])
        self.assertEqual(
            [{'name': 'br-int'}], list(self.target.charm_managed_bridges()))
        tables['bridge'].find.assert_called_once_with(
            'external_ids:charm-ovn-chassis=managed')

    def test_interfaces_filtered_on_external_id(self):
        # Internal interfaces and the patch and tunnel ports ovn-controller
        # manages do not carry the external-id and are not ours to touch.
        self.patch_ovsdb(interface=[
            {'name': 'bond1',
             'external_ids': {'charm-ovn-chassis': 'br-data'}},
            {'name': 'br-int', 'external_ids': {}},
            {'name': 'ovn-abcdef-0',
             'external_ids': {'ovn-chassis-id': 'other'}},
        ])
        self.assertEqual(
            ['bond1'],
            [row['name'] for row in self.target.charm_managed_interfaces()])


class TestConfigureBridgeDatapathType(Helper):

    def test_corrects_bridges(self):
        # The base charm hard codes "system", so every bridge it created has
        # to be corrected after it ran.
        tables = self.patch_ovsdb(bridge=[
            {'name': 'br-int', 'datapath_type': 'system'},
            {'name': 'br-data', 'datapath_type': 'system'},
        ])
        self.patch_doca_datapath()
        self.assertTrue(self.target.configure_bridge_datapath_type())
        tables['bridge'].set.assert_has_calls([
            mock.call('br-int', 'datapath_type', 'doca'),
            mock.call('br-data', 'datapath_type', 'doca'),
        ])

    def test_idempotent(self):
        tables = self.patch_ovsdb(bridge=[
            {'name': 'br-int', 'datapath_type': 'doca'},
        ])
        self.patch_doca_datapath()
        self.assertFalse(self.target.configure_bridge_datapath_type())
        tables['bridge'].set.assert_not_called()

    def test_kernel_datapath(self):
        tables = self.patch_ovsdb(bridge=[
            {'name': 'br-int', 'datapath_type': 'doca'},
        ])
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'doca'})
        self.assertTrue(self.target.configure_bridge_datapath_type())
        tables['bridge'].set.assert_called_once_with(
            'br-int', 'datapath_type', 'system')


class TestConfigureInterfaceType(Helper):

    def interface(self, **kwargs):
        return {
            'name': 'bond1',
            'external_ids': {'charm-ovn-chassis': 'br-data'},
            **kwargs,
        }

    def test_corrects_type_and_sets_doca_options(self):
        tables = self.patch_ovsdb(
            interface=[self.interface(type='system', options={})])
        self.patch_doca_datapath()
        self.assertTrue(self.target.configure_interface_type())
        tables['interface'].set.assert_has_calls([
            mock.call('bond1', 'type', 'doca'),
            mock.call('bond1', 'options:dpdk-lsc-interrupt', 'true'),
        ])

    def test_idempotent(self):
        tables = self.patch_ovsdb(interface=[self.interface(
            type='doca', options={'dpdk-lsc-interrupt': 'true'})])
        self.patch_doca_datapath()
        self.assertFalse(self.target.configure_interface_type())
        tables['interface'].set.assert_not_called()
        tables['interface'].remove.assert_not_called()

    def test_empty_type_is_system(self):
        # Open vSwitch presents an interface with no explicit type as having
        # an empty one, which is the same thing as "system".
        tables = self.patch_ovsdb(
            interface=[self.interface(type='', options={})])
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system'})
        self.assertFalse(self.target.configure_interface_type())
        tables['interface'].set.assert_not_called()

    def test_dpdk_type_for_netdev_datapath(self):
        tables = self.patch_ovsdb(
            interface=[self.interface(type='dpdk', options={})])
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'netdev'})
        self.target.options.enable_dpdk = True
        self.assertFalse(self.target.configure_interface_type())
        tables['interface'].set.assert_not_called()

    def test_doca_options_removed_off_doca_datapath(self):
        # The options are not recognized by upstream Open vSwitch at all, so
        # they are removed rather than written with a default.
        tables = self.patch_ovsdb(interface=[self.interface(
            type='system', options={'dpdk-lsc-interrupt': 'true'})])
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system'})
        self.assertTrue(self.target.configure_interface_type())
        tables['interface'].remove.assert_called_once_with(
            'bond1', 'options', 'dpdk-lsc-interrupt')
        tables['interface'].set.assert_not_called()


class TestConfigureDatapathType(Helper):

    def setUp(self):
        super().setUp()
        self.patch_object(ovn_chassis, 'ch_deferred_events')
        self.patch_object(ovn_chassis.ch_core, 'host')
        ovn_chassis.ch_core.host.service_running.return_value = True
        self.patch_object(self.target, 'configure_bridge_datapath_type',
                          return_value=False)
        self.patch_object(self.target, 'configure_interface_type',
                          return_value=False)

    def test_no_restart_when_nothing_changed(self):
        self.assertFalse(self.target.configure_datapath_type())
        self.assertFalse(
            ovn_chassis.ch_deferred_events.deferrable_svc_restart.called)

    def test_restarts_when_bridge_changed(self):
        self.configure_bridge_datapath_type.return_value = True
        self.assertTrue(self.target.configure_datapath_type())
        ovn_chassis.ch_deferred_events.deferrable_svc_restart \
            .assert_called_once_with(
                'ovs-vswitchd', reason='datapath type changed')

    def test_restarts_when_interface_changed(self):
        self.configure_interface_type.return_value = True
        self.assertTrue(self.target.configure_datapath_type())
        self.assertTrue(
            ovn_chassis.ch_deferred_events.deferrable_svc_restart.called)

    def test_interfaces_corrected_even_when_bridges_were_not(self):
        self.configure_bridge_datapath_type.return_value = True
        self.target.configure_datapath_type()
        self.configure_interface_type.assert_called_once_with()

    def test_skipped_while_database_unavailable(self):
        # ovs-vsctl talks to ovsdb-server, which is down for the duration of
        # the DOCA package installation.
        ovn_chassis.ch_core.host.service_running.return_value = False
        self.assertFalse(self.target.configure_datapath_type())
        self.configure_bridge_datapath_type.assert_not_called()
        self.configure_interface_type.assert_not_called()
        ovn_chassis.ch_core.host.service_running.assert_called_once_with(
            'ovsdb-server')


class TestConfigureBridges(Helper):

    def test_corrects_after_base_class(self):
        # The base charm resolves the datapath type itself, with no knowledge
        # of DOCA, so the result has to be corrected rather than seeded.
        calls = []
        self.patch_object(
            self.target, 'configure_datapath_type',
            side_effect=lambda: calls.append('datapath'))
        with mock.patch.object(
                ovn_chassis.charms.ovn_charm.BaseOVNChassisCharm,
                'configure_bridges',
                side_effect=lambda: calls.append('super')):
            self.target.configure_bridges()
        self.assertEqual(['super', 'datapath'], calls)


class TestConfigureOvs(Helper):

    def setUp(self):
        super().setUp()
        self.patch_object(ovn_chassis.ch_core, 'host')
        self.patch_object(self.target, 'configure_ovs_datapath')
        self.patch_object(self.target, 'check_if_paused',
                          return_value=(None, None))

    def test_mro_places_datapath_below_deferred_events(self):
        # DeferredEventMixin.configure_ovs decides whether reconfiguring Open
        # vSwitch is permitted, so it has to run first.
        names = [c.__name__ for c in
                 ovn_chassis.OVNChassisCharm.__mro__]
        self.assertLess(names.index('_FakeDeferredEventMixin'),
                        names.index('DatapathConfigurationMixin'))

    def test_accepts_check_deferred_events_kwarg(self):
        # actions/os_deferred_event_actions.py calls configure_ovs with this
        # keyword argument.
        self.patch_object(self.target, 'configure_ovs_doca_init',
                          return_value=False)
        self.target.configure_ovs('sb_conn', False,
                                  check_deferred_events=False)
        self.configure_ovs_datapath.assert_called_once_with()

    def test_deferred_restart_touches_nothing(self):
        self.patch_object(self.target, 'configure_ovs_doca_init')
        self.target.restart_permitted = False
        self.target.configure_ovs('sb_conn', False)
        self.configure_ovs_doca_init.assert_not_called()
        self.configure_ovs_datapath.assert_not_called()
        ovn_chassis.ch_core.host.service_start.assert_not_called()
        ovn_chassis.ch_core.host.service_restart.assert_not_called()

    def test_seeds_datapath_before_base_class(self):
        calls = []
        self.configure_ovs_datapath.side_effect = (
            lambda: calls.append('datapath'))
        with mock.patch.object(
                ovn_chassis.charms.ovn_charm.BaseOVNChassisCharm,
                'configure_ovs',
                side_effect=lambda *a: calls.append('super')):
            self.patch_object(self.target, 'configure_ovs_doca_init',
                              return_value=False)
            self.target.configure_ovs('sb_conn', False)
        self.assertEqual(['datapath', 'super'], calls)

    def test_restarts_when_doca_init_changed(self):
        self.patch_object(self.target, 'configure_ovs_doca_init',
                          return_value=True)
        self.target.configure_ovs('sb_conn', False)
        ovn_chassis.ch_core.host.service_restart.assert_called_once_with(
            'openvswitch-switch')

    def test_no_restart_when_doca_init_unchanged(self):
        self.patch_object(self.target, 'configure_ovs_doca_init',
                          return_value=False)
        self.target.configure_ovs('sb_conn', False)
        ovn_chassis.ch_core.host.service_restart.assert_not_called()
        ovn_chassis.ch_core.host.service_start.assert_called_once_with(
            'openvswitch-switch')

    def test_paused_unit_touches_nothing(self):
        self.patch_object(self.target, 'configure_ovs_doca_init')
        self.check_if_paused.return_value = ('maintenance', 'paused')
        self.target.configure_ovs('sb_conn', False)
        self.configure_ovs_doca_init.assert_not_called()
        self.configure_ovs_datapath.assert_not_called()
        ovn_chassis.ch_core.host.service_start.assert_not_called()
        ovn_chassis.ch_core.host.service_restart.assert_not_called()


class TestCustomAssessStatusLastCheck(Helper):

    def test_blocks_when_doca_requested_but_inactive(self):
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system'})
        self.target.options.install_doca = True
        status, message = self.target.custom_assess_status_last_check()
        self.assertEqual('blocked', status)
        self.assertIn('reboot', message)

    def test_ok_when_doca_active(self):
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system', 'doca'})
        self.target.options.install_doca = True
        self.assertEqual(
            (None, None), self.target.custom_assess_status_last_check())

    def test_ok_when_doca_not_requested(self):
        self.patch_object(self.target, 'ovs_datapath_types',
                          return_value={'system'})
        self.assertEqual(
            (None, None), self.target.custom_assess_status_last_check())

    def test_defers_to_base_class(self):
        with mock.patch.object(
                ovn_chassis.charms.ovn_charm.BaseOVNChassisCharm,
                'custom_assess_status_last_check',
                return_value=('blocked', 'base class message')):
            self.target.options.install_doca = True
            self.assertEqual(
                ('blocked', 'base class message'),
                self.target.custom_assess_status_last_check())


class DocaHelper(Helper):

    def setUp(self):
        super().setUp()
        self.target.options.doca_url = 'http://example.com/doca-host.deb'
        self.patch_object(ovn_chassis, 'subprocess')
        self.patch_object(ovn_chassis, 'ch_fetch')
        self.patch_object(ovn_chassis.ch_core, 'host')
        self.patch_object(ovn_chassis.os.path, 'exists', return_value=False)


class TestInstallDocaHostRepository(DocaHelper):

    def test_downloads_and_refreshes_index(self):
        self.target.install_doca_host_repository()
        ovn_chassis.subprocess.check_call.assert_has_calls([
            mock.call(['wget', '-O', '/tmp/doca-host.deb',
                       'http://example.com/doca-host.deb']),
            mock.call(['dpkg', '-i', '/tmp/doca-host.deb']),
        ])
        # The package only adds an apt source, so the index has to be
        # refreshed before the DOCA packages can be installed.
        ovn_chassis.ch_fetch.apt_update.assert_called_once_with(fatal=True)


class TestInstallDocaPackages(DocaHelper):

    def test_repository_installed_first(self):
        calls = []
        self.patch_object(
            self.target, 'install_doca_host_repository',
            side_effect=lambda: calls.append('repository'))
        ovn_chassis.ch_fetch.apt_install.side_effect = (
            lambda *a, **kw: calls.append('install'))
        self.target.install_doca_packages()
        self.assertEqual(['repository', 'install'], calls)
        ovn_chassis.ch_fetch.apt_install.assert_called_once_with(
            ['doca-networking', 'mlnx-fw-updater'], fatal=True)


class TestInstalledDocaPackages(DocaHelper):

    def test_filters_on_name_and_status(self):
        ovn_chassis.subprocess.check_output.return_value = (
            'installed doca-networking\n'
            'installed libdoca-flow0\n'
            'installed flexio-sdk\n'
            'installed mlnx-fw-updater\n'
            # Left over configuration of an already removed package.
            'config-files doca-sha-offload-engine\n'
            'installed openvswitch-switch\n'
        )
        self.assertEqual(
            ['doca-networking', 'libdoca-flow0', 'flexio-sdk',
             'mlnx-fw-updater'],
            self.target.installed_doca_packages())

    def test_nothing_installed(self):
        ovn_chassis.subprocess.check_output.return_value = (
            'installed openvswitch-switch\n')
        self.assertEqual([], self.target.installed_doca_packages())


class TestPurgeDocaPackages(DocaHelper):

    def test_purges_discovered_packages(self):
        self.patch_object(self.target, 'installed_doca_packages',
                          return_value=['doca-networking'])
        self.target.purge_doca_packages()
        ovn_chassis.ch_fetch.apt_purge.assert_called_once_with(
            ['doca-networking'], fatal=True)
        ovn_chassis.ch_fetch.apt_autoremove.assert_called_once_with(
            purge=True, fatal=True)

    def test_no_purge_without_packages(self):
        self.patch_object(self.target, 'installed_doca_packages',
                          return_value=[])
        self.target.purge_doca_packages()
        ovn_chassis.ch_fetch.apt_purge.assert_not_called()
        ovn_chassis.ch_fetch.apt_autoremove.assert_called_once_with(
            purge=True, fatal=True)

    def test_runs_ofed_uninstall_when_present(self):
        self.patch_object(self.target, 'installed_doca_packages',
                          return_value=[])
        self.exists.return_value = True
        self.target.purge_doca_packages()
        ovn_chassis.subprocess.check_call.assert_called_once_with(
            ['/usr/sbin/ofed_uninstall.sh', '--force'])

    def test_skips_ofed_uninstall_when_absent(self):
        self.patch_object(self.target, 'installed_doca_packages',
                          return_value=[])
        self.target.purge_doca_packages()
        ovn_chassis.subprocess.check_call.assert_not_called()


class TestConfigureDoca(DocaHelper):

    def setUp(self):
        super().setUp()
        self.patch_object(self.target, 'install_doca_packages')
        self.patch_object(self.target, 'purge_doca_packages')

    def test_installs_when_enabled(self):
        self.target.options.install_doca = True
        self.target.configure_doca()
        self.install_doca_packages.assert_called_once_with()
        self.purge_doca_packages.assert_not_called()

    def test_purges_when_disabled(self):
        self.target.configure_doca()
        self.purge_doca_packages.assert_called_once_with()
        self.install_doca_packages.assert_not_called()

    def test_ovn_controller_stopped_around_package_change(self):
        # A running ovn-controller would keep reconciling the Open vSwitch
        # database while the packages providing Open vSwitch are replaced
        # underneath it.
        calls = []
        ovn_chassis.ch_core.host.service_stop.side_effect = (
            lambda service: calls.append(('stop', service)))
        ovn_chassis.ch_core.host.service_start.side_effect = (
            lambda service: calls.append(('start', service)))
        self.install_doca_packages.side_effect = (
            lambda: calls.append(('install', None)))
        self.target.options.install_doca = True
        self.target.configure_doca()
        self.assertEqual([
            ('stop', 'ovn-controller'),
            ('install', None),
            ('start', 'ovn-controller'),
        ], calls)

    def test_ovn_controller_started_after_failure(self):
        self.install_doca_packages.side_effect = Exception('apt failed')
        self.target.options.install_doca = True
        with self.assertRaises(Exception):
            self.target.configure_doca()
        ovn_chassis.ch_core.host.service_start.assert_called_once_with(
            'ovn-controller')
