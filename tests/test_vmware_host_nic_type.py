# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
vCenter only knows the current link speed of a host pNIC, so the interface type it derives
is a guess. A physical type already in NetBox which supports that speed (i.e. an SFP+ port
set by the check_redfish source, running at 1G or without link) must be kept. Otherwise both
sources overwrite each other's type on every run.
"""
import pytest

from module.netbox.object_classes import NBDevice, NBInterface, NetBoxInterfaceType
from module.sources import instantiate_sources


def first_sync(vcsim, inventory, load_config, vmware_settings):
    load_config(vmware_settings)
    first_run = instantiate_sources()[0]
    inventory.resolve_relations()
    first_run.apply()

    interfaces = [i for i in inventory.get_all_items(NBInterface)
                  if isinstance(NetBoxInterfaceType.valid_types.get(i.data.get("type")), int)]
    if not interfaces:
        pytest.skip(f"vcsim dump '{vcsim.name}' has no physical host interfaces")

    # the second run finds the objects of the first one in NetBox
    for nb_id, this_object in enumerate(inventory.get_all_items(NBInterface) + inventory.get_all_items(NBDevice),
                                        start=1):
        this_object.nb_id = nb_id
        this_object.is_new = False
        this_object.updated_items = list()
        this_object.unset_items = list()

    return interfaces


def second_sync(inventory, load_config, vmware_settings):
    inventory.source_list = []
    load_config(vmware_settings)
    second_run = instantiate_sources()[0]
    inventory.resolve_relations()
    second_run.apply()


def type_value(interface):
    nic_type = interface.data.get("type")
    return nic_type.get("value") if isinstance(nic_type, dict) else nic_type


def test_faster_physical_type_is_kept(vcsim, inventory, load_config, vmware_settings):
    interfaces = first_sync(vcsim, inventory, load_config, vmware_settings)
    for interface in interfaces:
        # the shape NetBox hands back for a choice field
        interface.data["type"] = {"value": "400gbase-x-qsfpdd", "label": "QSFP-DD (400GE)"}

    second_sync(inventory, load_config, vmware_settings)

    for interface in interfaces:
        assert type_value(interface) == "400gbase-x-qsfpdd", \
            f"type of '{interface.get_display_name()}' was overwritten from the link speed"


def test_slower_physical_type_is_updated(vcsim, inventory, load_config, vmware_settings):
    interfaces = first_sync(vcsim, inventory, load_config, vmware_settings)
    discovered = {interface: interface.data.get("type") for interface in interfaces}
    for interface in interfaces:
        interface.data["type"] = {"value": "100base-tx", "label": "100BASE-TX (10/100ME)"}

    second_sync(inventory, load_config, vmware_settings)

    for interface, discovered_type in discovered.items():
        if discovered_type == "100base-tx":
            continue
        assert type_value(interface) == discovered_type, \
            f"type of '{interface.get_display_name()}' was not updated to the link speed"
