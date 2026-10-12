# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""Interface names longer than the NetBox limit have to match the name NetBox stored.

NetBox keeps interface names cut to 64 characters. A Lenovo XCC port is named after the
manager model and its Redfish id, which together are longer than that. Comparing the full
discovered name never matched the stored one, so the port was created again on every run
and NetBox rejected it with 'Interface with this Device and Name already exists.'
"""

from module.netbox.object_classes import NBInterface

XCC_PORT_NAME = "Lenovo XClarity Controller 2 (1:NIC:672cab626d845d56d397509738c8b03900c7d706)"


def xcc_port():
    return {XCC_PORT_NAME: {"name": XCC_PORT_NAME, "type": "other", "mgmt_only": True}}


def test_long_name_matches_the_stored_interface(check_redfish_source):

    context = check_redfish_source()
    stored = context.inventory.add_object(
        NBInterface, data={"name": XCC_PORT_NAME, "device": context.device, "type": "other"},
        source=context.source)
    assert stored.data.get("name") != XCC_PORT_NAME, "name was not cut to the NetBox limit"

    data = context.source.map_object_interfaces_to_current_interfaces(context.device, xcc_port(), True)

    assert data[XCC_PORT_NAME] is stored


def test_long_name_without_stored_interface_stays_unmatched(check_redfish_source):

    context = check_redfish_source()

    data = context.source.map_object_interfaces_to_current_interfaces(context.device, xcc_port(), True)

    assert data[XCC_PORT_NAME] is None
