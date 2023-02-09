# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

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
    "config_example": "10.0.0.0/8, !10.23.42.0/24"
}
