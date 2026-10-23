# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption


class HetznerConfig(ConfigBase):

    section_name = source_config_section_name
    source_name_example = "my-hetzner-example"

    def __init__(self):
        self.options = [

            ConfigOption(
                "enabled",
                bool,
                default_value=True,
                description="Enable or disable the Hetzner Cloud source."
            ),

            ConfigOption(
                "type",
                str,
                default_value="hetzner",
                description="Source type identifier. Must remain 'hetzner'."
            ),

            ConfigOption(
                "api_token",
                str,
                mandatory=True,
                description="Hetzner Cloud API token used to authenticate against the Hetzner Cloud API."
            ),
        ]

        super().__init__()
