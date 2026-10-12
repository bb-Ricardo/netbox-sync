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
is a guess. With 'overwrite_host_interface_type = False' a physical type already in NetBox
(i.e. an SFP+ port set by the check_redfish source, running at 1G or without link) is kept.
Otherwise both sources overwrite each other's type. By default vCenter still sets the type.
"""
import pytest

from module.netbox.object_classes import NBDevice, NBInterface
from module.sources import instantiate_sources

KEEP_TYPE = "\noverwrite_host_interface_type = False\n"


def first_sync(vcsim, inventory, load_config, settings):
    load_config(settings)
    first_run = instantiate_sources()[0]
    inventory.resolve_relations()
    first_run.apply()

    interfaces = [i for i in inventory.get_all_items(NBInterface)
                  if i.data.get("type") not in [None, "virtual", "other"]]
    if not interfaces:
        pytest.skip(f"vcsim dump '{vcsim.name}' has no physical host interfaces")

    # the second run finds the objects of the first one in NetBox
    for nb_id, this_object in enumerate(inventory.get_all_items(NBInterface) + inventory.get_all_items(NBDevice),
                                        start=1):
        this_object.nb_id = nb_id
        this_object.is_new = False
        this_object.updated_items = list()
        this_object.unset_items = list()

    return {interface: interface.data.get("type") for interface in interfaces}


def second_sync(inventory, load_config, settings, nic_type):
    for interface in inventory.get_all_items(NBInterface):
        if interface.data.get("type") not in [None, "virtual"]:
            # the shape NetBox hands back for a choice field
            interface.data["type"] = {"value": nic_type, "label": nic_type}

    inventory.source_list = []
    load_config(settings)
    second_run = instantiate_sources()[0]
    inventory.resolve_relations()
    second_run.apply()


def type_value(interface):
    nic_type = interface.data.get("type")
    return nic_type.get("value") if isinstance(nic_type, dict) else nic_type


def test_physical_type_is_kept(vcsim, inventory, load_config, vmware_settings):
    discovered = first_sync(vcsim, inventory, load_config, vmware_settings + KEEP_TYPE)

    second_sync(inventory, load_config, vmware_settings + KEEP_TYPE, "10gbase-x-sfpp")

    for interface in discovered:
        assert type_value(interface) == "10gbase-x-sfpp", \
            f"type of '{interface.get_display_name()}' was overwritten from the link speed"


def test_type_other_is_still_updated(vcsim, inventory, load_config, vmware_settings):
    discovered = first_sync(vcsim, inventory, load_config, vmware_settings + KEEP_TYPE)

    second_sync(inventory, load_config, vmware_settings + KEEP_TYPE, "other")

    for interface, discovered_type in discovered.items():
        assert type_value(interface) == discovered_type, \
            f"type 'other' of '{interface.get_display_name()}' was not updated"


def test_type_is_overwritten_by_default(vcsim, inventory, load_config, vmware_settings):
    """vCenter-only setups still get a wrong type corrected"""
    discovered = first_sync(vcsim, inventory, load_config, vmware_settings)

    second_sync(inventory, load_config, vmware_settings, "400gbase-x-qsfpdd")

    for interface, discovered_type in discovered.items():
        if discovered_type == "400gbase-x-qsfpdd":
            continue
        assert type_value(interface) == discovered_type, \
            f"type of '{interface.get_display_name()}' was not set from the link speed"
