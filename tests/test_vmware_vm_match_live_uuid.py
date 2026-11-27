# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
A VM must not take over the NetBox object of another VM that still exists in the same vCenter.

A VM's serial is its vCenter instance UUID. The name, MAC and primary IP matches are fuzzy: when
an admin builds a replacement VM, gives it the old VM's IP address and renames the old one to
"<name>_old", the new VM matched the old VM's object by primary IP, and the old VM matched the same
object again by MAC. The object switched between the two VMs on every run, and NetBox refused the
MAC address of the VM that lost it.

The source now knows the instance UUIDs of all VMs in the vCenter. A name, MAC or IP match is
refused when the object's serial is the UUID of another VM that still exists, and an object one VM
claimed in this run is not given to a second VM.
"""
from types import SimpleNamespace

from module.config.base import ConfigOptions
from module.config.group import ConfigOptionGroup
from module.config.option import ConfigOption
from module.netbox.object_classes import (
    NBCluster,
    NBClusterType,
    NBIPAddress,
    NBSite,
    NBVM,
    NBVMInterface,
)
from module.sources.vmware.config import VMWareConfig
from module.sources.vmware.connection import VMWareHandler

OLD_UUID = "50000000-0000-0000-0000-00000000000a"
NEW_UUID = "50000000-0000-0000-0000-00000000000b"
OTHER_UUID = "50000000-0000-0000-0000-00000000000c"
ADDRESS = "10.60.60.13/24"
MAC = "00:50:56:00:00:0a"


def _vmware_settings(**overrides):
    """Every VMWareConfig option at its default, with the given overrides."""
    values = {}
    for entry in VMWareConfig().options:
        declared = entry.options if isinstance(entry, ConfigOptionGroup) else [entry]
        for option in declared:
            if isinstance(option, ConfigOption) and option.removed is not True:
                values[option.key] = option.default_value
    values.update(overrides)
    return ConfigOptions(**values)


def _make_source(inventory, live_vm_uuids=None, match_vm_by_mac_address=True):
    src = object.__new__(VMWareHandler)
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.object_cache = dict()
    src.live_vm_uuids = live_vm_uuids
    src.claimed_vms = dict()
    src.settings = _vmware_settings(match_vm_by_mac_address=match_vm_by_mac_address,
                                    permitted_subnets=None, set_primary_ip="when-undefined")
    return src


def _cluster(inventory):
    site = inventory.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    ctype = inventory.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)
    return inventory.add_object(NBCluster, data={"name": "c1", "type": ctype, "scope": site},
                                read_from_netbox=True)


def _netbox_vm(inventory, cluster, name, serial=None, address=None, mac=None):
    """A VM object as read from NetBox, optionally with a primary IPv4 address and an interface MAC."""
    data = {"name": name, "cluster": cluster, "status": "active"}
    if serial is not None:
        data["serial"] = serial
    vm = inventory.add_object(NBVM, data=data, read_from_netbox=True)
    if address is not None:
        ip = inventory.add_object(NBIPAddress, data={"address": address}, read_from_netbox=True)
        vm.data["primary_ip4"] = ip
    if mac is not None:
        inventory.add_object(NBVMInterface, data={"name": "Network adapter 1", "virtual_machine": vm,
                                                  "mac_address": mac}, read_from_netbox=True)
    return vm


def _sync_vm(src, cluster, name, serial, address=None, mac=None):
    """What add_virtual_machine hands over for one vCenter VM."""
    vnic_data = dict()
    if mac is not None:
        vnic_data["Network adapter 1"] = {"name": "Network adapter 1", "mac_address": mac}
    src.add_device_vm_to_inventory(
        NBVM, object_data={"name": name, "cluster": cluster, "status": "active", "serial": serial},
        pnic_data=dict(), vnic_data=vnic_data, nic_ips=dict(), p_ipv4=address)


def _vms(inventory):
    return list(inventory.get_all_items(NBVM))


def _vm_with_serial(inventory, serial):
    matches = [vm for vm in _vms(inventory) if vm.data.get("serial") == serial]
    assert len(matches) == 1, f"{len(matches)} VMs with serial {serial}"
    return matches[0]


def test_replacement_vm_does_not_take_the_old_vms_object_by_ip(inventory):
    """The replacement has the old VM's IP; the old VM was renamed and still exists."""
    cluster = _cluster(inventory)
    old = _netbox_vm(inventory, cluster, "app1_old", serial=OLD_UUID, address=ADDRESS)
    src = _make_source(inventory, live_vm_uuids={OLD_UUID, NEW_UUID})

    _sync_vm(src, cluster, "app1", NEW_UUID, address=ADDRESS)

    assert len(_vms(inventory)) == 2
    assert old.data.get("name") == "app1_old"
    assert old.data.get("serial") == OLD_UUID
    assert _vm_with_serial(inventory, NEW_UUID).data.get("name") == "app1"


def test_renamed_old_vm_keeps_its_object_and_the_replacement_gets_a_new_one(inventory):
    """
    The object still has the old name (the rename happened after the last run). The replacement
    takes the name in vCenter; the old VM gets "_old".
    """
    cluster = _cluster(inventory)
    old = _netbox_vm(inventory, cluster, "app1", serial=OLD_UUID, address=ADDRESS, mac=MAC)
    src = _make_source(inventory, live_vm_uuids={OLD_UUID, NEW_UUID})

    # online VMs are parsed first: the replacement, then the old VM (powered off)
    _sync_vm(src, cluster, "app1", NEW_UUID, address=ADDRESS)
    _sync_vm(src, cluster, "app1_old", OLD_UUID, mac=MAC)

    assert len(_vms(inventory)) == 2
    assert old.data.get("serial") == OLD_UUID
    assert old.data.get("name") == "app1_old"
    assert _vm_with_serial(inventory, NEW_UUID).data.get("name") == "app1"


def test_copied_vm_with_the_same_mac_does_not_take_the_original(inventory):
    cluster = _cluster(inventory)
    original = _netbox_vm(inventory, cluster, "app1", serial=OLD_UUID, mac=MAC)
    src = _make_source(inventory, live_vm_uuids={OLD_UUID, NEW_UUID})

    _sync_vm(src, cluster, "app1-copy", NEW_UUID, mac=MAC)

    assert len(_vms(inventory)) == 2
    assert original.data.get("name") == "app1"
    assert original.data.get("serial") == OLD_UUID


def test_rebuilt_vm_reuses_the_object_when_the_old_vm_is_gone(inventory):
    """Deleted and built again with the same name: a new UUID, and the old one no longer exists."""
    cluster = _cluster(inventory)
    existing = _netbox_vm(inventory, cluster, "app1", serial=OLD_UUID, address=ADDRESS)
    src = _make_source(inventory, live_vm_uuids={NEW_UUID})

    _sync_vm(src, cluster, "app1", NEW_UUID, address=ADDRESS)

    assert _vms(inventory) == [existing]
    assert existing.data.get("serial") == NEW_UUID


def test_restored_vm_reuses_the_object_by_ip_when_the_old_vm_is_gone(inventory):
    """Restored from a backup under another name: a new UUID, the same IP, the old VM deleted."""
    cluster = _cluster(inventory)
    existing = _netbox_vm(inventory, cluster, "app1", serial=OLD_UUID, address=ADDRESS)
    src = _make_source(inventory, live_vm_uuids={NEW_UUID})

    _sync_vm(src, cluster, "app1-restored", NEW_UUID, address=ADDRESS)

    assert _vms(inventory) == [existing]
    assert existing.data.get("name") == "app1-restored"
    assert existing.data.get("serial") == NEW_UUID


def test_object_without_serial_is_given_to_one_vm_only(inventory):
    """No serial to compare (an object made by hand): the first VM that matches it keeps it."""
    cluster = _cluster(inventory)
    existing = _netbox_vm(inventory, cluster, "app1", address=ADDRESS)
    src = _make_source(inventory, live_vm_uuids={NEW_UUID, OTHER_UUID})

    _sync_vm(src, cluster, "app1", NEW_UUID, address=ADDRESS)
    _sync_vm(src, cluster, "app2", OTHER_UUID, address=ADDRESS)

    assert len(_vms(inventory)) == 2
    assert existing.data.get("name") == "app1"
    assert existing.data.get("serial") == NEW_UUID
    assert _vm_with_serial(inventory, OTHER_UUID).data.get("name") == "app2"


def test_same_vm_parsed_again_keeps_its_object(inventory):
    """A VM queued for reevaluation is parsed a second time in the same run."""
    cluster = _cluster(inventory)
    existing = _netbox_vm(inventory, cluster, "app1", serial=OLD_UUID, address=ADDRESS)
    src = _make_source(inventory, live_vm_uuids={OLD_UUID})

    _sync_vm(src, cluster, "app1", OLD_UUID, address=ADDRESS)
    _sync_vm(src, cluster, "app1", OLD_UUID, address=ADDRESS)

    assert _vms(inventory) == [existing]


def test_unknown_live_uuids_keep_the_old_matching(inventory):
    """If the UUIDs could not be read, a fuzzy match is still accepted, as before."""
    cluster = _cluster(inventory)
    old = _netbox_vm(inventory, cluster, "app1_old", serial=OLD_UUID, address=ADDRESS)
    src = _make_source(inventory, live_vm_uuids=None)

    _sync_vm(src, cluster, "app1", NEW_UUID, address=ADDRESS)

    assert _vms(inventory) == [old]
    assert old.data.get("serial") == NEW_UUID


class _FakeCollector:
    """A property collector which answers in pages, like vCenter does for large inventories."""

    def __init__(self, pages, error=None):
        self.pages = pages
        self.error = error
        self.requests = list()

    def _page(self, index):
        objects = [SimpleNamespace(propSet=[SimpleNamespace(name="config.instanceUuid", val=value)]
                                   if value is not None else list())
                   for value in self.pages[index]]
        token = str(index + 1) if index + 1 < len(self.pages) else None
        return SimpleNamespace(objects=objects, token=token)

    def RetrievePropertiesEx(self, spec_set, options):
        if self.error is not None:
            raise self.error
        self.requests.append(spec_set)
        return self._page(0)

    def ContinueRetrievePropertiesEx(self, token):
        return self._page(int(token))


def _container_view():
    from pyVmomi import vim
    return vim.view.ContainerView("session[test]view-1")


def test_instance_uuids_are_read_from_all_pages(inventory):
    src = _make_source(inventory)
    collector = _FakeCollector([[OLD_UUID.upper(), NEW_UUID], [None, OTHER_UUID]])
    src.session = SimpleNamespace(propertyCollector=collector)

    assert src.collect_vm_instance_uuids(_container_view()) == {OLD_UUID, NEW_UUID, OTHER_UUID}
    assert len(collector.requests) == 1


def test_instance_uuids_are_none_if_vcenter_refuses(inventory):
    src = _make_source(inventory)
    src.session = SimpleNamespace(propertyCollector=_FakeCollector([], error=RuntimeError("refused")))

    assert src.collect_vm_instance_uuids(_container_view()) is None


def test_a_sync_reads_the_instance_uuid_of_every_vm(vmware_sync, sdk):
    """Against vcsim: the property collector request returns every VM's instance UUID."""
    from pyVmomi import vim

    view = sdk.viewManager.CreateContainerView(sdk.rootFolder, [vim.VirtualMachine], True)
    try:
        expected = {vm.config.instanceUuid.lower() for vm in view.view}
    finally:
        view.Destroy()

    assert len(expected) > 0
    assert vmware_sync.source.live_vm_uuids == expected
