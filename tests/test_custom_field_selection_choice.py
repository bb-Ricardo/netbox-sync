# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Tests for selection / multi-selection custom fields on write (issue #598).

The NetBox REST API returns a selection custom field on *read* as a
``{"value": ..., "label": ...}`` dict (and a multi-selection as a list of such
dicts), but on *write* it expects the bare choice value (a list of bare values
for multi-selection). netbox-sync merges the current NetBox custom_fields into
every update so that unmanaged fields are not dropped; before issue #598 that
merge carried the dict form straight back into the PATCH body, which NetBox
rejected as an invalid choice:

    Invalid value for custom field 'Maintenance_window':
    Invalid choice ({'value': 'Business hours', 'label': 'Business Hours'})

These tests drive NBVM.update() directly against the in-memory inventory, so no
NetBox instance or source system is needed.
"""

import pytest

from module.netbox.object_classes import NBCustomField, NBCluster, NBClusterType, NBVM


def register_custom_field(inventory, name, field_type):
    """A custom field as read back from the NetBox API (type reported as a dict)."""
    return inventory.add_object(NBCustomField, data={
        "id": 1,
        "name": name,
        "type": {"value": field_type, "label": field_type.capitalize()},
        "object_types": [NBVM.object_type],
    }, read_from_netbox=True)


def register_custom_field_named(inventory, name, field_type, oid):
    return inventory.add_object(NBCustomField, data={
        "id": oid,
        "name": name,
        "type": {"value": field_type, "label": field_type.capitalize()},
        "object_types": [NBVM.object_type],
    }, read_from_netbox=True)


def existing_vm(inventory, custom_fields):
    """A VM present in NetBox carrying the given custom_fields (API read form)."""
    cluster_type = inventory.add_object(NBClusterType, data={"id": 1, "name": "vmware"},
                                        read_from_netbox=True)
    cluster = inventory.add_object(NBCluster, data={"id": 1, "name": "cluster1", "type": cluster_type},
                                   read_from_netbox=True)
    return inventory.add_object(NBVM, data={
        "id": 1,
        "name": "vm01",
        "cluster": cluster,
        "custom_fields": custom_fields,
    }, read_from_netbox=True)


def test_selection_custom_field_is_written_as_bare_value(inventory):
    """A select field read as {'value','label'} must go back to NetBox as the bare value.

    This is the exact issue #598 shape: the source does NOT manage the selection
    field, but it does manage some other custom field, so the whole custom_fields
    dict is merged and sent; the unmanaged selection field must be reduced to its
    bare value, not echoed back as the {value,label} dict NetBox rejects.
    """
    register_custom_field(inventory, "Maintenance_window", "select")
    register_custom_field_named(inventory, "source_owned", "text", 2)
    vm = existing_vm(inventory, {
        "Maintenance_window": {"value": "Business hours", "label": "Business Hours"},
    })

    # the source manages a different custom field, which triggers the merge that
    # pulls the unmanaged Maintenance_window selection back into the write
    vm.update(data={"custom_fields": {"source_owned": "x"}})

    written = vm.data["custom_fields"]["Maintenance_window"]
    assert written == "Business hours", \
        f"selection custom field must be written as the bare value, got {written!r}"


def test_unchanged_selection_field_does_not_queue_an_update(inventory):
    """Carrying a select field through unchanged must not look like a change every run."""
    register_custom_field(inventory, "Maintenance_window", "select")
    vm = existing_vm(inventory, {
        "Maintenance_window": {"value": "Business hours", "label": "Business Hours"},
    })

    # source sets the very same choice value it would read from vCenter
    vm.update(data={"custom_fields": {"Maintenance_window": "Business hours"}})

    assert "custom_fields" not in vm.updated_items, \
        "an unchanged selection custom field was wrongly queued as a NetBox update"


def test_multiselection_custom_field_is_written_as_list_of_values(inventory):
    """A multiselect field read as a list of dicts must go back as a list of bare values."""
    register_custom_field(inventory, "Environments", "multiselect")
    register_custom_field_named(inventory, "source_owned", "text", 2)
    vm = existing_vm(inventory, {
        "Environments": [
            {"value": "prod", "label": "Production"},
            {"value": "dr", "label": "Disaster Recovery"},
        ],
    })

    vm.update(data={"custom_fields": {"source_owned": "x"}})

    written = vm.data["custom_fields"]["Environments"]
    assert written == ["prod", "dr"], \
        f"multi-selection custom field must be written as a list of bare values, got {written!r}"


def test_changed_selection_field_is_still_updated(inventory):
    """The fix must not suppress a genuine change to a selection custom field."""
    register_custom_field(inventory, "Maintenance_window", "select")
    vm = existing_vm(inventory, {
        "Maintenance_window": {"value": "Business hours", "label": "Business Hours"},
    })

    vm.update(data={"custom_fields": {"Maintenance_window": "After hours"}})

    assert vm.data["custom_fields"]["Maintenance_window"] == "After hours"
    assert "custom_fields" in vm.updated_items, \
        "a real selection custom field change was not queued for NetBox"


@pytest.mark.parametrize("field_type,stored,expected", [
    # a chosen value that is falsy must survive, not be dropped
    ("select", {"value": "", "label": "(blank)"}, ""),
    # an unset selection comes back as None and must stay None
    ("select", None, None),
    # an empty multi-selection must stay an empty list
    ("multiselect", [], []),
    # a multi-selection already partly reduced (mixed dict/scalar) is normalised
    ("multiselect", [{"value": "a", "label": "A"}, "b"], ["a", "b"]),
], ids=["falsy-value", "none", "empty-multiselect", "mixed-multiselect"])
def test_selection_edge_cases_are_reduced_safely(inventory, field_type, stored, expected):
    """Lock in that the bare-value reduction never drops falsy values or crashes."""
    register_custom_field(inventory, "Field", field_type)
    register_custom_field_named(inventory, "source_owned", "text", 2)
    vm = existing_vm(inventory, {"Field": stored})

    vm.update(data={"custom_fields": {"source_owned": "x"}})

    assert vm.data["custom_fields"]["Field"] == expected


def test_unmanaged_selection_field_not_in_inventory_is_still_reduced(inventory):
    """A selection field netbox-sync does not manage (absent from inventory) must
    still be reduced to its bare value, so the {value,label} dict NetBox returned on
    read is not echoed back and rejected (the exact shape reported in issue #598)."""
    # note: Maintenance_window is deliberately NOT registered as a custom field
    register_custom_field_named(inventory, "source_owned", "text", 2)
    vm = existing_vm(inventory, {
        "Maintenance_window": {"value": "Business hours", "label": "Business Hours"},
    })

    vm.update(data={"custom_fields": {"source_owned": "x"}})

    assert vm.data["custom_fields"]["Maintenance_window"] == "Business hours"
