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


charm.use_defaults('charm.default-select-release')


class OVNChassisCharm(charms.ovn_charm.DeferredEventMixin,
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
