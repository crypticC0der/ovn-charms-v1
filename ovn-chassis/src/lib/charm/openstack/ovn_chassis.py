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

import contextlib
import os
import re
import subprocess

from charmhelpers.core.host import rsync, write_file
from charmhelpers.contrib.charmsupport import nrpe
import charmhelpers.contrib.network.ovs.ovsdb as ch_ovsdb
import charmhelpers.contrib.openstack.deferred_events as ch_deferred_events
import charmhelpers.fetch as ch_fetch
import charmhelpers.core as ch_core

import charms_openstack.charm as charm

import charms.ovn_charm

NAGIOS_PLUGINS = '/usr/local/lib/nagios/plugins'
SCRIPTS_DIR = '/usr/local/bin'
CERTCHECK_CRONFILE = '/etc/cron.d/ovn-chassis-cert-checks'
CRONJOB_CMD = "{schedule} root {command} 2>&1 | logger -p local0.notice\n"

# The ``doca-networking`` meta package pulls in the DOCA-OFED drivers, the
# DOCA flow libraries and the OVS-DOCA build of Open vSwitch, which together
# make up everything needed for the ``doca`` datapath.  ``mlnx-fw-updater``
# is not a dependency of it, but the DOCA libraries expect a NIC firmware
# version matching the DOCA release they come from.
DOCA_PACKAGES = ['doca-networking', 'mlnx-fw-updater']

# Destination for the DOCA host repository package downloaded from
# ``doca-url``.
DOCA_HOST_DEB = '/tmp/doca-host.deb'

# Package name fragments identifying the members of the DOCA archive.  The
# archive holds a couple of hundred packages whose names carry version and
# build information and whose composition changes between DOCA releases, so
# the installed set is discovered rather than enumerated.
DOCA_PACKAGE_NAME = re.compile(r'doca|flexio|mlnx-fw')

# Script left behind by the DOCA-OFED drivers, which unpicks the parts of the
# installation apt does not know about.
OFED_UNINSTALL = '/usr/sbin/ofed_uninstall.sh'

# Open vSwitch datapath types.  ``system`` is the in-kernel datapath and
# ``netdev`` the DPDK userspace datapath, both provided by upstream Open
# vSwitch.  ``doca`` is a third datapath provided only by NVIDIA's OVS-DOCA
# build of Open vSwitch, which is distributed through the DOCA-OFED archive.
DATAPATH_TYPE_DOCA = 'doca'
DATAPATH_TYPE_NETDEV = 'netdev'
DATAPATH_TYPE_SYSTEM = 'system'

# Open vSwitch interface type to use with each datapath type.  Ports on the
# ``netdev`` and ``doca`` datapaths are driven from userspace, so their
# interface type has to match the datapath of the bridge they are attached
# to.  An interface with an empty type is equivalent to one of type
# ``system``, which is why the latter is used for the normalized form.
INTERFACE_TYPE = {
    DATAPATH_TYPE_DOCA: 'doca',
    DATAPATH_TYPE_NETDEV: 'dpdk',
    DATAPATH_TYPE_SYSTEM: DATAPATH_TYPE_SYSTEM,
}

# Interface options that apply only to the ``doca`` datapath.  OVS-DOCA
# detects link state changes by polling unless told to use the link state
# change interrupt, and we want a bond member going down to be acted on
# promptly rather than at the next poll.
DOCA_INTERFACE_OPTIONS = (('dpdk-lsc-interrupt', 'true'),)

# Service to restart when the datapath type of an existing resource changes.
# ``ovs-vswitchd`` instantiates the datapath, and attaching a port to a
# datapath type it was not opened with leaves the port in error until the
# daemon reconfigures from scratch.  Restarting this one service rather than
# the whole of ``openvswitch-switch`` keeps ``ovsdb-server``, and with it the
# configuration we just wrote, untouched.
DATAPATH_RESTART_SERVICE = 'ovs-vswitchd'


charm.use_defaults('charm.default-select-release')


class DatapathConfigurationMixin(object):
    """Manage Open vSwitch datapath configuration for this unit.

    This is a mixin rather than methods on the charm class so that it can be
    ordered *after* ``charms.ovn_charm.DeferredEventMixin`` in the charm's
    MRO.  ``DeferredEventMixin.configure_ovs`` decides whether reconfiguring
    Open vSwitch is permitted at all, and only then delegates upwards, so
    anything placed ahead of it would run, and restart
    ``openvswitch-switch``, even when the operator has deferred restarts.
    """

    def ovs_datapath_types(self):
        """Datapath types supported by the running ``ovs-vswitchd``.

        On startup ``ovs-vswitchd`` publishes the datapath types it was built
        with in the ``datapath_types`` column of the ``Open_vSwitch`` table,
        which makes this an accurate runtime capability probe.

        We use it in preference to keying off the ``install-doca`` config
        option alone because the two can legitimately disagree: the DOCA
        kernel modules require a reboot before the OVS-DOCA packages are
        functional, and an operator may point ``doca-url`` at an archive that
        does not carry a DOCA enabled Open vSwitch at all.  Writing a datapath
        type the local ``ovs-vswitchd`` does not support would leave
        ``br-int`` unable to instantiate.

        :returns: Supported datapath types
        :rtype: Set[str]
        """
        for row in ch_ovsdb.SimpleOVSDB('ovs-vsctl').open_vswitch:
            datapath_types = row.get('datapath_types', [])
            # SimpleOVSDB has no schema knowledge and presents a set column
            # holding a single value as a plain string.
            if isinstance(datapath_types, str):
                datapath_types = [datapath_types]
            return set(datapath_types)
        return set()

    @property
    def datapath_type(self):
        """Datapath type to use for Open vSwitch resources on this unit.

        Note that the ``doca`` datapath is distinct from, and not a synonym
        for, the ``netdev`` datapath used by DPDK.

        :returns: Datapath type
        :rtype: str
        """
        if (self.options.install_doca and
                DATAPATH_TYPE_DOCA in self.ovs_datapath_types()):
            return DATAPATH_TYPE_DOCA
        if self.options.enable_dpdk:
            return DATAPATH_TYPE_NETDEV
        return DATAPATH_TYPE_SYSTEM

    def _set_open_vswitch(self, column_kv_pairs):
        """Reconcile keys in map columns of the ``Open_vSwitch`` table.

        Values are always written rather than removed when they revert to a
        default, so that a change of charm configuration converges in both
        directions.

        :param column_kv_pairs: Map of map column name, e.g. ``other_config``,
                                to the key/value pairs to ensure are set in
                                that column.
        :type column_kv_pairs: Dict[str,Iterable[Tuple[str,str]]]
        :returns: Whether something changed
        :rtype: bool
        """
        something_changed = False
        opvs = ch_ovsdb.SimpleOVSDB('ovs-vsctl').open_vswitch
        for row in opvs:
            for column, kv_pairs in column_kv_pairs.items():
                for k, v in kv_pairs:
                    if row.get(column, {}).get(k) == v:
                        continue
                    opvs.set('.', '{}:{}'.format(column, k), v)
                    something_changed = True
        return something_changed

    def configure_ovs_doca_init(self):
        """Enable or disable the DOCA offload provider in Open vSwitch.

        ``other_config:doca-init`` defaults to false and is only read when
        ``ovs-vswitchd`` starts, so a restart is required for a change to it
        to take effect.

        :returns: Whether something changed
        :rtype: bool
        """
        return self._set_open_vswitch({
            'other_config': (
                ('doca-init',
                 'true' if self.options.install_doca else 'false'),),
        })

    def configure_ovs_datapath(self):
        """Configure datapath type defaults in the Open vSwitch database.

        ``external_ids:ovn-bridge-datapath-type`` is what makes
        ``ovn-controller`` manage the integration bridge with the datapath we
        want.  ``ovn-controller`` creates ``br-int`` without a datapath type
        and then stamps the value resolved from this external-id onto the
        bridge, falling back to ``system`` when it is unset.  Because it
        re-applies the value on every iteration, setting this also repairs a
        ``br-int`` that ``ovn-controller`` already created as ``system``
        before this charm had a chance to run.

        ``other_config:default-datapath-type`` is an OVS-DOCA extension that
        supplies the datapath type for bridges and interfaces created without
        an explicit one.  It is not set by the DOCA packages, and it is only
        consulted at creation time, so it has to be in the database before
        anything else creates resources.  Upstream Open vSwitch ignores the
        key, so it is safe to set unconditionally.

        :returns: Whether something changed
        :rtype: bool
        """
        datapath_type = self.datapath_type
        return self._set_open_vswitch({
            'external_ids': (
                ('ovn-bridge-datapath-type', datapath_type),),
            'other_config': (
                ('default-datapath-type', datapath_type),),
        })

    def charm_managed_bridges(self):
        """Bridges created by this charm.

        The base charm stamps ``external_ids:charm-ovn-chassis=managed`` on
        every bridge it creates, including ``br-int``, and uses it to find
        them again, so it is the authoritative marker for the bridges we are
        allowed to reconfigure.

        :returns: Bridge rows
        :rtype: Iterator[Dict[str,any]]
        """
        return ch_ovsdb.SimpleOVSDB('ovs-vsctl').bridge.find(
            'external_ids:charm-ovn-chassis=managed')

    def charm_managed_interfaces(self):
        """Interfaces attached to bridges by this charm.

        The base charm stamps ``external_ids:charm-ovn-chassis`` with the
        name of the bridge on every interface it attaches.  Filtering on the
        presence of the key leaves out the internal interfaces Open vSwitch
        creates for the bridges themselves, and the patch ports and tunnel
        ports ``ovn-controller`` manages, neither of which take the datapath
        type of a physical port.

        :returns: Interface rows
        :rtype: Iterator[Dict[str,any]]
        """
        for row in ch_ovsdb.SimpleOVSDB('ovs-vsctl').interface:
            if 'charm-ovn-chassis' in row.get('external_ids', {}):
                yield row

    def configure_bridge_datapath_type(self):
        """Correct the datapath type of the bridges this charm created.

        ``other_config:default-datapath-type`` only applies to bridges
        created without an explicit datapath type, and the base charm always
        passes one, hard coded to ``netdev`` when DPDK is enabled and
        ``system`` otherwise.  It passes it on every call to
        ``configure_bridges``, which happens on nearly every hook, so the
        value has to be corrected each time or the unit is moved back onto
        the kernel datapath minutes after being put on the DOCA one.

        :returns: Whether something changed
        :rtype: bool
        """
        something_changed = False
        datapath_type = self.datapath_type
        bridges = ch_ovsdb.SimpleOVSDB('ovs-vsctl').bridge
        for row in self.charm_managed_bridges():
            if row.get('datapath_type') == datapath_type:
                continue
            bridges.set(row['name'], 'datapath_type', datapath_type)
            something_changed = True
        return something_changed

    def configure_interface_type(self):
        """Correct the type of the interfaces this charm attached.

        The base charm resolves the interface type through
        ``charmhelpers.contrib.openstack.context.BridgePortInterfaceMap``,
        which only knows about the interface types of upstream Open vSwitch
        and therefore cannot express ``doca``.  As with the bridges it is
        re-applied on nearly every hook, so it has to be corrected each time.

        :returns: Whether something changed
        :rtype: bool
        """
        something_changed = False
        datapath_type = self.datapath_type
        interface_type = INTERFACE_TYPE[datapath_type]
        interfaces = ch_ovsdb.SimpleOVSDB('ovs-vsctl').interface
        for row in self.charm_managed_interfaces():
            name = row['name']
            # Open vSwitch presents an interface with no explicit type as
            # having an empty one, which is the same thing as ``system``.
            if (row.get('type') or DATAPATH_TYPE_SYSTEM) != interface_type:
                interfaces.set(name, 'type', interface_type)
                something_changed = True
            if self.configure_doca_interface_options(
                    row, datapath_type == DATAPATH_TYPE_DOCA):
                something_changed = True
        return something_changed

    def configure_doca_interface_options(self, interface, enabled):
        """Reconcile the DOCA specific options of a single interface.

        The options are removed rather than written with a default when the
        unit is not on the DOCA datapath, as they are not recognized by
        upstream Open vSwitch at all.

        :param interface: Interface row to reconcile.
        :type interface: Dict[str,any]
        :param enabled: Whether the options should be present.
        :type enabled: bool
        :returns: Whether something changed
        :rtype: bool
        """
        something_changed = False
        name = interface['name']
        options = interface.get('options', {})
        interfaces = ch_ovsdb.SimpleOVSDB('ovs-vsctl').interface
        for key, value in DOCA_INTERFACE_OPTIONS:
            if enabled and options.get(key) != value:
                interfaces.set(name, 'options:{}'.format(key), value)
                something_changed = True
            elif not enabled and key in options:
                interfaces.remove(name, 'options', key)
                something_changed = True
        return something_changed

    def configure_datapath_type(self):
        """Correct the datapath type of existing Open vSwitch resources.

        The corrections are idempotent and derived from the capabilities of
        the running ``ovs-vswitchd``, so this is safe to call from any hook,
        which is what lets a unit converge after the reboot that activates
        its DOCA kernel modules.

        ``ovs-vswitchd`` opens the datapath when it instantiates a bridge, so
        a resource that changed datapath type stays broken until the daemon
        reconfigures from scratch.  The restart is requested through the
        deferred event machinery because, unlike ``configure_ovs``, this is
        not gated on restarts being permitted.

        :returns: Whether something changed
        :rtype: bool
        """
        if not ch_core.host.service_running('ovsdb-server'):
            # The database is unavailable, for example because the DOCA
            # packages are in the middle of being installed.  There is
            # nothing to correct until it is back.
            ch_core.hookenv.log(
                'ovsdb-server is not running, skipping datapath type '
                'configuration.', level=ch_core.hookenv.INFO)
            return False
        something_changed = self.configure_bridge_datapath_type()
        # Both are always run, as the interfaces need correcting even when
        # the bridges were already correct.
        if self.configure_interface_type():
            something_changed = True
        if something_changed:
            ch_deferred_events.deferrable_svc_restart(
                DATAPATH_RESTART_SERVICE,
                reason='datapath type changed')
        return something_changed

    def configure_bridges(self):
        """Run the inherited tasks, then correct the datapath type.

        The base charm creates the bridges and attaches the interfaces with a
        datapath type it resolves itself, with no knowledge of DOCA, so the
        result has to be corrected afterwards rather than seeded beforehand.
        """
        super().configure_bridges()
        self.configure_datapath_type()

    def configure_ovs(self, sb_conn, mlockall_changed):
        """Seed datapath configuration, then run the inherited tasks.

        The inherited implementation sets ``external_ids:ovn-remote``, which
        is what makes the local ``ovn-controller`` start creating resources,
        and goes on to create the bridges this unit needs, so the datapath
        defaults have to be in the database before it runs.

        :param sb_conn: Comma separated string of OVSDB connection methods.
        :type sb_conn: str
        :param mlockall_changed: Whether the mlockall config option changed.
        :type mlockall_changed: bool
        """
        if self.check_if_paused() != (None, None):
            # Let the inherited implementation log and skip run-time
            # configuration.
            return super().configure_ovs(sb_conn, mlockall_changed)

        # ``ovs-vsctl`` talks to ``ovsdb-server``, so the service must run
        # before any of the calls below or they will hang.
        ch_core.host.service_start('openvswitch-switch')
        if self.configure_ovs_doca_init():
            # Restart before probing the supported datapath types, as
            # ``doca-init`` is only read on daemon startup.
            ch_core.host.service_restart('openvswitch-switch')
        self.configure_ovs_datapath()

        super().configure_ovs(sb_conn, mlockall_changed)

    def custom_assess_status_last_check(self):
        """Surface a requested but inactive DOCA datapath.

        Without this the most likely failure mode, ``install-doca`` being
        enabled while the running ``ovs-vswitchd`` has no DOCA support (most
        commonly because the pending reboot for the DOCA kernel modules has
        not happened yet), is a silent fall back to the kernel datapath.

        :returns: status & message info
        :rtype: (status, message) or (None, None)
        """
        status, message = super().custom_assess_status_last_check()
        if status is not None:
            return status, message

        if (self.options.install_doca and
                self.datapath_type != DATAPATH_TYPE_DOCA):
            return ('blocked',
                    'install-doca is enabled but ovs-vswitchd has no "{}" '
                    'datapath support, a reboot may be required.'
                    .format(DATAPATH_TYPE_DOCA))

        return None, None


class OVNChassisCharm(charms.ovn_charm.DeferredEventMixin,
                      DatapathConfigurationMixin,
                      charms.ovn_charm.BaseOVNChassisCharm):
    # OpenvSwitch and OVN is distributed as part of the Ubuntu Cloud Archive
    # Pockets get their name from OpenStack releases.
    #
    # This defines the earliest version this charm can support, actually
    # installed version is selected by the principle charm.
    release = 'ussuri'
    name = 'ovn-chassis'

    # packages needed by nrpe checks
    nrpe_packages = ['python3-cryptography']

    # Setting an empty source_config_key activates special handling of release
    # selection suitable for subordinate charms
    source_config_key = ''

    @property
    def packages(self):
        return super().packages + self.nrpe_packages

    def install(self, check_deferred_events=True):
        super().install(check_deferred_events=check_deferred_events)

    @contextlib.contextmanager
    def ovn_controller_stopped(self):
        """Run the wrapped block with ``ovn-controller`` stopped.

        A running ``ovn-controller`` would keep reconciling the Open vSwitch
        database while the packages providing Open vSwitch are replaced
        underneath it, so it is stopped for the duration.  On the way down it
        withdraws its port bindings, chassis record and tunnel ports.

        Note that it does *not* remove the patch ports linking ``br-int`` to
        the physical bridges; those are only garbage collected while the
        daemon is running, so a datapath type change leaves them behind.

        :returns: Context manager
        """
        ch_core.host.service_stop('ovn-controller')
        try:
            yield
        finally:
            ch_core.host.service_start('ovn-controller')

    def install_doca_host_repository(self):
        """Install the NVIDIA DOCA host repository package.

        The package carries a copy of the DOCA archive and adds an apt source
        pointing at it, hence the package index refresh afterwards.

        It is downloaded from ``doca-url`` at runtime because the Canonical
        DOCA-OFED PPA is only available for releases newer than Noble.
        """
        subprocess.check_call(
            ['wget', '-O', DOCA_HOST_DEB, self.options.doca_url])
        subprocess.check_call(['dpkg', '-i', DOCA_HOST_DEB])
        ch_fetch.apt_update(fatal=True)

    def install_doca_packages(self):
        """Install the DOCA packages from the DOCA host repository."""
        self.install_doca_host_repository()
        ch_fetch.apt_install(DOCA_PACKAGES, fatal=True)

    def installed_doca_packages(self):
        """Installed packages originating from the DOCA archive.

        :returns: Package names
        :rtype: List[str]
        """
        packages = subprocess.check_output(
            ['dpkg-query', '-W', '-f=${db:Status-Status} ${binary:Package}\n'],
            universal_newlines=True)
        return [
            package
            for status, package in (
                line.split() for line in packages.splitlines() if line)
            if status == 'installed' and DOCA_PACKAGE_NAME.search(package)]

    def purge_doca_packages(self):
        """Remove the DOCA packages and anything they pulled in.

        The DOCA-OFED drivers install files apt does not track, so its
        uninstall script is run afterwards to complete the removal.
        """
        packages = self.installed_doca_packages()
        if packages:
            ch_fetch.apt_purge(packages, fatal=True)
        ch_fetch.apt_autoremove(purge=True, fatal=True)
        if os.path.exists(OFED_UNINSTALL):
            subprocess.check_call([OFED_UNINSTALL, '--force'])

    def configure_doca(self):
        """Install or remove the DOCA packages as configured.

        The DOCA kernel modules only take effect on a fresh boot, so the
        ``doca`` datapath does not become available until the unit is
        rebooted.  That reboot is deliberately left to the operator, as
        rebooting a hypervisor evicts its workloads and needs sequencing with
        the rest of the deployment; the unit reports the requirement through
        its workload status instead.
        """
        with self.ovn_controller_stopped():
            if self.options.install_doca:
                self.install_doca_packages()
            else:
                self.purge_doca_packages()

    def render_nrpe(self):
        hostname = nrpe.get_nagios_hostname()
        self.add_nrpe_certs_check(nrpe.NRPE(hostname=hostname))
        super().render_nrpe()

    def add_nrpe_certs_check(self, charm_nrpe):
        script = 'nrpe_check_ovn_certs.py'
        src = os.path.join(os.getenv('CHARM_DIR'), 'files', 'nagios', script)
        dst = os.path.join(NAGIOS_PLUGINS, script)
        rsync(src, dst)
        charm_nrpe.add_check(
            shortname='check_ovn_certs',
            description='Check that ovn certs are valid.',
            check_cmd=script
        )
        # Need to install this as a system package since it is needed by the
        # cron script that runs outside of the charm.
        ch_fetch.apt_install(['python3-cryptography'])
        script = 'check_ovn_certs.py'
        src = os.path.join(os.getenv('CHARM_DIR'), 'files', 'scripts', script)
        dst = os.path.join(SCRIPTS_DIR, script)
        rsync(src, dst)
        cronjob = CRONJOB_CMD.format(
            schedule='*/15 * * * *',
            command=dst)
        write_file(CERTCHECK_CRONFILE, cronjob)
