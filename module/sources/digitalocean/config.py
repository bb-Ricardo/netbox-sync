# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption


class DigitalOceanConfig(ConfigBase):

    section_name = source_config_section_name
    source_name_example = "my-digitalocean-example"

    def __init__(self):
        self.options = [
            ConfigOption(
                "enabled",
                bool,
                default_value=True,
                description="Enable or disable the DigitalOcean source."
            ),
            ConfigOption(
                "type",
                str,
                default_value="digitalocean",
                description="Source type identifier. Must remain 'digitalocean'."
            ),
            ConfigOption(
                "api_token",
                str,
                mandatory=True,
                sensitive=True,
                description="DigitalOcean API token with read access to the resources being synced."
            ),
            ConfigOption(
                "sync_volumes",
                bool,
                default_value=True,
                description="Sync attached block volumes as virtual disks."
            ),
            ConfigOption(
                "sync_databases",
                bool,
                default_value=True,
                description="Sync managed database clusters as VM inventory records."
            ),
        ]

        super().__init__()
