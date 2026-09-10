"""
A fan's reading is a live measurement. Syncing it means NetBox records a change on
every run, for every fan of every server (reported by @marcinpsk on #473).
"""
from module.sources.check_redfish.import_inventory import CheckRedfish


def _fan(reading):
    return {"inventory": {"fan": [{
        "id": "Fan.Embedded.1", "name": "Fan1A", "health_status": "OK",
        "physical_context": "SystemBoard", "reading": reading, "reading_unit": "RPM",
    }]}}


def _collect(inventory, reading):
    source = object.__new__(CheckRedfish)
    source.inventory = inventory
    source.name = "redfish"
    source.source_tag = "Source: redfish"
    source.inventory_file_content = _fan(reading)
    collected = []
    source.update_all_items = lambda items, inventory_type: collected.extend(items)
    source.update_fan()
    return collected


def test_a_spinning_fan_produces_the_same_item(inventory):
    slow = _collect(inventory, 7015)
    fast = _collect(inventory, 9120)

    assert slow == fast, "the fan item changes with its rpm, so NetBox is written on every run"


def test_the_fan_is_still_described(inventory):
    # control: dropping the reading must not empty the item
    item = _collect(inventory, 8280)[0]

    assert item["full_name"] == "Fan1A (ID: Fan.Embedded.1)"
    assert item["health"] == "OK"
    assert "Context: SystemBoard" in item["description"]
