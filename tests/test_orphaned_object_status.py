"""
Tests for the 'orphaned_device_status' and 'orphaned_vm_status' options (PR #514).

Both options are opt-in, so with neither of them configured the status of a device or a
VM has to stay exactly as it is, no matter if the object goes orphaned or shows up in a
source again. Resetting a reappearing object back to 'active' is only allowed to undo a
status this program parked it at, otherwise a status a user set on purpose (say
'planned') would be overwritten on the next run.

tag_all_the_things() is driven directly against the in-memory NetBoxInventory, so
neither a NetBox instance nor a source system is needed.
"""

import types

import pytest

from module.netbox.connection import NetBoxHandler
from module.netbox.object_classes import NBCluster, NBClusterType, NBDevice, NBSite, NBVM
from module.sources.common.source_base import SourceBase


def make_netbox_handler(orphaned_device_status=None, orphaned_vm_status=None, prune_enabled=True):
    """A NetBoxHandler carrying only the settings tag_all_the_things() reads."""

    handler = object.__new__(NetBoxHandler)
    handler.settings = types.SimpleNamespace(
        ignore_unknown_source_object_pruning=False,
        prune_enabled=prune_enabled,
        orphaned_device_status=orphaned_device_status,
        orphaned_vm_status=orphaned_vm_status,
    )
    return handler


def make_source(inventory):
    """An enabled source, registered with the inventory the way a real run does."""

    source = SourceBase()
    source.inventory = inventory
    source.name = "test"
    source.source_tag = "Source: test"
    source.settings = types.SimpleNamespace(enabled=True)
    inventory.source_list.append(source)
    return source


def netbox_status(status):
    """A status the way the NetBox API reports it."""

    return {"value": status, "label": status.capitalize()}


def existing_device(inventory, status):
    """A device which is present in NetBox with the given status."""

    site = inventory.add_object(NBSite, data={"id": 1, "name": "site1"}, read_from_netbox=True)
    return inventory.add_object(NBDevice, data={
        "id": 1,
        "name": "server01",
        "site": site,
        "status": netbox_status(status),
    }, read_from_netbox=True)


def existing_vm(inventory, status):
    """A virtual machine which is present in NetBox with the given status."""

    cluster_type = inventory.add_object(NBClusterType, data={"id": 1, "name": "vmware"},
                                        read_from_netbox=True)
    cluster = inventory.add_object(NBCluster, data={"id": 1, "name": "cluster1", "type": cluster_type},
                                   read_from_netbox=True)
    return inventory.add_object(NBVM, data={
        "id": 1,
        "name": "vm01",
        "cluster": cluster,
        "status": netbox_status(status),
    }, read_from_netbox=True)


def reappeared(this_object, source):
    """An object which a previous run tagged as orphaned and which a source reports again."""

    this_object.add_tags([NetBoxHandler.primary_tag, source.source_tag, NetBoxHandler.orphaned_tag])
    this_object.source = source
    this_object.updated_items = list()

    return this_object


def vanished(this_object, source):
    """An object which a previous run synced but which no source reports anymore."""

    this_object.add_tags([NetBoxHandler.primary_tag, source.source_tag])
    this_object.source = None
    this_object.updated_items = list()

    return this_object


@pytest.mark.parametrize("add_object", [existing_device, existing_vm], ids=["device", "vm"])
def test_status_of_a_reappearing_object_is_untouched_if_no_status_option_is_set(inventory, add_object):
    """Without either option this program never set a status, so it must not reset one."""

    source = make_source(inventory)
    this_object = reappeared(add_object(inventory, "planned"), source)

    inventory.tag_all_the_things(make_netbox_handler())

    assert NetBoxHandler.orphaned_tag not in this_object.get_tags()
    assert this_object.data.get("status") == netbox_status("planned"), \
        "a status set by hand was overwritten while removing the orphaned tag"
    assert "status" not in this_object.updated_items, "an unwanted status update was queued for NetBox"


@pytest.mark.parametrize("add_object", [existing_device, existing_vm], ids=["device", "vm"])
def test_status_of_an_orphaned_object_is_untouched_if_no_status_option_is_set(inventory, add_object):
    """Same the other way around: no option, no status change when an object goes orphaned."""

    source = make_source(inventory)
    this_object = vanished(add_object(inventory, "active"), source)

    inventory.tag_all_the_things(make_netbox_handler())

    assert NetBoxHandler.orphaned_tag in this_object.get_tags()
    assert this_object.data.get("status") == netbox_status("active")
    assert "status" not in this_object.updated_items, "an unwanted status update was queued for NetBox"


def test_orphaned_device_gets_the_configured_status(inventory):
    """The feature itself: an orphaned device is set to the configured status."""

    source = make_source(inventory)
    device = vanished(existing_device(inventory, "active"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_device_status="decommissioning"))

    assert NetBoxHandler.orphaned_tag in device.get_tags()
    assert device.data.get("status") == "decommissioning"
    assert "status" in device.updated_items


def test_orphaned_vm_gets_the_configured_status(inventory):
    """The feature itself: an orphaned VM is set to the configured status."""

    source = make_source(inventory)
    vm = vanished(existing_vm(inventory, "active"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_vm_status="decommissioning"))

    assert NetBoxHandler.orphaned_tag in vm.get_tags()
    assert vm.data.get("status") == "decommissioning"
    assert "status" in vm.updated_items


def test_reappearing_device_is_reset_to_active(inventory):
    """A device parked at the configured status is active again once it shows up again."""

    source = make_source(inventory)
    device = reappeared(existing_device(inventory, "decommissioning"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_device_status="decommissioning"))

    assert NetBoxHandler.orphaned_tag not in device.get_tags()
    assert device.data.get("status") == "active"
    assert "status" in device.updated_items


def test_reappearing_vm_is_reset_to_active(inventory):
    """A VM parked at the configured status is active again once it shows up again."""

    source = make_source(inventory)
    vm = reappeared(existing_vm(inventory, "decommissioning"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_vm_status="decommissioning"))

    assert NetBoxHandler.orphaned_tag not in vm.get_tags()
    assert vm.data.get("status") == "active"
    assert "status" in vm.updated_items


def test_device_option_does_not_apply_to_virtual_machines(inventory):
    """Each object type has its own option and must not be affected by the other one."""

    source = make_source(inventory)
    vm = vanished(existing_vm(inventory, "active"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_device_status="decommissioning"))

    assert vm.data.get("status") == netbox_status("active")
    assert "status" not in vm.updated_items


def test_vm_option_does_not_apply_to_devices(inventory):
    """Each object type has its own option and must not be affected by the other one."""

    source = make_source(inventory)
    device = vanished(existing_device(inventory, "active"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_vm_status="decommissioning"))

    assert device.data.get("status") == netbox_status("active")
    assert "status" not in device.updated_items


def test_status_which_was_not_set_by_this_program_is_kept(inventory):
    """
    A status which does not match the configured one was set by a user or reported by the
    source of this object. Removing the orphaned tag must not reset it to 'active'.
    """

    source = make_source(inventory)
    device = reappeared(existing_device(inventory, "planned"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_device_status="decommissioning"))

    assert NetBoxHandler.orphaned_tag not in device.get_tags()
    assert device.data.get("status") == netbox_status("planned")
    assert "status" not in device.updated_items


def test_no_status_is_set_while_pruning_is_disabled(inventory):
    """
    Pruning is switched off whenever an enabled source was unavailable. Every object of
    that source looks orphaned then, so their status must be left alone.
    """

    source = make_source(inventory)
    device = vanished(existing_device(inventory, "active"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_device_status="decommissioning",
                                                    prune_enabled=False))

    assert NetBoxHandler.orphaned_tag in device.get_tags()
    assert device.data.get("status") == netbox_status("active")
    assert "status" not in device.updated_items


def test_a_status_unknown_to_netbox_is_rejected(inventory):
    """A status which is not part of the NetBox device model must not be sent to NetBox."""

    source = make_source(inventory)
    device = vanished(existing_device(inventory, "active"), source)

    inventory.tag_all_the_things(make_netbox_handler(orphaned_device_status="retired"))

    assert device.data.get("status") == netbox_status("active")
    assert "status" not in device.updated_items, "an invalid status was queued for NetBox"
