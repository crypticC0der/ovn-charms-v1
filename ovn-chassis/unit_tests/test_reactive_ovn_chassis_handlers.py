# Copyright 2019 Canonical Ltd
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

import reactive.ovn_chassis_handlers as handlers

import charms_openstack.test_utils as test_utils


class TestRegisteredHooks(test_utils.TestRegisteredHooks):

    def setUp(self):
        super().setUp()

    def test_hooks(self):
        hook_set = {
            'when_none': {
                'configure_nrpe': ('charm.paused', 'is-update-status-hook',),
                'configure_doca': ('is-update-status-hook',),
                # Deliberately permitted during the update status hook, as
                # Juju runs no hook when a unit boots.
                'configure_datapath_type': ('charm.paused',),
            },
            'when_not': {
                'enable_ovn_chassis_handlers': ('MOCKED_FLAG',),
                'configure_deferred_restarts': ('is-update-status-hook',),
            },
            'when': {
                'configure_nrpe': ('config.rendered',),
                'configure_doca': ('charm.installed',),
                'configure_datapath_type': ('config.rendered',),
            },
            'when_any': {
                'configure_nrpe': ('config.changed.nagios_context',
                                   'config.changed.nagios_servicegroups',
                                   'endpoint.nrpe-external-master.changed',
                                   'nrpe-external-master.available',),
                'configure_doca': ('config.changed.install-doca',),
            },
        }
        # test that the hooks were registered via the
        # reactive.ovn_handlers
        self.registered_hooks_test_helper(handlers, hook_set, {})


class TestOvnHandlers(test_utils.PatchHelper):

    def test_enable_ovn_chassis_handlers(self):
        self.patch_object(handlers.reactive, 'set_flag')
        handlers.enable_ovn_chassis_handlers()
        self.set_flag.assert_called_once_with('MOCKED_FLAG')

    def test_configure_datapath_type(self):
        self.patch_object(handlers.charm, 'provide_charm_instance')
        instance = self.provide_charm_instance.return_value.__enter__ \
            .return_value
        handlers.configure_datapath_type()
        instance.configure_datapath_type.assert_called_once_with()

    def test_configure_doca(self):
        self.patch_object(handlers.charm, 'provide_charm_instance')
        instance = self.provide_charm_instance.return_value.__enter__ \
            .return_value
        handlers.configure_doca()
        instance.configure_doca.assert_called_once_with()
