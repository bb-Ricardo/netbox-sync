
from ipaddress import ip_network, ip_interface
import aiodns
import logging



def format_ip(ip_addr):
    """
    Formats IPv4 addresses and subnet to IP with CIDR standard notation.

    :param ip_addr: IP address with subnet; example `192.168.0.0/255.255.255.0`
    :type ip_addr: str
    :return: IP address with CIDR notation; example `192.168.0.0/24`
    :rtype: str
    """
    try:
        return ip_interface(ip_addr).compressed
    except Exception:
        return None

def normalize_mac_address(mac_address=None):

    if mac_address is None:
        return None
        
    mac_address = mac_address.upper()

    # add colons to interface address
    if ":" not in mac_address:
        mac_address = ':'.join(mac_address[i:i+2] for i in range(0,len(mac_address),2))

    return mac_address

