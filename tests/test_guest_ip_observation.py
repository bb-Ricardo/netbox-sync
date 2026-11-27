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
    assert NetBoxHandler.orphaned_tag not in disagreed.get_tags()

    age(missing, 40)
    age(both, 40)
    age(disagreed, 40)
    assert prune_deletes(handler) == []
    assert "missing-owner" in caplog.text
    assert "two-owners" in caplog.text
    assert "10.9.0.5/24" in caplog.text
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
