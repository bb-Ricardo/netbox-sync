# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""An interface the source discovered no IPs for must keep the IPs it already has.

Drives the real add_update_interface() IP removal loop against real NBInterface and
NBIPAddress objects.
"""

from module.common.misc import grab
from module.netbox.object_classes import NBInterface, NBIPAddress
from module.sources.common.permitted_subnets import PermittedSubnets


def seed_interface_with_ip(context, name="pnet0", address="172.10.10.12/24"):
    interface = context.inventory.add_object(
        NBInterface, data={"name": name, "device": context.device}, source=context.source)
    ip = context.inventory.add_object(
        NBIPAddress, data={"address": address, "assigned_object_id": interface}, source=context.source)
    assert ip in interface.get_ip_addresses()
    return interface, ip


def test_ip_is_kept_when_the_source_discovered_no_ips(check_redfish_source):
    """The management IP on a bond or bridge matched only by a shared MAC must survive a sync."""

    context = check_redfish_source()
    interface, ip = seed_interface_with_ip(context)

    context.source.add_update_interface(interface, context.device, {"name": "pnet0"}, [], keep_undiscovered_ips=True)

    # unset_attribute() queues the de-assignment in unset_items, it does not mutate data
    assert "assigned_object_id" not in ip.unset_items


def test_ip_is_still_removed_by_default(check_redfish_source):
    """Other sources are unchanged: an IP no longer reported is still removed."""

    context = check_redfish_source()
    interface, ip = seed_interface_with_ip(context)

    context.source.add_update_interface(interface, context.device, {"name": "pnet0"}, [])

    assert "assigned_object_id" in ip.unset_items


def test_ip_is_still_removed_when_other_ips_are_discovered(check_redfish_source):
    """The guard covers an empty discovery only. An IP dropped from a non-empty set still goes."""

    context = check_redfish_source()
    interface, ip = seed_interface_with_ip(context)

    context.source.add_update_interface(interface, context.device, {"name": "pnet0"}, ["198.51.100.7/24"],
                                keep_undiscovered_ips=True)

    assert "assigned_object_id" in ip.unset_items


def test_ip_is_still_removed_when_the_discovered_ips_are_unusable(check_redfish_source):
    """A non-empty discovery is a statement about the interface even when none of the addresses
    survive parsing, so the guard must not treat it as "discovered nothing"."""

    context = check_redfish_source()
    interface, ip = seed_interface_with_ip(context)

    context.source.add_update_interface(interface, context.device, {"name": "pnet0"}, ["not-an-ip"],
                                keep_undiscovered_ips=True)

    assert "assigned_object_id" in ip.unset_items


def run_network_interface_sync(check_redfish_source, **settings):
    """Sync a server NIC port and a BMC port which both report IP addresses, like an HPE iLO
    with AMS which reports the IPs of the operating system on the physical NICs."""

    context = check_redfish_source(**settings)
    source = context.source
    context.inventory.netbox_api_version = "4.2.0"
    source.interface_adapter_type_dict = {}
    source.manager_name = "iLO 5"
    source.settings.permitted_subnets = PermittedSubnets("10.0.0.0/8")
    source.settings.ip_tenant_inheritance_order = []

    source.inventory_file_content = {
        "inventory": {
            "network_port": [
                {"id": "2.2", "addresses": ["8C:DC:D4:0F:D7:84"],
                 "operation_status": "Enabled", "link_status": "Up", "manager_ids": [],
                 "ipv4_addresses": ["10.40.232.163/22"], "ipv6_addresses": []},
                {"id": "1:1", "name": "Manager Dedicated Network Interface",
                 "addresses": ["08:F1:EA:93:F3:D6"], "operation_status": "Enabled",
                 "link_status": "Up", "manager_ids": ["1"],
                 "ipv4_addresses": ["10.44.102.102/22"], "ipv6_addresses": []},
            ],
        }
    }

    source.update_network_interface()

    return {grab(ip, "data.assigned_object_id.data.name"): str(grab(ip, "data.address"))
            for ip in context.inventory.get_all_items(NBIPAddress)}


def test_os_reported_ips_are_synced_by_default(check_redfish_source):
    """Without skip_os_reported_ips the IPs of all ports are synced, as before."""

    assigned_ips = run_network_interface_sync(check_redfish_source)

    assert assigned_ips == {"2.2": "10.40.232.163/22", "iLO 5 (1:1)": "10.44.102.102/22"}


def test_os_reported_ips_are_skipped(check_redfish_source):
    """With skip_os_reported_ips only the IPs of the BMC ports are synced."""

    assigned_ips = run_network_interface_sync(check_redfish_source, skip_os_reported_ips=True)

    assert assigned_ips == {"iLO 5 (1:1)": "10.44.102.102/22"}
