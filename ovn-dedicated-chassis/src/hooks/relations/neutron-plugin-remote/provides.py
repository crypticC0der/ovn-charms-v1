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

"""The neutron-plugin-remote metadata protocol for remotely managed chassis.

This provider is local to the charm because the subordinate interface creates
per-unit secrets. With a global relation every compute sees every DPU, so all
DPUs must publish and use the same secret. A distinct interface keeps remote
chassis separate from container-scoped neutron-plugin relations.
"""

import uuid

from charmhelpers.core import hookenv
from charms.reactive import Endpoint, toggle_flag, when, when_not


METADATA_KEY = 'metadata-shared-secret'


class NeutronPluginRemoteProvides(Endpoint):
    """Provide metadata proxy credentials to remote Nova compute hosts."""

    @when('endpoint.{endpoint_name}.joined')
    @when_not('is-update-status-hook')
    def manage_flags(self):
        """Enable the shared OVN handlers once host and secret are available.

        Endpoint calls this during relation hooks. The reactive handler also
        retries on leader election/settings changes, when a follower may first
        receive the secret without a change to the compute relation data.
        """
        ready = bool(self.metadata_host and self.get_or_create_shared_secret())
        toggle_flag(self.expand_name('{endpoint_name}.connected'), ready)

    @property
    def metadata_host(self):
        """Select a related compute API, consistently across hooks.

        Endpoint excludes departing units from all_joined_units. Selecting a
        replacement therefore also updates the rendered configuration when the
        previously selected compute unit leaves the relation.
        """
        for unit in self.all_joined_units:
            address = unit.received_raw.get('private-address')
            if address:
                return address
        return None

    def get_or_create_shared_secret(self):
        """Return the application secret, creating it only on the leader."""
        secret = hookenv.leader_get(METADATA_KEY)
        if not secret and hookenv.is_leader():
            secret = str(uuid.uuid4())
            hookenv.leader_set({METADATA_KEY: secret})
        return secret

    def publish_shared_secret(self):
        """Enable Nova's metadata API with the secret used on every DPU."""
        secret = self.get_or_create_shared_secret()
        if secret:
            for relation in self.relations:
                relation.to_publish_raw[METADATA_KEY] = secret
