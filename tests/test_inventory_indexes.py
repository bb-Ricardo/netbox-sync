# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
The inventory keeps indexes so lookups don't have to compare every object of a type. These
tests check that every indexed lookup returns exactly what the plain scan over all objects
returned before, also after objects were renamed, moved, re-addressed or replaced behind
the inventory's back.
"""
import json
import random
from ipaddress import ip_address, ip_network
from types import SimpleNamespace

import pytest

from module.common.misc import grab
from module.netbox.object_classes import (
    NBDataDict, NBSite, NBCluster, NBVM, NBVMInterface, NBIPAddress, NBMACAddress, NBPrefix, NBDevice,
    NBInterface, NBVirtualDisk, NBVLAN, NBClusterType,
)
from module.sources.common.source_base import SourceBase

TS = "2026-01-01T00:00:00Z"


# reference implementations: the scans the inventory used before it had indexes

def scan_get_by_data(inv, object_type, data):
    object_name_to_find = None
    for this_object in inv.get_all_items(object_type):
        if object_name_to_find is None:
            object_name_to_find = this_object.get_display_name(data, including_second_key=True)
        if f"{object_name_to_find}".lower() == f"{this_object.get_display_name(including_second_key=True)}".lower():
            return this_object
    return None


def scan_get_by_id(inv, object_type, nb_id):
    for this_object in inv.get_all_items(object_type):
        if this_object.nb_id == nb_id:
            return this_object
    return None


def scan_get_all_interfaces(inv, this_object):
    if isinstance(this_object, NBVM):
        return [i for i in inv.get_all_items(NBVMInterface) if grab(i, "data.virtual_machine") == this_object]
    return [i for i in inv.get_all_items(NBInterface) if grab(i, "data.device") == this_object]


def scan_ips(inv, address):
    return [ip for ip in inv.get_all_items(NBIPAddress)
            if grab(ip, "data.address", fallback="").startswith(f"{address}/")]


def scan_longest_prefix(inv, ip, site_object):
    longest_length, longest = 0, None
    for prefix in inv.get_all_items(NBPrefix):
        if not prefix.matches_site(site_object):
            continue
        network = grab(prefix, "data.prefix")
        if network is None:
            continue
        if ip in network and network.prefixlen >= longest_length:
            longest_length, longest = network.prefixlen, prefix
    return longest


def indexed_longest_prefix(inv, ip, site_name):
    return SourceBase.return_longest_matching_prefix_for_ip(SimpleNamespace(inventory=inv), ip, site_name)


@pytest.fixture
def base(inventory):
    inventory.load_from_netbox(NBSite, [{"id": 1, "name": "S1", "slug": "s1", "last_updated": TS},
                                        {"id": 2, "name": "S2", "slug": "s2", "last_updated": TS}])
    inventory.load_from_netbox(NBClusterType, [{"id": 1, "name": "T", "slug": "t", "last_updated": TS}])
    inventory.load_from_netbox(NBCluster, [
        {"id": 1, "name": "C1", "type": {"id": 1}, "scope_type": "dcim.site", "scope_id": 1, "last_updated": TS},
        {"id": 2, "name": "C2", "type": {"id": 1}, "scope_type": "dcim.site", "scope_id": 2, "last_updated": TS},
    ])
    inventory.resolve_relations()
    return SimpleNamespace(inventory=inventory,
                           c1=inventory.get_by_id(NBCluster, nb_id=1), c2=inventory.get_by_id(NBCluster, nb_id=2),
                           s1=inventory.get_by_id(NBSite, nb_id=1), s2=inventory.get_by_id(NBSite, nb_id=2))


def add_vm(inv, name, cluster, nb_id):
    return inv.load_from_netbox(NBVM, [{"id": nb_id, "name": name, "cluster": cluster, "last_updated": TS}]) or \
        inv.get_all_items(NBVM)[-1]


def test_data_is_a_tracking_dict(base):
    vm = add_vm(base.inventory, "vm1", {"id": 1}, 10)
    assert isinstance(vm.data, NBDataDict) and isinstance(vm.data, dict)
    assert vm.data == {"id": 10, "name": "vm1", "cluster": {"id": 1}, "last_updated": TS}
    assert json.loads(json.dumps(vm.data))["name"] == "vm1"
    vm.data = {"id": 10, "name": "other"}
    assert isinstance(vm.data, NBDataDict)


def test_lookup_is_case_insensitive(base):
    inv = base.inventory
    add_vm(inv, "MyVM", {"id": 1}, 10)
    inv.resolve_relations()
    found = inv.get_by_data(NBVM, data={"name": "myvm", "cluster": base.c1})
    assert found is not None and found.data["name"] == "MyVM"
    assert inv.get_by_data(NBVM, data={"name": "MYVM", "cluster": base.c2}) is None


def test_rename_moves_object_in_index(base):
    inv = base.inventory
    vm = add_vm(inv, "old", {"id": 1}, 10)
    inv.resolve_relations()
    assert inv.get_by_data(NBVM, data={"name": "old", "cluster": base.c1}) is vm

    vm.update(data={"name": "new"})
    assert inv.get_by_data(NBVM, data={"name": "new", "cluster": base.c1}) is vm
    assert inv.get_by_data(NBVM, data={"name": "old", "cluster": base.c1}) is None

    # direct writes to the data dict are tracked as well
    vm.data["name"] = "direct"
    assert inv.get_by_data(NBVM, data={"name": "direct", "cluster": base.c1}) is vm
    vm.data = dict(vm.data, name="replaced")
    assert inv.get_by_data(NBVM, data={"name": "replaced", "cluster": base.c1}) is vm
    assert inv.get_by_data(NBVM, data={"name": "direct", "cluster": base.c1}) is None


def test_duplicate_names_return_first_in_inventory_order(base):
    inv = base.inventory
    first = add_vm(inv, "dup", {"id": 1}, 10)
    second = add_vm(inv, "DUP", {"id": 1}, 11)
    inv.resolve_relations()
    assert inv.get_by_data(NBVM, data={"name": "dup", "cluster": base.c1}) is first

    # renaming the first one away and back keeps the inventory order, not the order of renames
    first.update(data={"name": "away"})
    assert inv.get_by_data(NBVM, data={"name": "dup", "cluster": base.c1}) is second
    first.update(data={"name": "dup"})
    assert inv.get_by_data(NBVM, data={"name": "dup", "cluster": base.c1}) is first


def test_secondary_key_change_is_found(base):
    inv = base.inventory
    vm = add_vm(inv, "vm1", {"id": 1}, 10)
    inv.resolve_relations()
    vm.update(data={"cluster": base.c2})
    assert inv.get_by_data(NBVM, data={"name": "vm1", "cluster": base.c2}) is vm
    assert inv.get_by_data(NBVM, data={"name": "vm1", "cluster": base.c1}) is None

    # renaming the referenced object changes the display name of the VM without touching the VM
    base.c2.update(data={"name": "C2-renamed"})
    assert inv.get_by_data(NBVM, data={"name": "vm1", "cluster": base.c2}) is vm


def test_names_with_parentheses(base):
    inv = base.inventory
    tricky = add_vm(inv, "a (C1", {"id": 1}, 10)
    plain = add_vm(inv, "a", {"id": 1}, 11)
    inv.resolve_relations()
    for data in ({"name": "a (C1", "cluster": base.c1}, {"name": "a", "cluster": base.c1},
                 {"name": "a (c1)", "cluster": base.c1}):
        assert inv.get_by_data(NBVM, data=data) is scan_get_by_data(inv, NBVM, data)
    assert inv.get_by_data(NBVM, data={"name": "a", "cluster": base.c1}) is plain
    assert inv.get_by_data(NBVM, data={"name": "a (C1", "cluster": base.c1}) is tricky


def test_vlan_display_names(base):
    inv = base.inventory
    inv.load_from_netbox(NBVLAN, [{"id": 1, "vid": 100, "name": "one", "site": {"id": 1}, "last_updated": TS},
                                  {"id": 2, "vid": 100, "name": "two", "site": {"id": 2}, "last_updated": TS},
                                  {"id": 3, "vid": 200, "name": "three", "last_updated": TS}])
    inv.resolve_relations()
    for data in ({"vid": 100, "name": "x", "site": base.s2}, {"vid": 100, "name": "x", "site": base.s1},
                 {"vid": 200, "name": "three"}, {"vid": 200, "name": "THREE"}, {"vid": 300, "name": "x"}):
        assert inv.get_by_data(NBVLAN, data=data) is scan_get_by_data(inv, NBVLAN, data)


def test_get_by_id_follows_id_changes(base):
    inv = base.inventory
    vm = add_vm(inv, "vm1", {"id": 1}, 10)
    assert inv.get_by_id(NBVM, nb_id=10) is vm
    vm.update(data={"id": 20, "name": "vm1", "cluster": {"id": 1}}, read_from_netbox=True)
    assert inv.get_by_id(NBVM, nb_id=10) is None
    assert inv.get_by_id(NBVM, nb_id=20) is vm


def test_interfaces_loaded_from_netbox_then_resolved(base):
    inv = base.inventory
    inv.load_from_netbox(NBVM, [{"id": 10, "name": "vm1", "cluster": {"id": 1}, "last_updated": TS},
                                {"id": 11, "name": "vm2", "cluster": {"id": 1}, "last_updated": TS}])
    inv.load_from_netbox(NBVMInterface, [
        {"id": 1, "name": "eth0", "virtual_machine": {"id": 10}, "last_updated": TS},
        {"id": 2, "name": "eth1", "virtual_machine": {"id": 10}, "last_updated": TS},
        {"id": 3, "name": "eth0", "virtual_machine": {"id": 11}, "last_updated": TS},
    ])
    vm1, vm2 = inv.get_by_id(NBVM, nb_id=10), inv.get_by_id(NBVM, nb_id=11)

    # not resolved yet, a dict never equals an object
    assert inv.get_all_interfaces(vm1) == scan_get_all_interfaces(inv, vm1) == []

    inv.resolve_relations()
    assert [i.nb_id for i in inv.get_all_interfaces(vm1)] == [1, 2]
    assert [i.nb_id for i in inv.get_all_interfaces(vm2)] == [3]

    # re-parent an interface
    interface = inv.get_by_id(NBVMInterface, nb_id=1)
    interface.update(data={"virtual_machine": vm2})
    assert [i.nb_id for i in inv.get_all_interfaces(vm1)] == [2]
    assert [i.nb_id for i in inv.get_all_interfaces(vm2)] == [1, 3]


def test_ip_reassignment_and_readdressing(base):
    inv = base.inventory
    inv.load_from_netbox(NBVM, [{"id": 10, "name": "vm1", "cluster": {"id": 1}, "last_updated": TS}])
    inv.load_from_netbox(NBVMInterface, [
        {"id": 1, "name": "eth0", "virtual_machine": {"id": 10}, "last_updated": TS},
        {"id": 2, "name": "eth1", "virtual_machine": {"id": 10}, "last_updated": TS},
    ])
    inv.load_from_netbox(NBIPAddress, [
        {"id": 1, "address": "10.0.0.1/24", "assigned_object_type": "virtualization.vminterface",
         "assigned_object_id": 1, "last_updated": TS},
        {"id": 2, "address": "10.0.0.10/24", "last_updated": TS},
    ])
    inv.resolve_relations()
    eth0, eth1 = inv.get_by_id(NBVMInterface, nb_id=1), inv.get_by_id(NBVMInterface, nb_id=2)
    ip1 = inv.get_by_id(NBIPAddress, nb_id=1)

    # a prefix match of the string must not count: 10.0.0.1 is no candidate for 10.0.0.10
    assert inv.get_index_candidates(NBIPAddress, "address", ["10.0.0.1"]) == scan_ips(inv, "10.0.0.1") == [ip1]
    assert eth0.get_ip_addresses() == [ip1] and eth1.get_ip_addresses() == []

    ip1.update(data={"assigned_object_id": eth1})
    assert eth0.get_ip_addresses() == [] and eth1.get_ip_addresses() == [ip1]

    ip1.update(data={"address": "10.0.0.99/24"})
    assert inv.get_index_candidates(NBIPAddress, "address", ["10.0.0.1"]) == []
    assert inv.get_index_candidates(NBIPAddress, "address", ["10.0.0.99"]) == [ip1]


def test_list_replaced_behind_the_inventory(base):
    inv = base.inventory
    add_vm(inv, "vm1", {"id": 1}, 10)
    assert inv.get_by_id(NBVM, nb_id=10) is not None

    # tests reset single object types like this
    inv.base_structure[NBVM.name] = []
    assert inv.get_by_id(NBVM, nb_id=10) is None
    vm = add_vm(inv, "vm1", {"id": 1}, 10)
    assert inv.get_by_id(NBVM, nb_id=10) is vm


def test_virtual_disks_follow_their_vm(base):
    inv = base.inventory
    inv.load_from_netbox(NBVM, [{"id": 10, "name": "vm1", "cluster": {"id": 1}, "last_updated": TS},
                                {"id": 11, "name": "vm2", "cluster": {"id": 1}, "last_updated": TS}])
    inv.load_from_netbox(NBVirtualDisk, [{"id": 1, "name": "d1", "size": 1, "virtual_machine": {"id": 10},
                                          "last_updated": TS}])
    inv.resolve_relations()
    vm1, vm2 = inv.get_by_id(NBVM, nb_id=10), inv.get_by_id(NBVM, nb_id=11)
    disk = inv.get_by_id(NBVirtualDisk, nb_id=1)
    assert vm1.get_virtual_disks() == [disk] and vm2.get_virtual_disks() == []
    disk.update(data={"virtual_machine": vm2})
    assert vm1.get_virtual_disks() == [] and vm2.get_virtual_disks() == [disk]


def test_prefix_longest_match_equals_scan(base):
    inv = base.inventory
    prefixes = ["10.0.0.0/8", "10.1.0.0/16", "10.1.2.0/24", "10.1.2.0/24", "192.168.0.0/16", "0.0.0.0/0",
                "2001:db8::/32", "2001:db8:1::/48"]
    inv.load_from_netbox(NBPrefix, [
        {"id": i + 1, "prefix": p, "scope_type": "dcim.site" if i % 3 == 0 else None,
         "scope_id": 1 if i % 3 == 0 else None, "last_updated": TS} for i, p in enumerate(prefixes)])
    inv.resolve_relations()

    for ip in ["10.1.2.3", "10.1.3.3", "10.2.0.1", "192.168.1.1", "8.8.8.8", "2001:db8:1::1", "2001:db8:2::1",
               "2001:db9::1"]:
        for site_name, site in [(None, None), ("S1", base.s1)]:
            assert indexed_longest_prefix(inv, ip_address(ip), site_name) is \
                scan_longest_prefix(inv, ip_address(ip), site), (ip, site_name)

    # changing a prefix re-files it (prefixes are read only, so only a direct change is possible)
    prefix = inv.get_by_id(NBPrefix, nb_id=5)
    prefix.data["prefix"] = ip_network("10.1.2.0/25")
    assert indexed_longest_prefix(inv, ip_address("10.1.2.3"), None) is \
        scan_longest_prefix(inv, ip_address("10.1.2.3"), None)


def test_mac_and_primary_ip_candidates(base):
    inv = base.inventory
    inv.load_from_netbox(NBVM, [
        {"id": 10, "name": "vm1", "cluster": {"id": 1}, "primary_ip4": {"id": 1}, "last_updated": TS},
        {"id": 11, "name": "vm2", "cluster": {"id": 1}, "last_updated": TS}])
    inv.load_from_netbox(NBVMInterface, [
        {"id": 1, "name": "eth0", "virtual_machine": {"id": 10}, "mac_address": "AA:AA:AA:AA:AA:01",
         "last_updated": TS},
        {"id": 2, "name": "eth0", "virtual_machine": {"id": 11}, "mac_address": "AA:AA:AA:AA:AA:02",
         "last_updated": TS},
    ])
    inv.load_from_netbox(NBIPAddress, [{"id": 1, "address": "10.0.0.1/24", "last_updated": TS}])
    inv.load_from_netbox(NBMACAddress, [{"id": 1, "mac_address": "AA:AA:AA:AA:AA:01", "last_updated": TS}])
    inv.resolve_relations()
    vm1 = inv.get_by_id(NBVM, nb_id=10)

    assert [i.nb_id for i in inv.get_index_candidates(NBVMInterface, "mac_address",
                                                      ["AA:AA:AA:AA:AA:02", "AA:AA:AA:AA:AA:01"])] == [1, 2]
    assert [m.nb_id for m in inv.get_index_candidates(NBMACAddress, "mac_address", ["AA:AA:AA:AA:AA:01"])] == [1]
    assert inv.get_primary_ip_candidates(NBVM, ["10.0.0.1", None]) == [vm1]
    assert inv.get_primary_ip_candidates(NBVM, ["10.0.0.2"]) == []

    # a primary IP which is not resolved to an object is always a candidate
    vm2 = inv.get_by_id(NBVM, nb_id=11)
    vm2.data["primary_ip4"] = {"id": 99}
    assert inv.get_primary_ip_candidates(NBVM, ["10.0.0.2"]) == [vm2]

    # the primary IPv6 address is looked up as well
    inv.load_from_netbox(NBIPAddress, [{"id": 2, "address": "2001:db8::5/64", "last_updated": TS}])
    vm1.data["primary_ip6"] = inv.get_by_id(NBIPAddress, nb_id=2)
    # vm2 is still a candidate through its unresolved primary IPv4
    assert inv.get_primary_ip_candidates(NBVM, [None, "2001:db8::5"]) == [vm1, vm2]


def test_randomized_mutations_match_scans(base):
    """mutate objects in every possible way and compare all indexed lookups with the scans"""
    inv = base.inventory
    rnd = random.Random(4711)
    names = ["vm", "VM", "web (1)", "web", "db", "a (b) (c", "None", "x"]
    addresses = [f"10.0.{a}.{b}" for a in range(2) for b in range(3)] + ["2001:db8::1", "2001:db8::10"]
    clusters = [base.c1, base.c2]

    def new_vm():
        return inv.add_object(NBVM, data={"name": rnd.choice(names), "cluster": rnd.choice(clusters)})

    vms = [new_vm() for _ in range(15)]
    interfaces = [inv.add_object(NBVMInterface, data={"name": f"vNIC {rnd.randint(1, 3)}",
                                                      "virtual_machine": rnd.choice(vms)}) for _ in range(25)]
    ips = [inv.add_object(NBIPAddress, data={"address": f"{rnd.choice(addresses)}/24",
                                             "assigned_object_id": rnd.choice(interfaces)}) for _ in range(25)]

    def check():
        for name in names + [n.upper() for n in names]:
            for cluster in clusters:
                data = {"name": name, "cluster": cluster}
                assert inv.get_by_data(NBVM, data=data) is scan_get_by_data(inv, NBVM, data), data
        for vm in vms:
            assert inv.get_all_interfaces(vm) == scan_get_all_interfaces(inv, vm)
            assert inv.get_by_id(NBVM, nb_id=vm.nb_id) is scan_get_by_id(inv, NBVM, vm.nb_id)
        for address in addresses:
            assert [ip for ip in inv.get_index_candidates(NBIPAddress, "address", [address])
                    if ip.data["address"].startswith(f"{address}/")] == scan_ips(inv, address)
        for interface in interfaces:
            assert interface.get_ip_addresses() == \
                [ip for ip in inv.get_all_items(NBIPAddress) if ip.data.get("assigned_object_id") == interface]

    check()
    for step in range(300):
        action = rnd.randrange(9)
        if action == 0:
            rnd.choice(vms).update(data={"name": rnd.choice(names)})
        elif action == 1:
            rnd.choice(vms).update(data={"cluster": rnd.choice(clusters)})
        elif action == 2:
            rnd.choice(interfaces).update(data={"virtual_machine": rnd.choice(vms)})
        elif action == 3:
            rnd.choice(ips).update(data={"address": f"{rnd.choice(addresses)}/24"})
        elif action == 4:
            rnd.choice(ips).update(data={"assigned_object_id": rnd.choice(interfaces)})
        elif action == 5:
            vm = rnd.choice(vms)
            vm.update(data={"id": rnd.randint(1, 20), "name": rnd.choice(names), "cluster": rnd.choice(clusters)},
                      read_from_netbox=True)
        elif action == 6:
            vms.append(new_vm())
        elif action == 7:
            rnd.choice(vms).data["name"] = rnd.choice(names)
        else:
            base.c1.update(data={"name": rnd.choice(["C1", "C1 (x)", "c2"])})
        check()
