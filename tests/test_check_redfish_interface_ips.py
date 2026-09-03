"""An interface the source discovered no IPs for must keep the IPs it already has.

Drives the real add_update_interface() IP removal loop against real NBInterface and
NBIPAddress objects.
"""

from module.netbox.object_classes import NBInterface, NBIPAddress


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
