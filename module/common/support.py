
from ipaddress import ip_interface
import aiodns
import asyncio

from module.common.logging import get_logger

log = get_logger()

def normalize_ip_to_string(ip_addr):

    try:
        return ip_interface(ip_addr).compressed
    except ValueError:
        return None

def normalize_mac_address(mac_address=None):

    if mac_address is None:
        return None

    mac_address = mac_address.upper()

    # add colons to interface address
    if ":" not in mac_address:
        mac_address = ':'.join(mac_address[i:i+2] for i in range(0,len(mac_address),2))

    return mac_address

def ip_valid_to_add_to_netbox(ip, permitted_subnets, interface_name=None):

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
    return {k:v for x in results for k,v in x.items()}


async def reverse_lookup(resolver, ip):

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
            log.debug(f"PTR record for {ip}: {resolved_name}")

        else:
            log.debug(f"PTR record contains invalid characters: {response.name}")

    return {ip: resolved_name}

# EOF
