"""
An address whose NetBox role marks it as non-unique (anycast, vip, vrrp, hsrp,
glbp, carp) lives on more than one interface by definition. Discovering it on a
second machine has to assign it there as well instead of skipping it (issue #344).
"""
from types import SimpleNamespace

import pytest

from module.netbox.inventory import NetBoxInventory
from module.netbox.object_classes import (
    NBSite, NBClusterType, NBCluster, NBVM, NBVMInterface, NBIPAddress,
)
from module.sources.common.source_base import SourceBase

NON_UNIQUE_ROLES = ["anycast", "vip", "vrrp", "hsrp", "glbp", "carp"]
ADDRESS = "10.0.0.5/24"

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


def _add_ip(inv, nic, role=None, address=ADDRESS):
    data = {
        "address": address,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": nic,
    }
    if role is not None:
        data["role"] = role
    return inv.add_object(NBIPAddress, data=data, read_from_netbox=True)


def _sync(src, vm, nic, address=ADDRESS):
    _iface, ip_objects = src.add_update_interface(
        interface_object=nic, device_object=vm,
        interface_data={"name": "eth0"}, interface_ips=[address],
        vmware_object=_VM_TOOLS_RUNNING,
    )
    return ip_objects


@pytest.mark.parametrize("role", NON_UNIQUE_ROLES)
def test_non_unique_address_is_assigned_to_the_second_interface(inventory, role):
    _vm_a, nic_a = _vm_and_nic(inventory, "vm-a")
    existing = _add_ip(inventory, nic_a, role=role)
    vm_b, nic_b = _vm_and_nic(inventory, "vm-b")

    ip_objects = _sync(_make_source(inventory), vm_b, nic_b)

    assert ip_objects, f"a {role} address must be assigned to this interface as well"
    assigned = ip_objects[0]
    assert assigned is not existing, "the other interface must keep its own object"
    assert assigned.get_interface() is nic_b
    assert existing.get_interface() is nic_a, "the other interface lost its address"
    assert str(assigned.data.get("role")) == role, "the new object must carry the same role"


def test_plain_duplicate_is_still_skipped(inventory):
    # control: without a non-unique role the address stays ambiguous and is skipped
    _vm_a, nic_a = _vm_and_nic(inventory, "vm-a")
    _add_ip(inventory, nic_a)
    vm_b, nic_b = _vm_and_nic(inventory, "vm-b")

    assert _sync(_make_source(inventory), vm_b, nic_b) == []


def test_second_run_reuses_the_object_it_created(inventory):
    # control: the fix must not create a new object on every run
    _vm_a, nic_a = _vm_and_nic(inventory, "vm-a")
    _add_ip(inventory, nic_a, role="anycast")
    vm_b, nic_b = _vm_and_nic(inventory, "vm-b")
    src = _make_source(inventory)

    first = _sync(src, vm_b, nic_b)
    second = _sync(src, vm_b, nic_b)

    assert first and second and first[0] is second[0], "a second run created another object"
    assert len(list(inventory.get_all_items(NBIPAddress))) == 2
