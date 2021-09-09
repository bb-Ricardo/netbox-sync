# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2021 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from ipaddress import ip_interface
import asyncio

import aiodns

from module.common.logging import get_logger
from module.common.misc import grab
from module.netbox.inventory import NBDevice, NBVM, NBInterface, NBVMInterface

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

    if "/" not in ip:
        log.error(f"IP {ip} must contain subnet or prefix length")
        return False

    ip_text = f"'{ip}'"
    if interface_name is not None:
        ip_text = f"{ip_text} for {interface_name}"

    try:
        ip_a = ip_interface(ip).ip
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

# EOF
