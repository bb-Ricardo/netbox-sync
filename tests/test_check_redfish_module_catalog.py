"""
Modelling components as modules has to read the existing modules back from NetBox,
otherwise every run tries to create them again, and a module type has to be the
hardware model so the catalog is shared instead of holding one entry per component.
"""
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import NBModule, NBModuleBay, NBModuleType
from module.sources.check_redfish.import_inventory import CheckRedfish


def _source(inventory, use_modules):
    source = object.__new__(CheckRedfish)
    source.inventory = inventory
    source.name = "redfish"
    source.source_tag = "Source: redfish"
    source.settings = SimpleNamespace(model_components_as_modules=use_modules)
    source.device_object = None
    return source


@pytest.mark.parametrize("use_modules", [True, False])
def test_module_objects_are_only_requested_when_enabled(inventory, use_modules):
    dependencies = list(CheckRedfish.dependent_netbox_objects)
    if use_modules:
        dependencies += [NBModuleBay, NBModuleType, NBModule]

    for module_class in (NBModuleBay, NBModuleType, NBModule):
        assert (module_class in dependencies) is use_modules, \
            f"{module_class.name} must be read from NetBox exactly when the option is on"


@pytest.mark.parametrize("item_data, expected", [
    ({"model": "ST2000NX0273", "full_name": "Disk.Bay.0 (HDD ST2000NX0273)",
      "inventory_type": "Physical Drive"}, "ST2000NX0273"),
    ({"part_number": "MTA36ASF8G72PZ", "full_name": "DIMM.A1 (DDR4)",
      "inventory_type": "DIMM"}, "MTA36ASF8G72PZ"),
    ({"full_name": "Fan1A (ID: Fan.Embedded.1)", "inventory_type": "Fan"}, "Fan"),
])
def test_module_type_is_the_hardware_model_not_the_instance_name(inventory, item_data, expected):
    source = _source(inventory, True)
    source.device_manufacturer_name = lambda: "Dell"

    module_type = source.resolve_module_type(item_data)

    assert module_type.data.get("model") == expected
