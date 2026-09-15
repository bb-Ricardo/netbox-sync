"""Modeling check_redfish hardware components as NetBox modules.

Drives the real CheckRedfish methods against the real NetBoxInventory and NetBoxObject
classes. Only the NetBox REST API itself is out of scope.
"""

import pytest

from module.common.misc import grab
from module.netbox.object_classes import (
    NBDevice,
    NBDeviceType,
    NBInterface,
    NBInventoryItem,
    NBManufacturer,
    NBModule,
    NBModuleBay,
    NBModuleType,
    NBPowerPort,
)


@pytest.fixture
def modules_source(check_redfish_source):
    """The shared check_redfish fixture, with the modules option and a NetBox version to test."""
    def _make(model_components_as_modules: bool, netbox_api_version: str, **extra: object):
        context = check_redfish_source(
            model_components_as_modules=model_components_as_modules, **extra)
        context.inventory.netbox_api_version = netbox_api_version
        return context.source, context.inventory, context.device
    return _make


def cpu_item(bay_name="Socket 1",
             model="Intel Xeon Gold 6248R",
             serial="CPU-AAA",
             manufacturer="Intel",
             health="OK",
             full_name=None):
    """Build a normalized CPU component item as produced by CheckRedfish.update_proc().

    bay_name is the stable physical slot (the module bay identity); full_name is the
    display name and by default embeds the model, exactly like the real parser does.
    """
    return {
        "description": ["x86-64", "Cores: 24", "Threads: 48"],
        "manufacturer": manufacturer,
        "bay_name": bay_name,
        "full_name": full_name if full_name is not None else f"{bay_name} ({model})",
        "model": model,
        "serial": serial,
        "health": health,
        "size": "24/48",
        "speed": "3.0GHz",
    }


@pytest.mark.parametrize("flag, api_version, expected", [
    (True, "4.3.0", True),
    (True, "4.3.1", True),
    (True, "5.0.0", True),
    (True, "4.2.9", False),   # NetBox too old -> fall back to inventory items
    (True, "4.0.0", False),
    (False, "4.3.0", False),  # feature disabled -> inventory items
    (False, "5.0.0", False),
])
def test_use_modules_decision_matrix(modules_source, flag, api_version, expected):
    source, _, _ = modules_source(flag, api_version)
    assert source.use_modules() is expected


def test_creates_full_module_graph_for_cpu(modules_source):
    source, inventory, device = modules_source(True, "4.3.0")

    source.update_all_items([cpu_item()], "CPU")

    modules = inventory.get_all_items(NBModule)
    bays = inventory.get_all_items(NBModuleBay)
    module_types = inventory.get_all_items(NBModuleType)

    # exactly one of each object is created and no deprecated inventory item is touched
    assert len(modules) == 1
    assert len(bays) == 1
    assert len(module_types) == 1
    assert len(inventory.get_all_items(NBInventoryItem)) == 0

    module = modules[0]
    bay = bays[0]
    module_type = module_types[0]

    # the module is wired to the device, its bay and its module type (same object instances)
    assert module.data["device"] is device
    assert module.data["module_bay"] is bay
    assert module.data["module_type"] is module_type
    assert module.data["status"] == "active"
    assert module.data["serial"] == "CPU-AAA"

    # descriptive data lives in custom fields on the module
    assert grab(module, "data.custom_fields.inventory_type") == "CPU"
    assert grab(module, "data.custom_fields.inventory_size") == "24/48"
    assert grab(module, "data.custom_fields.inventory_speed") == "3.0GHz"
    assert grab(module, "data.custom_fields.health") == "OK"

    # the bay is the stable physical slot (model lives in the module type, not the bay name)
    assert bay.data["name"] == "Socket 1"
    assert bay.data["device"] is device

    # the module type is the catalog entry carrying the real CPU model + manufacturer
    assert module_type.data["model"] == "Intel Xeon Gold 6248R"
    assert grab(module_type, "data.manufacturer.data.name") == "Intel"

    # the module derives its display name from the bay (it has no name of its own)
    assert module.get_display_name(including_second_key=True) == "Socket 1 (server01)"


def test_module_sync_is_idempotent(modules_source):
    source, inventory, _ = modules_source(True, "4.3.0")

    source.update_all_items([cpu_item()], "CPU")
    source.update_all_items([cpu_item()], "CPU")

    # a second run with identical data must not create duplicates
    assert len(inventory.get_all_items(NBModule)) == 1
    assert len(inventory.get_all_items(NBModuleBay)) == 1
    assert len(inventory.get_all_items(NBModuleType)) == 1


def test_same_model_reuses_module_type_across_devices(modules_source):
    source, inventory, _ = modules_source(True, "4.3.0")

    # first device gets a CPU
    source.update_all_items([cpu_item(serial="CPU-AAA")], "CPU")

    # a second device with the exact same CPU model
    device2 = inventory.add_object(NBDevice, data={"name": "server02"}, source=source)
    source.device_object = device2
    source.update_all_items([cpu_item(serial="CPU-BBB")], "CPU")

    # the module type (catalog entry) is shared, but each device gets its own bay + module
    assert len(inventory.get_all_items(NBModuleType)) == 1
    assert len(inventory.get_all_items(NBModule)) == 2
    assert len(inventory.get_all_items(NBModuleBay)) == 2
    assert len(inventory.get_all_items(NBManufacturer)) == 1


def test_different_model_creates_distinct_module_type(modules_source):
    """This is the 'one server type, different CPUs' use case."""
    source, inventory, _ = modules_source(True, "4.3.0")

    source.update_all_items([cpu_item(model="Intel Xeon Gold 6248R", serial="CPU-AAA")], "CPU")

    device2 = inventory.add_object(NBDevice, data={"name": "server02"}, source=source)
    source.device_object = device2
    source.update_all_items([cpu_item(model="Intel Xeon Gold 5318Y", serial="CPU-BBB")], "CPU")

    models = sorted(grab(mt, "data.model") for mt in inventory.get_all_items(NBModuleType))
    assert models == ["Intel Xeon Gold 5318Y", "Intel Xeon Gold 6248R"]
    assert len(inventory.get_all_items(NBModule)) == 2


def test_same_bay_new_model_updates_module_type(modules_source):
    """Same device + same physical bay + a replaced CPU model across runs.

    Regression for CodeRabbit PR #1: the bay must be keyed on a stable slot (not the
    model-bearing display name), so a model swap reuses the same bay and the module
    re-points to the new module type instead of churning the bay or keeping a stale type.
    """
    source, inventory, _ = modules_source(True, "4.3.0")

    # first run: CPU model A installed in socket "Socket 1"
    source.update_all_items([cpu_item(bay_name="Socket 1",
                                      model="Intel Xeon Gold 6248R", serial="CPU-AAA")], "CPU")

    # second run: same device, same socket, a different CPU model is now installed
    source.update_all_items([cpu_item(bay_name="Socket 1",
                                      model="Intel Xeon Gold 5318Y", serial="CPU-BBB")], "CPU")

    bays = inventory.get_all_items(NBModuleBay)
    modules = inventory.get_all_items(NBModule)

    # the physical bay is stable: still a single bay holding a single module on the device
    assert len(bays) == 1
    assert len(modules) == 1
    assert bays[0].data["name"] == "Socket 1"

    module = modules[0]
    # the module re-points to the replaced part's module type (no stale catalog reference)
    assert grab(module, "data.module_type.data.model") == "Intel Xeon Gold 5318Y"
    assert module.data["serial"] == "CPU-BBB"


def test_missing_component_marks_module_health_absent(modules_source):
    source, inventory, _ = modules_source(True, "4.3.0")

    cpu1 = cpu_item(bay_name="Socket 1", serial="CPU-AAA")
    cpu2 = cpu_item(bay_name="Socket 2", serial="CPU-BBB")
    source.update_all_items([cpu1, cpu2], "CPU")

    assert len(inventory.get_all_items(NBModule)) == 2

    # second CPU disappears from the inventory file
    source.update_all_items([cpu1], "CPU")

    modules_by_bay = {
        grab(m, "data.module_bay.data.name"): m for m in inventory.get_all_items(NBModule)
    }
    assert grab(modules_by_bay["Socket 1"], "data.custom_fields.health") == "OK"
    assert grab(modules_by_bay["Socket 2"], "data.custom_fields.health") == "Absent"


def test_mixed_bay_transition_does_not_remap_modules(modules_source):
    """One bay disappears while a different new bay appears in the same sync. Because the module
    bay is the authoritative physical slot (and update_module never moves a module between bays),
    the removed bay must go Absent and the new bay must get its own module - the new component
    must NOT be silently remapped onto the removed slot."""
    source, inventory, _ = modules_source(True, "4.3.0")

    source.update_all_items([
        cpu_item(bay_name="Socket 1", serial="CPU-AAA"),
        cpu_item(bay_name="Socket 2", serial="CPU-BBB"),
    ], "CPU")
    assert len(inventory.get_all_items(NBModule)) == 2

    # Socket 2 is removed and a brand new Socket 3 appears in the same run
    source.update_all_items([
        cpu_item(bay_name="Socket 1", serial="CPU-AAA"),
        cpu_item(bay_name="Socket 3", serial="CPU-CCC"),
    ], "CPU")

    modules_by_bay = {
        grab(m, "data.module_bay.data.name"): m for m in inventory.get_all_items(NBModule)
    }
    # three distinct bays now exist: the kept one, the removed one (Absent), and the new one
    assert set(modules_by_bay) == {"Socket 1", "Socket 2", "Socket 3"}
    assert grab(modules_by_bay["Socket 1"], "data.custom_fields.health") == "OK"
    # the removed slot is marked Absent and keeps its own component data (not overwritten)
    assert grab(modules_by_bay["Socket 2"], "data.custom_fields.health") == "Absent"
    assert grab(modules_by_bay["Socket 2"], "data.serial") == "CPU-BBB"
    # the new slot is its own active module carrying the new component's data
    assert grab(modules_by_bay["Socket 3"], "data.serial") == "CPU-CCC"
    assert grab(modules_by_bay["Socket 3"], "data.custom_fields.health") == "OK"


def fan_item(bay_name="System Board Fan1 (ID: 0.56)", health="OK"):
    """A component that reports no manufacturer (fans, enclosures, PCIe extenders, ...)."""
    return {
        "description": ["Context: SystemBoard"],
        "full_name": bay_name,
        "health": health,
        "speed": "9240RPM",
    }


def test_component_without_manufacturer_uses_device_manufacturer(modules_source):
    """NetBox requires a manufacturer on a module type. Components that report none (fans,
    storage enclosures, PCIe extenders) must still get one, otherwise the module-type POST
    fails with 'manufacturer required' and the whole module create cascade fails."""
    source, inventory, device = modules_source(True, "4.3.0")

    # give the device a manufacturer via its device type, like a real synced device has
    manufacturer = inventory.add_object(NBManufacturer, data={"name": "Acme"}, source=source)
    device_type = inventory.add_object(
        NBDeviceType, data={"model": "PowerEdge R650", "manufacturer": manufacturer}, source=source)
    device.update(data={"device_type": device_type}, source=source)

    source.update_all_items([fan_item()], "Fan")

    module_types = inventory.get_all_items(NBModuleType)
    assert len(module_types) == 1
    assert len(inventory.get_all_items(NBModule)) == 1

    # the (required) manufacturer is populated from the device's vendor
    device_manufacturer = grab(device, "data.device_type.data.manufacturer.data.name")
    assert grab(module_types[0], "data.manufacturer.data.name") == device_manufacturer


def test_component_without_manufacturer_falls_back_to_unknown(modules_source):
    """When neither the component nor the device exposes a manufacturer, fall back to a
    placeholder so the required module type field is always populated."""
    source, inventory, _ = modules_source(True, "4.3.0")  # device has no device type / manufacturer

    source.update_all_items([fan_item()], "Fan")

    module_types = inventory.get_all_items(NBModuleType)
    assert len(module_types) == 1
    assert grab(module_types[0], "data.manufacturer.data.name") == "Unknown"
    assert len(inventory.get_all_items(NBModule)) == 1


def test_existing_module_type_manufacturer_is_preserved(modules_source):
    """If a module type for this model already exists in NetBox with a manufacturer (set by a
    previous sync or curated by hand), a later sync of a component that reports no manufacturer
    must reuse it, not overwrite it with the device-vendor / 'Unknown' fallback."""
    source, inventory, _ = modules_source(True, "4.3.0")

    # a module type for this model already exists in NetBox, manufacturer "Globex"
    globex = inventory.add_object(NBManufacturer, data={"name": "Globex"}, source=source)
    inventory.add_object(
        NBModuleType, data={"model": "PCIe Extender", "manufacturer": globex}, source=source)

    # the PCIe extender reports no manufacturer
    source.update_all_items([{
        "full_name": "PCIe Extender",
        "model": "PCIe Extender",
        "health": "OK",
        "description": ["LDs: 1, PDs: 1"],
    }], "Storage Controller")

    module_types = [mt for mt in inventory.get_all_items(NBModuleType)
                    if grab(mt, "data.model") == "PCIe Extender"]
    assert len(module_types) == 1
    # the pre-existing manufacturer is preserved, not clobbered by the fallback
    assert grab(module_types[0], "data.manufacturer.data.name") == "Globex"


def test_nic_and_bmc_interfaces_are_attached_to_their_modules(modules_source):
    """NIC port interfaces are attached to their adapter's module and the BMC interface to the
    manager module, so NetBox cascade-deletes them when the module is removed (module FK)."""
    source, inventory, _ = modules_source(True, "4.3.0")
    source.interface_adapter_type_dict = {}
    source.nic_module_bay_by_adapter_id = {}
    source.manager_name = None
    source.settings.overwrite_interface_name = False
    source.settings.overwrite_interface_attributes = False
    source.settings.permitted_subnets = None  # ports carry no IPs, so this is never dereferenced
    source.settings.ip_tenant_inheritance_order = []

    source.inventory_file_content = {
        "inventory": {
            "manager": [
                {"name": "iDRAC 9", "model": None, "licenses": [], "firmware": "7.0",
                 "health_status": "OK"}
            ],
            "network_adapter": [
                {"id": "NIC.Slot.1", "name": "NIC.Slot.1", "model": "BCM57414",
                 "manufacturer": "Broadcom", "operation_status": "Enabled", "num_ports": "2",
                 "serial": "NIC-AAA", "firmware": "1.0"}
            ],
            "network_port": [
                {"id": "NIC.Slot.1-1", "name": "Slot 1 Port 1", "adapter_id": "NIC.Slot.1",
                 "operation_status": "Enabled", "link_status": "Up", "addresses": [],
                 "capable_speed": 10000, "manager_ids": []},
                {"id": "NIC.1", "name": "iDRAC", "adapter_id": None,
                 "operation_status": "Enabled", "link_status": "Up", "addresses": [],
                 "capable_speed": 1000, "manager_ids": ["iDRAC.Embedded.1"]},
            ],
        }
    }

    source.update_manager()
    source.update_network_adapter()
    source.update_network_interface()

    interfaces = {grab(i, "data.name"): i for i in inventory.get_all_items(NBInterface)}
    nic_interface = interfaces["NIC.Slot.1-1"]
    bmc_interface = interfaces["iDRAC 9 (NIC.1)"]

    # the NIC port belongs to its adapter's module, the BMC port to the manager module
    assert grab(nic_interface, "data.module.data.module_bay.data.name") == "NIC.Slot.1"
    assert grab(bmc_interface, "data.module.data.module_bay.data.name") == "iDRAC 9"

    # and it really is the same module object created for this device
    assert grab(nic_interface, "data.module") is source.find_device_module_by_bay_name("NIC.Slot.1")


def test_nic_port_interface_named_by_stable_redfish_id(modules_source):
    """With modules on, a NIC port is named by its stable redfish id (e.g. NIC.Slot.1-1) rather
    than the long human label prepended to it; the descriptive label moves to the description."""
    source, inventory, _ = modules_source(True, "4.3.0")
    source.interface_adapter_type_dict = {}
    source.nic_module_bay_by_adapter_id = {}
    source.manager_name = None
    source.settings.overwrite_interface_name = False
    source.settings.overwrite_interface_attributes = False
    source.settings.permitted_subnets = None
    source.settings.ip_tenant_inheritance_order = []

    source.inventory_file_content = {
        "inventory": {
            "network_adapter": [
                {"id": "NIC.Integrated.1", "name": "NIC.Integrated.1", "model": "BCM57412",
                 "manufacturer": "Broadcom", "operation_status": "Enabled", "num_ports": "1"}
            ],
            "network_port": [
                {"id": "NIC.Integrated.1-1", "name": "Integrated NIC 1 Port 1 Partition 1",
                 "adapter_id": "NIC.Integrated.1", "operation_status": "Enabled",
                 "link_status": "Up", "addresses": [], "capable_speed": 10000,
                 "manager_ids": []},
            ],
        }
    }

    source.update_network_adapter()
    source.update_network_interface()

    interfaces = {grab(i, "data.name"): i for i in inventory.get_all_items(NBInterface)}

    # the stable id is the name; the description carries the human label, not the name
    assert "NIC.Integrated.1-1" in interfaces
    assert "Integrated NIC 1 Port 1 Partition 1 (NIC.Integrated.1-1)" not in interfaces
    assert grab(interfaces["NIC.Integrated.1-1"], "data.description") == \
        "Integrated NIC 1 Port 1 Partition 1"


def test_nic_module_bay_stable_when_adapter_label_changes(modules_source):
    """The NIC module bay is keyed on the stable adapter id (e.g. NIC.Slot.1), not the mutable
    human label - so a relabeled adapter in the same physical slot reuses the bay instead of
    churning a new one (which strict bay matching would otherwise mark the old one Absent for)."""
    source, inventory, _ = modules_source(True, "4.3.0")
    source.interface_adapter_type_dict = {}
    source.nic_module_bay_by_adapter_id = {}

    def adapter(label):
        return {"inventory": {"network_adapter": [
            {"id": "NIC.Slot.1", "name": label, "model": "BCM57414", "manufacturer": "Broadcom",
             "operation_status": "Enabled", "num_ports": "2", "serial": "NIC-AAA"}]}}

    source.inventory_file_content = adapter("Broadcom Adapter")
    source.update_network_adapter()
    source.inventory_file_content = adapter("Broadcom Adapter rev2")
    source.update_network_adapter()

    bays = inventory.get_all_items(NBModuleBay)
    assert len(bays) == 1
    assert bays[0].data["name"] == "NIC.Slot.1"
    assert len(inventory.get_all_items(NBModule)) == 1
    # and the in-memory adapter->bay map used for interface linking is the stable id too
    assert source.nic_module_bay_by_adapter_id["NIC.Slot.1"] == "NIC.Slot.1"


def test_dimm_module_bay_stable_when_dimm_type_changes(modules_source):
    """A DIMM's module bay is the stable slot (e.g. "DIMM A1"); the memory type appended to the
    display name must not be part of the bay identity, so swapping the DIMM reuses the bay and
    only re-points its module type instead of churning a new bay. Drives the real update_memory()."""
    source, inventory, _ = modules_source(True, "4.3.0")

    def dimm(dimm_type, part):
        return {"inventory": {"memory": [
            {"name": "DIMM A1", "type": dimm_type, "manufacturer": "Samsung", "part_number": part,
             "serial": "DIMM-AAA", "size_in_mb": 32768, "speed": 3200,
             "health_status": "OK", "operation_status": "GoodInUse"}]}}

    source.inventory_file_content = dimm("DDR4", "PN-DDR4")
    source.update_memory()
    source.inventory_file_content = dimm("DDR5", "PN-DDR5")
    source.update_memory()

    bays = inventory.get_all_items(NBModuleBay)
    assert len(bays) == 1
    assert bays[0].data["name"] == "DIMM A1"
    assert len(inventory.get_all_items(NBModule)) == 1
    # the swap re-points the module type to the new part instead of creating a second bay
    assert grab(inventory.get_all_items(NBModule)[0], "data.module_type.data.model") == "PN-DDR5"


def test_physical_drive_module_bay_stable_when_model_changes(modules_source):
    """A physical drive's module bay is the stable slot; the type/model appended to the display
    name must not churn the bay, so replacing the drive in a slot reuses the bay (a real swap also
    brings a new serial). Drives the real update_physical_drive()."""
    source, inventory, _ = modules_source(True, "4.3.0")

    def drive(model, serial):
        return {"inventory": {"physical_drive": [
            {"name": "Solid State Disk", "id": "Disk.Bay.0", "location": "Slot 5", "type": "SSD",
             "model": model, "manufacturer": "Samsung", "serial": serial, "part_number": "PN-DRV",
             "size_in_byte": 512000000000, "health_status": "OK", "operation_status": "GoodInUse"}]}}

    source.inventory_file_content = drive("MZ-A", "DRV-AAA")
    source.update_physical_drive()
    source.inventory_file_content = drive("MZ-B", "DRV-BBB")
    source.update_physical_drive()

    bays = inventory.get_all_items(NBModuleBay)
    assert len(bays) == 1
    assert bays[0].data["name"] == "Solid State Disk Slot 5"
    assert len(inventory.get_all_items(NBModule)) == 1
    assert grab(inventory.get_all_items(NBModule)[0], "data.serial") == "DRV-BBB"


def test_interfaces_not_attached_to_modules_when_feature_disabled(modules_source):
    """With the modules feature off, interfaces must not get a module reference."""
    source, inventory, _ = modules_source(False, "4.3.0")
    source.interface_adapter_type_dict = {}
    source.nic_module_bay_by_adapter_id = {}
    source.manager_name = None
    source.settings.overwrite_interface_name = False
    source.settings.overwrite_interface_attributes = False
    source.settings.permitted_subnets = None
    source.settings.ip_tenant_inheritance_order = []

    source.inventory_file_content = {
        "inventory": {
            "network_adapter": [
                {"id": "NIC.Slot.1", "name": "NIC.Slot.1", "model": "BCM57414",
                 "manufacturer": "Broadcom", "operation_status": "Enabled", "num_ports": "2"}
            ],
            "network_port": [
                {"id": "NIC.Slot.1-1", "name": "Slot 1 Port 1", "adapter_id": "NIC.Slot.1",
                 "operation_status": "Enabled", "link_status": "Up", "addresses": [],
                 "capable_speed": 10000, "manager_ids": []},
            ],
        }
    }

    source.update_network_adapter()
    source.update_network_interface()

    interfaces = inventory.get_all_items(NBInterface)
    assert len(interfaces) == 1
    assert grab(interfaces[0], "data.module") is None
    # with the feature off, the legacy "<label> (<id>)" interface name is preserved unchanged
    assert grab(interfaces[0], "data.name") == "Slot 1 Port 1 (NIC.Slot.1-1)"
    assert len(inventory.get_all_items(NBModule)) == 0


def _psu_inventory():
    return {
        "inventory": {
            "power_supply": [
                {"name": "PS1", "type": "AC", "vendor": "Dell", "model": "PWR-750W",
                 "serial": "PSU-AAA", "part_number": "0ABC", "capacity_in_watt": 750,
                 "firmware": "1.2", "health_status": "OK", "operation_status": "Enabled"}
            ]
        }
    }


def test_power_port_attached_to_its_power_supply_module(modules_source):
    """A power supply is modeled as a module; its power port hangs off that module (module FK)
    so NetBox cascade-deletes the port when the PSU module is removed."""
    source, inventory, _ = modules_source(True, "4.3.0")
    source.settings.overwrite_power_supply_name = False
    source.settings.overwrite_power_supply_attributes = False
    source.inventory_file_content = _psu_inventory()

    source.update_power_supply()

    # the PSU is modeled as a module in its own bay (not a deprecated inventory item)
    modules = inventory.get_all_items(NBModule)
    assert len(modules) == 1
    psu_module = modules[0]
    assert grab(psu_module, "data.custom_fields.inventory_type") == "Power Supply"
    assert len(inventory.get_all_items(NBInventoryItem)) == 0

    # the power port exists and is linked to that exact PSU module object
    power_ports = inventory.get_all_items(NBPowerPort)
    assert len(power_ports) == 1
    assert grab(power_ports[0], "data.module") is psu_module
    # the PSU sits in a stable, slot-based bay (the volatile "(AC)" type is not part of it)
    assert grab(psu_module, "data.module_bay.data.name") == "PS1"


def test_power_supply_bay_keyed_on_stable_slot_not_type(modules_source):
    """The PSU module bay must be the stable physical slot (e.g. "PS1"), independent of the
    AC/DC type which is part of the supply, not the slot - so a later swap reuses the bay."""
    source, inventory, _ = modules_source(True, "4.3.0")
    source.settings.overwrite_power_supply_name = False
    source.settings.overwrite_power_supply_attributes = False
    source.inventory_file_content = _psu_inventory()  # name "PS1", type "AC"

    source.update_power_supply()

    bays = inventory.get_all_items(NBModuleBay)
    assert len(bays) == 1
    # the bay identity is the slot only; the volatile "(AC)" type must not be in the bay name
    assert bays[0].data["name"] == "PS1"


def test_power_supply_swap_reuses_bay_and_repoints_module_type(modules_source):
    """Swapping the supply in a slot (AC -> DC, different model) reuses the same module bay and
    re-points the module type, instead of churning a new bay - exactly like a CPU socket swap."""
    source, inventory, _ = modules_source(True, "4.3.0")
    source.settings.overwrite_power_supply_name = False
    source.settings.overwrite_power_supply_attributes = False

    def psu(ps_type, model, part):
        return {"inventory": {"power_supply": [
            {"name": "PS1", "type": ps_type, "vendor": "Dell", "model": model,
             "serial": "PSU-AAA", "part_number": part, "capacity_in_watt": 750,
             "firmware": "1.2", "health_status": "OK", "operation_status": "Enabled"}]}}

    source.inventory_file_content = psu("AC", "PWR-AC-750", "PN-AC")
    source.update_power_supply()
    source.inventory_file_content = psu("DC", "PWR-DC-1100", "PN-DC")
    source.update_power_supply()

    # the slot is reused - no bay/module churn ...
    bays = inventory.get_all_items(NBModuleBay)
    assert len(bays) == 1
    assert bays[0].data["name"] == "PS1"
    modules = inventory.get_all_items(NBModule)
    assert len(modules) == 1
    # ... and the module type now reflects the newly installed (DC) part
    assert grab(modules[0], "data.module_type.data.model") == "PWR-DC-1100"
    # the power port stays linked to that single module
    assert grab(inventory.get_all_items(NBPowerPort)[0], "data.module") is modules[0]


def test_power_port_module_link_detached_when_feature_disabled_after_enable(modules_source):
    """An enable -> disable transition must clear the previously persisted power-port module link.
    unset_attribute marks the field for a real None PATCH (update() alone silently skips None), so
    ownership is not left stale and a later module prune can't cascade-delete a port we manage."""
    source, inventory, _ = modules_source(True, "4.3.0")
    source.settings.overwrite_power_supply_name = False
    source.settings.overwrite_power_supply_attributes = False

    # run 1: modules on -> the power port gets linked to its PSU module
    source.inventory_file_content = _psu_inventory()
    source.update_power_supply()
    power_port = inventory.get_all_items(NBPowerPort)[0]
    assert grab(power_port, "data.module") is not None

    # run 2: modules off -> the now-stale link must be detached
    source.settings.model_components_as_modules = False
    source.update_power_supply()
    assert "module" in power_port.unset_items


def test_interface_module_link_detached_when_feature_disabled_after_enable(modules_source):
    """Same enable -> disable detach guarantee for interfaces. Uses the BMC/management interface
    because its name is stable across the modules and inventory-item paths, so it is matched by
    name on the second run (a regular NIC port is renamed and would not match)."""
    source, inventory, _ = modules_source(True, "4.3.0")
    source.interface_adapter_type_dict = {}
    source.nic_module_bay_by_adapter_id = {}
    source.manager_name = None
    source.settings.overwrite_interface_name = False
    source.settings.overwrite_interface_attributes = False
    source.settings.permitted_subnets = None
    source.settings.ip_tenant_inheritance_order = []

    inv = {
        "inventory": {
            "manager": [
                {"name": "iDRAC 9", "model": None, "licenses": [], "firmware": "7.0",
                 "health_status": "OK"}
            ],
            "network_port": [
                {"id": "NIC.1", "name": "iDRAC", "adapter_id": None,
                 "operation_status": "Enabled", "link_status": "Up", "addresses": [],
                 "capable_speed": 1000, "manager_ids": ["iDRAC.Embedded.1"]},
            ],
        }
    }

    # run 1: modules on -> the BMC interface is linked to the manager module
    source.inventory_file_content = inv
    source.update_manager()
    source.update_network_interface()
    interface = inventory.get_all_items(NBInterface)[0]
    assert grab(interface, "data.module") is not None

    # run 2: modules off -> the now-stale interface->module link must be detached
    source.settings.model_components_as_modules = False
    source.update_manager()
    source.update_network_interface()
    assert "module" in interface.unset_items


def test_power_port_not_attached_to_module_when_feature_disabled(modules_source):
    """With the modules feature off, the PSU stays a deprecated inventory item and its power port
    must not get a module reference."""
    source, inventory, _ = modules_source(False, "4.3.0")
    source.settings.overwrite_power_supply_name = False
    source.settings.overwrite_power_supply_attributes = False
    source.inventory_file_content = _psu_inventory()

    source.update_power_supply()

    power_ports = inventory.get_all_items(NBPowerPort)
    assert len(power_ports) == 1
    assert grab(power_ports[0], "data.module") is None
    assert len(inventory.get_all_items(NBModule)) == 0
    # the deprecated inventory-item path is used for the PSU instead
    assert len(inventory.get_all_items(NBInventoryItem)) == 1


def test_inventory_item_backend_when_feature_disabled(modules_source):
    source, inventory, device = modules_source(False, "4.3.0")

    source.update_all_items([cpu_item()], "CPU")

    # flag off -> the deprecated inventory item path is used, no modules created
    assert len(inventory.get_all_items(NBInventoryItem)) == 1
    assert len(inventory.get_all_items(NBModule)) == 0
    assert len(inventory.get_all_items(NBModuleBay)) == 0

    item = inventory.get_all_items(NBInventoryItem)[0]
    assert item.data["device"] is device
    assert grab(item, "data.custom_fields.inventory_type") == "CPU"


def test_inventory_item_backend_on_old_netbox_even_with_flag(modules_source):
    source, inventory, _ = modules_source(True, "4.2.9")

    source.update_all_items([cpu_item()], "CPU")

    # NetBox too old for modules -> inventory items regardless of the flag
    assert len(inventory.get_all_items(NBInventoryItem)) == 1
    assert len(inventory.get_all_items(NBModule)) == 0


# A Dell structured `location` blob as check_redfish can hand it back: not a plain string but a
# nested Oem dict. str()-ing it produces ~200 chars of "{'Oem': {'Dell': {'@odata.type': ...}}}".
_DELL_LOCATION = {
    "Oem": {
        "Dell": {
            "@odata.type": "#DellLocation.v1_2_0.DellLocation",
            "Locator": "BP_PSV 0:1",
        }
    }
}


def _enclosure(location):
    return {"inventory": {"storage_enclosure": [
        {"name": "BP_PSV 0:1", "model": "BP14G+EXP", "location": location,
         "manufacturer": "DELL", "serial": "ENC-AAA", "part_number": "PN-ENC",
         "firmware": "1.0", "health_status": "OK", "num_bays": 24,
         "operation_status": "Enabled"}]}}


def test_storage_enclosure_dell_location_dict_does_not_churn_module_bay(modules_source):
    """A Dell structured `location` must not be stringified into the bay/module name: it would blow
    past NetBox's 64-char limit, get truncated on store, then never match the (untruncated) name
    computed on the next sync -> the bay+module is recreated every run. Modules backend uses strict
    bay matching, so this churn is direct. Drives the real update_storage_enclosure() twice."""
    source, inventory, _ = modules_source(True, "4.3.0")

    source.inventory_file_content = _enclosure(_DELL_LOCATION)
    source.update_storage_enclosure()
    source.update_storage_enclosure()   # identical second sync must be idempotent

    bays = inventory.get_all_items(NBModuleBay)
    modules = inventory.get_all_items(NBModule)
    assert len(bays) == 1
    assert len(modules) == 1

    name = bays[0].data["name"]
    assert "Oem" not in name
    assert "Dell" not in name
    assert "{" not in name
    assert len(name) <= 64
    assert name == "BP_PSV 0:1"


def test_long_module_bay_name_does_not_churn(modules_source):
    """A module bay name longer than NetBox's 64-char limit is truncated on store, so the match key
    must be truncated the same way - otherwise the bay + module is recreated every sync (the module
    path uses strict matching with no alphabetical fallback, unlike inventory items). Reproduces the
    prod churn from a physical drive whose slot/location string exceeds 64 chars (e.g. device 5's
    'Solid State Disk 0:1:0 RAID.SL.3-1:0:Disk.Bay.0:...'). Drives the real update_physical_drive()."""
    source, inventory, _ = modules_source(True, "4.3.0")

    def drive():
        return {"inventory": {"physical_drive": [
            {"name": "Solid State Disk", "id": "Disk.Bay.0",
             "location": "0:1:0 RAID.SL.3-1:0:Disk.Bay.0:Enclosure.Internal.0-1",
             "type": "SSD", "model": "MZ7L3480", "manufacturer": "Samsung", "serial": "DRV-AAA",
             "part_number": "PN-DRV", "size_in_byte": 480000000000,
             "health_status": "OK", "operation_status": "GoodInUse"}]}}

    source.inventory_file_content = drive()
    source.update_physical_drive()
    source.inventory_file_content = drive()
    source.update_physical_drive()   # identical second sync must be idempotent

    bays = inventory.get_all_items(NBModuleBay)
    assert len(bays) == 1, [b.data["name"] for b in bays]
    assert len(inventory.get_all_items(NBModule)) == 1
    assert len(bays[0].data["name"]) <= 64


def test_long_module_bay_names_sharing_a_prefix_do_not_collide(modules_source):
    """Two physically distinct drives whose slot strings are identical for the first 64 chars but
    differ afterwards must remain two separate module bays. A plain name[:64] truncation collapses
    them to the same match key, silently merging two slots into one (update_all_modules matches by
    module_bay_name, create_module stores it as the bay name). Drives the real update_physical_drive()."""
    source, inventory, _ = modules_source(True, "4.3.0")

    # a common location prefix long enough that the disambiguating suffix lands beyond char 64
    common = "RAID.SL.3-1:0:Enclosure.Internal.0-1:Backplane.Slot.Group.A."

    def drive(disambig, serial):
        return {"name": "Solid State Disk", "id": f"Disk.Bay.{disambig}",
                "location": f"{common}Disk.Bay.{disambig}",
                "type": "SSD", "model": "MZ7L3480", "manufacturer": "Samsung", "serial": serial,
                "part_number": "PN-DRV", "size_in_byte": 480000000000,
                "health_status": "OK", "operation_status": "GoodInUse"}

    source.inventory_file_content = {"inventory": {"physical_drive": [
        drive("0", "DRV-AAA"), drive("1", "DRV-BBB")]}}
    source.update_physical_drive()

    bays = inventory.get_all_items(NBModuleBay)
    names = [b.data["name"] for b in bays]
    # the two slots share the first 64 chars, so they must NOT collapse onto one bay/module
    assert len(bays) == 2, names
    assert len(set(names)) == 2, names
    assert len(inventory.get_all_items(NBModule)) == 2
    assert all(len(n) <= 64 for n in names), names


def _seed_existing_module_graph(source, inventory, device,
                                bay_name="Socket 1",
                                model="Intel Xeon Gold 6248R",
                                serial="CPU-AAA",
                                inventory_type="CPU",
                                custom_fields=None):
    """
    Seed a module graph the way query_current_data() does: objects that already exist in
    NetBox are read into the inventory with read_from_netbox=True and therefore carry no
    source until a run of that source touches them.
    """

    manufacturer = inventory.add_object(NBManufacturer, data={"name": "Intel"}, read_from_netbox=True)
    module_type = inventory.add_object(NBModuleType,
                                       data={"model": model, "manufacturer": manufacturer},
                                       read_from_netbox=True)
    bay = inventory.add_object(NBModuleBay,
                               data={"device": device, "name": bay_name},
                               read_from_netbox=True)
    module = inventory.add_object(NBModule,
                                  data={
                                      "device": device,
                                      "module_bay": bay,
                                      "module_type": module_type,
                                      "status": "active",
                                      "serial": serial,
                                      "custom_fields": {"inventory_type": inventory_type,
                                                        **(custom_fields or {})},
                                  },
                                  read_from_netbox=True)

    assert bay.source is None
    assert module.source is None

    return bay, module


def test_existing_module_bay_is_marked_seen_by_the_source(modules_source):
    """
    A component whose module already exists must still mark its module bay as seen.

    tag_all_the_things() adds the orphaned tag to every object carrying the primary tag whose
    source is None after a run. update_module() re-registered only the module, never the bay it
    is installed in, so on every device whose modules already existed the bays were tagged
    orphaned on every run and never recovered: 571 bays were tagged while the 482 modules
    installed in them stayed healthy.
    """

    source, inventory, device = modules_source(True, "4.3.0")
    bay, module = _seed_existing_module_graph(source, inventory, device)

    source.update_all_items([cpu_item()], "CPU")

    # the existing objects are reused, not duplicated
    assert len(inventory.get_all_items(NBModule)) == 1
    assert len(inventory.get_all_items(NBModuleBay)) == 1

    # the module is seen by the source, and so is the bay it is installed in
    assert module.source is source
    assert bay.source is source, "module bay was not marked as seen -> it gets orphan tagged"



def _fan_inventory(reading, health="OK"):
    """A check_redfish inventory dump holding a single fan, as update_fan() parses it."""

    return {
        "inventory": {
            "fan": [
                {
                    "name": "Fan1",
                    "id": "0.1",
                    "operation_status": "Enabled",
                    "health_status": health,
                    "physical_context": "SystemBoard",
                    "reading": reading,
                    "reading_unit": "RPM",
                }
            ]
        }
    }


def _seed_fan_module(source, inventory, device, health="OK", inventory_speed=None):
    """Seed the fan module graph that update_fan() will match, as it exists in NetBox."""

    return _seed_existing_module_graph(source, inventory, device,
                                       bay_name="Fan1 (ID: 0.1)",
                                       model="Fan1 (ID: 0.1)",
                                       serial=None,
                                       inventory_type="Fan",
                                       custom_fields={"health": health,
                                                      "inventory_speed": inventory_speed})


def test_component_type_missing_from_scan_marks_modules_absent_instead_of_orphaning(modules_source):
    """
    update_all_items() returned early on an empty batch, so when a scan reported no component of
    a type at all, the existing modules were never marked Absent and never registered with the
    source - tag_all_the_things() then orphan tagged them and their bays.
    """

    source, inventory, device = modules_source(True, "4.3.0")
    bay, module = _seed_fan_module(source, inventory, device)

    # the scan reports no fans at all
    source.inventory_file_content = {"inventory": {"fan": []}}
    source.update_fan()

    # the component is gone: say so, rather than letting the object be orphan tagged
    assert grab(module, "data.custom_fields.health") == "Absent"
    assert module.source is source
    assert bay.source is source

    # nothing is created for a component that is not there
    assert len(inventory.get_all_items(NBModule)) == 1
    assert len(inventory.get_all_items(NBModuleBay)) == 1


def test_module_already_absent_is_still_marked_seen(modules_source):
    """
    Marking a module absent was guarded by its current health, so a module already absent was
    never touched again and therefore never registered with the source on later runs. It then
    carried the orphaned tag permanently even though the source still manages it.
    """

    source, inventory, device = modules_source(True, "4.3.0")
    bay, module = _seed_fan_module(source, inventory, device, health="Absent")

    source.inventory_file_content = {"inventory": {"fan": []}}
    source.update_fan()

    assert grab(module, "data.custom_fields.health") == "Absent"
    assert module.source is source
    assert bay.source is source

    # already absent: nothing changed, so there is nothing to write
    assert module.updated_items == []
