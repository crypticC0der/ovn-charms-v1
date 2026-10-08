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

import configparser
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import jinja2
import charms_openstack.test_utils as test_utils


SOURCE = Path(__file__).resolve().parents[1] / 'src'
spec = importlib.util.spec_from_file_location(
    'metadata_provider',
    SOURCE / 'hooks/relations/neutron-plugin/provides.py')
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)


class TestMetadataRelation(test_utils.PatchHelper):

    def setUp(self):
        super().setUp()
        self.patch_object(provider, 'hookenv')
        self.patch_object(provider, 'toggle_flag')
        self.hookenv.leader_get.return_value = None
        self.hookenv.is_leader.return_value = False
        self.endpoint = provider.NeutronPluginProvides()
        self.endpoint.expand_name = lambda flag: flag.replace(
            '{endpoint_name}', 'nova-compute')
        self.endpoint.all_joined_units = [self.unit('192.0.2.10')]

    @staticmethod
    def unit(address):
        return SimpleNamespace(received_raw={'private-address': address})

    def test_leader_creates_persistent_secret(self):
        secrets = {}
        self.hookenv.leader_get.side_effect = secrets.get
        self.hookenv.leader_set.side_effect = secrets.update
        self.hookenv.is_leader.return_value = True
        self.patch_object(provider.uuid, 'uuid4', return_value='shared-secret')

        self.assertEqual('shared-secret',
                         self.endpoint.get_or_create_shared_secret())
        self.assertEqual('shared-secret',
                         self.endpoint.get_or_create_shared_secret())
        self.uuid4.assert_called_once_with()
        self.hookenv.leader_set.assert_called_once_with(
            {'metadata-shared-secret': 'shared-secret'})

    def test_follower_waits_for_secret(self):
        self.endpoint.manage_flags()
        self.toggle_flag.assert_called_once_with('nova-compute.connected',
                                                 False)
        self.hookenv.leader_set.assert_not_called()

        self.hookenv.leader_get.return_value = 'shared-secret'
        self.endpoint.manage_flags()
        self.toggle_flag.assert_called_with('nova-compute.connected', True)

    def test_new_leader_reuses_secret(self):
        self.hookenv.is_leader.return_value = True
        self.hookenv.leader_get.return_value = 'existing-secret'
        self.assertEqual('existing-secret',
                         self.endpoint.get_or_create_shared_secret())
        self.hookenv.leader_set.assert_not_called()

    def test_disconnected_without_compute_address(self):
        self.hookenv.leader_get.return_value = 'shared-secret'
        for units in ([], [self.unit(None)]):
            with self.subTest(units=units):
                self.endpoint.all_joined_units = units
                self.endpoint.manage_flags()
                self.toggle_flag.assert_called_with('nova-compute.connected',
                                                    False)

    def test_selects_remaining_compute_when_unit_departs(self):
        remaining = self.unit('192.0.2.11')
        self.endpoint.all_joined_units.append(remaining)
        self.assertEqual('192.0.2.10', self.endpoint.metadata_host)
        self.endpoint.all_joined_units = [remaining]
        self.assertEqual('192.0.2.11', self.endpoint.metadata_host)

    def test_skips_compute_without_address(self):
        self.endpoint.all_joined_units.insert(0, self.unit(None))
        self.assertEqual('192.0.2.10', self.endpoint.metadata_host)

    def test_all_dpus_publish_same_raw_secret_to_all_relations(self):
        self.hookenv.leader_get.return_value = 'shared-secret'
        for is_leader in (True, False):
            with self.subTest(is_leader=is_leader):
                self.hookenv.is_leader.return_value = is_leader
                endpoint = provider.NeutronPluginProvides()
                endpoint.relations = [
                    SimpleNamespace(to_publish_raw={}),
                    SimpleNamespace(to_publish_raw={}),
                ]
                endpoint.publish_shared_secret()
                for relation in endpoint.relations:
                    self.assertEqual(
                        {'metadata-shared-secret': 'shared-secret'},
                        relation.to_publish_raw)
        self.hookenv.leader_set.assert_not_called()

    def test_does_not_publish_missing_secret(self):
        relation = SimpleNamespace(to_publish_raw={})
        self.endpoint.relations = [relation]
        self.endpoint.publish_shared_secret()
        self.assertEqual({}, relation.to_publish_raw)


class TestMetadataConfiguration(test_utils.PatchHelper):

    def test_renders_remote_api_and_ovn_connections(self):
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(SOURCE / 'templates'),
            undefined=jinja2.StrictUndefined)
        template = env.get_template('neutron_ovn_metadata_agent.ini')
        for address in ('192.0.2.10', '2001:db8::10'):
            with self.subTest(address=address):
                config = configparser.ConfigParser()
                config.read_string(template.render(
                    options=SimpleNamespace(
                        debug=False,
                        ovn_key='/etc/ovn/key_host',
                        ovn_cert='/etc/ovn/cert_host',
                        ovn_ca_cert='/etc/ovn/ovn-dedicated-chassis.crt'),
                    nova_compute=SimpleNamespace(
                        metadata_host=address,
                        metadata_shared_secret='shared-secret'),
                    ovsdb=SimpleNamespace(db_sb_connection_strs=[
                        'ssl:192.0.2.20:6642', 'ssl:192.0.2.21:6642'])))
                self.assertEqual(address,
                                 config['DEFAULT']['nova_metadata_host'])
                self.assertEqual('shared-secret', config['DEFAULT'][
                    'metadata_proxy_shared_secret'])
                self.assertEqual('tcp:127.0.0.1:6640',
                                 config['ovs']['ovsdb_connection'])
                self.assertEqual(
                    'ssl:192.0.2.20:6642,ssl:192.0.2.21:6642',
                    config['ovn']['ovn_sb_connection'])
                self.assertEqual('/etc/ovn/key_host',
                                 config['ovn']['ovn_sb_private_key'])
                self.assertEqual('/etc/ovn/cert_host',
                                 config['ovn']['ovn_sb_certificate'])
                self.assertEqual('/etc/ovn/ovn-dedicated-chassis.crt',
                                 config['ovn']['ovn_sb_ca_cert'])
