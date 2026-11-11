# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Dummy interfaces are guest NICs reported by VMware Tools that have no virtual network
card behind them, for example the dummy interfaces carrying anycast addresses on DNS
servers (#367). 'sync_vm_dummy_interfaces' syncs them for every VM, while
'vm_dummy_interfaces_include_filter' syncs them only for VMs whose name matches.

Neither captured vCenter has such a NIC, so this test starts vcsim from a copy of the
vchvr capture in which two VMs report one extra guest NIC each.
"""
import re
import socket
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import NBIPAddress, NBVMInterface
from module.sources import instantiate_sources

FIXTURE = Path(__file__).parent / "fixtures" / "vcsim" / "vchvr.tar.gz"

# VM name -> (MAC, address) of the dummy NIC added to its guest info
DUMMY_NICS = {
    "vm031_git001": ("02:00:00:00:53:01", "192.0.2.53"),
    "ABCDC03-HAJ": ("02:00:00:00:53:02", "192.0.2.54"),
}

DUMMY_NET = """      <net>
        <network></network>
        <ipAddress>{address}</ipAddress>
        <macAddress>{mac}</macAddress>
        <connected>true</connected>
        <deviceConfigId>-1</deviceConfigId>
        <ipConfig>
          <ipAddress>
            <ipAddress>{address}</ipAddress>
            <prefixLength>32</prefixLength>
            <origin>manual</origin>
            <state>preferred</state>
          </ipAddress>
        </ipConfig>
      </net>
"""


def _vm_name(xml: str) -> str:
    match = re.search(r"<name>name</name>\s*<val[^>]*>([^<]*)</val>", xml)
    return match.group(1) if match else ""


def _add_dummy_nics(load_dir: Path) -> None:
    patched = set()
    for vm_file in load_dir.glob("*-VirtualMachine-*.xml"):
        xml = vm_file.read_text()
        name = _vm_name(xml)
        if name not in DUMMY_NICS:
            continue
        mac, address = DUMMY_NICS[name]
        # append the dummy NIC after the guest NICs the capture already has
        last_net_end = xml.rindex("</net>") + len("</net>\n")
        vm_file.write_text(xml[:last_net_end] + DUMMY_NET.format(mac=mac, address=address) + xml[last_net_end:])
        patched.add(name)
    assert patched == set(DUMMY_NICS), f"VMs not found in the capture: {set(DUMMY_NICS) - patched}"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def dummy_vcsim(vcsim_binary, tmp_path_factory):
    extract_dir = tmp_path_factory.mktemp("vcsim-dummy")
    with tarfile.open(FIXTURE) as archive:
        archive.extractall(extract_dir, filter="data")
    load_dir = next(p for p in extract_dir.iterdir() if p.is_dir())
    _add_dummy_nics(load_dir)

    host, port = "127.0.0.1", _free_port()
    process = subprocess.Popen([vcsim_binary, "-load", str(load_dir), "-l", f"{host}:{port}"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 30
        while True:
            assert process.poll() is None, process.stderr.read().decode()
            try:
                socket.create_connection((host, port), timeout=1).close()
                break
            except OSError:
                assert time.monotonic() < deadline, "vcsim did not start"
                time.sleep(0.2)
        yield SimpleNamespace(host=host, port=port)
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _sync(dummy_vcsim, inventory, load_config, extra_settings: str):
    load_config(f"""
[netbox]
api_token = not-used-by-these-tests
host_fqdn = 127.0.0.1

[source/vchvr]
type = vmware
host_fqdn = {dummy_vcsim.host}
port = {dummy_vcsim.port}
username = user
password = pass
validate_tls_certs = False
permitted_subnets = 0.0.0.0/0, ::/0
dns_name_lookup = False
{extra_settings}
""")
    sources = instantiate_sources()
    assert len(sources) == 1 and sources[0].init_successful, "VMware source failed to initialise"
    inventory.resolve_relations()
    sources[0].apply()

    dummy_interfaces = {}
    for interface in inventory.get_all_items(NBVMInterface):
        if interface.data.get("name", "").startswith("vNIC Dummy-"):
            dummy_interfaces[interface.data["virtual_machine"].data["name"]] = interface

    addresses = {}
    for ip in inventory.get_all_items(NBIPAddress):
        assigned = ip.data.get("assigned_object_id")
        if assigned in dummy_interfaces.values():
            addresses[assigned.data["virtual_machine"].data["name"]] = ip.data["address"]
    return dummy_interfaces, addresses


@pytest.mark.parametrize("extra_settings, expected_vms", [
    ("", set()),
    ("sync_vm_dummy_interfaces = True", set(DUMMY_NICS)),
    ("vm_dummy_interfaces_include_filter = vm031_git001$", {"vm031_git001"}),
    ("vm_dummy_interfaces_include_filter = ABCDC03", {"ABCDC03-HAJ"}),
    ("vm_dummy_interfaces_include_filter = nothing-matches", set()),
    # anchored at the beginning of the name like 'vm_include_filter'
    ("vm_dummy_interfaces_include_filter = git001", set()),
    ("sync_vm_dummy_interfaces = True\nvm_dummy_interfaces_include_filter = vm031_git001$", set(DUMMY_NICS)),
], ids=["default", "all", "filter-one", "filter-prefix", "filter-none", "filter-anchored", "all-with-filter"])
def test_dummy_interfaces_follow_the_vm_filter(dummy_vcsim, inventory, load_config, extra_settings, expected_vms):
    dummy_interfaces, addresses = _sync(dummy_vcsim, inventory, load_config, extra_settings)

    assert set(dummy_interfaces) == expected_vms
    for vm_name in expected_vms:
        mac, address = DUMMY_NICS[vm_name]
        assert dummy_interfaces[vm_name].data["name"] == "vNIC Dummy-{}".format("".join(mac.split(":")[-2:]))
        assert addresses[vm_name] == f"{address}/32"


def test_invalid_filter_fails_the_source(dummy_vcsim, inventory, load_config):
    load_config(f"""
[netbox]
api_token = not-used-by-these-tests
host_fqdn = 127.0.0.1

[source/vchvr]
type = vmware
host_fqdn = {dummy_vcsim.host}
port = {dummy_vcsim.port}
username = user
password = pass
vm_dummy_interfaces_include_filter = ns[0-9
""")
    with pytest.raises(SystemExit):
        instantiate_sources()
