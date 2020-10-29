
from ipaddress import ip_network, ip_interface
import aiodns
import logging

def format_ip(ip_addr):

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

# EOF
