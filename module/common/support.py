# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import asyncio
import socket
from ipaddress import ip_address

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


def perform_ptr_lookups(ips, dns_servers=None):
    """
    Perform DNS reverse lookups for IP addresses to find corresponding DNS name

    Parameters
    ----------
    ips: list
        a list of IP addresses to look up
    dns_servers: list
        a list of DNS servers to use to look up list of IP addresses

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
        if all([bool(str(c).lower() in valid_hostname_characters) for c in response.name]):
            resolved_name = response.name.lower()
            log.debug2(f"PTR record for {ip}: {resolved_name}")

        else:
            log.warning(f"PTR record contains invalid characters: {response.name}")

    return {ip: resolved_name}


def perform_forward_lookups(names, dns_servers=None):
    """
    Perform DNS forward (A record) lookups for host names

    Parameters
    ----------
    names: list
        a list of host names to look up
    dns_servers: list
        a list of DNS servers to use to look up list of host names

    Returns
    -------
    dict: of {"name": ["ip", ...]} for requested names, list will be empty if nothing was resolved
    """

    loop = asyncio.get_event_loop()

    resolver = aiodns.DNSResolver(loop=loop)

    if dns_servers is not None:
        if isinstance(dns_servers, list):
            log.debug2("using provided DNS servers to perform lookup: %s" % ", ".join(dns_servers))
            resolver.nameservers = dns_servers
        else:
            log.error(f"List of provided DNS servers invalid: {dns_servers}")

    queue = asyncio.gather(*(forward_lookup(resolver, name) for name in names))
    results = loop.run_until_complete(queue)

    # return dictionary instead of a list of dictionaries
    return {k: v for x in results for k, v in x.items()}


async def forward_lookup(resolver, name):
    """
    Perform actual forward lookup

    Parameters
    ----------
    resolver: aiodns.DNSResolver
        handler to DNS resolver
    name: str
        host name to look up

    Returns
    -------
    dict: of {"name": ["ip", ...]} for requested name, list will be empty if nothing was resolved
    """

    valid_hostname_characters = "abcdefghijklmnopqrstuvwxyz0123456789-."

    resolved_ips = list()
    response = None

    if name is None or len(f"{name}") == 0:
        return dict()

    # validate name to check if this is a valid host name before querying it
    if not all([bool(str(c).lower() in valid_hostname_characters) for c in name]):
        log.warning(f"Host name contains invalid characters, skipping A record lookup: {name}")
        return {name: resolved_ips}

    log.debug2(f"Requesting A record: {name}")

    try:
        # getaddrinfo (instead of the deprecated query()/gethostbyname()) also honors
        # the resolver's search list and /etc/hosts, which helps when VM names are
        # synced without their domain suffix (see 'strip_vm_domain_name')
        response = await resolver.getaddrinfo(name, socket.AF_INET)
    except aiodns.error.DNSError as err:
        log.debug("Unable to find an A record for %s: %s", name, err.args[1])

    for node in getattr(response, "nodes", None) or list():
        node_address = (getattr(node, "addr", None) or (None,))[0]
        if isinstance(node_address, bytes):
            node_address = node_address.decode()
        try:
            resolved_ips.append(str(ip_address(node_address)))
        except ValueError:
            log.warning(f"A record for '{name}' returned an invalid IP address: {node_address}")

    if len(resolved_ips) > 0:
        log.debug2("A record(s) for %s: %s" % (name, ", ".join(resolved_ips)))

    return {name: resolved_ips}

# EOF
