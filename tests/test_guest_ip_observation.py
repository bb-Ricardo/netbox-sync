# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Guest IP data that was not collected is not evidence the address was removed.

Unknown observations keep the assignment and the primary IP, clear a stale orphan
tag, and block pruning even if that tag write does not stick. A later trustworthy
removal, or a VM that is absent from a finished listing, uses the ordinary orphan
and prune lifecycle. Prune reads last_updated; there is no separate orphan clock.
"""

import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from module.netbox.connection import NetBoxHandler
from module.netbox.object_classes import (
    NBCluster, NBClusterType, NBDevice, NBIPAddress, NBSite, NBVM, NBVMInterface, NBInterface,
)
from module.sources.common.source_base import SourceBase
from module.sources.vmware.connection import VMWareHandler

TOOLS_DOWN = SimpleNamespace(
    guest=SimpleNamespace(toolsRunningStatus="guestToolsNotRunning",
                          net=[SimpleNamespace(ipAddress=["10.0.0.5"])]))
TOOLS_RUNNING_NO_NICS = SimpleNamespace(
    guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning", net=[]))
TOOLS_RUNNING_EMPTY_NIC = SimpleNamespace(
    guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning",
                          net=[SimpleNamespace(ipAddress=[])]))


@pytest.fixture
def source_settings():
    return SimpleNamespace(
        skip_fhrp_group_ips=False,
        preserve_primary_ips=False,
        ip_tenant_inheritance_order=["disabled"],
        disable_vlan_sync=True,
        vlan_group_relation_by_id=None,
        vlan_group_relation_by_name=None,
        vlan_sync_exclude_by_id=None,
        vlan_sync_exclude_by_name=None,
        overwrite_vm_platform=False,
        overwrite_vm_tenant=False,
        overwrite_vm_interface_name=True,
        overwrite_device_interface_name=True,
        vm_status_preserve=None,
        vm_interface_exclude_filter=None,
        host_interface_exclude_filter=None,
        set_primary_ip="never",
        match_vm_by_mac_address=False,
        match_vm_by_ip_address=False,
        enabled=True,
    )


def make_source(inventory, name, settings):
    source = SourceBase()
    source.inventory = inventory
    source.name = name
    source.source_tag = f"Source: {name}"
    source.settings = settings
    source.inventory_complete = True
    source.init_successful = True
    inventory.source_list.append(source)
    return source


def make_handler(inventory, prune_delay_in_days=30):
    handler = object.__new__(NetBoxHandler)
    handler.inventory = inventory
    handler.settings = SimpleNamespace(
        ignore_unknown_source_object_pruning=False,
        prune_enabled=True,
        orphaned_device_status=None,
        orphaned_vm_status=None,
        prune_delay_in_days=prune_delay_in_days,
    )
    return handler


def stamp(obj, *tags):
    obj.add_tags(list(tags))
    obj.source = None
    obj.updated_items = list()
    obj.unset_items = list()


def vm_with_ip(inventory, name="vm1", address="10.0.0.5/24", interface_name="eth0"):
    site = inventory.add_object(NBSite, data={"name": f"site-{name}"}, read_from_netbox=True)
    cluster_type = inventory.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)
    cluster = inventory.add_object(NBCluster, data={"name": f"cluster-{name}", "type": cluster_type, "scope": site},
                                   read_from_netbox=True)
    vm = inventory.add_object(NBVM, data={"name": name, "cluster": cluster, "status": "active"},
                              read_from_netbox=True)
    nic = inventory.add_object(NBVMInterface, data={
        "name": interface_name, "virtual_machine": vm, "enabled": True,
    }, read_from_netbox=True)
    ip = inventory.add_object(NBIPAddress, data={
        "address": address,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": nic,
    }, read_from_netbox=True)
    vm.nb_id = 11
    nic.nb_id = 22
    ip.nb_id = 33
    vm.data["primary_ip4"] = ip
    return vm, nic, ip


def device_with_ip(inventory):
    site = inventory.add_object(NBSite, data={"name": "site-device"}, read_from_netbox=True)
    device = inventory.add_object(NBDevice, data={"name": "server1", "site": site, "status": "active"},
                                  read_from_netbox=True)
    nic = inventory.add_object(NBInterface, data={"name": "eth0", "device": device, "enabled": True},
                               read_from_netbox=True)
    ip = inventory.add_object(NBIPAddress, data={
        "address": "10.1.0.5/24",
        "assigned_object_type": "dcim.interface",
        "assigned_object_id": nic,
    }, read_from_netbox=True)
    device.nb_id = 41
    nic.nb_id = 42
    ip.nb_id = 43
    return device, nic, ip


def age(obj, days):
    moment = datetime.now() - timedelta(days=days)
    obj.data["last_updated"] = moment.strftime("%Y-%m-%dT%H:%M:%S")


def prune_deletes(handler):
    deleted = []

    def request(object_class, req_type="GET", data=None, params=None, nb_id=None):
        deleted.append((req_type, getattr(object_class, "name", object_class), nb_id))
        return True

    handler.request = request
    handler.prune_data()
    return deleted


def test_tools_down_is_unknown_and_a_reported_nic_with_tools_running_is_complete():
    assert VMWareHandler.guest_ip_observation_state(TOOLS_DOWN) == "unknown"
    assert VMWareHandler.guest_ip_observation_state(TOOLS_RUNNING_NO_NICS) == "unknown"
    assert VMWareHandler.guest_ip_observation_state(TOOLS_RUNNING_EMPTY_NIC) == "complete"


def test_unknown_observation_is_recorded_before_interface_reconciliation(inventory, source_settings, caplog):
    """
    Tools are running and the reported NIC has no addresses, which would unassign
    the IP if the unknown flag were applied only after interface reconciliation.
    """

    source = make_source(inventory, "vcenter", source_settings)
    vm, nic, ip = vm_with_ip(inventory)
    stamp(vm, NetBoxHandler.primary_tag, source.source_tag)
    stamp(nic, NetBoxHandler.primary_tag, source.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, source.source_tag, NetBoxHandler.orphaned_tag)

    caplog.set_level(logging.INFO, logger="NetBox-Sync")
    handler = object.__new__(VMWareHandler)
    handler.inventory = inventory
    handler.name = source.name
    handler.source_tag = source.source_tag
    handler.settings = source_settings

    handler.add_device_vm_to_inventory(
        NBVM,
        object_data={"name": "vm1", "status": "active", "cluster": vm.data["cluster"]},
        vnic_data={"eth0": {"name": "eth0"}},
        nic_ips={"eth0": []},
        vmware_object=TOOLS_RUNNING_EMPTY_NIC,
        guest_ip_observation="unknown",
    )

    assert vm.guest_ip_observations[source.source_tag] == "unknown"
    assert "VMware guest IP data unavailable for 'vm1'" in caplog.text
    assert "existing NetBox IP assignments retained" in caplog.text
    assert "assigned_object_id" not in ip.unset_items
    assert "primary_ip4" not in vm.unset_items
    assert ip.source is None


def test_unknown_guest_data_keeps_assignment_primary_and_clears_orphan_tag(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    vm, nic, ip = vm_with_ip(inventory)
    stamp(vm, NetBoxHandler.primary_tag, source.source_tag)
    stamp(nic, NetBoxHandler.primary_tag, source.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, source.source_tag, NetBoxHandler.orphaned_tag)
    vm.guest_ip_observations = {source.source_tag: "unknown"}

    source.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=["10.0.0.5/24"], vmware_object=TOOLS_DOWN)

    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)

    assert "assigned_object_id" not in ip.unset_items
    assert vm.data["primary_ip4"] is ip
    assert "primary_ip4" not in vm.unset_items
    assert ip.source is None
    assert ip.unverified_retain is True
    assert NetBoxHandler.orphaned_tag not in ip.get_tags()
    assert ip.retain_from_prune is True

    age(ip, 40)
    assert prune_deletes(handler) == []


def test_unvisited_interface_is_kept_with_its_ip(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    vm, nic, ip = vm_with_ip(inventory)
    other = inventory.add_object(NBVMInterface, data={"name": "eth1", "virtual_machine": vm, "enabled": True},
                                 read_from_netbox=True)
    other_ip = inventory.add_object(NBIPAddress, data={
        "address": "10.0.0.6/24",
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": other,
    }, read_from_netbox=True)
    other.nb_id = 23
    other_ip.nb_id = 34
    for obj in (vm, nic, ip, other, other_ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag)
    vm.guest_ip_observations = {source.source_tag: "unknown"}

    source.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=[], vmware_object=TOOLS_DOWN)

    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    age(ip, 40)
    age(other_ip, 40)
    age(other, 40)

    assert NetBoxHandler.orphaned_tag not in other_ip.get_tags()
    assert NetBoxHandler.orphaned_tag not in other.get_tags()
    assert prune_deletes(handler) == []


def test_empty_guest_net_holds_and_a_reported_empty_nic_still_unassigns(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    vm, nic, ip = vm_with_ip(inventory)
    stamp(vm, NetBoxHandler.primary_tag, source.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, source.source_tag)
    vm.guest_ip_observations = {source.source_tag: "unknown"}

    source.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=[], vmware_object=TOOLS_RUNNING_NO_NICS)
    assert "assigned_object_id" not in ip.unset_items

    ip.unset_items = list()
    ip.unverified_retain = False
    vm.guest_ip_observations = {source.source_tag: "complete"}
    source.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=[], vmware_object=TOOLS_RUNNING_EMPTY_NIC)
    assert "assigned_object_id" in ip.unset_items


def test_unknown_then_trusted_removal_then_delayed_prune(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    vm, nic, ip = vm_with_ip(inventory)
    for obj in (vm, nic, ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, source.source_tag, NetBoxHandler.orphaned_tag)
    vm.guest_ip_observations = {source.source_tag: "unknown"}
    handler = make_handler(inventory)

    source.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=[], vmware_object=TOOLS_DOWN)
    inventory.tag_all_the_things(handler)
    age(ip, 0)
    assert NetBoxHandler.orphaned_tag not in ip.get_tags()
    assert prune_deletes(handler) == []

    ip.unverified_retain = False
    ip.unset_items = list()
    vm.guest_ip_observations = {source.source_tag: "complete"}
    source.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=[], vmware_object=TOOLS_RUNNING_EMPTY_NIC)
    inventory.tag_all_the_things(handler)
    age(ip, 0)
    assert NetBoxHandler.orphaned_tag in ip.get_tags()
    assert prune_deletes(handler) == []

    # A following run sees the orphan tag and no source tag. The delete waits
    # until last_updated is old enough.
    inventory.tag_all_the_things(handler)
    age(ip, 0)
    assert prune_deletes(handler) == []
    age(ip, 40)
    deleted = prune_deletes(handler)
    assert ("DELETE", NBIPAddress.name, ip.nb_id) in deleted


def test_absent_vm_is_not_given_an_unknown_observation(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    vm, nic, ip = vm_with_ip(inventory)
    for obj in (vm, nic, ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag)
    assert getattr(vm, "guest_ip_observations", None) in (None, {})

    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert getattr(vm, "guest_ip_observations", None) in (None, {})
    assert NetBoxHandler.orphaned_tag in vm.get_tags()
    assert vm.retain_from_prune is False

    age(vm, 40)
    deleted = prune_deletes(handler)
    assert ("DELETE", NBVM.name, vm.nb_id) in deleted


def test_failed_orphan_tag_removal_still_blocks_prune(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    vm, nic, ip = vm_with_ip(inventory)
    for obj in (vm, nic):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, source.source_tag, NetBoxHandler.orphaned_tag)
    vm.guest_ip_observations = {source.source_tag: "unknown"}
    ip.unverified_retain = True

    original = ip.remove_tags
    ip.remove_tags = lambda *args, **kwargs: None

    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    ip.remove_tags = original

    assert NetBoxHandler.orphaned_tag in ip.get_tags()
    assert ip.retain_from_prune is True
    age(ip, 40)
    assert prune_deletes(handler) == []


def test_redfish_keep_undiscovered_ips_protects_the_device_interface(inventory, source_settings):
    source = make_source(inventory, "redfish", source_settings)
    device, nic, ip = device_with_ip(inventory)
    for obj in (device, nic, ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag)

    source.add_update_interface(
        interface_object=nic, device_object=device, interface_data={"name": "eth0"},
        interface_ips=[], keep_undiscovered_ips=True)

    assert "assigned_object_id" not in ip.unset_items
    assert ip.unverified_retain is True
    assert ip.source is None

    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert NetBoxHandler.orphaned_tag not in ip.get_tags()
    assert NetBoxHandler.orphaned_tag not in nic.get_tags()
    assert nic.retain_from_prune is True
    age(ip, 40)
    age(nic, 40)
    assert prune_deletes(handler) == []


def test_source_order_does_not_let_another_source_clear_an_unknown_observation(inventory, source_settings):
    owner = make_source(inventory, "owner", source_settings)
    other = make_source(inventory, "other", source_settings)

    def run(first, second):
        vm, nic, ip = vm_with_ip(inventory, name=f"vm-{first.name}")
        for obj in (vm, nic, ip):
            stamp(obj, NetBoxHandler.primary_tag, owner.source_tag)
        vm.guest_ip_observations = {}

        def unknown(actor):
            vm.guest_ip_observations[owner.source_tag] = "unknown"
            actor.add_update_interface(
                interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
                interface_ips=[], vmware_object=TOOLS_DOWN)

        def removal(actor):
            actor.add_update_interface(
                interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
                interface_ips=[], vmware_object=TOOLS_RUNNING_EMPTY_NIC)

        steps = {owner: unknown, other: removal}
        steps[first](first)
        steps[second](second)
        assert "assigned_object_id" not in ip.unset_items
        assert vm.guest_ip_observations[owner.source_tag] == "unknown"

    run(owner, other)
    run(other, owner)


def test_ambiguous_owner_blocks_deletion_and_is_reported(inventory, source_settings, caplog):
    source = make_source(inventory, "vcenter", source_settings)
    other = make_source(inventory, "other", source_settings)
    handler = make_handler(inventory)

    missing, missing_nic, missing_ip = vm_with_ip(inventory, name="missing-owner")
    for obj in (missing, missing_nic, missing_ip):
        stamp(obj, NetBoxHandler.primary_tag)

    both, both_nic, both_ip = vm_with_ip(inventory, name="two-owners")
    for obj in (both, both_nic, both_ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag, other.source_tag)

    parent, parent_nic, disagreed = vm_with_ip(inventory, name="disagreed", address="10.9.0.5/24")
    stamp(parent, NetBoxHandler.primary_tag, source.source_tag)
    stamp(parent_nic, NetBoxHandler.primary_tag, source.source_tag)
    stamp(disagreed, NetBoxHandler.primary_tag, other.source_tag)

    for obj in (missing_ip, both_ip, disagreed):
        source.add_update_interface(
            interface_object=obj.get_interface(), device_object=obj.get_device_vm(),
            interface_data={"name": "eth0"}, interface_ips=[], vmware_object=TOOLS_RUNNING_EMPTY_NIC)
        assert "assigned_object_id" not in obj.unset_items

    inventory.tag_all_the_things(handler)
    assert NetBoxHandler.orphaned_tag not in missing.get_tags()
    assert NetBoxHandler.orphaned_tag not in both.get_tags()
    assert "assigned_object_id" not in disagreed.unset_items

    age(missing, 40)
    age(both, 40)
    age(missing_ip, 40)
    age(both_ip, 40)
    assert prune_deletes(handler) == []
    assert "missing-owner" in caplog.text
    assert "10.9.0.5/24" in caplog.text
    assert "tagged for a different source" in caplog.text
    assert "(no source tag)" in caplog.text
    assert source.source_tag in caplog.text and other.source_tag in caplog.text


def test_incomplete_source_listing_does_not_orphan_or_prune(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    source.inventory_complete = False
    vm, nic, ip = vm_with_ip(inventory)
    for obj in (vm, nic, ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag, NetBoxHandler.orphaned_tag)

    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert vm.retain_from_prune is True
    assert source.source_tag in vm.get_tags()
    assert source.source_tag in ip.get_tags()
    age(vm, 40)
    age(ip, 40)
    assert prune_deletes(handler) == []


def begin_next_run(inventory):
    """Drop in-memory flags the way a later process would when it reloads NetBox."""

    version = inventory.netbox_api_version
    inventory.base_structure = {}
    inventory.source_list = []
    inventory.init()
    inventory.netbox_api_version = version


def restore_vm(inventory, name, address, vm_tags, nic_tags, ip_tags):
    vm, nic, ip = vm_with_ip(inventory, name=name, address=address)
    stamp(vm, *vm_tags)
    stamp(nic, *nic_tags)
    stamp(ip, *ip_tags)
    return vm, nic, ip


def test_incomplete_protection_survives_into_the_next_run_and_absence_still_cleans_up(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    source.inventory_complete = False
    fresh_vm, fresh_nic, fresh_ip = vm_with_ip(inventory, name="fresh-vm", address="10.2.0.5/24")
    old_vm, old_nic, old_ip = vm_with_ip(inventory, name="old-vm", address="10.2.0.6/24")
    for obj in (fresh_vm, fresh_nic, fresh_ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag)
    for obj in (old_vm, old_nic, old_ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag, NetBoxHandler.orphaned_tag)

    inventory.tag_all_the_things(make_handler(inventory))
    saved = {
        "fresh": (fresh_vm.get_tags(), fresh_nic.get_tags(), fresh_ip.get_tags()),
        "old": (old_vm.get_tags(), old_nic.get_tags(), old_ip.get_tags()),
    }
    assert source.source_tag in saved["fresh"][0]
    assert source.source_tag in saved["old"][2]
    assert NetBoxHandler.orphaned_tag not in saved["fresh"][0]

    begin_next_run(inventory)
    source = make_source(inventory, "vcenter", source_settings)
    source.inventory_complete = False
    fresh_vm, fresh_nic, fresh_ip = restore_vm(inventory, "fresh-vm", "10.2.0.5/24", *saved["fresh"])
    old_vm, old_nic, old_ip = restore_vm(inventory, "old-vm", "10.2.0.6/24", *saved["old"])
    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert fresh_vm.retention_reason == "incomplete"
    assert old_ip.retention_reason == "incomplete"
    assert source.source_tag in fresh_vm.get_tags()
    age(old_vm, 40)
    age(old_ip, 40)
    assert prune_deletes(handler) == []
    saved = {
        "fresh": (fresh_vm.get_tags(), fresh_nic.get_tags(), fresh_ip.get_tags()),
        "old": (old_vm.get_tags(), old_nic.get_tags(), old_ip.get_tags()),
    }

    begin_next_run(inventory)
    source = make_source(inventory, "vcenter", source_settings)
    source.inventory_complete = True
    fresh_vm, fresh_nic, fresh_ip = restore_vm(inventory, "fresh-vm", "10.2.0.5/24", *saved["fresh"])
    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert NetBoxHandler.orphaned_tag in fresh_vm.get_tags()
    assert NetBoxHandler.orphaned_tag in fresh_ip.get_tags()
    assert fresh_vm.retain_from_prune is False
    age(fresh_vm, 40)
    age(fresh_ip, 40)
    deleted = prune_deletes(handler)
    assert ("DELETE", NBVM.name, fresh_vm.nb_id) in deleted
    assert ("DELETE", NBIPAddress.name, fresh_ip.nb_id) in deleted


def test_orphan_from_a_complete_listing_is_not_pruned_during_a_later_incomplete_listing(inventory, source_settings):
    source = make_source(inventory, "vcenter", source_settings)
    source.inventory_complete = True
    vm, nic, ip = vm_with_ip(inventory, name="gone-vm", address="10.4.0.5/24")
    for obj in (vm, nic, ip):
        stamp(obj, NetBoxHandler.primary_tag, source.source_tag)

    inventory.tag_all_the_things(make_handler(inventory))
    assert NetBoxHandler.orphaned_tag in vm.get_tags()
    assert NetBoxHandler.orphaned_tag in ip.get_tags()
    assert source.source_tag not in vm.get_tags()
    assert source.source_tag not in ip.get_tags()
    saved = (vm.get_tags(), nic.get_tags(), ip.get_tags())

    begin_next_run(inventory)
    source = make_source(inventory, "vcenter", source_settings)
    source.inventory_complete = False
    vm, nic, ip = restore_vm(inventory, "gone-vm", "10.4.0.5/24", *saved)
    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert vm.retention_reason == "incomplete"
    assert ip.retention_reason == "incomplete"
    age(vm, 40)
    age(nic, 40)
    age(ip, 40)
    assert prune_deletes(handler) == []
    saved = (vm.get_tags(), nic.get_tags(), ip.get_tags())

    begin_next_run(inventory)
    source = make_source(inventory, "vcenter", source_settings)
    source.inventory_complete = True
    vm, nic, ip = restore_vm(inventory, "gone-vm", "10.4.0.5/24", *saved)
    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert vm.retain_from_prune is False
    assert ip.retain_from_prune is False
    age(vm, 40)
    age(ip, 40)
    deleted = prune_deletes(handler)
    assert ("DELETE", NBVM.name, vm.nb_id) in deleted
    assert ("DELETE", NBIPAddress.name, ip.nb_id) in deleted


TOOLS_RUNNING_WITH_IP = SimpleNamespace(
    guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning",
                          net=[SimpleNamespace(ipAddress=["10.0.0.5"])]))


def test_other_source_cannot_move_or_claim_an_ip_while_the_owner_is_unknown(inventory, source_settings):
    def build():
        begin_next_run(inventory)
        owner = make_source(inventory, "owner", source_settings)
        other = make_source(inventory, "other", source_settings)
        vm, eth0, ip = vm_with_ip(inventory, name="shared-vm")
        eth1 = inventory.add_object(NBVMInterface, data={
            "name": "eth1", "virtual_machine": vm, "enabled": True,
        }, read_from_netbox=True)
        eth1.nb_id = 24
        for obj in (vm, eth0, eth1, ip):
            stamp(obj, NetBoxHandler.primary_tag, owner.source_tag)
        vm.guest_ip_observations = {owner.source_tag: "unknown"}
        owner.retain_existing_ip(ip)
        return owner, other, vm, eth0, eth1, ip

    def owner_pass(owner, vm, eth0):
        owner.add_update_interface(
            interface_object=eth0, device_object=vm, interface_data={"name": "eth0"},
            interface_ips=[], vmware_object=TOOLS_DOWN)

    def other_pass(other, vm, nic, ips):
        other.add_update_interface(
            interface_object=nic, device_object=vm, interface_data={"name": nic.data["name"]},
            interface_ips=ips, vmware_object=TOOLS_RUNNING_WITH_IP)

    for first_other in (True, False):
        for nic_name, ips in (("eth1", ["10.0.0.5/24"]), ("eth0", ["10.0.0.5/24"])):
            owner, other, vm, eth0, eth1, ip = build()
            nic = eth1 if nic_name == "eth1" else eth0
            steps = []
            if first_other:
                steps = [lambda: other_pass(other, vm, nic, ips), lambda: owner_pass(owner, vm, eth0)]
            else:
                steps = [lambda: owner_pass(owner, vm, eth0), lambda: other_pass(other, vm, nic, ips)]
            for step in steps:
                step()

            assert ip.get_interface() is eth0
            assert "assigned_object_id" not in ip.updated_items
            assert ip.source is not other
            assert owner.source_tag in ip.retained_by
            assert ip.unverified_retain is True
            inventory.tag_all_the_things(make_handler(inventory))
            assert other.source_tag not in ip.get_tags()
            assert owner.source_tag in ip.get_tags()


def test_shared_host_same_interface_is_seen_without_taking_ownership(inventory, source_settings, caplog):
    for first in ("vmware", "redfish"):
        caplog.clear()
        begin_next_run(inventory)
        vmware = make_source(inventory, "vmware", source_settings)
        redfish = make_source(inventory, "redfish", source_settings)
        device, nic, ip = device_with_ip(inventory)
        nic.data["name"] = "vmk0"
        stamp(device, NetBoxHandler.primary_tag, vmware.source_tag, redfish.source_tag)
        stamp(nic, NetBoxHandler.primary_tag, vmware.source_tag)
        stamp(ip, NetBoxHandler.primary_tag, vmware.source_tag)
        ip.retained_by = {vmware.source_tag, "Source: keeper"}
        ip.unverified_retain = True

        def report(actor):
            actor.add_update_interface(
                interface_object=nic, device_object=device, interface_data={"name": "vmk0"},
                interface_ips=["10.1.0.5/24"], vmware_object=TOOLS_RUNNING_WITH_IP)

        order = (vmware, redfish) if first == "vmware" else (redfish, vmware)
        for actor in order:
            report(actor)

        assert ip.get_interface() is nic
        assert ip.source is not redfish
        assert "Source: keeper" in ip.retained_by
        assert ip.unverified_retain is True
        assert "Unresolved source metadata" not in caplog.text
        inventory.tag_all_the_things(make_handler(inventory))
        assert vmware.source_tag in ip.get_tags()
        assert redfish.source_tag not in ip.get_tags()


def test_seen_ip_is_not_pruned_when_orphan_tag_removal_fails(inventory, source_settings):
    vmware = make_source(inventory, "vmware", source_settings)
    device, nic, ip = device_with_ip(inventory)
    stamp(device, NetBoxHandler.primary_tag, vmware.source_tag)
    stamp(nic, NetBoxHandler.primary_tag, vmware.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, vmware.source_tag, NetBoxHandler.orphaned_tag)
    ip.seen_this_run = True
    ip.remove_tags = lambda *args, **kwargs: None

    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert NetBoxHandler.orphaned_tag in ip.get_tags()
    assert ip.retain_from_prune is True
    age(ip, 40)
    assert prune_deletes(handler) == []


def test_horizon_reuses_one_unassigned_orphan_ip(inventory, source_settings):
    begin_next_run(inventory)
    vmware = make_source(inventory, "vmware", source_settings)
    vm, nic, ip = vm_with_ip(inventory, name="horizon-vm")
    ip.data["assigned_object_id"] = None
    ip.data["assigned_object_type"] = None
    stamp(vm, NetBoxHandler.primary_tag, vmware.source_tag)
    stamp(nic, NetBoxHandler.primary_tag, vmware.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, NetBoxHandler.orphaned_tag, "phpIPAM")

    vmware.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=["10.0.0.5/24"], vmware_object=TOOLS_RUNNING_WITH_IP)

    assert ip.get_interface() is nic
    assert ip.source is vmware
    inventory.tag_all_the_things(make_handler(inventory))
    assert NetBoxHandler.orphaned_tag not in ip.get_tags()
    assert "phpIPAM" in ip.get_tags()
    assert vmware.source_tag in ip.get_tags()


def test_duplicate_unassigned_ips_are_not_chosen(inventory, source_settings, caplog):
    vmware = make_source(inventory, "vmware", source_settings)
    vm, nic, ip = vm_with_ip(inventory, name="dup-vm")
    other = inventory.add_object(NBIPAddress, data={"address": "10.0.0.5/24"}, read_from_netbox=True)
    for obj in (ip, other):
        obj.data["assigned_object_id"] = None
        obj.data["assigned_object_type"] = None
        stamp(obj, NetBoxHandler.primary_tag)
    stamp(vm, NetBoxHandler.primary_tag, vmware.source_tag)
    stamp(nic, NetBoxHandler.primary_tag, vmware.source_tag)

    vmware.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
        interface_ips=["10.0.0.5/24"], vmware_object=TOOLS_RUNNING_WITH_IP)

    assert ip.get_interface() is None
    assert other.get_interface() is None
    assert "more than one record matches this address in the same VRF" in caplog.text


def test_shared_parent_incomplete_listing_waits_for_a_complete_absence(inventory, source_settings):
    vmware = make_source(inventory, "vmware", source_settings)
    redfish = make_source(inventory, "redfish", source_settings)
    vmware.inventory_complete = False
    device, nic, ip = device_with_ip(inventory)
    stamp(device, NetBoxHandler.primary_tag, vmware.source_tag, redfish.source_tag)
    stamp(nic, NetBoxHandler.primary_tag, vmware.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, vmware.source_tag)
    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert device.retention_reason == "incomplete"
    assert ip.retention_reason == "incomplete"
    age(device, 40)
    age(ip, 40)
    assert prune_deletes(handler) == []
    saved_device = device.get_tags()
    saved_nic = nic.get_tags()
    saved_ip = ip.get_tags()

    begin_next_run(inventory)
    vmware = make_source(inventory, "vmware", source_settings)
    redfish = make_source(inventory, "redfish", source_settings)
    device, nic, ip = device_with_ip(inventory)
    stamp(device, *saved_device)
    stamp(nic, *saved_nic)
    stamp(ip, *saved_ip)
    handler = make_handler(inventory)
    inventory.tag_all_the_things(handler)
    assert device.retain_from_prune is False
    age(device, 40)
    age(ip, 40)
    deleted = prune_deletes(handler)
    assert ("DELETE", NBDevice.name, device.nb_id) in deleted
    assert ("DELETE", NBIPAddress.name, ip.nb_id) in deleted


ADDRESS = "10.0.0.5/24"
HOST = "10.0.0.5"


def _vmware_pair(inventory, source_settings):
    begin_next_run(inventory)
    old = make_source(inventory, "old-vcenter", source_settings)
    new = make_source(inventory, "new-vcenter", source_settings)
    old.source_type = "vmware"
    new.source_type = "vmware"
    return old, new


def _disabled_assignment(inventory, tag_the_ip, old):
    """An address on a disabled NIC, and a second VM whose NIC is enabled."""

    old_vm, old_nic, ip = vm_with_ip(inventory, name="old-vm", address=ADDRESS)
    old_nic.data["enabled"] = False
    new_vm, new_nic, other = vm_with_ip(inventory, name="new-vm", address="10.9.9.9/24")
    other.data["assigned_object_id"] = None
    other.data["assigned_object_type"] = None
    for obj in (old_vm, old_nic):
        stamp(obj, NetBoxHandler.primary_tag, old.source_tag)
    if tag_the_ip:
        stamp(ip, NetBoxHandler.primary_tag, old.source_tag)
    else:
        stamp(ip, NetBoxHandler.primary_tag)
    stamp(new_vm, NetBoxHandler.primary_tag, old.source_tag)
    stamp(new_nic, NetBoxHandler.primary_tag, old.source_tag)
    return old_vm, old_nic, ip, new_vm, new_nic


def _report_on(source, vm, nic, addresses):
    source.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": nic.data["name"]},
        interface_ips=addresses, vmware_object=TOOLS_RUNNING_WITH_IP)


def test_complete_guest_report_records_addresses_and_unknown_clears_them(inventory, source_settings, caplog):
    source = make_source(inventory, "vcenter", source_settings)
    source.source_type = "vmware"
    vm, nic, ip = vm_with_ip(inventory)
    stamp(vm, NetBoxHandler.primary_tag, source.source_tag)
    stamp(nic, NetBoxHandler.primary_tag, source.source_tag)
    stamp(ip, NetBoxHandler.primary_tag, source.source_tag)

    caplog.set_level(logging.INFO, logger="NetBox-Sync")
    handler = object.__new__(VMWareHandler)
    handler.inventory = inventory
    handler.name = source.name
    handler.source_tag = source.source_tag
    handler.settings = source_settings

    handler.add_device_vm_to_inventory(
        NBVM,
        object_data={"name": "vm1", "status": "active", "cluster": vm.data["cluster"]},
        vnic_data={"eth0": {"name": "eth0", "enabled": True}},
        nic_ips={"eth0": [ADDRESS]},
        vmware_object=TOOLS_RUNNING_WITH_IP,
        guest_ip_observation="complete",
    )
    assert vm.guest_ip_observations[source.source_tag] == "complete"
    assert vm.guest_reported_ips[source.source_tag] == {HOST}

    handler.add_device_vm_to_inventory(
        NBVM,
        object_data={"name": "vm1", "status": "active", "cluster": vm.data["cluster"]},
        vnic_data={"eth0": {"name": "eth0", "enabled": True}},
        nic_ips={"eth0": []},
        vmware_object=TOOLS_DOWN,
        guest_ip_observation="unknown",
    )
    assert vm.guest_ip_observations[source.source_tag] == "unknown"
    assert source.source_tag not in vm.guest_reported_ips


def test_disabled_interface_ip_is_not_moved_until_the_old_source_says_it_is_gone(inventory, source_settings):
    """
    No guest observation is not a complete observation. A later unknown cancels
    a move that the other source already requested, whichever source ran first.
    A complete report that omits the address lets the move finish, in either order.
    """

    def run(tag_the_ip, new_first, mark):
        old, new = _vmware_pair(inventory, source_settings)
        old_vm, old_nic, ip, new_vm, new_nic = _disabled_assignment(inventory, tag_the_ip, old)

        def report_new():
            _report_on(new, new_vm, new_nic, [ADDRESS])

        if new_first:
            report_new()
            assert ip.get_interface() is old_nic
            assert [request["interface"] for request in ip.deferred_moves] == [new_nic]
            mark(old_vm, old)
        else:
            mark(old_vm, old)
            report_new()
        inventory.settle_deferred_ip_moves()
        return old_vm, old_nic, ip, new_nic

    def unknown(vm, source):
        vm.guest_ip_observations = {source.source_tag: "unknown"}

    def omitted(vm, source):
        vm.guest_ip_observations = {source.source_tag: "complete"}
        vm.guest_reported_ips = {source.source_tag: set()}

    def still_present(vm, source):
        vm.guest_ip_observations = {source.source_tag: "complete"}
        vm.guest_reported_ips = {source.source_tag: {HOST}}

    def bare_complete(vm, source):
        vm.guest_ip_observations = {source.source_tag: "complete"}

    for tag_the_ip in (True, False):
        for new_first in (True, False):
            _old_vm, old_nic, ip, _new_nic = run(tag_the_ip, new_first, unknown)
            assert ip.get_interface() is old_nic
            assert getattr(ip, "deferred_moves", None) is None
            assert "assigned_object_id" not in ip.updated_items

            _old_vm, old_nic, ip, _new_nic = run(tag_the_ip, new_first, still_present)
            assert ip.get_interface() is old_nic
            assert "assigned_object_id" not in ip.updated_items

            _old_vm, old_nic, ip, _new_nic = run(tag_the_ip, new_first, bare_complete)
            assert ip.get_interface() is old_nic
            assert "assigned_object_id" not in ip.updated_items

            old_vm, old_nic, ip, new_nic = run(tag_the_ip, new_first, omitted)
            assert ip.get_interface() is new_nic
            assert ip not in old_nic.get_ip_addresses()
            assert "primary_ip4" in old_vm.unset_items
            assert "assigned_object_id" not in ip.unset_items


def test_same_source_still_moves_an_address_its_complete_report_omitted(inventory, source_settings):
    begin_next_run(inventory)
    source = make_source(inventory, "vcenter", source_settings)
    source.source_type = "vmware"
    old_vm, old_nic, ip, new_vm, new_nic = _disabled_assignment(inventory, True, source)

    _report_on(source, new_vm, new_nic, [ADDRESS])
    assert ip.get_interface() is old_nic

    old_vm.guest_ip_observations = {source.source_tag: "complete"}
    old_vm.guest_reported_ips = {source.source_tag: set()}
    inventory.settle_deferred_ip_moves()
    assert ip.get_interface() is new_nic

    begin_next_run(inventory)
    source = make_source(inventory, "vcenter", source_settings)
    source.source_type = "vmware"
    old_vm, old_nic, ip, new_vm, new_nic = _disabled_assignment(inventory, True, source)
    old_vm.guest_ip_observations = {source.source_tag: "complete"}
    old_vm.guest_reported_ips = {source.source_tag: {"10.9.0.8"}}
    _report_on(source, new_vm, new_nic, [ADDRESS])
    inventory.settle_deferred_ip_moves()
    assert ip.get_interface() is new_nic


def test_a_later_report_on_the_old_interface_cancels_the_deferred_move(inventory, source_settings):
    old, new = _vmware_pair(inventory, source_settings)
    old_vm, old_nic, ip, new_vm, new_nic = _disabled_assignment(inventory, True, old)
    _report_on(new, new_vm, new_nic, [ADDRESS])
    assert [request["interface"] for request in ip.deferred_moves] == [new_nic]

    old_vm.guest_ip_observations = {old.source_tag: "complete"}
    old_vm.guest_reported_ips = {old.source_tag: set()}
    _report_on(old, old_vm, old_nic, [ADDRESS])
    inventory.settle_deferred_ip_moves()

    assert old.source_tag in ip.confirmed_by
    assert ip.get_interface() is old_nic
    assert "assigned_object_id" not in ip.updated_items


def test_an_assigned_duplicate_blocks_claiming_the_unassigned_record(inventory, source_settings, caplog):
    caplog.set_level(logging.WARNING, logger="NetBox-Sync")

    for unassigned_first in (True, False):
        caplog.clear()
        begin_next_run(inventory)
        source = make_source(inventory, "vmware", source_settings)
        source.source_type = "vmware"
        if unassigned_first:
            unassigned = inventory.add_object(NBIPAddress, data={"address": ADDRESS}, read_from_netbox=True)
            stamp(unassigned, NetBoxHandler.primary_tag)
        vm, nic, assigned = vm_with_ip(inventory, name="assigned-vm")
        nic.data["enabled"] = False
        if not unassigned_first:
            unassigned = inventory.add_object(NBIPAddress, data={"address": ADDRESS}, read_from_netbox=True)
            stamp(unassigned, NetBoxHandler.primary_tag)
        for obj in (vm, nic, assigned):
            stamp(obj, NetBoxHandler.primary_tag, source.source_tag)
        new_vm, new_nic, _other = vm_with_ip(inventory, name="claimer", address="10.9.9.9/24")
        stamp(new_vm, NetBoxHandler.primary_tag, source.source_tag)
        stamp(new_nic, NetBoxHandler.primary_tag, source.source_tag)

        assert source._may_claim_unassigned_ip(unassigned, new_vm, None) is False
        before = len(list(inventory.get_all_items(NBIPAddress)))
        _report_on(source, new_vm, new_nic, [ADDRESS])

        assert unassigned.get_interface() is None
        assert assigned.get_interface() is nic
        assert len(list(inventory.get_all_items(NBIPAddress))) == before
        assert "more than one record matches this address in the same VRF" in caplog.text


def test_a_non_unique_role_does_not_block_the_one_regular_unassigned_address(inventory, source_settings):
    source = make_source(inventory, "vmware", source_settings)
    source.source_type = "vmware"
    vm, nic, vip = vm_with_ip(inventory, name="vip-vm")
    vip.data["role"] = "vip"
    unassigned = inventory.add_object(NBIPAddress, data={"address": ADDRESS}, read_from_netbox=True)
    stamp(vip, NetBoxHandler.primary_tag, source.source_tag)
    stamp(unassigned, NetBoxHandler.primary_tag)
    stamp(vm, NetBoxHandler.primary_tag, source.source_tag)
    stamp(nic, NetBoxHandler.primary_tag, source.source_tag)
    new_vm, new_nic, _other = vm_with_ip(inventory, name="regular-vm", address="10.9.9.9/24")
    stamp(new_vm, NetBoxHandler.primary_tag, source.source_tag)
    stamp(new_nic, NetBoxHandler.primary_tag, source.source_tag)

    assert source._may_claim_unassigned_ip(unassigned, new_vm, None) is True
    _report_on(source, new_vm, new_nic, [ADDRESS])
    assert unassigned.get_interface() is new_nic
    assert vip.get_interface() is nic


def test_only_the_parent_source_may_remove_a_source_less_ip(inventory, source_settings, caplog):
    caplog.set_level(logging.WARNING, logger="NetBox-Sync")

    def build(observation=None):
        begin_next_run(inventory)
        owner = make_source(inventory, "owner", source_settings)
        other = make_source(inventory, "other", source_settings)
        owner.source_type = "vmware"
        vm, nic, ip = vm_with_ip(inventory, name="parent-vm", address="10.8.0.5/24")
        stamp(vm, NetBoxHandler.primary_tag, owner.source_tag)
        stamp(nic, NetBoxHandler.primary_tag, owner.source_tag)
        stamp(ip, NetBoxHandler.primary_tag)
        if observation is not None:
            vm.guest_ip_observations = {owner.source_tag: observation}
            if observation == "complete":
                vm.guest_reported_ips = {owner.source_tag: set()}
        return owner, other, vm, nic, ip

    def empty(actor, vm, nic, tools):
        actor.add_update_interface(
            interface_object=nic, device_object=vm, interface_data={"name": "eth0"},
            interface_ips=[], vmware_object=tools)

    # No observation yet, and a complete observation: a different source still
    # may not remove the address. The parent source may, once it is not unknown.
    for observation in (None, "complete"):
        owner, other, vm, nic, ip = build(observation)
        assert other.may_remove_ip_assignments(vm, ip=ip) is False
        assert owner.may_remove_ip_assignments(vm, ip=ip) is True
        assert "only its parent source can remove it" in caplog.text
        caplog.clear()

    for other_first in (True, False):
        owner, other, vm, nic, ip = build("complete")
        if other_first:
            empty(other, vm, nic, TOOLS_RUNNING_EMPTY_NIC)
            assert "assigned_object_id" not in ip.unset_items
            empty(owner, vm, nic, TOOLS_RUNNING_EMPTY_NIC)
        else:
            empty(owner, vm, nic, TOOLS_RUNNING_EMPTY_NIC)
            # The owner's removal waits until competing reports from this run are known.
            assert "assigned_object_id" not in ip.unset_items
            assert ip.pending_removals[0]["source"] is owner
            empty(other, vm, nic, TOOLS_RUNNING_EMPTY_NIC)
        inventory.settle_deferred_ip_moves()
        assert "assigned_object_id" in ip.unset_items
        assert "primary_ip4" in vm.unset_items

    for other_first in (True, False):
        owner, other, vm, nic, ip = build("unknown")
        steps = [other, owner] if other_first else [owner, other]
        for actor in steps:
            empty(actor, vm, nic, TOOLS_DOWN if actor is owner else TOOLS_RUNNING_EMPTY_NIC)
        assert "assigned_object_id" not in ip.unset_items
        assert ip.get_interface() is nic

    owner, other, vm, nic, ip = build(None)
    stamp(vm, other.source_tag)
    assert owner.may_remove_ip_assignments(vm, ip=ip) is False
    assert other.may_remove_ip_assignments(vm, ip=ip) is False
    empty(other, vm, nic, TOOLS_RUNNING_EMPTY_NIC)
    empty(owner, vm, nic, TOOLS_RUNNING_EMPTY_NIC)
    assert "assigned_object_id" not in ip.unset_items


def _settings(source_settings, **changes):
    values = dict(vars(source_settings))
    values.update(changes)
    return SimpleNamespace(**values)


def _vmware_handler(inventory, source):
    handler = object.__new__(VMWareHandler)
    handler.inventory = inventory
    handler.name = source.name
    handler.source_tag = source.source_tag
    handler.settings = source.settings
    return handler


def _report_vm(handler, vm, addresses, primary):
    return handler.add_device_vm_to_inventory(
        NBVM,
        object_data={"name": vm.data["name"], "status": "active", "cluster": vm.data["cluster"]},
        vnic_data={"eth0": {"name": "eth0", "enabled": True}},
        nic_ips={"eth0": addresses},
        p_ipv4=primary,
        vmware_object=TOOLS_RUNNING_WITH_IP,
        guest_ip_observation="complete",
    )


def test_deferred_move_keeps_the_new_vm_primary_in_both_orders(inventory, source_settings):
    """
    The deferred report does not return the IP, so primary selection cannot
    run during that pass. Settlement applies the same set_primary_ip policy.
    """

    def prepare(policy):
        begin_next_run(inventory)
        settings = _settings(source_settings, set_primary_ip=policy)
        old = make_source(inventory, "old-vcenter", settings)
        new = make_source(inventory, "new-vcenter", settings)
        old.source_type = "vmware"
        new.source_type = "vmware"
        old_vm, old_nic, ip, new_vm, new_nic = _disabled_assignment(inventory, True, old)
        new_vm.data["primary_ip4"] = None
        return old, new, old_vm, old_nic, ip, new_vm, new_nic

    def omitted(vm, source):
        handler = _vmware_handler(inventory, source)
        handler.add_device_vm_to_inventory(
            NBVM,
            object_data={"name": vm.data["name"], "status": "active", "cluster": vm.data["cluster"]},
            vnic_data={"eth0": {"name": "eth0", "enabled": False}},
            nic_ips={"eth0": []},
            vmware_object=TOOLS_RUNNING_EMPTY_NIC,
            guest_ip_observation="complete",
        )

    for policy in ("when-undefined", "always"):
        for new_first in (True, False):
            old, new, old_vm, old_nic, ip, new_vm, new_nic = prepare(policy)
            handler = _vmware_handler(inventory, new)

            def report_new():
                device, interfaces = _report_vm(handler, new_vm, [ADDRESS], ADDRESS)
                assert device is new_vm
                assert interfaces["eth0"] is new_nic
                return interfaces["eth0"].get_ip_addresses()

            if new_first:
                reported = report_new()
                omitted(old_vm, old)
            else:
                omitted(old_vm, old)
                reported = report_new()
            assert ip not in reported
            assert new_vm.data.get("primary_ip4") is None
            assert ip.deferred_moves[0]["primary_version"] == 4
            assert ip.get_interface() is old_nic
            assert "assigned_object_id" not in ip.unset_items
            assert "primary_ip4" not in old_vm.unset_items
            inventory.settle_deferred_ip_moves()

            assert ip.get_interface() is new_nic
            assert new_vm.data["primary_ip4"] is ip
            assert "primary_ip4" in old_vm.unset_items
            assert "primary_ip4" not in new_vm.unset_items

    for new_first in (True, False):
        old, new, old_vm, old_nic, ip, new_vm, new_nic = prepare("when-undefined")
        handler = _vmware_handler(inventory, new)
        if new_first:
            _report_vm(handler, new_vm, [ADDRESS], ADDRESS)
            old_vm.guest_ip_observations = {old.source_tag: "unknown"}
        else:
            old_vm.guest_ip_observations = {old.source_tag: "unknown"}
            _report_vm(handler, new_vm, [ADDRESS], ADDRESS)
        inventory.settle_deferred_ip_moves()
        assert ip.get_interface() is old_nic
        assert new_vm.data.get("primary_ip4") is None
        assert "primary_ip4" not in old_vm.unset_items
        assert old_vm.data["primary_ip4"] is ip

    old, new, old_vm, old_nic, ip, new_vm, new_nic = prepare("when-undefined")
    kept_primary = inventory.add_object(NBIPAddress, data={"address": "10.9.9.8/24"}, read_from_netbox=True)
    new_vm.data["primary_ip4"] = kept_primary
    handler = _vmware_handler(inventory, new)
    _report_vm(handler, new_vm, [ADDRESS], ADDRESS)
    omitted(old_vm, old)
    inventory.settle_deferred_ip_moves()
    assert ip.get_interface() is new_nic
    assert new_vm.data["primary_ip4"] is kept_primary

    old, new, old_vm, old_nic, ip, new_vm, new_nic = prepare("always")
    previous = inventory.add_object(NBIPAddress, data={"address": "10.9.9.8/24"}, read_from_netbox=True)
    new_vm.data["primary_ip4"] = previous
    handler = _vmware_handler(inventory, new)
    _report_vm(handler, new_vm, [ADDRESS], ADDRESS)
    omitted(old_vm, old)
    inventory.settle_deferred_ip_moves()
    assert new_vm.data["primary_ip4"] is ip

    old, new, old_vm, old_nic, ip, new_vm, new_nic = prepare("never")
    handler = _vmware_handler(inventory, new)
    _report_vm(handler, new_vm, [ADDRESS], ADDRESS)
    omitted(old_vm, old)
    inventory.settle_deferred_ip_moves()
    assert ip.get_interface() is new_nic
    assert new_vm.data.get("primary_ip4") is None
    assert "primary_ip4" in old_vm.unset_items


def _writes(inventory):
    """Run the NetBox write passes and return every request payload."""

    handler = make_handler(inventory)
    payloads = []

    def request(object_class, req_type="GET", data=None, params=None, nb_id=None):
        payloads.append((req_type, object_class.name, nb_id, data))
        return None

    handler.request = request
    for object_class in (NBIPAddress, NBVM, NBVMInterface):
        handler.update_object(object_class, unset=True)
        handler.update_object(object_class)
        handler.update_object(object_class, last_run=True)
    return payloads


def _payload_clears(payloads, name, nb_id, key):
    for req_type, object_name, written_id, data in payloads:
        if object_name == name and written_id == nb_id and isinstance(data, dict) and data.get(key) is None \
                and key in data:
            return True
    return False


def test_competing_claims_follow_the_whole_run_not_source_order(inventory, source_settings, caplog):
    """
    The old interface is reconciled for real. Its removal stays unqueued until
    every claimant has reported. Two claimants leave the assignment and primary
    in place through the write. One claimant moves and the primary follows.
    """

    caplog.set_level(logging.WARNING, logger="NetBox-Sync")

    def build():
        begin_next_run(inventory)
        caplog.clear()
        settings = _settings(source_settings, set_primary_ip="when-undefined")
        old = make_source(inventory, "old-vcenter", settings)
        first = make_source(inventory, "claim-a", settings)
        second = make_source(inventory, "claim-b", settings)
        for source in (old, first, second):
            source.source_type = "vmware"
        old_vm, old_nic, ip = vm_with_ip(inventory, name="old-vm", address=ADDRESS)
        old_nic.data["enabled"] = False
        old_vm.nb_id = 11
        old_nic.nb_id = 21
        ip.nb_id = 31
        vm_a, nic_a, extra_a = vm_with_ip(inventory, name="vm-a", address="10.9.1.1/24")
        vm_b, nic_b, extra_b = vm_with_ip(inventory, name="vm-b", address="10.9.1.2/24")
        vm_a.nb_id = 12
        nic_a.nb_id = 22
        vm_b.nb_id = 13
        nic_b.nb_id = 23
        for extra in (extra_a, extra_b):
            extra.data["assigned_object_id"] = None
            extra.data["assigned_object_type"] = None
        vm_a.data["primary_ip4"] = None
        vm_b.data["primary_ip4"] = None
        for obj in (old_vm, old_nic, ip):
            stamp(obj, NetBoxHandler.primary_tag, old.source_tag)
        for source, vm, nic in ((first, vm_a, nic_a), (second, vm_b, nic_b)):
            stamp(vm, NetBoxHandler.primary_tag, source.source_tag)
            stamp(nic, NetBoxHandler.primary_tag, source.source_tag)
        steps = {
            "old": lambda: _vmware_handler(inventory, old).add_device_vm_to_inventory(
                NBVM,
                object_data={"name": "old-vm", "status": "active", "cluster": old_vm.data["cluster"]},
                vnic_data={"eth0": {"name": "eth0", "enabled": False}},
                nic_ips={"eth0": []},
                vmware_object=TOOLS_RUNNING_EMPTY_NIC,
                guest_ip_observation="complete",
            ),
            "a": lambda: _report_vm(_vmware_handler(inventory, first), vm_a, [ADDRESS], ADDRESS),
            "b": lambda: _report_vm(_vmware_handler(inventory, second), vm_b, [ADDRESS], ADDRESS),
        }
        return old_vm, old_nic, ip, nic_a, nic_b, vm_a, steps

    def retained(old_vm, old_nic, ip, nic_a, nic_b):
        assert ip.get_interface() is old_nic
        assert ip not in nic_a.get_ip_addresses()
        assert ip not in nic_b.get_ip_addresses()
        assert old_vm.data["primary_ip4"] is ip
        assert "assigned_object_id" not in ip.unset_items
        assert "assigned_object_type" not in ip.unset_items
        assert "primary_ip4" not in old_vm.unset_items

    for order in (("a", "b", "old"), ("old", "a", "b"), ("a", "old", "b")):
        old_vm, old_nic, ip, nic_a, nic_b, vm_a, steps = build()
        for step in order:
            steps[step]()
        retained(old_vm, old_nic, ip, nic_a, nic_b)
        assert {request["interface"] for request in ip.deferred_moves} == {nic_a, nic_b}
        inventory.settle_deferred_ip_moves()
        retained(old_vm, old_nic, ip, nic_a, nic_b)
        assert "more than one interface requested it" in caplog.text
        payloads = _writes(inventory)
        assert _payload_clears(payloads, NBIPAddress.name, ip.nb_id, "assigned_object_id") is False
        assert _payload_clears(payloads, NBVM.name, old_vm.nb_id, "primary_ip4") is False
        retained(old_vm, old_nic, ip, nic_a, nic_b)

    old_vm, old_nic, ip, nic_a, nic_b, vm_a, steps = build()
    steps["old"]()
    steps["a"]()
    assert [request["interface"] for request in ip.deferred_moves] == [nic_a]
    assert ip.get_interface() is old_nic
    assert "primary_ip4" not in old_vm.unset_items
    inventory.settle_deferred_ip_moves()
    assert ip.get_interface() is nic_a
    assert vm_a.data["primary_ip4"] is ip
    assert "primary_ip4" in old_vm.unset_items
    assert "assigned_object_id" not in ip.unset_items
    payloads = _writes(inventory)
    assert _payload_clears(payloads, NBVM.name, old_vm.nb_id, "primary_ip4") is True
    assert any(item[0] == "PATCH" and item[1] == NBIPAddress.name and isinstance(item[3], dict)
               and item[3].get("assigned_object_id") == nic_a.nb_id for item in payloads)

    old_vm, old_nic, ip, nic_a, nic_b, vm_a, steps = build()
    steps["old"]()
    assert ip.get_interface() is old_nic
    assert "assigned_object_id" not in ip.unset_items
    inventory.settle_deferred_ip_moves()
    assert "assigned_object_id" in ip.unset_items
    assert "primary_ip4" in old_vm.unset_items


def test_a_later_report_on_the_same_nic_cancels_a_held_removal(inventory, source_settings):
    """
    An empty NIC holds a removal. A later report of the address on that NIC
    retires the hold. The opposite order still removes it. Both go through the
    interface method and the write.
    """

    def prepare():
        begin_next_run(inventory)
        source = make_source(inventory, "vcenter", source_settings)
        source.source_type = "vmware"
        vm, nic, ip = vm_with_ip(inventory, name="same-nic")
        vm.nb_id = 11
        nic.nb_id = 22
        ip.nb_id = 33
        for obj in (vm, nic, ip):
            stamp(obj, NetBoxHandler.primary_tag, source.source_tag)
        handler = _vmware_handler(inventory, source)

        def report(addresses, tools, observation):
            handler.add_device_vm_to_inventory(
                NBVM,
                object_data={"name": vm.data["name"], "status": "active", "cluster": vm.data["cluster"]},
                vnic_data={"eth0": {"name": "eth0", "enabled": True}},
                nic_ips={"eth0": addresses},
                p_ipv4=addresses[0] if addresses else None,
                vmware_object=tools,
                guest_ip_observation=observation,
            )

        return vm, nic, ip, report

    for order in ("empty-then-present", "present-then-empty"):
        vm, nic, ip, report = prepare()
        empty = lambda: report([], TOOLS_RUNNING_EMPTY_NIC, "complete")
        present = lambda: report([ADDRESS], TOOLS_RUNNING_WITH_IP, "complete")
        if order == "empty-then-present":
            empty()
            assert ip.pending_removals
            assert "assigned_object_id" not in ip.unset_items
            present()
            assert not ip.pending_removals
            inventory.settle_deferred_ip_moves()
            assert ip.get_interface() is nic
            assert vm.data["primary_ip4"] is ip
            assert "assigned_object_id" not in ip.unset_items
            assert "primary_ip4" not in vm.unset_items
            payloads = _writes(inventory)
            assert _payload_clears(payloads, NBIPAddress.name, ip.nb_id, "assigned_object_id") is False
            assert _payload_clears(payloads, NBVM.name, vm.nb_id, "primary_ip4") is False
            assert ip.get_interface() is nic
            assert vm.data["primary_ip4"] is ip
        else:
            present()
            assert not getattr(ip, "pending_removals", None)
            empty()
            assert ip.pending_removals
            assert "assigned_object_id" not in ip.unset_items
            inventory.settle_deferred_ip_moves()
            assert "assigned_object_id" in ip.unset_items
            assert "assigned_object_type" in ip.unset_items
            assert "primary_ip4" in vm.unset_items
            payloads = _writes(inventory)
            assert _payload_clears(payloads, NBIPAddress.name, ip.nb_id, "assigned_object_id") is True
            assert _payload_clears(payloads, NBIPAddress.name, ip.nb_id, "assigned_object_type") is True
            assert _payload_clears(payloads, NBVM.name, vm.nb_id, "primary_ip4") is True


def test_a_later_empty_nic_supersedes_an_earlier_confirmation_for_a_new_claimant(inventory, source_settings):
    """
    confirmed_by keeps the earlier report. Move eligibility follows the later
    one. Present then empty lets the new VM take the address; empty then
    present keeps the old assignment. The claimant may arrive before that
    later report.
    """

    def prepare():
        begin_next_run(inventory)
        settings = _settings(source_settings, set_primary_ip="when-undefined")
        old = make_source(inventory, "old-vcenter", settings)
        new = make_source(inventory, "new-vcenter", settings)
        old.source_type = "vmware"
        new.source_type = "vmware"
        old_vm, old_nic, ip = vm_with_ip(inventory, name="old-vm", address=ADDRESS)
        old_nic.data["enabled"] = False
        old_vm.nb_id = 11
        old_nic.nb_id = 21
        ip.nb_id = 31
        new_vm, new_nic, extra = vm_with_ip(inventory, name="new-vm", address="10.9.9.9/24")
        extra.data["assigned_object_id"] = None
        extra.data["assigned_object_type"] = None
        new_vm.data["primary_ip4"] = None
        new_vm.nb_id = 12
        new_nic.nb_id = 22
        for obj in (old_vm, old_nic, ip):
            stamp(obj, NetBoxHandler.primary_tag, old.source_tag)
        stamp(new_vm, NetBoxHandler.primary_tag, new.source_tag)
        stamp(new_nic, NetBoxHandler.primary_tag, new.source_tag)
        old_handler = _vmware_handler(inventory, old)
        new_handler = _vmware_handler(inventory, new)

        def report_old(addresses, tools):
            old_handler.add_device_vm_to_inventory(
                NBVM,
                object_data={"name": old_vm.data["name"], "status": "active", "cluster": old_vm.data["cluster"]},
                vnic_data={"eth0": {"name": "eth0", "enabled": False}},
                nic_ips={"eth0": addresses},
                p_ipv4=addresses[0] if addresses else None,
                vmware_object=tools,
                guest_ip_observation="complete",
            )

        def claim():
            new_handler.add_device_vm_to_inventory(
                NBVM,
                object_data={"name": new_vm.data["name"], "status": "active", "cluster": new_vm.data["cluster"]},
                vnic_data={"eth0": {"name": "eth0", "enabled": True}},
                nic_ips={"eth0": [ADDRESS]},
                p_ipv4=ADDRESS,
                vmware_object=TOOLS_RUNNING_WITH_IP,
                guest_ip_observation="complete",
            )

        steps = {
            "present": lambda: report_old([ADDRESS], TOOLS_RUNNING_WITH_IP),
            "empty": lambda: report_old([], TOOLS_RUNNING_EMPTY_NIC),
            "claim": claim,
        }
        return old_vm, old_nic, ip, new_vm, new_nic, steps

    def moved(old_vm, old_nic, ip, new_vm, new_nic):
        inventory.settle_deferred_ip_moves()
        assert ip.get_interface() is new_nic
        assert ip not in old_nic.get_ip_addresses()
        assert new_vm.data["primary_ip4"] is ip
        assert "primary_ip4" in old_vm.unset_items
        assert "assigned_object_id" not in ip.unset_items
        payloads = _writes(inventory)
        assert _payload_clears(payloads, NBVM.name, old_vm.nb_id, "primary_ip4") is True
        assert any(item[0] == "PATCH" and item[1] == NBIPAddress.name and isinstance(item[3], dict)
                   and item[3].get("assigned_object_id") == new_nic.nb_id for item in payloads)

    def kept(old_vm, old_nic, ip, new_vm, new_nic):
        inventory.settle_deferred_ip_moves()
        assert ip.get_interface() is old_nic
        assert new_vm.data.get("primary_ip4") is None
        assert old_vm.data["primary_ip4"] is ip
        assert "assigned_object_id" not in ip.unset_items
        assert "primary_ip4" not in old_vm.unset_items
        payloads = _writes(inventory)
        assert _payload_clears(payloads, NBIPAddress.name, ip.nb_id, "assigned_object_id") is False
        assert _payload_clears(payloads, NBVM.name, old_vm.nb_id, "primary_ip4") is False
        assert ip.get_interface() is old_nic
        assert old_vm.data["primary_ip4"] is ip

    for order in (("present", "empty", "claim"), ("present", "claim", "empty")):
        old_vm, old_nic, ip, new_vm, new_nic, steps = prepare()
        for step in order:
            steps[step]()
        moved(old_vm, old_nic, ip, new_vm, new_nic)

    for order in (("empty", "present", "claim"), ("empty", "claim", "present")):
        old_vm, old_nic, ip, new_vm, new_nic, steps = prepare()
        for step in order:
            steps[step]()
        kept(old_vm, old_nic, ip, new_vm, new_nic)
