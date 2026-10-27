# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.common.logging import get_logger
from module.common.misc import quoted_split
from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption

log = get_logger()


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
                "region_mapping",
                str,
                config_example="fra1=Germany, nyc1=United States",
                description="Optional comma-separated DigitalOcean region slug=NetBox Region name mappings. "
                            "Missing Regions are created on NetBox 4.2+. Unmapped regions use the "
                            "DigitalOcean region name and slug."
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

    def validate_options(self):
        option = self.get_option_by_name("region_mapping")
        mapping = {}
        if option.value is not None:
            if not isinstance(option.value, str):
                log.error("Config option 'region_mapping' must be a comma-separated string of slug=name mappings.")
                self.set_validation_failed()
                return

            for entry in quoted_split(option.value):
                slug, separator, name = entry.partition("=")
                slug = slug.strip(' "\'')
                name = name.strip(' "\'')
                if not separator or not slug or not name or len(name) > 100 or slug in mapping:
                    log.error(f"Invalid region_mapping entry '{entry}': expected a unique region slug and "
                              "a non-empty NetBox Region name of at most 100 characters (slug=name).")
                    self.set_validation_failed()
                    continue
                mapping[slug] = name

        option.set_value(mapping)
