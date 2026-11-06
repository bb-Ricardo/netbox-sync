"""Whether the source discovered addresses is decided before permitted_subnets filtering.

keep_undiscovered_ips exists because Redfish does not report host NIC, bond or bridge
addresses, so an interface matched only by a shared MAC must keep the management IP it
already has. An address that Redfish did report but the operator excluded is a different
case: the source was not blind, so a stale address on that interface is still cleaned up.

Drives the real update_network_interface() with a real PermittedSubnets.
"""

import pytest

from module.netbox.object_classes import NBInterface, NBIPAddress, NBMACAddress
from module.sources.common.permitted_subnets import PermittedSubnets

MAC = "AA:BB:CC:DD:EE:01"
STALE = "192.0.2.50/24"
REPORTED = "198.51.100.7/24"
REPORTED_SUBNET = "198.51.100.0/24"


def nic_inventory(ipv4_addresses, ipv6_addresses=()):
    """One adapter with one port, reporting the given addresses."""

    return {"inventory": {
        "network_adapter": [
            {"id": "NIC.Integrated.1", "name": "Integrated NIC 1", "model": "X710",
             "manufacturer": "Intel", "serial": "NIC-AAA", "firmware": "1.0",
             "health_status": "OK", "operation_status": "Enabled"}],
        "network_port": [
            {"id": "NIC.Integrated.1-1", "name": "Integrated NIC 1 Port 1",
             "adapter_id": "NIC.Integrated.1", "addresses": [MAC], "link_status": "Up",
             "health_status": "OK", "operation_status": "Enabled", "manager_ids": [],
             "ipv4_addresses": ipv4_addresses, "ipv6_addresses": list(ipv6_addresses),
             "link_speed": 10000, "hostname": None, "port_id": "1"}],
    }}


def seed_interface_with_stale_ip(context, name="NIC.Integrated.1-1"):
    interface = context.inventory.add_object(
        NBInterface, data={"name": name, "device": context.device}, source=context.source)
    # NetBox >= 4.2 matches an interface by its primary MAC address object
    mac = context.inventory.add_object(
        NBMACAddress, data={"mac_address": MAC, "assigned_object_id": interface,
                            "assigned_object_type": NBInterface}, source=context.source)
    interface.update(data={"primary_mac_address": mac}, source=context.source)
    ip = context.inventory.add_object(
        NBIPAddress, data={"address": STALE, "assigned_object_id": interface}, source=context.source)
    assert ip in interface.get_ip_addresses()
    return interface, ip


def run(context, ipv4_addresses, permitted, ipv6_addresses=()):
    subnets = PermittedSubnets(permitted)
    # a subnet string that fails to parse would silently permit everything and let these
    # tests pass for the wrong reason
    assert subnets.validation_failed is False
    context.source.settings.permitted_subnets = subnets
    context.source.inventory_file_content = nic_inventory(ipv4_addresses, ipv6_addresses)
    context.source.update_network_adapter()
    context.source.update_network_interface()


def test_stale_ip_is_removed_when_the_reported_address_is_excluded(check_redfish_source):
    """The regression: Redfish reported an address, so the interface was seen. The operator
    excluding that subnet must not be read as "the source discovered nothing"."""

    context = check_redfish_source()
    _, stale = seed_interface_with_stale_ip(context)

    run(context, [REPORTED], permitted=f"!{REPORTED_SUBNET}, 0.0.0.0/0")

    assert "assigned_object_id" in stale.unset_items


def test_stale_ip_is_kept_when_redfish_reported_nothing(check_redfish_source):
    """The case the option exists for, which must keep working."""

    context = check_redfish_source()
    _, stale = seed_interface_with_stale_ip(context)

    run(context, [], permitted="0.0.0.0/0")

    assert "assigned_object_id" not in stale.unset_items


def test_stale_ip_is_removed_when_a_different_address_is_reported(check_redfish_source):
    """A reported and permitted address that is not the stale one still replaces it."""

    context = check_redfish_source()
    _, stale = seed_interface_with_stale_ip(context)

    run(context, [REPORTED], permitted="0.0.0.0/0")

    assert "assigned_object_id" in stale.unset_items


@pytest.mark.parametrize("permitted", ["0.0.0.0/0", f"!{REPORTED_SUBNET}, 0.0.0.0/0"])
def test_reported_address_is_only_synced_when_permitted(check_redfish_source, permitted):
    """permitted_subnets still governs what gets added, either way."""

    context = check_redfish_source()
    seed_interface_with_stale_ip(context)

    run(context, [REPORTED], permitted=permitted)

    synced = [ip.data["address"] for ip in context.inventory.get_all_items(NBIPAddress)]
    if permitted == "0.0.0.0/0":
        assert REPORTED in synced
    else:
        assert REPORTED not in synced


def test_stale_ip_is_kept_when_os_reported_ips_are_skipped(check_redfish_source):
    """skip_os_reported_ips means the source ignores this port's addresses, so it did not see them."""

    context = check_redfish_source()
    context.source.settings.skip_os_reported_ips = True
    _, stale = seed_interface_with_stale_ip(context)

    run(context, [REPORTED], permitted="0.0.0.0/0")

    assert "assigned_object_id" not in stale.unset_items


@pytest.mark.parametrize("reported", [[None], ["not-an-ip"], [None, "N/A"]])
def test_stale_ip_is_kept_when_no_reported_entry_is_an_address(check_redfish_source, reported):
    """An entry which is not an IP address is not a discovery, so it must not trigger pruning."""

    context = check_redfish_source()
    _, stale = seed_interface_with_stale_ip(context)

    run(context, reported, permitted="0.0.0.0/0")

    assert "assigned_object_id" not in stale.unset_items


def test_stale_ip_is_removed_when_an_excluded_address_is_reported_with_junk(check_redfish_source):
    """A valid address next to an invalid entry still counts, even when permitted_subnets excludes it."""

    context = check_redfish_source()
    _, stale = seed_interface_with_stale_ip(context)

    run(context, [None, REPORTED], permitted=f"!{REPORTED_SUBNET}, 0.0.0.0/0")

    assert "assigned_object_id" in stale.unset_items


@pytest.mark.parametrize("ipv4, ipv6", [
    ([], ["fe80::a8bb:ccff:fedd:ee01/64"]),
    (["169.254.10.10/16"], []),
    (["127.0.0.1/8"], ["::1/128"]),
])
@pytest.mark.parametrize("permitted", ["0.0.0.0/0, ::/0", "10.0.0.0/8, fd00::/8"])
def test_stale_ip_is_kept_when_only_link_local_or_loopback_is_reported(check_redfish_source,
                                                                       ipv4, ipv6, permitted):
    """permitted_subnets drops link-local and loopback whatever it is set to, so a port that
    reports only those (e.g. a bridge member with its fe80 address) was not seen either."""

    context = check_redfish_source()
    _, stale = seed_interface_with_stale_ip(context)

    run(context, ipv4, permitted=permitted, ipv6_addresses=ipv6)

    assert "assigned_object_id" not in stale.unset_items


def test_stale_ip_is_removed_when_an_excluded_address_is_reported_with_link_local(check_redfish_source):
    """A link-local address next to an excluded one does not hide the excluded one."""

    context = check_redfish_source()
    _, stale = seed_interface_with_stale_ip(context)

    run(context, [REPORTED], permitted=f"!{REPORTED_SUBNET}, 0.0.0.0/0, ::/0",
        ipv6_addresses=["fe80::a8bb:ccff:fedd:ee01/64"])

    assert "assigned_object_id" in stale.unset_items
