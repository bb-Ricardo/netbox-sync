"""
vm_status_on_create and vm_status_preserve are opt-in: unset, a VM's status keeps
following its power state exactly as before (PR #528).
"""
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import NBCluster, NBClusterType, NBSite, NBVM
from module.sources.vmware.config import VMWareConfig
from module.sources.vmware.connection import VMWareHandler

MINIMAL_CONFIG = """
[netbox]
host_fqdn = netbox.example.com
api_token = xyz

[source/vc]
type = vmware
host_fqdn = vcenter.example.com
username = u
password = p
"""


def _make_source(inventory, status_on_create=None, status_preserve=None):
    src = object.__new__(VMWareHandler)
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.object_cache = dict()
    src.settings = SimpleNamespace(
        match_host_by_serial=True,
        match_vm_by_serial=True,
        match_vm_by_mac_address=True,
        match_vm_by_ip_address=True,
        overwrite_device_platform=False,
        overwrite_vm_platform=False,
        host_role_relation=[],
        vm_role_relation=[],
        host_interface_exclude_filter=None,
        vm_interface_exclude_filter=None,
        set_primary_ip="when-undefined",
        vm_exclude_disk_sync=None,
        vm_exclude_disk_sync_by_tag=None,
        vm_status_on_create=status_on_create,
        vm_status_preserve=status_preserve,
    )
    return src


def _cluster(inventory):
    site = inventory.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    ctype = inventory.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)
    return inventory.add_object(NBCluster, data={"name": "c1", "type": ctype, "scope": site},
                                read_from_netbox=True)


def _sync_vm(src, cluster, name="vm1", status="active"):
    # the handler stores the object in the inventory, it does not hand it back
    src.add_device_vm_to_inventory(
        NBVM, object_data={"name": name, "cluster": cluster, "status": status},
        pnic_data=dict(), vnic_data=dict())
    return src.inventory.get_by_data(NBVM, data={"name": name, "cluster": cluster})


def _status_of(vm):
    status = vm.data.get("status")
    return status.get("value") if isinstance(status, dict) else status


@pytest.mark.parametrize("power_state_status", ["active", "offline"])
def test_new_vm_keeps_the_power_state_status_by_default(inventory, power_state_status):
    cluster = _cluster(inventory)

    vm = _sync_vm(_make_source(inventory), cluster, status=power_state_status)

    assert _status_of(vm) == power_state_status


def test_new_vm_gets_the_configured_status(inventory):
    cluster = _cluster(inventory)

    vm = _sync_vm(_make_source(inventory, status_on_create="planned"), cluster, status="offline")

    assert _status_of(vm) == "planned"


def test_existing_vm_status_follows_the_power_state_by_default(inventory):
    cluster = _cluster(inventory)
    existing = inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster, "status": "planned"},
                                    read_from_netbox=True)

    vm = _sync_vm(_make_source(inventory), cluster, status="active")

    assert vm is existing
    assert _status_of(vm) == "active"


def test_existing_vm_status_is_preserved_when_listed(inventory):
    cluster = _cluster(inventory)
    existing = inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster, "status": "planned"},
                                    read_from_netbox=True)

    vm = _sync_vm(_make_source(inventory, status_preserve=["planned"]), cluster, status="active")

    assert vm is existing
    assert _status_of(vm) == "planned"


def test_both_options_are_unset_by_default(load_config):
    """A config that does not mention them must leave today's behaviour in place."""
    load_config(MINIMAL_CONFIG)
    handler = VMWareConfig()
    handler.source_name = "vc"
    settings = handler.parse(do_log=False)

    assert settings.vm_status_on_create is None
    assert not settings.vm_status_preserve
