# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""Removing the interface association of a MAC address must unset it as primary MAC of the interface.

NetBox (>= 4.2) refuses to unassign a MAC address which is still the primary MAC of its interface.
"""

from module.netbox.object_classes import NBInterface, NBMACAddress


def test_remove_interface_association_unsets_primary_mac_of_the_interface(check_redfish_source):

    context = check_redfish_source()
    interface = context.inventory.add_object(
        NBInterface, data={"name": "vmk0", "device": context.device, "type": "virtual"}, source=context.source)
    mac_object = context.inventory.add_object(
        NBMACAddress, data={"mac_address": "00:50:56:67:39:DA", "assigned_object_id": interface,
                            "assigned_object_type": NBInterface}, source=context.source)
    interface.update(data={"primary_mac_address": mac_object}, source=context.source)

    mac_object.remove_interface_association()

    assert "primary_mac_address" in interface.unset_items
    assert "assigned_object_id" in mac_object.unset_items
