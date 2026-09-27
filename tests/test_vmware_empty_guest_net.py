"""
VMware Tools that report as running but hand back no interfaces at all are broken,
not a statement that every IP is gone. Trusting them tears the IP assignments off a
VM on every run (from #509 by @dirtycache).
"""
from types import SimpleNamespace

import pytest

from module.netbox.inventory import NetBoxInventory
from module.netbox.object_classes import (
    NBCluster, NBClusterType, NBIPAddress, NBSite, NBVM, NBVMInterface,
)
from module.sources.common.source_base import SourceBase

TOOLS_RUNNING_NO_NICS = SimpleNamespace(
    guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning", net=[]))
TOOLS_RUNNING_WITH_NIC = SimpleNamespace(
    guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning",
                          net=[SimpleNamespace(ipAddress=[])]))


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


def _source(inventory):
    src = SourceBase()
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.settings = SimpleNamespace(
        skip_fhrp_group_ips=False, preserve_primary_ips=False,
        ip_tenant_inheritance_order=["disabled"], disable_vlan_sync=True,
        vlan_group_relation_by_id=None, vlan_group_relation_by_name=None,
        vlan_sync_exclude_by_id=None, vlan_sync_exclude_by_name=None,
    )
    src.return_longest_matching_prefix_for_ip = lambda *a, **k: None
    return src


def _vm_with_ip(inv):
    site = inv.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    ctype = inv.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)
    cluster = inv.add_object(NBCluster, data={"name": "c1", "type": ctype, "scope": site},
                             read_from_netbox=True)
    vm = inv.add_object(NBVM, data={"name": "vm1", "cluster": cluster, "status": "active"},
                        read_from_netbox=True)
    nic = inv.add_object(NBVMInterface, data={"name": "eth0", "virtual_machine": vm, "enabled": True},
                         read_from_netbox=True)
    ip = inv.add_object(NBIPAddress, data={
        "address": "10.0.0.5/24",
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": nic,
    }, read_from_netbox=True)
    return vm, nic, ip


def test_broken_tools_reporting_no_interfaces_keep_the_ips(inventory):
    vm, nic, ip = _vm_with_ip(inventory)

    _source(inventory).add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=[], vmware_object=TOOLS_RUNNING_NO_NICS)

    assert "assigned_object_id" not in ip.unset_items, \
        "the VM lost its IP because the guest tools returned nothing"


def test_a_reported_interface_without_ips_still_removes_them(inventory):
    # control: the guard must not stop a real removal, where the NIC is reported but has no IP
    vm, nic, ip = _vm_with_ip(inventory)

    _source(inventory).add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=[], vmware_object=TOOLS_RUNNING_WITH_NIC)

    assert "assigned_object_id" in ip.unset_items, \
        "a genuinely empty interface must still drop its IP"
