"""
Modelling components as modules has to read the existing modules back from NetBox,
otherwise every run tries to create them again, and a module type has to be the
hardware model so the catalog is shared instead of holding one entry per component.

Modules are read back on every run regardless of the option, so interfaces and power
ports that reference a module can always resolve that relation - even on a run where the
option is off again.
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


def test_module_objects_are_always_requested_so_relations_resolve():
    # Modules must be read back on every run, even with the option off. Interfaces and power
    # ports created by an earlier modules-on run reference a module, and that relation only
    # resolves when the module objects were loaded. Against a live NetBox an option-off run
    # otherwise logs "Problems resolving relation 'module'" for every such object.
    for module_class in (NBModuleBay, NBModuleType, NBModule):
        assert module_class in CheckRedfish.dependent_netbox_objects, \
            f"{module_class.name} must always be read from NetBox so module relations resolve"


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
