"""Integration tests for the check_redfish source and the inventory items it maintains.

Drives the real CheckRedfish methods against the real NetBoxInventory and NetBoxObject
classes. Only the NetBox REST API itself is out of scope.
"""

from module.netbox.object_classes import NBInventoryItem

# a Dell `location` as check_redfish can hand it back: a nested Oem object, not a string
DELL_LOCATION = {
    "Oem": {
        "Dell": {
            "@odata.type": "#DellLocation.v1_2_0.DellLocation",
            "Locator": "BP_PSV 0:1",
        }
    }
}


def enclosure(name, location, serial="ENC-AAA"):
    return {"inventory": {"storage_enclosure": [
        {"name": name, "model": "BP14G+EXP", "location": location,
         "manufacturer": "DELL", "serial": serial, "part_number": "PN-ENC",
         "firmware": "1.0", "health_status": "OK", "num_bays": 24,
         "operation_status": "Enabled"}]}}


def test_structured_location_is_not_stringified_into_the_item_name(check_redfish_source):
    """A structured location must not reach the name as its Python repr."""

    context = check_redfish_source()

    context.source.inventory_file_content = enclosure("BP_PSV 0:1", DELL_LOCATION)
    context.source.update_storage_enclosure()

    items = context.inventory.get_all_items(NBInventoryItem)
    assert len(items) == 1

    name = items[0].data["name"]
    assert "Oem" not in name
    assert "@odata.type" not in name
    assert "{" not in name
    assert len(name) <= 64
    assert name == "BP_PSV 0:1"


def test_plain_string_location_is_kept_in_the_item_name(check_redfish_source):
    """A location that really is a string is still used."""

    context = check_redfish_source()

    context.source.inventory_file_content = enclosure("BP_PSV 0:1", "Slot 3")
    context.source.update_storage_enclosure()

    items = context.inventory.get_all_items(NBInventoryItem)
    assert len(items) == 1
    assert items[0].data["name"] == "BP_PSV 0:1 Slot 3"


def test_two_enclosures_with_structured_locations_stay_distinct(check_redfish_source):
    """Dropping the unusable location must not merge two enclosures onto one name."""

    context = check_redfish_source()

    context.source.inventory_file_content = {"inventory": {"storage_enclosure": [
        enclosure("BP_PSV 0:1", DELL_LOCATION, "ENC-AAA")["inventory"]["storage_enclosure"][0],
        enclosure("BP_PSV 0:2", DELL_LOCATION, "ENC-BBB")["inventory"]["storage_enclosure"][0],
    ]}}
    context.source.update_storage_enclosure()

    names = sorted(item.data["name"] for item in context.inventory.get_all_items(NBInventoryItem))
    assert names == ["BP_PSV 0:1", "BP_PSV 0:2"]
