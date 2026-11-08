# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
add_update_interface() looks up the existing IP address objects for every address a
source reports. SourceBase.ip_address_objects_for() answers that from an index by
address instead of scanning every IP in the inventory (tens of thousands where NetBox
is fed from an IPAM). It must return what the scan returned, in the same order, and
see objects added to the inventory after the index was built.
"""
from module.netbox.object_classes import NBIPAddress
from module.sources.common.source_base import SourceBase


def _source(inventory):
    src = SourceBase()
    src.inventory = inventory
    return src


def _add(inventory, address):
    return inventory.add_object(NBIPAddress, data={"address": address}, read_from_netbox=True)


def _scan(inventory, address):
    # what add_update_interface() did before the index
    return [ip for ip in inventory.get_all_items(NBIPAddress)
            if ip.data.get("address", "").startswith(f"{address}/")]


def test_lookup_matches_a_full_scan_in_inventory_order(inventory):
    for address in ["10.0.0.5/24", "10.0.0.50/24", "10.0.0.5/32", "2001:db8::5/64", "10.0.0.6/24"]:
        _add(inventory, address)
    src = _source(inventory)

    for address in ["10.0.0.5", "10.0.0.50", "10.0.0.6", "2001:db8::5", "10.0.0.7"]:
        assert src.ip_address_objects_for(address) == _scan(inventory, address), address


def test_lookup_sees_objects_added_after_the_first_call(inventory):
    first = _add(inventory, "10.0.0.5/24")
    src = _source(inventory)
    assert src.ip_address_objects_for("10.0.0.5") == [first]

    second = _add(inventory, "10.0.0.5/32")
    _add(inventory, "10.0.0.9/24")

    assert src.ip_address_objects_for("10.0.0.5") == [first, second]
    assert len(src.ip_address_objects_for("10.0.0.9")) == 1
