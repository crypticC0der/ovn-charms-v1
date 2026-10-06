# Copyright 2018 Canonical Ltd
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

import sys

sys.path.append('src')
sys.path.append('src/lib')

# Mock out charmhelpers so that we can test without it.
import charms_openstack.test_mocks  # noqa
charms_openstack.test_mocks.mock_charmhelpers()

import mock


class _fake_decorator(object):

    def __init__(self, *args):
        pass

    def __call__(self, f):
        return f


charms = mock.MagicMock()
sys.modules['charms'] = charms
charms.leadership = mock.MagicMock()
sys.modules['charms.leadership'] = charms.leadership
charms.reactive = mock.MagicMock()
charms.reactive.when = _fake_decorator
charms.reactive.when_all = _fake_decorator
charms.reactive.when_any = _fake_decorator
charms.reactive.when_not = _fake_decorator
charms.reactive.when_none = _fake_decorator
charms.reactive.when_not_all = _fake_decorator
charms.reactive.not_unless = _fake_decorator
charms.reactive.when_file_changed = _fake_decorator
charms.reactive.collect_metrics = _fake_decorator
charms.reactive.meter_status_changed = _fake_decorator
charms.reactive.only_once = _fake_decorator
charms.reactive.hook = _fake_decorator
charms.reactive.bus = mock.MagicMock()
charms.reactive.flags = mock.MagicMock()
charms.reactive.relations = mock.MagicMock()
sys.modules['charms.reactive'] = charms.reactive
sys.modules['charms.reactive.bus'] = charms.reactive.bus
sys.modules['charms.reactive.bus'] = charms.reactive.decorators
sys.modules['charms.reactive.flags'] = charms.reactive.flags
sys.modules['charms.reactive.relations'] = charms.reactive.relations
sys.modules['charms.leadership'] = charms.leadership
netaddr = mock.MagicMock()
sys.modules['netaddr'] = netaddr

# ``mock_charmhelpers()`` replaces ``charmhelpers.contrib.network`` with a
# MagicMock, which stops its submodules from being imported by name.
sys.modules['charmhelpers.contrib.network.ovs'] = mock.MagicMock()
sys.modules['charmhelpers.contrib.network.ovs.ovsdb'] = mock.MagicMock()


# The real base classes live in the ``layer:ovn`` build-time layer, which is
# assembled by charmcraft and is not available here.  Provide minimal stand
# ins so that ``charm.openstack.ovn_chassis`` can be imported and its own
# overrides exercised.  These deliberately mirror the parts of the real
# classes the charm cooperates with, in particular that
# ``DeferredEventMixin.configure_ovs`` takes a ``check_deferred_events``
# keyword argument and only delegates upwards when a restart is permitted.
class _FakeBaseOVNChassisCharm(object):

    def configure_ovs(self, sb_conn, mlockall_changed):
        pass

    def configure_bridges(self):
        pass

    def check_if_paused(self):
        return (None, None)

    def custom_assess_status_last_check(self):
        return (None, None)


class _FakeDeferredEventMixin(object):

    # Set to False by tests that need to model deferred restarts.
    restart_permitted = True

    def configure_ovs(self, sb_conn, mlockall_changed,
                      check_deferred_events=True):
        if check_deferred_events and not self.restart_permitted:
            return
        super().configure_ovs(sb_conn, mlockall_changed)


charms.ovn_charm = mock.MagicMock()
charms.ovn_charm.BaseOVNChassisCharm = _FakeBaseOVNChassisCharm
charms.ovn_charm.DeferredEventMixin = _FakeDeferredEventMixin
sys.modules['charms.ovn_charm'] = charms.ovn_charm

import reactive
reactive.ovn_chassis_charm_handlers = mock.MagicMock()
reactive.ovn_chassis_charm_handlers.OVN_CHASSIS_ENABLE_HANDLERS_FLAG = \
    'MOCKED_FLAG'
sys.modules['reactive.ovn_chassis_charm_handlers'] = \
    reactive.ovn_chassis_charm_handlers
