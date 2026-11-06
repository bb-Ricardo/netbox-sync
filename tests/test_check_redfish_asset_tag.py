# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""The asset tag a BMC reports can be set as the NetBox device asset tag.

Drives the real update_device() against real NBDevice objects.
"""

from module.netbox.object_classes import NBDevice

ASSET_TAG = "AS00174157"


def system(system_asset_tag=ASSET_TAG, chassis_asset_tag=None):
    """A system as check_redfish reports it, with the asset tag on system and chassis."""

    return {"inventory": {
        "system": [{"id": "1", "name": "System", "manufacturer": "HPE", "model": "ProLiant DL380 Gen10",
                    "serial": "CZJ0000001", "host_name": "server01", "asset_tag": system_asset_tag,
                    "health_status": "OK", "power_state": "On"}],
        "chassis": [{"id": "1", "asset_tag": chassis_asset_tag}],
    }}


def run_update_device(context, content):
    context.source.inventory_file_content = content
    context.source.update_device()
    return context.device


def test_asset_tag_is_not_synced_by_default(check_redfish_source):
    """Existing behaviour with the option off, which must not change."""

    device = run_update_device(check_redfish_source(), system())

    assert device.data.get("asset_tag") is None


def test_option_sets_the_system_asset_tag(check_redfish_source):
    device = run_update_device(check_redfish_source(collect_hardware_asset_tag=True),
                               system(chassis_asset_tag="CHASSIS-TAG"))

    assert device.data["asset_tag"] == ASSET_TAG


def test_chassis_asset_tag_is_the_fallback(check_redfish_source):
    """Some BMCs report the asset tag only on the chassis."""

    device = run_update_device(check_redfish_source(collect_hardware_asset_tag=True),
                               system(system_asset_tag=None, chassis_asset_tag=ASSET_TAG))

    assert device.data["asset_tag"] == ASSET_TAG


def test_padded_asset_tag_is_stripped(check_redfish_source):
    device = run_update_device(check_redfish_source(collect_hardware_asset_tag=True),
                               system(system_asset_tag=f"  {ASSET_TAG} "))

    assert device.data["asset_tag"] == ASSET_TAG


def test_placeholder_asset_tags_are_ignored(check_redfish_source):
    """A placeholder on the system must not hide a real chassis asset tag, nor be synced itself."""

    context = check_redfish_source(collect_hardware_asset_tag=True)

    device = run_update_device(context, system(system_asset_tag="Unknown", chassis_asset_tag=ASSET_TAG))
    assert device.data["asset_tag"] == ASSET_TAG

    other = context.inventory.add_object(NBDevice, data={"name": "server02"}, source=context.source)
    context.source.device_object = other
    context.source.inventory_file_content = system(system_asset_tag="To Be Filled By O.E.M.",
                                                   chassis_asset_tag="   ")
    context.source.update_device()

    assert other.data.get("asset_tag") is None


def test_asset_tag_of_another_device_is_not_set(check_redfish_source):
    """NetBox rejects a duplicate asset tag and with it every other change of the update."""

    context = check_redfish_source(collect_hardware_asset_tag=True)
    context.inventory.add_object(NBDevice, data={"name": "other", "asset_tag": ASSET_TAG}, source=context.source)

    device = run_update_device(context, system())

    assert device.data.get("asset_tag") is None
    assert device.data["serial"] == "CZJ0000001"


def test_own_asset_tag_is_kept(check_redfish_source):
    """The device which already has the asset tag is not a conflict with itself."""

    context = check_redfish_source(collect_hardware_asset_tag=True)
    context.device.data["asset_tag"] = ASSET_TAG

    device = run_update_device(context, system())

    assert device.data["asset_tag"] == ASSET_TAG
    assert "asset_tag" not in device.updated_items


def test_long_asset_tag_of_another_device_is_not_set(check_redfish_source, caplog):
    """NBDevice cuts the asset tag to the 50 characters NetBox allows, so the duplicate
    check has to compare the cut value, or NetBox rejects the whole update."""

    long_asset_tag = "A" * 50 + "-0123456789"
    context = check_redfish_source(collect_hardware_asset_tag=True)
    context.inventory.add_object(NBDevice, data={"name": "other", "asset_tag": long_asset_tag},
                                 source=context.source)

    device = run_update_device(context, system(system_asset_tag=long_asset_tag))

    assert device.data.get("asset_tag") is None
    assert device.data["serial"] == "CZJ0000001"
    assert "is already used by device 'other'" in caplog.text


def test_long_own_asset_tag_is_kept(check_redfish_source):
    """The device which already has the cut asset tag is not a conflict with itself."""

    long_asset_tag = "A" * 50 + "-0123456789"
    context = check_redfish_source(collect_hardware_asset_tag=True)
    context.device.data["asset_tag"] = long_asset_tag[:50]

    device = run_update_device(context, system(system_asset_tag=long_asset_tag))

    assert device.data["asset_tag"] == long_asset_tag[:50]
    assert "asset_tag" not in device.updated_items
