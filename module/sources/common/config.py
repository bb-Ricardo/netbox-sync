# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

config_option_type_definition = {
    "key": "type",
    "value_type": str,
    "description": "type of source. This defines which source handler to use",
    "config_example": "UNDEFINED",
    "mandatory": True
}

config_option_enabled_definition = {
    "key": "enabled",
    "value_type": bool,
    "description": "Defines if this source is enabled or not",
    "default_value": True
}

config_option_permitted_subnets_definition = {
    "key": "permitted_subnets",
    "value_type": str,
    "description": """IP networks eligible to be synced to NetBox. If an IP address is not part of
    this networks then it WON'T be synced to NetBox. To excluded small blocks from bigger IP blocks
    a leading '!' has to be added
    """,
    "config_example": "172.16.0.0/12, 10.0.0.0/8, 192.168.0.0/16, fd00::/8, !10.23.42.0/24"
}

config_option_ip_tenant_inheritance_order_definition = {
    "key": "ip_tenant_inheritance_order",
    "value_type": str,
    "description": """\
    define in which order the IP address tenant will be assigned if tenant is undefined.
    possible values:
      * device : host or VM tenant will be assigned to the IP address
      * prefix : if the IP address belongs to an existing prefix and this prefix has a tenant assigned, then this one is used
      * disabled : no tenant assignment to the IP address will be performed
    the order of the definition is important, the default is "device, prefix" which means:
    If the device has a tenant then this one will be used. If not, the prefix tenant will be used if defined
    """,
    "default_value": "device, prefix"
}
