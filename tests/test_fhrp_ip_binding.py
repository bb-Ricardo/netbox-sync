"""
Regression tests for the skip_fhrp_group_ips feature (issue #445, PR #476).

Issue #476 reported that, with skip_fhrp_group_ips enabled, the presence of any
FHRP-group IP in the inventory caused every regular IP to be unbound from its
interface. These tests exercise the real add_update_interface path against the
in-memory NetBoxInventory (no live NetBox or vCenter needed) and cover both
inventory orders and the VRF-scoping of the FHRP match.
"""
from types import SimpleNamespace

import pytest

from module.netbox.inventory import NetBoxInventory
from module.netbox.object_classes import (
    NBSite, NBClusterType, NBCluster, NBVM, NBVMInterface, NBIPAddress, NBVRF,
)
from module.sources.common.source_base import SourceBase

# vmware object whose guest tools report as running, so IP handling is not skipped
_VM_TOOLS_RUNNING = SimpleNamespace(guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning"))


@pytest.fixture
def inventory():
    # NetBoxInventory is a singleton with class-level state; reset it before and
    # after each test so cases do not leak objects into each other or into any
    # unrelated test that runs later.
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


def _make_source(inventory, skip_fhrp):
    src = SourceBase()
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.settings = SimpleNamespace(
        skip_fhrp_group_ips=skip_fhrp,
        ip_tenant_inheritance_order=["disabled"],
        disable_vlan_sync=True,
        vlan_group_relation_by_id=None,
        vlan_group_relation_by_name=None,
        vlan_sync_exclude_by_id=None,
        vlan_sync_exclude_by_name=None,
    )
    # no prefixes configured; skip prefix matching entirely
    src.return_longest_matching_prefix_for_ip = lambda *a, **k: None
    return src


def _vm_and_nic(inv):
    site = inv.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    ctype = inv.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)
    cluster = inv.add_object(NBCluster, data={"name": "c1", "type": ctype, "scope": site},
                             read_from_netbox=True)
    vm = inv.add_object(NBVM, data={"name": "vm1", "cluster": cluster}, read_from_netbox=True)
    nic = inv.add_object(NBVMInterface, data={"name": "eth0", "virtual_machine": vm, "enabled": True},
                         read_from_netbox=True)
    return vm, nic


def _add_regular(inv, nic, address="10.0.0.5/24"):
    return inv.add_object(NBIPAddress, data={
        "address": address,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": nic,
    }, read_from_netbox=True)


def _add_fhrp(inv, address="10.0.0.1/24", vrf=None):
    data = {"address": address, "assigned_object_type": "ipam.fhrpgroup"}
    if vrf is not None:
        data["vrf"] = vrf
    return inv.add_object(NBIPAddress, data=data, read_from_netbox=True)


def _sync_one(src, vm, nic, address):
    _iface, ip_objects = src.add_update_interface(
        interface_object=nic, device_object=vm,
        interface_data={"name": "eth0"}, interface_ips=[address],
        vmware_object=_VM_TOOLS_RUNNING,
    )
    return ip_objects


@pytest.mark.parametrize("fhrp_first", [True, False])
def test_regular_ip_stays_bound_regardless_of_inventory_order(inventory, fhrp_first):
    # An unrelated FHRP-group IP must not unbind a regular IP, no matter which
    # object comes first in the inventory (issue #476).
    vm, nic = _vm_and_nic(inventory)
    if fhrp_first:
        _add_fhrp(inventory)
        regular = _add_regular(inventory, nic)
    else:
        regular = _add_regular(inventory, nic)
        _add_fhrp(inventory)

    src = _make_source(inventory, skip_fhrp=True)
    ip_objects = _sync_one(src, vm, nic, "10.0.0.5/24")

    assert regular in ip_objects, "regular IP was dropped due to an unrelated FHRP-group IP"
    assert regular.get_interface() is nic, "regular IP lost its interface binding"


def test_matching_fhrp_ip_is_not_reassigned(inventory):
    # The feature itself (#445): a matching FHRP-group IP must not be rebound to
    # the VM interface when skip_fhrp_group_ips is on.
    vm, nic = _vm_and_nic(inventory)
    fhrp_ip = _add_fhrp(inventory, address="10.0.0.1/24")

    src = _make_source(inventory, skip_fhrp=True)
    ip_objects = _sync_one(src, vm, nic, "10.0.0.1/24")

    assert fhrp_ip.get_interface() is not nic, "FHRP-group IP was wrongly reassigned to the VM interface"
    assert fhrp_ip not in ip_objects, "FHRP-group IP should have been skipped, not bound"


def test_fhrp_match_is_scoped_to_same_vrf(inventory):
    # An FHRP-group IP with the same address but a different VRF must not
    # suppress a legitimate IP; VRF is part of identity here.
    vm, nic = _vm_and_nic(inventory)
    vrf_b = inventory.add_object(NBVRF, data={"name": "vrf-b"}, read_from_netbox=True)
    # FHRP IP lives in vrf-b; the synced address resolves to no prefix, so its vrf is None
    _add_fhrp(inventory, address="10.0.0.5/24", vrf=vrf_b)
    regular = _add_regular(inventory, nic, "10.0.0.5/24")

    src = _make_source(inventory, skip_fhrp=True)
    ip_objects = _sync_one(src, vm, nic, "10.0.0.5/24")

    assert regular in ip_objects, "regular IP was suppressed by an FHRP IP in a different VRF"


def test_regular_ip_binds_when_flag_disabled(inventory):
    # Control: with the flag off, a regular IP binds normally.
    vm, nic = _vm_and_nic(inventory)
    regular = _add_regular(inventory, nic)

    src = _make_source(inventory, skip_fhrp=False)
    ip_objects = _sync_one(src, vm, nic, "10.0.0.5/24")

    assert regular in ip_objects
    assert regular.get_interface() is nic
