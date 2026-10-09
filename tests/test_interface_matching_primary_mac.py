# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""Interface matching by MAC must use the current primary MAC address object (NetBox >= 4.2).

The legacy interface 'mac_address' field is only what NetBox returned at read time. Once a
previous source moved the primary MAC to another address, matching on the stale value merged
a BMC port (i.e. Lenovo XCC) into the ESXi vmk0 interface on every run.
"""

from module.netbox.object_classes import NBInterface, NBMACAddress

BMC_MAC = "38:68:DD:34:33:C5"
VMK_MAC = "00:50:56:67:39:DA"


def seed_vmk0(context, legacy_mac, primary_mac):
    interface = context.inventory.add_object(
        NBInterface, data={"name": "vmk0", "device": context.device, "type": "virtual", "mac_address": legacy_mac},
        source=context.source)
    mac_object = context.inventory.add_object(
        NBMACAddress, data={"mac_address": primary_mac, "assigned_object_id": interface,
                            "assigned_object_type": NBInterface}, source=context.source)
    interface.update(data={"primary_mac_address": mac_object}, source=context.source)
    return interface


def bmc_port():
    return {"XCC (1:NIC:abc)": {"name": "XCC (1:NIC:abc)", "mac_address": BMC_MAC, "type": "other",
                                "mgmt_only": True}}


def test_bmc_port_ignores_stale_legacy_mac(check_redfish_source):

    context = check_redfish_source()
    seed_vmk0(context, legacy_mac=BMC_MAC, primary_mac=VMK_MAC)

    data = context.source.map_object_interfaces_to_current_interfaces(context.device, bmc_port(), True)

    assert data["XCC (1:NIC:abc)"] is None


def test_bmc_port_still_matches_by_primary_mac(check_redfish_source):

    context = check_redfish_source()
    vmk0 = seed_vmk0(context, legacy_mac=VMK_MAC, primary_mac=BMC_MAC)

    data = context.source.map_object_interfaces_to_current_interfaces(context.device, bmc_port(), True)

    assert data["XCC (1:NIC:abc)"] is vmk0
