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
