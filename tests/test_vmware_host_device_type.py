"""
Regression tests for issue #460 (device type of ESXi hosts overwritten).

Some BIOSes report placeholders such as "Default string" instead of the real
hardware model. netbox-sync turned that into a device type and re-assigned it on
every run, replacing a device type the user had set by hand in NetBox. These tests
drive the real add_device_vm_to_inventory path of the VMware handler against the
in-memory NetBoxInventory (no vCenter needed).
"""
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import NBSite, NBManufacturer, NBDeviceType, NBDevice
from module.sources.vmware.connection import VMWareHandler


def _make_source(inventory):
    # build the handler without its network-bound __init__
    src = object.__new__(VMWareHandler)
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.object_cache = dict()
    src.settings = SimpleNamespace(
        match_host_by_serial=True,
        match_vm_by_serial=True,
        match_vm_by_mac_address=True,
        match_vm_by_ip_address=True,
        overwrite_device_platform=False,
        overwrite_vm_platform=False,
        host_role_relation=[],
        host_interface_exclude_filter=None,
        vm_interface_exclude_filter=None,
        set_primary_ip="when-undefined",
        vm_exclude_disk_sync=None,
        vm_exclude_disk_sync_by_tag=None,
    )
    return src


def _existing_host(inventory, model="PowerEdge R740", manufacturer="Dell"):
    # a device as it would be read from NetBox, with a device type set by hand
    site = inventory.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    vendor = inventory.add_object(NBManufacturer, data={"name": manufacturer}, read_from_netbox=True)
    device_type = inventory.add_object(NBDeviceType, data={"model": model, "manufacturer": vendor},
                                       read_from_netbox=True)
    return inventory.add_object(NBDevice, data={
        "name": "esxi01", "site": site, "device_type": device_type, "serial": "SN123", "status": "active",
    }, read_from_netbox=True)


def _sync_host(src, model, manufacturer="Generic Vendor", name="esxi01", serial="SN123"):
    # the host data add_host() hands over for an ESXi host reporting the given model
    host_data = {
        "name": name,
        "device_type": {"model": model, "manufacturer": {"name": manufacturer}},
        "site": {"name": "site1"},
        "status": "active",
        "serial": serial,
    }
    src.add_device_vm_to_inventory(NBDevice, object_data=host_data, pnic_data={}, vnic_data={}, nic_ips={})
    return src.inventory.get_by_data(NBDevice, data={"name": name, "site": {"name": "site1"}})


def _model_of(device):
    return device.data.get("device_type").data.get("model")


@pytest.mark.parametrize("junk_model", ["Default string", "Generic Model"])
def test_existing_device_type_survives_unknown_model(inventory, junk_model):
    # a BIOS placeholder, or the generic name add_host() substitutes for it, must not
    # replace the device type set in NetBox (issue #460)
    device = _existing_host(inventory)

    synced = _sync_host(_make_source(inventory), model=junk_model)

    assert synced is device
    assert _model_of(device) == "PowerEdge R740", "device type was replaced by the unknown model"
    assert "device_type" not in device.updated_items


def test_existing_device_type_follows_real_model(inventory):
    # a real model reported by vCenter still updates the device type
    device = _existing_host(inventory)

    synced = _sync_host(_make_source(inventory), model="PowerEdge R750", manufacturer="Dell")

    assert synced is device
    assert _model_of(device) == "PowerEdge R750"


def test_new_device_gets_placeholder_device_type(inventory):
    # NetBox requires a device type, so a new host with an unknown model is still
    # created with the generic placeholder
    inventory.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)

    device = _sync_host(_make_source(inventory), model="Generic Model", name="esxi02", serial="SN456")

    assert device is not None and device.is_new
    assert _model_of(device) == "Generic Model"
    assert device.data.get("device_type").data.get("manufacturer").data.get("name") == "Generic Vendor"


@pytest.mark.parametrize("value", ["Default string", "N/A", "To Be Filled By O.E.M.", "Unknown", "Generic Model", None])
def test_unknown_hardware_identifiers(value):
    # the same list of BIOS placeholders is used for asset tag, vendor and model
    assert VMWareHandler.hardware_identifier_is_unknown(value) is True


@pytest.mark.parametrize("value", ["PowerEdge R740", "Dell Inc."])
def test_real_hardware_identifiers(value):
    assert VMWareHandler.hardware_identifier_is_unknown(value) is False
