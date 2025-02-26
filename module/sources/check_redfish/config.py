# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import os

from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption
from module.sources.common.config import *
from module.common.logging import get_logger
from module.common.misc import quoted_split
from module.sources.common.permitted_subnets import PermittedSubnets

log = get_logger()


class CheckRedfishConfig(ConfigBase):

    section_name = source_config_section_name
    source_name = None
    source_name_example = "my-redfish-example"

    def __init__(self):
        self.options = [
            ConfigOption(**config_option_enabled_definition),

            ConfigOption(**{**config_option_type_definition, "config_example": "check_redfish"}),

            ConfigOption("inventory_file_path",
                         str,
                         description="define the full path where the check_redfish inventory json files are located",
                         config_example="/full/path/to/inventory/files",
                         mandatory=True),

            ConfigOption(**config_option_permitted_subnets_definition),

            ConfigOption("overwrite_host_name",
                         bool,
                         description="""define if the host name discovered via check_redfish
                         overwrites the device host name in NetBox""",
                         default_value=False),

            ConfigOption("overwrite_power_supply_name",
                         bool,
                         description="""define if the name of the power supply discovered via check_redfish
                         overwrites the power supply name in NetBox""",
                         default_value=False),

            ConfigOption("overwrite_power_supply_attributes",
                         bool,
                         description="""define if existing power supply attributes are overwritten with data discovered
                         via check_redfish if False only data which is not preset in NetBox will be added""",
                         default_value=True),

            ConfigOption("overwrite_interface_name",
                         bool,
                         description="""define if the name of the interface discovered via check_redfish
                         overwrites the interface name in NetBox""",
                         default_value=False),

            ConfigOption("overwrite_interface_attributes",
                         bool,
                         description="""define if existing interface attributes are overwritten with data discovered
                         via check_redfish if False only data which is not preset in NetBox will be added""",
                         default_value=False),

            ConfigOption(**config_option_ip_tenant_inheritance_order_definition),
        ]

        super().__init__()

    def validate_options(self):

        for option in self.options:

            if option.key == "inventory_file_path":
                if option.value is None:
                    self.set_validation_failed()
                    continue

                if not os.path.exists(option.value):
                    log.error(f"Inventory file path '{option.value}' not found.")
                    self.set_validation_failed()
                    continue

                if os.path.isfile(option.value):
                    log.error(f"Inventory file path '{option.value}' needs to be a directory.")
                    self.set_validation_failed()
                    continue

                if not os.access(option.value, os.X_OK | os.R_OK):
                    log.error(f"Inventory file path '{option.value}' not readable.")
                    self.set_validation_failed()
                    continue

            if option.key == "ip_tenant_inheritance_order":
                option.set_value(quoted_split(option.value))
                for ip_tenant_inheritance in option.value:
                    if ip_tenant_inheritance not in ["device", "prefix", "disabled"]:
                        log.error(f"Config value '{ip_tenant_inheritance}' invalid for "
                                  f"config option 'ip_tenant_inheritance_order'!")
                        self.set_validation_failed()

                if len(option.value) > 2:
                    log.error("Config option 'ip_tenant_inheritance_order' can contain only 2 items max")
                    self.set_validation_failed()

        permitted_subnets_option = self.get_option_by_name("permitted_subnets")

        if permitted_subnets_option is not None:
            permitted_subnets = PermittedSubnets(permitted_subnets_option.value)
            if permitted_subnets.validation_failed is True:
                self.set_validation_failed()

            permitted_subnets_option.set_value(permitted_subnets)
