"""The Dell Service Tag can be used as the NetBox device serial instead of the system serial.

Drives the real update_device() and find_device_object() against real NBDevice objects.
"""

from module.common.misc import grab
from module.netbox.object_classes import NBDevice

SYSTEM_SERIAL = "CNEXAMPLE00001"
SERVICE_TAG = "ABC1234"


def dell_system(system_serial=SYSTEM_SERIAL, service_tag=SERVICE_TAG, with_chassis=True):
    """A Dell system as check_redfish reports it: system.serial is the board PPID,
    the Service Tag is chassis.sku."""

    content = {"inventory": {"system": [
        {"id": "1", "name": "System", "manufacturer": "Dell Inc.", "model": "PowerEdge R650",
         "serial": system_serial, "host_name": "server01",
         "health_status": "OK", "power_state": "On"}]}}
    if with_chassis:
        content["inventory"]["chassis"] = [{"id": "1", "sku": service_tag}]
    return content


def run_update_device(source, content, dell_serial_from_service_tag):
    source.settings.dell_serial_from_service_tag = dell_serial_from_service_tag
    source.inventory_file_content = content
    source.update_device()
    return source.device_object


def match_content(system_serial=SYSTEM_SERIAL, service_tag=SERVICE_TAG):
    """An inventory file without meta.inventory_id, so matching falls back to the serial."""

    return {"inventory": {
        "system": [{"manufacturer": "Dell Inc.", "serial": system_serial}],
        "chassis": [{"sku": service_tag}],
    }}


def test_serial_defaults_to_the_system_serial(check_redfish_source):
    """Existing behaviour with the option off, which must not change."""

    context = check_redfish_source()
    device = run_update_device(context.source, dell_system(), dell_serial_from_service_tag=False)

    assert device.data["serial"] == SYSTEM_SERIAL
    assert grab(device, "data.custom_fields.service_tag") == SERVICE_TAG
    assert grab(device, "data.custom_fields.system_serial") is None


def test_option_makes_the_service_tag_the_serial(check_redfish_source):
    context = check_redfish_source()
    device = run_update_device(context.source, dell_system(), dell_serial_from_service_tag=True)

    assert device.data["serial"] == SERVICE_TAG
    assert grab(device, "data.custom_fields.system_serial") == SYSTEM_SERIAL
    assert grab(device, "data.custom_fields.service_tag") == SERVICE_TAG


def test_option_falls_back_when_no_service_tag_is_reported(check_redfish_source):
    """No Service Tag means no swap, so the serial is not lost."""

    context = check_redfish_source()
    device = run_update_device(context.source, dell_system(with_chassis=False),
                               dell_serial_from_service_tag=True)

    assert device.data["serial"] == SYSTEM_SERIAL
    assert grab(device, "data.custom_fields.system_serial") is None


def test_blank_service_tag_is_not_a_service_tag(check_redfish_source):
    context = check_redfish_source()
    device = run_update_device(context.source, dell_system(service_tag="   "),
                               dell_serial_from_service_tag=True)

    assert grab(device, "data.custom_fields.service_tag") is None
    assert device.data["serial"] == SYSTEM_SERIAL
    assert grab(device, "data.custom_fields.system_serial") is None


def test_system_serial_custom_field_is_not_overwritten_with_none(check_redfish_source):
    """A transient missing system serial must not clear the field on a later sync."""

    context = check_redfish_source()
    device = run_update_device(context.source, dell_system(), dell_serial_from_service_tag=True)
    assert grab(device, "data.custom_fields.system_serial") == SYSTEM_SERIAL

    run_update_device(context.source, dell_system(system_serial=None), dell_serial_from_service_tag=True)

    assert grab(device, "data.custom_fields.system_serial") == SYSTEM_SERIAL


def test_device_is_matched_by_system_serial(check_redfish_source):
    """Existing fallback matching, which must not change."""

    context = check_redfish_source()
    context.source.settings.dell_serial_from_service_tag = False
    existing = context.inventory.add_object(
        NBDevice, data={"name": "dell-host", "serial": SYSTEM_SERIAL}, source=context.source)

    context.source.inventory_file_content = match_content()

    assert context.source.find_device_object("dell-host.json") is True
    assert context.source.device_object is existing


def test_device_not_yet_migrated_is_matched_by_system_serial_with_the_option_on(check_redfish_source):
    context = check_redfish_source()
    context.source.settings.dell_serial_from_service_tag = True
    existing = context.inventory.add_object(
        NBDevice, data={"name": "dell-host", "serial": SYSTEM_SERIAL}, source=context.source)

    context.source.inventory_file_content = match_content()

    assert context.source.find_device_object("dell-host.json") is True
    assert context.source.device_object is existing


def test_device_persisted_with_the_service_tag_is_matched_by_it(check_redfish_source):
    """Without the Service Tag fallback such a device is skipped and stops being updated."""

    context = check_redfish_source()
    context.source.settings.dell_serial_from_service_tag = True
    existing = context.inventory.add_object(
        NBDevice, data={"name": "dell-host", "serial": SERVICE_TAG}, source=context.source)

    context.source.inventory_file_content = match_content()

    assert context.source.find_device_object("dell-host.json") is True
    assert context.source.device_object is existing


def test_service_tag_matching_does_not_depend_on_the_option(check_redfish_source):
    """Disabling the option must not strand a device already persisted with the Service Tag."""

    context = check_redfish_source()
    context.source.settings.dell_serial_from_service_tag = False
    existing = context.inventory.add_object(
        NBDevice, data={"name": "dell-host", "serial": SERVICE_TAG}, source=context.source)

    context.source.inventory_file_content = match_content()

    assert context.source.find_device_object("dell-host.json") is True
    assert context.source.device_object is existing


def test_padded_system_serial_still_matches(check_redfish_source):
    """update_device() stores the serial stripped, so the lookup must strip it too."""

    context = check_redfish_source()
    existing = context.inventory.add_object(
        NBDevice, data={"name": "dell-host", "serial": SYSTEM_SERIAL}, source=context.source)

    context.source.inventory_file_content = match_content(system_serial=f"  {SYSTEM_SERIAL}  ")

    assert context.source.find_device_object("dell-host.json") is True
    assert context.source.device_object is existing


def test_missing_serial_does_not_match_a_serial_less_device(check_redfish_source):
    """get_by_data() matches on exact dict equality, so probing serial=None would match wrongly."""

    context = check_redfish_source()
    context.inventory.add_object(NBDevice, data={"name": "serial-less"}, source=context.source)

    context.source.inventory_file_content = {"inventory": {
        "system": [{"manufacturer": "Dell Inc."}],
    }}

    assert context.source.find_device_object("dell-host.json") is False


def seed_device_with_nb_id(source, inventory, nb_id, name="wrong-device"):
    device = inventory.add_object(NBDevice, data={"name": name}, source=source)
    device.nb_id = nb_id
    return device


def id_content(inventory_id):
    content = match_content()
    content["meta"] = {"inventory_id": inventory_id}
    return content


def test_integer_inventory_id_is_used(check_redfish_source):
    """The normal path, which must keep working."""

    context = check_redfish_source()
    wanted = seed_device_with_nb_id(context.source, context.inventory, 1, name="by-id")

    context.source.inventory_file_content = id_content(1)

    assert context.source.find_device_object("host.json") is True
    assert context.source.device_object is wanted


def test_digit_string_inventory_id_is_used(check_redfish_source):
    """meta.inventory_id arrives from JSON, where it may be quoted."""

    context = check_redfish_source()
    wanted = seed_device_with_nb_id(context.source, context.inventory, 1, name="by-id")

    context.source.inventory_file_content = id_content("1")

    assert context.source.find_device_object("host.json") is True
    assert context.source.device_object is wanted


def test_boolean_inventory_id_does_not_match_device_one(check_redfish_source):
    """int(True) is 1, so a JSON `true` would silently claim the device with id 1."""

    context = check_redfish_source()
    seed_device_with_nb_id(context.source, context.inventory, 1)
    by_serial = context.inventory.add_object(
        NBDevice, data={"name": "dell-host", "serial": SYSTEM_SERIAL}, source=context.source)

    context.source.inventory_file_content = id_content(True)

    assert context.source.find_device_object("host.json") is True
    assert context.source.device_object is by_serial


def test_float_inventory_id_does_not_match_the_truncated_device(check_redfish_source):
    """int(1.9) is 1, so a JSON float would silently claim the device with id 1."""

    context = check_redfish_source()
    seed_device_with_nb_id(context.source, context.inventory, 1)
    by_serial = context.inventory.add_object(
        NBDevice, data={"name": "dell-host", "serial": SYSTEM_SERIAL}, source=context.source)

    context.source.inventory_file_content = id_content(1.9)

    assert context.source.find_device_object("host.json") is True
    assert context.source.device_object is by_serial


def test_non_positive_inventory_id_is_rejected(check_redfish_source):
    """NetBox ids start at 1, so zero and negatives are not usable ids."""

    context = check_redfish_source()
    seed_device_with_nb_id(context.source, context.inventory, 0)
    by_serial = context.inventory.add_object(
        NBDevice, data={"name": "dell-host", "serial": SYSTEM_SERIAL}, source=context.source)

    context.source.inventory_file_content = id_content(0)

    assert context.source.find_device_object("host.json") is True
    assert context.source.device_object is by_serial
