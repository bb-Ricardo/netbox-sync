# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
A run reads only the object types its sources work with. An object can still point at a
type the run did not read, e.g. the module of an interface in a VMware only setup. Such a
relation stays as NetBox returned it: it is not an error and the sync must not change it.
"""
import logging

import pytest

from module.netbox.object_classes import NBDevice, NBInterface, NBMACAddress, NBModule, NBPowerPort
from module.sources import instantiate_sources

# how NetBox returns the module of a component
NETBOX_MODULE = {"id": 5, "display": "Embedded LOM"}


def relation_errors(caplog):
    return [record.getMessage() for record in caplog.records
            if record.levelno == logging.ERROR and "Problems resolving relation" in record.getMessage()]


@pytest.mark.parametrize("component_type", [NBInterface, NBPowerPort])
def test_relation_to_a_type_the_run_did_not_read_is_kept(inventory, caplog, component_type):
    caplog.set_level(logging.ERROR, logger="NetBox-Sync")
    inventory.load_from_netbox(NBDevice, [{"id": 1, "name": "esx01"}])
    inventory.load_from_netbox(component_type, [{"id": 10, "name": "port0", "device": {"id": 1},
                                                 "module": dict(NETBOX_MODULE)}])

    inventory.resolve_relations()

    component = inventory.get_by_id(component_type, nb_id=10)
    assert relation_errors(caplog) == []
    assert component.data["device"] is inventory.get_by_id(NBDevice, nb_id=1)
    assert component.data["module"] == NETBOX_MODULE


def test_relation_to_an_object_the_run_created_is_resolved(inventory, caplog):
    caplog.set_level(logging.ERROR, logger="NetBox-Sync")
    # a source can create objects of a type it did not read, NetBox then returns them as a reference
    mac_address = inventory.add_object(NBMACAddress, data={"mac_address": "00:11:22:33:44:55"})
    mac_address.nb_id = 7
    inventory.load_from_netbox(NBDevice, [{"id": 1, "name": "esx01"}])
    inventory.load_from_netbox(NBInterface, [{"id": 10, "name": "port0", "device": {"id": 1},
                                              "primary_mac_address": {"id": 7}}])

    inventory.resolve_relations()

    assert relation_errors(caplog) == []
    assert inventory.get_by_id(NBInterface, nb_id=10).data["primary_mac_address"] is mac_address


def test_relation_to_a_missing_object_of_a_read_type_is_an_error(inventory, caplog):
    caplog.set_level(logging.ERROR, logger="NetBox-Sync")
    inventory.load_from_netbox(NBDevice, [{"id": 1, "name": "esx01"}])
    inventory.load_from_netbox(NBModule, [])
    inventory.load_from_netbox(NBInterface, [{"id": 10, "name": "port0", "device": {"id": 1},
                                              "module": dict(NETBOX_MODULE)}])

    inventory.resolve_relations()

    assert relation_errors(caplog) == [
        f"Problems resolving relation 'module' for object 'port0 (esx01)' and value '{NETBOX_MODULE}'"]


def test_vmware_sync_keeps_the_module_of_host_interfaces(vcsim, inventory, load_config, vmware_settings, caplog):
    load_config(vmware_settings)
    first_run = instantiate_sources()[0]
    inventory.resolve_relations()
    first_run.apply()

    # the second run finds the objects of the first one in NetBox, the host interfaces with a module
    host_interfaces = [i for i in inventory.get_all_items(NBInterface) if "virtual" not in str(i.data.get("type"))]
    if not host_interfaces:
        pytest.skip(f"vcsim dump '{vcsim.name}' has no physical host interfaces")
    for nb_id, this_object in enumerate(inventory.get_all_items(NBInterface) + inventory.get_all_items(NBDevice),
                                        start=1):
        this_object.nb_id = nb_id
        this_object.is_new = False
        this_object.updated_items = list()
        this_object.unset_items = list()
    for interface in host_interfaces:
        interface.data["module"] = dict(NETBOX_MODULE)

    caplog.set_level(logging.ERROR, logger="NetBox-Sync")
    inventory.source_list = []
    load_config(vmware_settings)
    second_run = instantiate_sources()[0]
    inventory.resolve_relations()
    second_run.apply()

    assert relation_errors(caplog) == []
    for interface in host_interfaces:
        assert interface.data["module"] == NETBOX_MODULE, interface.get_display_name()
        assert "module" not in interface.updated_items + interface.unset_items, interface.get_display_name()
