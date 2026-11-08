# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
A VMkernel interface without an IPv4 address reports an empty address and mask (Dell
VxRail hosts' vmk0 carries IPv6 link-local addresses only). That is no address to
add and no error: the interface is still synced, just without an IPv4 address,
instead of logging "IP address '/' for vmk0 invalid!" on every run.

vcsim has no UpdateVirtualNic, so the host's address is blanked in a copy of a
captured inventory before vcsim loads it.
"""
import logging
import re
import subprocess
import tarfile
from types import SimpleNamespace

import pytest

from conftest import FIXTURE_DIR, _free_port, _wait_for_port
from module.netbox.object_classes import NBDevice, NBInterface, NBIPAddress
from module.sources import instantiate_sources

DUMP = "vc001"
_VMK0_IPV4 = re.compile(
    r"(<vnic>\s*<device>vmk0</device>.*?<ip>\s*<dhcp>false</dhcp>\s*)"
    r"<ipAddress>([^<]*)</ipAddress>\s*<subnetMask>[^<]*</subnetMask>",
    re.S,
)


@pytest.fixture
def vcsim_host_vmk0_without_ipv4(vcsim_binary, tmp_path):
    with tarfile.open(FIXTURE_DIR / f"{DUMP}.tar.gz") as archive:
        archive.extractall(tmp_path, filter="data")
    load_dir = tmp_path / DUMP

    for host_file in sorted(load_dir.glob("*-HostSystem-*.xml")):
        text = host_file.read_text()
        match = _VMK0_IPV4.search(text)
        if match is not None:
            host_file.write_text(text[:match.start()] + match.group(1) +
                                 "<ipAddress></ipAddress><subnetMask></subnetMask>" + text[match.end():])
            blanked_address = match.group(2)
            break
    else:
        pytest.fail(f"no host in {DUMP} has a vmk0 with a static IPv4 address")

    host, port = "127.0.0.1", _free_port()
    process = subprocess.Popen([vcsim_binary, "-load", str(load_dir), "-l", f"{host}:{port}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _wait_for_port(host, port, process)
        yield SimpleNamespace(host=host, port=port, blanked_address=blanked_address)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def test_vmk_without_ipv4_is_synced_without_error(vcsim_host_vmk0_without_ipv4, inventory, load_config, caplog):
    sim = vcsim_host_vmk0_without_ipv4
    load_config(f"""
[netbox]
api_token = not-used-by-these-tests
host_fqdn = 127.0.0.1

[source/{DUMP}]
type = vmware
host_fqdn = {sim.host}
port = {sim.port}
username = user
password = pass
validate_tls_certs = False
permitted_subnets = 0.0.0.0/0, ::/0
dns_name_lookup = False
""")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful
    inventory.resolve_relations()

    with caplog.at_level(logging.INFO):
        sources[0].apply()

    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert not [e for e in errors if "vmk0" in e], f"errors for vmk0: {errors}"

    hosts = list(inventory.get_all_items(NBDevice))
    vmk0 = [i for i in inventory.get_all_items(NBInterface) if i.data.get("name") == "vmk0"]
    assert hosts and len(vmk0) == len(hosts), "every host must still have its vmk0 interface"

    addresses = [ip.data.get("address", "").split("/")[0] for ip in inventory.get_all_items(NBIPAddress)]
    assert sim.blanked_address not in addresses
    assert "" not in addresses, "an empty IPv4 address was added"
