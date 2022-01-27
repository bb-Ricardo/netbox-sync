# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from ipaddress import ip_interface, ip_address
import asyncio

import aiodns

from module.common.logging import get_logger

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

# EOF
