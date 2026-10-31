# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
ESXi hosts are matched by serial number and asset tag before MAC addresses.

With MAC address pooling (e.g. Cisco UCS) the same MAC can be used by blades in
different sites, so matching by MAC first could attach a host to a device in the
wrong site. 'disable_host_mac_matching' turns the MAC fallback off for hosts.
"""
from types import SimpleNamespace

from module.netbox.object_classes import NBSite, NBManufacturer, NBDeviceType, NBDevice, NBInterface
from module.sources.vmware.config import VMWareConfig
from module.sources.vmware.connection import VMWareHandler

POOLED_MAC = "00:25:B5:00:00:01"


def _default_settings():
    # all VMware source options with their default values, the host NICs passed below
    # run the interface sync which reads a lot more settings than the matching does
    settings = dict()
    for option in VMWareConfig().options:
        for this_option in getattr(option, "options", [option]):
            settings[this_option.key] = this_option.value
    return settings


def _make_source(inventory, match_host_by_serial=True, disable_host_mac_matching=False):
    # build the handler without its network-bound __init__
    src = object.__new__(VMWareHandler)
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.object_cache = dict()
    src.settings = SimpleNamespace(**{
        **_default_settings(),
        "match_host_by_serial": match_host_by_serial,
        "disable_host_mac_matching": disable_host_mac_matching,
        "host_role_relation": [],
        "ip_tenant_inheritance_order": ["device", "prefix"],
    })
    return src


def _existing_host(inventory, name, site_name, serial=None, mac=None):
    # a device as it would be read from NetBox, optionally with a NIC using the given MAC
    site = inventory.get_by_data(NBSite, data={"name": site_name}) or \
        inventory.add_object(NBSite, data={"name": site_name}, read_from_netbox=True)
    vendor = inventory.add_object(NBManufacturer, data={"name": "Cisco"}, read_from_netbox=True)
    device_type = inventory.add_object(NBDeviceType, data={"model": "UCSB-B200-M5", "manufacturer": vendor},
                                       read_from_netbox=True)
    device = inventory.add_object(NBDevice, data={
        "name": name, "site": site, "device_type": device_type, "serial": serial, "status": "active",
    }, read_from_netbox=True)
    if mac is not None:
        inventory.add_object(NBInterface, data={"name": "vmnic0", "device": device, "mac_address": mac},
                             read_from_netbox=True)
    return device


def _match_host(src, name, site_name, serial=None, mac=None):
    # the host data add_host() hands over for an ESXi host; returns the device it was matched to
    host_data = {
        "name": name,
        "device_type": {"model": "UCSB-B200-M5", "manufacturer": {"name": "Cisco"}},
        "site": {"name": site_name},
        "status": "active",
    }
    if serial is not None:
        host_data["serial"] = serial
    pnic_data = {"vmnic0": {"name": "vmnic0", "mac_address": mac}} if mac is not None else {}
    before = set(src.inventory.get_all_items(NBDevice))
    src.add_device_vm_to_inventory(NBDevice, object_data=host_data, pnic_data=pnic_data, vnic_data={},
                                   nic_ips={})
    added = [x for x in src.inventory.get_all_items(NBDevice) if x not in before]
    return added[0] if added else src.inventory.get_by_data(NBDevice, data={"serial": serial})


def _setup_pooled_macs(inventory):
    # two blades in different sites; only the one in site1 has the pooled MAC recorded
    blade_a = _existing_host(inventory, "esxi-a", "site1", serial="SN-A", mac=POOLED_MAC)
    blade_b = _existing_host(inventory, "esxi-b", "site2", serial="SN-B")
    return blade_a, blade_b


def test_serial_wins_over_pooled_mac(inventory):
    # a renamed blade in site2 shares a pooled MAC with a blade in site1, the serial must win
    blade_a, blade_b = _setup_pooled_macs(inventory)

    matched = _match_host(_make_source(inventory), "esxi-b-new", "site2", serial="SN-B", mac=POOLED_MAC)

    assert matched is blade_b
    assert blade_a.data.get("name") == "esxi-a"


def test_mac_fallback_without_serial_match(inventory):
    # no serial match, MAC matching still works by default
    blade_a, _ = _setup_pooled_macs(inventory)

    src = _make_source(inventory)
    _match_host(src, "esxi-a-new", "site1", serial="SN-UNKNOWN", mac=POOLED_MAC)

    assert blade_a.data.get("name") == "esxi-a-new"


def test_disable_host_mac_matching(inventory):
    # with MAC matching disabled for hosts an unknown serial creates a new device
    blade_a, _ = _setup_pooled_macs(inventory)

    matched = _match_host(_make_source(inventory, disable_host_mac_matching=True),
                          "esxi-c", "site1", serial="SN-C", mac=POOLED_MAC)

    assert matched is not blade_a and matched.is_new
    assert blade_a.data.get("name") == "esxi-a"


def test_match_host_by_serial_disabled(inventory):
    # serial matching is skipped when disabled, even if the serial is known
    _, blade_b = _setup_pooled_macs(inventory)

    src = _make_source(inventory, match_host_by_serial=False, disable_host_mac_matching=True)
    _match_host(src, "esxi-b-new", "site2", serial="SN-B")

    assert blade_b.data.get("name") == "esxi-b"


def test_missing_serial_does_not_match_device_without_serial(inventory):
    # a host reporting no serial must not be matched to an existing device that has no serial either
    existing = _existing_host(inventory, "esxi-noserial", "site1")

    matched = _match_host(_make_source(inventory, disable_host_mac_matching=True), "esxi-other", "site1")

    assert matched is not existing and matched.is_new
    assert existing.data.get("name") == "esxi-noserial"
