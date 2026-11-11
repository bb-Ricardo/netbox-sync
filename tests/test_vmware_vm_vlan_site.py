# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
A VM in a cluster without a site (no 'cluster_site_relation', or '<NONE>' for a cluster
spanning sites) has no site, while its host always has one, the placeholder site of the
source if nothing else. The VLANs of standard port groups are synced for the host uplinks
with the site of the host. The VMs on these port groups looked their VLANs up without a
site, found none and created each of them a second time without a site. They now look
them up in the site of the host they run on.

The hosts of the vc001 capture have standard port groups with VLANs, but their uplinks also
carry a trunk port group (VLAN 4095), so the hosts sync no VLAN at all. The 'tagged' copy
takes the uplinks out of the trunk port group, so the hosts tag the VLANs of their port groups.
"""
import re
import socket
import subprocess
import tarfile
import time
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import (
    NBCluster, NBClusterType, NBInterface, NBSite, NBVLAN, NBVM, NBVMInterface,
)
from module.sources import instantiate_sources
from module.sources.common.source_base import SourceBase

FIXTURE = Path(__file__).parent / "fixtures" / "vcsim" / "vc001.tar.gz"
TRUNK_PORT_GROUP = "vlantrunk70-71"


def _take_uplinks_out_of_the_trunk(load_dir: Path) -> None:
    patched = 0
    for host_file in load_dir.glob("*-HostSystem-*.xml"):
        xml = host_file.read_text()
        match = re.search(rf"<portgroup>\s*<key>key-vim.host.PortGroup-{TRUNK_PORT_GROUP}</key>.*?</portgroup>",
                          xml, re.S)
        if match is None:
            continue
        port_group = re.sub(r"\s*<(activeNic|standbyNic)>[^<]*</\1>", "", match.group(0))
        host_file.write_text(xml.replace(match.group(0), port_group))
        patched += 1
    assert patched == 3, f"trunk port group found on {patched} of 3 hosts"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _vcsim(vcsim_binary, load_dir: Path):
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


def _extract(tmp_path_factory) -> Path:
    extract_dir = tmp_path_factory.mktemp("vcsim-vlan-site")
    with tarfile.open(FIXTURE) as archive:
        archive.extractall(extract_dir, filter="data")
    return next(p for p in extract_dir.iterdir() if p.is_dir())


@pytest.fixture(scope="module")
def trunk_vcsim(vcsim_binary, tmp_path_factory):
    with _vcsim(vcsim_binary, _extract(tmp_path_factory)) as served:
        yield served


@pytest.fixture(scope="module")
def tagged_vcsim(vcsim_binary, tmp_path_factory):
    load_dir = _extract(tmp_path_factory)
    _take_uplinks_out_of_the_trunk(load_dir)
    with _vcsim(vcsim_binary, load_dir) as served:
        yield served


def _sync(served, inventory, load_config, extra_settings=""):
    load_config(f"""
[netbox]
api_token = not-used-by-these-tests
host_fqdn = 127.0.0.1

[source/vc001]
type = vmware
host_fqdn = {served.host}
port = {served.port}
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

    host_vlans = list()
    for interface in inventory.get_all_items(NBInterface):
        host_vlans.extend(interface.data.get("tagged_vlans") or list())

    vm_vlans = list()
    for interface in inventory.get_all_items(NBVMInterface):
        if interface.data.get("untagged_vlan") is not None:
            vm_vlans.append(interface.data["untagged_vlan"])

    return host_vlans, vm_vlans


def _site_name(vlan):
    site = vlan.data.get("site")
    return None if site is None else site.data["name"]


@pytest.mark.parametrize("extra_settings, site_name", [
    ("", "vCenter: vc001"),
    ("cluster_site_relation = .* = Cluster Site", "Cluster Site"),
], ids=["no-cluster-site", "cluster-site"])
def test_vms_use_the_vlans_of_their_host(tagged_vcsim, inventory, load_config, extra_settings, site_name):
    host_vlans, vm_vlans = _sync(tagged_vcsim, inventory, load_config, extra_settings)

    assert len(vm_vlans) > 0
    assert {_site_name(vlan) for vlan in host_vlans} == {site_name}
    assert all(any(vlan is host_vlan for host_vlan in host_vlans) for vlan in vm_vlans)

    duplicated = [vid for vid, count in Counter(v.data["vid"] for v in inventory.get_all_items(NBVLAN)).items()
                  if count > 1]
    assert duplicated == []


def test_vm_without_site_keeps_no_site(tagged_vcsim, inventory, load_config):
    _sync(tagged_vcsim, inventory, load_config)

    assert {vm.data.get("site") for vm in inventory.get_all_items(NBVM)} == {None}


def test_vlans_the_hosts_do_not_carry_are_still_created_without_site(trunk_vcsim, inventory, load_config):
    host_vlans, vm_vlans = _sync(trunk_vcsim, inventory, load_config)

    assert host_vlans == []
    assert len(vm_vlans) > 0
    assert {_site_name(vlan) for vlan in vm_vlans} == {None}




# the VMs with VLANs of the capture all run on 172.29.50.69
@pytest.mark.parametrize("host_site_relation", [
    "172.29.50.69 = Site B, .* = Site A",
    "172.29.50.69 = Site A, .* = Site B",
], ids=["vm-host-in-site-b", "vm-host-in-site-a"])
def test_vms_of_a_multi_site_cluster_use_the_vlans_of_their_host_site(tagged_vcsim, inventory, load_config,
                                                                     host_site_relation):
    host_vlans, vm_vlans = _sync(tagged_vcsim, inventory, load_config, "\n".join([
        "cluster_site_relation = .* = <NONE>",
        f"host_site_relation = {host_site_relation}",
        "track_vm_host = True",
    ]))

    # the hosts of both sites carry the same VLAN IDs
    assert {_site_name(vlan) for vlan in host_vlans} == {"Site A", "Site B"}

    assert len(vm_vlans) > 0
    for interface in inventory.get_all_items(NBVMInterface):
        vlan = interface.data.get("untagged_vlan")
        if vlan is not None:
            assert vlan.data["site"] is interface.data["virtual_machine"].data["device"].data["site"]


def _source(inventory):
    src = SourceBase()
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.settings = SimpleNamespace(
        skip_fhrp_group_ips=False,
        preserve_primary_ips=False,
        ip_tenant_inheritance_order=["disabled"],
        disable_vlan_sync=False,
        vlan_group_relation_by_id=None,
        vlan_group_relation_by_name=None,
        vlan_sync_exclude_by_id=None,
        vlan_sync_exclude_by_name=None,
    )
    return src


@pytest.mark.parametrize("vlan_field, vlan_data", [
    ("untagged_vlan", {"name": "pg-vlan20", "vid": 20, "site": {"name": None}}),
    ("tagged_vlans", [{"name": "pg-vlan20", "vid": 20, "site": {"name": None}}]),
], ids=["untagged", "tagged"])
@pytest.mark.parametrize("vm_site, lookup_site, expected", [
    (None, "site-a", "site-a"),
    (None, "site-b", "site-b"),
    (None, None, None),
    # the site of the VM itself wins
    ("site-a", "site-b", "site-a"),
], ids=["lookup-site-a", "lookup-site-b", "no-site", "vm-site-wins"])
def test_vlans_of_an_interface_without_site_are_looked_up_in_the_given_site(inventory, vlan_field, vlan_data,
                                                                           vm_site, lookup_site, expected):
    sites = {name: inventory.add_object(NBSite, data={"name": name, "slug": name}, read_from_netbox=True)
             for name in ["site-a", "site-b"]}
    vlans = {name: inventory.add_object(NBVLAN, data={"name": f"vlan20-{name}", "vid": 20, "site": site},
                                        read_from_netbox=True) for name, site in sites.items()}
    vlans[None] = inventory.add_object(NBVLAN, data={"name": "vlan20", "vid": 20}, read_from_netbox=True)

    cluster_type = inventory.add_object(NBClusterType, data={"name": "VMware ESXi"}, read_from_netbox=True)
    cluster = inventory.add_object(NBCluster, data={"name": "cluster", "type": cluster_type}, read_from_netbox=True)
    vm = inventory.add_object(NBVM, data={"name": "vm", "cluster": cluster, "status": "active",
                                          "site": sites.get(vm_site)}, read_from_netbox=True)

    interface, _ = _source(inventory).add_update_interface(
        None, vm, {"name": "vNIC 1", "virtual_machine": vm, vlan_field: vlan_data},
        vlan_lookup_site=sites.get(lookup_site))

    assigned = interface.data[vlan_field]
    if vlan_field == "tagged_vlans":
        assert len(assigned) == 1
        assigned = assigned[0]
    assert assigned is vlans[expected]
