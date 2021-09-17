# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2021 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from ipaddress import ip_interface, ip_address, IPv6Address, IPv4Address, IPv6Network, IPv4Network
import asyncio

import aiodns

from module.common.logging import get_logger
from module.common.misc import grab
from module.netbox.inventory import NBDevice, NBVM, NBInterface, NBVMInterface, NBSite, NBPrefix, NBIPAddress, NBVLAN

log = get_logger()


def normalize_mac_address(mac_address=None):
    """
    normalize a MAC address
        * format letters to upper case
        * add colons if missing

    Parameters
    ----------
    mac_address: str
        MAC address to normalize

    Returns
    -------
    str: result of normalization
    """

    if mac_address is None:
        return None

    mac_address = mac_address.upper()

    # add colons to interface address
    if ":" not in mac_address:
        mac_address = ':'.join(mac_address[i:i+2] for i in range(0, len(mac_address), 2))

    return mac_address


def ip_valid_to_add_to_netbox(ip, permitted_subnets, interface_name=None):
    """
    performs a couple of checks to see if an IP address is valid and allowed
    to be added to NetBox

    IP address must always be passed as interface notation
        * 192.168.0.1/24
        * fd00::0/64
        * 192.168.23.24/255.255.255.24

    Parameters
    ----------
    ip: str
        IP address to validate
    permitted_subnets:
        list of permitted subnets where each subnet/prefix is an instance of IP4Network or IP6Network
    interface_name: str
        name of the interface this IP shall be added. Important for meaningful log messages

    Returns
    -------
    bool: if IP address is valid
    """

    if ip is None:
        log.error("No IP address provided")
        return False

    if permitted_subnets is None:
        return False

    ip_text = f"'{ip}'"
    if interface_name is not None:
        ip_text = f"{ip_text} for {interface_name}"

    try:
        if "/" in ip:
            ip_a = ip_interface(ip).ip
        else:
            ip_a = ip_address(ip)
    except ValueError:
        log.error(f"IP address {ip_text} invalid!")
        return False

    if ip_a.is_link_local is True:
        log.debug(f"IP address {ip_text} is a link local address. Skipping.")
        return False

    if ip_a.is_loopback is True:
        log.debug(f"IP address {ip_text} is a loopback address. Skipping.")
        return False

    ip_permitted = False

    for permitted_subnet in permitted_subnets:
        if ip_a in permitted_subnet:
            ip_permitted = True
            break

    if ip_permitted is False:
        log.debug(f"IP address {ip_text} not part of any permitted subnet. Skipping.")
        return False

    return True


def perform_ptr_lookups(ips, dns_servers=None):
    """
    Perform DNS reverse lookups for IP addresses to find corresponding DNS name

    Parameters
    ----------
    ips: list
        list of IP addresses to look up
    dns_servers: list
        list of DNS servers to use to look up list of IP addresses

    Returns
    -------
    dict: of {"ip": "hostname"} for requested ips, hostname will be None if no hostname returned
    """

    loop = asyncio.get_event_loop()

    resolver = aiodns.DNSResolver(loop=loop)

    if dns_servers is not None:
        if isinstance(dns_servers, list):
            log.debug2("using provided DNS servers to perform lookup: %s" % ", ".join(dns_servers))
            resolver.nameservers = dns_servers
        else:
            log.error(f"List of provided DNS servers invalid: {dns_servers}")

    queue = asyncio.gather(*(reverse_lookup(resolver, ip) for ip in ips))
    results = loop.run_until_complete(queue)

    # return dictionary instead of a list of dictionaries
    return {k: v for x in results for k, v in x.items()}


async def reverse_lookup(resolver, ip):
    """
    Perform actual revers lookup

    Parameters
    ----------
    resolver: aiodns.DNSResolver
        handler to DNS resolver
    ip: str
        IP address to look up

    Returns
    -------
    dict: of {"ip": "hostname"} for requested ip, hostname will be None if no hostname returned
    """

    valid_hostname_characters = "abcdefghijklmnopqrstuvwxyz0123456789-."

    resolved_name = None
    response = None

    log.debug2(f"Requesting PTR record: {ip}")

    try:
        response = await resolver.gethostbyaddr(ip)
    except aiodns.error.DNSError as err:
        log.debug("Unable to find a PTR record for %s: %s", ip, err.args[1])

    if response is not None and response.name is not None:

        # validate record to check if this is a valid host name
        if all([bool(c.lower() in valid_hostname_characters) for c in response.name]):
            resolved_name = response.name.lower()
            log.debug2(f"PTR record for {ip}: {resolved_name}")

        else:
            log.warning(f"PTR record contains invalid characters: {response.name}")

    return {ip: resolved_name}


def map_object_interfaces_to_current_interfaces(inventory, device_vm_object, interface_data_dict=None):
    """
    Try to match current object interfaces to discovered ones. This will be done
    by multiple approaches. Order as following listing whatever matches first will be chosen.

        by simple name:
            both interface names match exactly
        by MAC address separated by physical and virtual NICs:
            MAC address of interfaces match exactly, distinguish between physical and virtual interfaces
        by MAC regardless of interface type
            MAC address of interfaces match exactly, type of interface does not matter

        If there are interfaces which don't match at all then the unmatched interfaces will be
        matched 1:1. Sort both lists (unmatched current interfaces, unmatched new new interfaces)
        by name and assign them each other.

            eth0 > vNIC 1
            eth1 > vNIC 2
            ens1 > vNIC 3
            ...  > ...

    Parameters
    ----------
    inventory: NetBoxInventory
        inventory handler
    device_vm_object: (NBDevice, NBVM)
        object type to look for
    interface_data_dict: dict
        dictionary with interface data to compare to existing machine

    Returns
    -------
    dict: {"$interface_name": associated_interface_object}
        if no current current interface was left to match "None" will be returned instead of
        a matching interface object
    """

    """
        trying multiple ways to match interfaces
    """

    if not isinstance(device_vm_object, (NBDevice, NBVM)):
        raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

    if not isinstance(interface_data_dict, dict):
        raise ValueError(f"Value for 'interface_data_dict' must be a dict, got: {interface_data_dict}")

    log.debug2("Trying to match current object interfaces in NetBox with discovered interfaces")

    current_object_interfaces = {
        "virtual": dict(),
        "physical": dict()
    }

    current_object_interface_names = list()

    return_data = dict()

    # grab current data
    for interface in inventory.get_all_interfaces(device_vm_object):
        int_mac = grab(interface, "data.mac_address")
        int_name = grab(interface, "data.name")
        int_type = "virtual"
        if "virtual" not in str(grab(interface, "data.type", fallback="virtual")):
            int_type = "physical"

        if int_mac is not None:
            current_object_interfaces[int_type][int_mac] = interface
            current_object_interfaces[int_mac] = interface

        if int_name is not None:
            current_object_interfaces[int_name] = interface
            current_object_interface_names.append(int_name)

    log.debug2("Found '%d' NICs in Netbox for '%s'" %
               (len(current_object_interface_names), device_vm_object.get_display_name()))

    unmatched_interface_names = list()

    for int_name, int_data in interface_data_dict.items():

        return_data[int_name] = None

        int_mac = grab(int_data, "mac_address", fallback="XX:XX:YY:YY:ZZ:ZZ")
        int_type = "virtual"
        if "virtual" not in str(grab(int_data, "type", fallback="virtual")):
            int_type = "physical"

        # match simply by name
        matching_int = None
        if int_name in current_object_interface_names:
            log.debug2(f"Found 1:1 name match for NIC '{int_name}'")
            matching_int = current_object_interfaces.get(int_name)

        # match mac by interface type
        elif grab(current_object_interfaces, f"{int_type}.{int_mac}") is not None:
            log.debug2(f"Found 1:1 MAC address match for {int_type} NIC '{int_name}'")
            matching_int = grab(current_object_interfaces, f"{int_type}.{int_mac}")

        # match mac regardless of interface type
        elif current_object_interfaces.get(int_mac) is not None and \
                current_object_interfaces.get(int_mac) not in return_data.values():
            log.debug2(f"Found 1:1 MAC address match for NIC '{int_name}' (ignoring interface type)")
            matching_int = current_object_interfaces.get(int_mac)

        if isinstance(matching_int, (NBInterface, NBVMInterface)):
            return_data[int_name] = matching_int
            # ToDo:
            # check why sometimes names are not present anymore and remove fails
            if grab(matching_int, "data.name") in current_object_interface_names:
                current_object_interface_names.remove(grab(matching_int, "data.name"))

        # no match found, we match the left overs just by #1 -> #1, #2 -> #2, ...
        else:
            unmatched_interface_names.append(int_name)

    current_object_interface_names.sort()
    unmatched_interface_names.sort()

    matching_nics = dict(zip(unmatched_interface_names, current_object_interface_names))

    for new_int, current_int in matching_nics.items():
        current_int_object = current_object_interfaces.get(current_int)
        log.debug2(f"Matching '{new_int}' to NetBox Interface '{current_int_object.get_display_name()}'")
        return_data[new_int] = current_int_object

    return return_data


def return_longest_matching_prefix_for_ip(inventory=None, ip_to_match=None, site_name=None):
    """
    This is a lazy approach to find longest matching prefix to an IP address.
    If site_name is set only IP prefixes from that site are matched.

    Parameters
    ----------
    inventory: NetBoxInventory
        inventory handler
    ip_to_match: (IPv4Address, IPv6Address)
        IP address to find prefix for
    site_name: str
        name of the site the prefix needs to be in

    Returns
    -------
    (NBPrefix, None): longest matching IP prefix, or None if no matching prefix was found
    """

    if ip_to_match is None or inventory is None:
        return

    if not isinstance(ip_to_match, (IPv4Address, IPv6Address)):
        raise ValueError("Value of 'ip_to_match' needs to be an IPv4Address or IPv6Address this_object.")

    site_object = None
    if site_name is not None:
        site_object = inventory.get_by_data(NBSite, data={"name": site_name})

        if site_object is None:
            log.error(f"Unable to find site '{site_name}' for IP {ip_to_match}. "
                      "Skipping to find Prefix for this IP.")

    current_longest_matching_prefix_length = 0
    current_longest_matching_prefix = None

    for prefix in inventory.get_all_items(NBPrefix):

        if grab(prefix, "data.site") != site_object:
            continue

        prefix_network = grab(prefix, f"data.{NBPrefix.primary_key}")
        if prefix_network is None:
            continue

        if ip_to_match in prefix_network and \
                prefix_network.prefixlen >= current_longest_matching_prefix_length:

            current_longest_matching_prefix_length = prefix_network.prefixlen
            current_longest_matching_prefix = prefix

    return current_longest_matching_prefix


def add_ip_address(source_handler, nic_ip, nic_object, site):

    # get IP and prefix length
    try:
        if "/" in nic_ip:
            ip_object = ip_interface(nic_ip)
        else:
            ip_object = ip_address(nic_ip)
    except ValueError:
        log.error(f"IP '{nic_ip}' ({nic_object.get_display_name()}) does not appear "
                  "to be a valid IP address. Skipping!")
        return

    log.debug2(f"Trying to find prefix for IP: {ip_object}")

    possible_ip_vrf = None
    possible_ip_tenant = None

    # test for site prefixes first
    matching_site_name = site
    matching_ip_prefix = return_longest_matching_prefix_for_ip(source_handler.inventory,
                                                               ip_object,
                                                               matching_site_name)

    # nothing was found then check prefixes with site name
    if matching_ip_prefix is None:
        matching_site_name = None
        matching_ip_prefix = return_longest_matching_prefix_for_ip(source_handler.inventory, ip_object)

    # matching prefix found, get data from prefix
    if matching_ip_prefix is not None:

        this_prefix = grab(matching_ip_prefix, f"data.{NBPrefix.primary_key}")
        if matching_site_name is None:
            log.debug2(f"Found IP '{ip_object}' matches global prefix '{this_prefix}'")
        else:
            log.debug2(f"Found IP '{ip_object}' matches site '{matching_site_name}' prefix "
                       f"'{this_prefix}'")

        # check if prefix net size and ip address prefix length match
        if not isinstance(ip_object, (IPv6Address, IPv4Address)) and \
                this_prefix.prefixlen != ip_object.network.prefixlen:
            log.warning(f"IP prefix length of '{ip_object}' ({nic_object.get_display_name()}) "
                        f"does not match network prefix length '{this_prefix}'!")

        # get prefix data
        possible_ip_vrf = grab(matching_ip_prefix, "data.vrf")
        prefix_tenant = grab(matching_ip_prefix, "data.tenant")
        prefix_vlan = grab(matching_ip_prefix, "data.vlan")

        # get NIC VLAN data
        nic_vlan = grab(nic_object, "data.untagged_vlan")
        nic_vlan_tenant = None
        if nic_vlan is not None:
            nic_vlan_tenant = grab(nic_vlan, "data.tenant")

        # check if interface VLAN matches prefix VLAN for IP address

        if isinstance(nic_vlan, NBVLAN) and isinstance(prefix_vlan, NBPrefix) and nic_vlan != prefix_vlan:
            log.warning(f"Prefix vlan '{prefix_vlan.get_display_name()}' does not match interface vlan "
                        f"'{nic_vlan.get_display_name()}' for '{nic_object.get_display_name()}")

        if prefix_tenant is not None:
            possible_ip_tenant = prefix_tenant
        elif nic_vlan_tenant is not None:
            possible_ip_tenant = nic_vlan_tenant

    else:
        log_text = f"No matching NetBox prefix for '{ip_object}' found"

        if isinstance(ip_object, (IPv6Address, IPv4Address)):
            log.warning(f"{log_text}. Unable to add IP address to NetBox.")
            return None
        else:
            log.debug2(log_text)

    if matching_ip_prefix is not None and isinstance(ip_object, (IPv6Address, IPv4Address)):
        this_prefix = grab(matching_ip_prefix, "data.prefix")
        if isinstance(this_prefix, (IPv4Network, IPv6Network)):
            ip_object = ip_interface(f"{ip_object}/{this_prefix.prefixlen}")
        else:
            log.warning(f"{this_prefix.name} got wrong format. Unable to add IP to NetBox")
            return None

    # try to find matching IP address object
    this_ip_object = None
    skip_this_ip = False
    for ip in source_handler.inventory.get_all_items(NBIPAddress):

        # check if address matches (without prefix length)
        ip_address_string = grab(ip, "data.address", fallback="")

        # not a matching address
        if not ip_address_string.startswith(f"{ip_object.ip.compressed}/"):
            continue

        current_nic = grab(ip, "data.assigned_object_id")

        # is it our current ip interface?
        if current_nic == nic_object:
            this_ip_object = ip
            break

        # check if IP has the same prefix
        # continue if
        #   * both are in global scope
        #   * both ara part of the same vrf
        if possible_ip_vrf != grab(ip, "data.vrf"):
            continue

        # IP address is not assigned to any interface
        if not isinstance(current_nic, (NBInterface, NBVMInterface)):
            this_ip_object = ip
            break

        # get current IP interface status
        current_nic_enabled = grab(current_nic, "data.enabled", fallback=True)
        this_nic_enabled = grab(nic_object, "data.enabled", fallback=True)

        if current_nic_enabled is True and this_nic_enabled is False:
            log.debug(f"Current interface '{current_nic.get_display_name()}' for IP '{ip_object}'"
                      f" is enabled and this one '{nic_object.get_display_name()}' is disabled. "
                      f"IP assignment skipped!")
            skip_this_ip = True
            break

        if current_nic_enabled is False and this_nic_enabled is True:
            log.debug(f"Current interface '{current_nic.get_display_name()}' for IP '{ip_object}'"
                      f" is disabled and this one '{nic_object.get_display_name()}' is enabled. "
                      f"IP will be assigned to this interface.")

            this_ip_object = ip

        if current_nic_enabled == this_nic_enabled:
            state = "enabled" if this_nic_enabled is True else "disabled"
            log.warning(f"Current interface '{current_nic.get_display_name()}' for IP "
                        f"'{ip_object}' and this one '{nic_object.get_display_name()}' are "
                        f"both {state}. "
                        f"IP assignment skipped because it is unclear which one is the correct one!")
            skip_this_ip = True
            break

    if skip_this_ip is True:
        return

    nic_ip_data = {
        "address": ip_object.compressed,
        "assigned_object_id": nic_object,
    }

    if not isinstance(this_ip_object, NBIPAddress):
        log.debug(f"No existing {NBIPAddress.name} object found. Creating a new one.")

        if possible_ip_vrf is not None:
            nic_ip_data["vrf"] = possible_ip_vrf
        if possible_ip_tenant is not None:
            nic_ip_data["tenant"] = possible_ip_tenant

        this_ip_object = source_handler.inventory.add_object(NBIPAddress, data=nic_ip_data, source=source_handler)

    # update IP address with additional data if not already present
    else:

        log.debug2(f"Found existing NetBox {NBIPAddress.name} object: {this_ip_object.get_display_name()}")

        if grab(this_ip_object, "data.vrf") is None and possible_ip_vrf is not None:
            nic_ip_data["vrf"] = possible_ip_vrf

        if grab(this_ip_object, "data.tenant") is None and possible_ip_tenant is not None:
            nic_ip_data["tenant"] = possible_ip_tenant

        this_ip_object.update(data=nic_ip_data, source=source_handler)

    return this_ip_object

# EOF
