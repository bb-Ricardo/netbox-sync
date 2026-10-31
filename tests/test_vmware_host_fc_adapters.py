# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Fibre Channel HBAs as host interfaces (issue #343).

Behind the opt-in `sync_host_fc_adapters` option each vim.host.FibreChannelHba of an
ESXi host is synced as a NetBox interface on that device. The type is derived from the
adapter model's "<n>G"/"<n>Gb" capability (falling back to the port speed when the
model gives none), the port WWN is converted from the decimal the vCenter API reports
to the colon separated upper case hex notation NetBox expects.
"""
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import NBDevice, NBInterface, NBIPAddress, NBMACAddress
from module.sources import instantiate_sources
from module.sources.vmware.connection import VMWareHandler


def _find_device(inventory, name):
    for device in inventory.get_all_items(NBDevice):
        if device.data.get("name") == name:
            return device
    raise AssertionError(f"device '{name}' not found in inventory")


def _fc_interfaces(inventory, device):
    return [interface for interface in inventory.get_all_interfaces(device)
            if "gfc" in str(interface.data.get("type", ""))]


@pytest.mark.parametrize("speed, expected", [
    (1, "1gfc-sfp"),
    (2, "2gfc-sfp"),
    (4, "4gfc-sfp"),
    (8, "8gfc-sfpp"),
    (16, "16gfc-sfpp"),
    (32, "32gfc-sfp28"),
    (64, "64gfc-qsfpp"),
    (128, "128gfc-qsfp28"),
])
def test_fc_hba_type_from_port_speed(speed, expected):
    assert VMWareHandler.fc_hba_interface_type(speed) == expected


def test_fc_hba_type_guessed_from_model_when_speed_is_zero():
    assert VMWareHandler.fc_hba_interface_type(
        0, "QLE2700/QLE2800 32/64G SP/DP Fibre Channel Adapter") == "32gfc-sfp28"


def test_fc_hba_type_model_takes_precedence_over_port_speed():
    # a port that lost link (speed 0) keeps the type derived from the model, and a live
    # port reports the capability of the model, not the negotiated link speed
    assert VMWareHandler.fc_hba_interface_type(
        16, "QLE2700/QLE2800 32/64G SP/DP Fibre Channel Adapter") == "32gfc-sfp28"


def test_fc_hba_type_parses_gb_model_capability():
    assert VMWareHandler.fc_hba_interface_type(0, "QLE2694 16Gb Fibre Channel Adapter") == "16gfc-sfpp"


def test_fc_hba_type_is_other_for_model_without_speed():
    assert VMWareHandler.fc_hba_interface_type(0, "Emulex LPe12000") == "other"


def test_fc_hba_type_is_other_when_speed_and_model_are_unknown():
    assert VMWareHandler.fc_hba_interface_type(None, None) == "other"


def test_fc_hba_wwn_is_converted_to_hex_notation():
    assert VMWareHandler.fc_hba_wwn(2378169741649783014) == "21:00:F4:C7:AA:9E:28:E6"


def test_fc_hba_wwn_is_zero_padded_to_16_hex_digits():
    assert VMWareHandler.fc_hba_wwn(111111) == "00:00:00:00:00:01:B2:07"


def test_fc_hba_wwn_none_stays_none():
    assert VMWareHandler.fc_hba_wwn(None) is None


def _matching_source(inventory):
    """A VMware handler with nothing but the state interface matching needs."""
    source = object.__new__(VMWareHandler)
    source.inventory = inventory
    source.name = "test"
    source.settings = SimpleNamespace(
        host_interface_exclude_filter=None,
        vm_interface_exclude_filter=None,
    )
    return source


def test_fc_hbas_do_not_hijack_unmatched_nics(inventory):
    # two plain NICs and a BMC port as they would be read from NetBox, then two FC HBAs
    # that vCenter reports. The HBAs must be created as new interfaces, never paired 1:1
    # with the leftover NICs, so the BMC keeps its name and its IP (issue #343).
    source = _matching_source(inventory)
    device = inventory.add_object(NBDevice, data={"name": "esxi01"}, source=source)

    inventory.add_object(NBInterface, data={
        "name": "vmnic0", "device": device, "type": "1000base-t", "mac_address": "00:50:56:00:00:01",
    }, source=source)
    inventory.add_object(NBInterface, data={
        "name": "vmk0", "device": device, "type": "virtual", "mac_address": "00:50:56:00:00:02",
    }, source=source)
    bmc = inventory.add_object(NBInterface, data={
        "name": "iDRAC iDRAC9 (1:1)", "device": device, "type": "other", "mac_address": "38:68:DD:34:33:C5",
    }, source=source)
    inventory.add_object(NBIPAddress, data={"address": "10.0.0.10/24", "assigned_object_id": bmc},
                         source=source)

    fc_hbas = {
        "vmhba3": {"name": "vmhba3", "device": None, "type": "32gfc-sfp28",
                   "wwn": "21:00:F4:C7:AA:9E:28:E6"},
        "vmhba4": {"name": "vmhba4", "device": None, "type": "32gfc-sfp28",
                   "wwn": "21:00:F4:C7:AA:9E:28:E7"},
    }

    mapped = source.map_object_interfaces_to_current_interfaces(device, fc_hbas)

    # both HBAs become new interfaces; the BMC keeps its name and its IP
    assert mapped["vmhba3"] is None
    assert mapped["vmhba4"] is None
    assert bmc not in mapped.values()
    bmc_ips = bmc.get_ip_addresses()
    assert len(bmc_ips) == 1 and bmc_ips[0].data.get("assigned_object_id") is bmc


def test_fc_hba_matches_existing_fc_interface_by_wwn(inventory):
    # when the HBA was renamed (or re-enumerated) the WWN still finds its interface
    source = _matching_source(inventory)
    device = inventory.add_object(NBDevice, data={"name": "esxi01"}, source=source)

    existing = inventory.add_object(NBInterface, data={
        "name": "vmhba3", "device": device, "type": "32gfc-sfp28", "wwn": "21:00:F4:C7:AA:9E:28:E6",
    }, source=source)

    mapped = source.map_object_interfaces_to_current_interfaces(
        device, {"vmhba99": {"name": "vmhba99", "device": None, "type": "32gfc-sfp28",
                             "wwn": "21:00:F4:C7:AA:9E:28:E6"}}, True)

    assert mapped["vmhba99"] is existing


def test_fc_hbas_sharing_a_wwn_are_not_merged(inventory):
    # two ports of one adapter report the same WWN; each still gets its own interface
    source = _matching_source(inventory)
    device = inventory.add_object(NBDevice, data={"name": "esxi01"}, source=source)

    existing = inventory.add_object(NBInterface, data={
        "name": "vmhba3", "device": device, "type": "32gfc-sfp28", "wwn": "21:00:F4:C7:AA:9E:28:E6",
    }, source=source)

    mapped = source.map_object_interfaces_to_current_interfaces(
        device, {
            "vmhba64": {"name": "vmhba64", "device": None, "type": "32gfc-sfp28",
                        "wwn": "21:00:F4:C7:AA:9E:28:E6"},
            "vmhba65": {"name": "vmhba65", "device": None, "type": "32gfc-sfp28",
                        "wwn": "21:00:F4:C7:AA:9E:28:E6"},
        }, True)

    assert mapped["vmhba64"] is existing
    assert mapped["vmhba65"] is None


def test_current_fc_interface_is_never_handed_to_an_ethernet_nic(inventory):
    # the only leftover current interface is an FC port; a new NIC must not be paired with it
    source = _matching_source(inventory)
    device = inventory.add_object(NBDevice, data={"name": "esxi01"}, source=source)
    inventory.add_object(NBInterface, data={
        "name": "vmhba3", "device": device, "type": "32gfc-sfp28", "wwn": "21:00:F4:C7:AA:9E:28:E6",
    }, source=source)

    mapped = source.map_object_interfaces_to_current_interfaces(
        device, {"vmnic8": {"name": "vmnic8", "device": None, "type": "1000base-t",
                            "mac_address": "00:50:56:00:00:08"}})

    assert mapped["vmnic8"] is None


def test_fc_type_alone_marks_a_current_interface_as_fc(inventory):
    # no WWN stored, the FC interface type is enough to keep it out of the leftover pairing
    source = _matching_source(inventory)
    device = inventory.add_object(NBDevice, data={"name": "esxi01"}, source=source)
    inventory.add_object(NBInterface, data={"name": "vmhba3", "device": device, "type": "16gfc-sfpp"},
                         source=source)

    mapped = source.map_object_interfaces_to_current_interfaces(
        device, {"vmnic8": {"name": "vmnic8", "device": None, "type": "1000base-t",
                            "mac_address": "00:50:56:00:00:08"}})

    assert mapped["vmnic8"] is None


def test_wwn_without_mac_alone_marks_an_interface_as_fc(inventory):
    # an FC port whose type could not be determined still has a WWN and no MAC address
    source = _matching_source(inventory)
    device = inventory.add_object(NBDevice, data={"name": "esxi01"}, source=source)
    inventory.add_object(NBInterface, data={
        "name": "fc0", "device": device, "type": "other", "wwn": "21:00:F4:C7:AA:9E:28:E6",
    }, source=source)
    inventory.add_object(NBInterface, data={"name": "eth9", "device": device, "type": "other"}, source=source)

    mapped = source.map_object_interfaces_to_current_interfaces(
        device, {
            "vmnic8": {"name": "vmnic8", "device": None, "type": "1000base-t",
                       "mac_address": "00:50:56:00:00:08"},
            "vmhba9": {"name": "vmhba9", "device": None, "type": "other", "wwn": "21:00:F4:C7:AA:9E:28:F0"},
        })

    # the NIC is paired with the leftover Ethernet interface, never with the FC port; the FC
    # port with an unknown type is created new instead of taking the leftover
    assert mapped["vmnic8"].data.get("name") == "eth9"
    assert mapped["vmhba9"] is None


def test_wwn_match_ignores_case(inventory):
    source = _matching_source(inventory)
    device = inventory.add_object(NBDevice, data={"name": "esxi01"}, source=source)
    existing = inventory.add_object(NBInterface, data={
        "name": "vmhba3", "device": device, "type": "32gfc-sfp28", "wwn": "21:00:f4:c7:aa:9e:28:e6",
    }, source=source)

    mapped = source.map_object_interfaces_to_current_interfaces(
        device, {"vmhba99": {"name": "vmhba99", "device": None, "type": "32gfc-sfp28",
                             "wwn": "21:00:F4:C7:AA:9E:28:E6"}}, True)

    assert mapped["vmhba99"] is existing


def test_cna_port_with_mac_and_wwn_is_still_matched_by_mac(inventory):
    # a converged adapter port (check_redfish reports MAC and WWN) whose name changed is
    # found by its MAC address like any Ethernet interface
    source = _matching_source(inventory)
    device = inventory.add_object(NBDevice, data={"name": "srv01"}, source=source)
    existing = inventory.add_object(NBInterface, data={
        "name": "NIC.Integrated.1-2 (old)", "device": device, "type": "other",
        "mac_address": "3C:EC:EF:1A:2B:3C",
    }, source=source)
    mac_object = inventory.add_object(NBMACAddress, data={
        "mac_address": "3C:EC:EF:1A:2B:3C", "assigned_object_id": existing, "assigned_object_type": NBInterface,
    }, source=source)
    existing.update(data={"primary_mac_address": mac_object}, source=source)

    mapped = source.map_object_interfaces_to_current_interfaces(
        device, {"NIC.Integrated.1-2": {"name": "NIC.Integrated.1-2", "device": None, "type": "other",
                                        "mac_address": "3C:EC:EF:1A:2B:3C",
                                        "wwn": "50:01:43:80:12:34:56:78"}})

    assert mapped["NIC.Integrated.1-2"] is existing


def test_fc_adapters_are_not_synced_by_default(vcsim, inventory, load_config, vmware_settings):
    load_config(vmware_settings)
    sources = instantiate_sources()
    assert sources and sources[0].init_successful
    assert sources[0].settings.sync_host_fc_adapters is False

    inventory.resolve_relations()
    sources[0].apply()

    # with the option off no FC adapter interfaces exist, matching the base behaviour
    vmhba_interfaces = [interface for interface in inventory.get_all_items(NBInterface)
                        if str(interface.data.get("name", "")).startswith("vmhba")]
    assert vmhba_interfaces == []


def test_fc_adapters_synced_for_vc001(vcsim, inventory, load_config, vmware_settings):
    if vcsim.name != "vc001":
        pytest.skip("FC adapter specifics are asserted against the vc001 capture")

    load_config(vmware_settings + "\nsync_host_fc_adapters = True\n")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful

    inventory.resolve_relations()
    sources[0].apply()

    host = _find_device(inventory, "172.29.50.68")
    fc_interfaces = _fc_interfaces(inventory, host)

    assert len(fc_interfaces) == 8

    # the host also has a PCIe NVMe controller and two SATA controllers (vmhba0-2); only the
    # Fibre Channel adapters become interfaces
    hba_interface_names = {interface.data.get("name") for interface in inventory.get_all_interfaces(host)
                           if str(interface.data.get("name", "")).startswith("vmhba")}
    assert hba_interface_names == {"vmhba3", "vmhba4", "vmhba5", "vmhba6",
                                   "vmhba64", "vmhba65", "vmhba66", "vmhba67"}

    # every port is a QLE2700/QLE2800, whose "32/64G" capability wins over the
    # negotiated link speed (16) and over the offline ports' speed 0
    assert {interface.data.get("type") for interface in fc_interfaces} == {"32gfc-sfp28"}

    by_name = {interface.data.get("name"): interface for interface in fc_interfaces}
    assert by_name["vmhba3"].data.get("description") == \
        "QLE2700/QLE2800 32/64G SP/DP Fibre Channel Adapter (qlnativefc)"

    vmhba3 = by_name["vmhba3"]
    assert vmhba3.data.get("wwn") == "21:00:F4:C7:AA:9E:28:E6"
    assert vmhba3.data.get("enabled") is False

    vmhba4 = by_name["vmhba4"]
    assert vmhba4.data.get("wwn") == "21:00:F4:C7:AA:9E:28:E7"
    assert vmhba4.data.get("enabled") is True


def test_fc_adapters_synced_for_every_capture(vcsim, inventory, load_config, vmware_settings):
    load_config(vmware_settings + "\nsync_host_fc_adapters = True\n")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful

    inventory.resolve_relations()
    sources[0].apply()

    fc_interfaces = [interface for interface in inventory.get_all_items(NBInterface)
                     if "gfc" in str(interface.data.get("type", ""))]
    assert fc_interfaces, "the option on must create FC interfaces on every capture"


def test_host_interface_exclude_filter_applies_to_fc_adapters(vcsim, inventory, load_config, vmware_settings):
    load_config(vmware_settings + "\nsync_host_fc_adapters = True\nhost_interface_exclude_filter = ^vmhba\n")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful

    inventory.resolve_relations()
    sources[0].apply()

    assert [interface for interface in inventory.get_all_items(NBInterface)
            if str(interface.data.get("name", "")).startswith("vmhba")] == []
