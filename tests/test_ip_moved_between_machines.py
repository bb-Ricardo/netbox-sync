# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
An address that moves from one running machine to another (a failover cluster's
address, a keepalived/ucarp VIP, an address handed to a replacement VM) has to follow
in the run that sees the move. The old machine no longer reports it, so its removal
is queued; that queued removal must make the address free for the new machine,
whichever of the two is parsed first. An address both machines still report stays
ambiguous and is skipped, as before.
"""
from types import SimpleNamespace

import pytest

from module.netbox.inventory import NetBoxInventory
from module.netbox.object_classes import (
    NBSite, NBClusterType, NBCluster, NBVM, NBVMInterface, NBIPAddress,
)
from module.sources.common.source_base import SourceBase

SHARED = "10.0.0.5/24"
OWN_A = "10.0.0.6/24"
OWN_B = "10.0.0.7/24"

# vmware object whose guest tools report as running, so IP handling is not skipped
_VM_TOOLS_RUNNING = SimpleNamespace(guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning"))


@pytest.fixture
def inventory():
    def _reset():
        inv = NetBoxInventory()
        inv.base_structure = {}
        inv.source_list = []
        inv.init()
        inv.netbox_api_version = "4.0.0"
        return inv

    inv = _reset()
    yield inv
    _reset()


def _make_source(inventory):
    src = SourceBase()
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.settings = SimpleNamespace(
        skip_fhrp_group_ips=False,
        preserve_primary_ips=False,
        ip_tenant_inheritance_order=["disabled"],
        disable_vlan_sync=True,
        vlan_group_relation_by_id=None,
        vlan_group_relation_by_name=None,
        vlan_sync_exclude_by_id=None,
        vlan_sync_exclude_by_name=None,
    )
    src.return_longest_matching_prefix_for_ip = lambda *a, **k: None
    return src


def _vm_and_nic(inv, name):
    site = inv.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    ctype = inv.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)
    cluster = inv.add_object(NBCluster, data={"name": "c1", "type": ctype, "scope": site},
                             read_from_netbox=True)
    vm = inv.add_object(NBVM, data={"name": name, "cluster": cluster, "status": "active"},
                        read_from_netbox=True)
    nic = inv.add_object(NBVMInterface, data={"name": "eth0", "virtual_machine": vm, "enabled": True},
                         read_from_netbox=True)
    return vm, nic


def _add_ip(inv, nic, address=SHARED):
    return inv.add_object(NBIPAddress, data={
        "address": address,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": nic,
    }, read_from_netbox=True)


def _sync(src, vm, nic, addresses):
    _iface, ip_objects = src.add_update_interface(
        interface_object=nic, device_object=vm,
        interface_data={"name": "eth0"}, interface_ips=addresses,
        vmware_object=_VM_TOOLS_RUNNING,
    )
    return ip_objects


@pytest.fixture
def two_machines(inventory):
    # NetBox has the shared address on vm-a; in vCenter it has moved to vm-b
    vm_a, nic_a = _vm_and_nic(inventory, "vm-a")
    vm_b, nic_b = _vm_and_nic(inventory, "vm-b")
    shared = _add_ip(inventory, nic_a)
    return SimpleNamespace(src=_make_source(inventory), vm_a=vm_a, nic_a=nic_a,
                           vm_b=vm_b, nic_b=nic_b, shared=shared)


def _assert_moved_to_b(m, ip_objects):
    assert m.shared in ip_objects, "the moved address was not assigned to the new machine"
    assert m.shared.get_interface() is m.nic_b
    assert m.shared not in m.nic_a.get_ip_addresses()
    # one PATCH to the new interface: no queued removal left to send first
    assert "assigned_object_id" in m.shared.updated_items
    assert "assigned_object_id" not in m.shared.unset_items
    assert "assigned_object_type" not in m.shared.unset_items
    assert len(list(m.src.inventory.get_all_items(NBIPAddress))) == 3, "a duplicate address was created"


def test_moved_address_follows_when_old_machine_is_parsed_first(two_machines):
    m = two_machines

    _sync(m.src, m.vm_a, m.nic_a, [OWN_A])     # vm-a no longer reports it: removal queued
    ip_objects = _sync(m.src, m.vm_b, m.nic_b, [SHARED, OWN_B])

    _assert_moved_to_b(m, ip_objects)


def test_moved_address_follows_on_reevaluation_when_new_machine_is_parsed_first(two_machines):
    m = two_machines

    # vm-b first: the address is still on vm-a's enabled interface, so it is skipped for now
    first = _sync(m.src, m.vm_b, m.nic_b, [SHARED, OWN_B])
    assert m.shared not in first
    _sync(m.src, m.vm_a, m.nic_a, [OWN_A])     # removal queued
    # the reevaluation pass parses vm-b again
    ip_objects = _sync(m.src, m.vm_b, m.nic_b, [SHARED, OWN_B])

    _assert_moved_to_b(m, ip_objects)


def test_address_reported_by_both_machines_is_still_skipped(two_machines):
    # control: while vm-a still reports the address it stays ambiguous and stays on vm-a
    m = two_machines

    _sync(m.src, m.vm_a, m.nic_a, [SHARED, OWN_A])
    ip_objects = _sync(m.src, m.vm_b, m.nic_b, [SHARED, OWN_B])

    assert m.shared not in ip_objects
    assert m.shared.get_interface() is m.nic_a
    assert m.shared.unset_items == []


def test_moved_primary_address_is_unset_on_the_old_machine(two_machines):
    # control: the old machine still loses the address as its primary IP
    m = two_machines
    m.vm_a.data["primary_ip4"] = m.shared

    _sync(m.src, m.vm_a, m.nic_a, [OWN_A])
    _sync(m.src, m.vm_b, m.nic_b, [SHARED, OWN_B])

    assert m.shared.get_interface() is m.nic_b
    assert "primary_ip4" in m.vm_a.unset_items
