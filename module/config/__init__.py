# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

default_config_file_path = "./settings.ini"

common_config_section_name = "common"
netbox_config_section_name = "netbox"
source_config_section_name = "source"

env_var_prefix = "NBS"
env_var_source_prefix = f"{env_var_prefix}_{source_config_section_name.upper()}"
