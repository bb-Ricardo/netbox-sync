"""Inventory items the check_redfish source still manages must not be tagged orphaned.

Drives the real CheckRedfish methods and the real tag_all_the_things() against the real
NetBoxInventory. Only the NetBox REST API itself is out of scope.
"""

import types

from module.netbox.connection import NetBoxHandler
from module.netbox.object_classes import NBInventoryItem


def make_netbox_handler():
    """A NetBoxHandler carrying only what tag_all_the_things() reads, with the real tag names."""

    handler = object.__new__(NetBoxHandler)
    handler.settings = types.SimpleNamespace(ignore_unknown_source_object_pruning=False)
    return handler


def existing_item(source, device, name, inventory_type, health="OK"):
    """Seed an item the way query_current_data() does: read from NetBox, tagged, unclaimed."""

    item = source.inventory.add_object(NBInventoryItem, data={
        "device": device,
        "name": name,
        "custom_fields": {"inventory_type": inventory_type, "health": health},
    }, read_from_netbox=True)
    item.add_tags([NetBoxHandler.primary_tag, source.source_tag])
    item.source = None
    item.updated_items = list()
    return item


def test_components_are_marked_absent_when_the_scan_reports_none_of_their_type(check_redfish_source):
    """A scan reporting no fan says the fans are gone, so they must be marked absent."""

    context = check_redfish_source()
    context.inventory.source_list.append(context.source)
    fan = existing_item(context.source, context.device, "Fan 1 (ID: 1)", "Fan")

    context.source.inventory_file_content = {"inventory": {"fan": []}}
    context.source.update_fan()

    assert fan.data["custom_fields"]["health"] == "Absent"
    assert fan.source is context.source, "the run must claim the item, or it is tagged orphaned"

    context.inventory.tag_all_the_things(make_netbox_handler())
    assert NetBoxHandler.orphaned_tag not in fan.get_tags()


def test_components_already_absent_are_claimed_again_on_every_run(check_redfish_source):
    """An item already at absent must still be claimed, or it is tagged orphaned."""

    context = check_redfish_source()
    context.inventory.source_list.append(context.source)
    fan = existing_item(context.source, context.device, "Fan 1 (ID: 1)", "Fan", health="Absent")

    context.source.inventory_file_content = {"inventory": {"fan": []}}
    context.source.update_fan()

    assert fan.source is context.source, "an already absent item must still be claimed by the run"

    context.inventory.tag_all_the_things(make_netbox_handler())
    assert NetBoxHandler.orphaned_tag not in fan.get_tags()


def test_components_of_another_type_are_left_alone(check_redfish_source):
    """An empty fan batch says nothing about the CPUs, which must not be marked absent."""

    context = check_redfish_source()
    context.inventory.source_list.append(context.source)
    cpu = existing_item(context.source, context.device, "Socket 1", "CPU")

    context.source.inventory_file_content = {"inventory": {"fan": []}}
    context.source.update_fan()

    assert cpu.data["custom_fields"]["health"] == "OK"


def test_reported_components_are_still_updated_normally(check_redfish_source):
    """The empty batch handling must not disturb the ordinary path."""

    context = check_redfish_source()

    context.source.inventory_file_content = {"inventory": {"fan": [
        {"name": "Fan 1", "id": "1", "health_status": "OK", "operation_status": "Enabled",
         "physical_context": "CPU", "reading": 4200, "reading_unit": "RPM"}]}}
    context.source.update_fan()

    items = context.inventory.get_all_items(NBInventoryItem)
    assert len(items) == 1
    assert items[0].data["custom_fields"]["health"] == "OK"
    assert items[0].data["custom_fields"]["inventory_type"] == "Fan"
