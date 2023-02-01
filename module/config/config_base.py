# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import configparser
import os

from module.common.misc import do_error_exit
from module.common.logging import get_logger

log = get_logger()


class ConfigBase:
    """
        Base class to parse config data
    """

    sensitive_keys = [
        "password",
        "token",
    ]

    not_config_vars = [
        "config_section_name",
        "__module__",
        "__doc__"
    ]

    parser_error = False

    def __init__(self, config_data: configparser.ConfigParser):

        if not isinstance(config_data, configparser.ConfigParser):
            do_error_exit("config data is not a config parser object")

        self.parse_config(config_data)

    @staticmethod
    def to_bool(value):
        """
            converts a string to a boolean
        """
        valid = {
             'true': True, 't': True, '1': True,
             'false': False, 'f': False, '0': False,
             }

        if isinstance(value, bool):
            return value

        elif isinstance(value, str):
            if value.lower() in valid:
                return valid[value.lower()]

        raise ValueError

    def parse_config(self, config_data):
        """
            generic method to parse config data and also takes care of reading equivalent env var
        """

        config_section_name = getattr(self.__class__, "config_section_name")

        if config_section_name is None:
            raise KeyError(f"Class '{self.__class__.__name__}' is missing 'config_section_name' attribute")

        for config_option in [x for x in vars(self.__class__) if x not in self.__class__.not_config_vars]:

            var_config = getattr(self.__class__, config_option)

            if not isinstance(var_config, dict):
                continue

            var_type = var_config.get("type", str)
            var_alt = var_config.get("alt")
            var_default = var_config.get("default")

            config_value = config_data.get(config_section_name, config_option, fallback=None)
            if config_value is None and var_alt is not None:
                config_value = config_data.get(config_section_name, var_alt, fallback=None)

            config_value = os.environ.get(f"{config_section_name}_{config_option}".upper(), config_value)

            if config_value is not None and var_type == bool:
                try:
                    config_value = self.to_bool(config_value)
                except ValueError:
                    log.error(f"Unable to parse '{config_value}' for '{config_option}' as bool")
                    config_value = var_default

            elif config_value is not None and var_type == int:
                try:
                    config_value = int(config_value)
                except ValueError:
                    log.error(f"Unable to parse '{config_value}' for '{config_option}' as int")
                    config_value = var_default

            else:
                if config_value is None:
                    config_value = var_default

            debug_value = config_value
            if isinstance(debug_value, str) and config_option in self.sensitive_keys:
                debug_value = config_value[0:3] + "***"

            log.debug(f"Config: {config_section_name}.{config_option} = {debug_value}")

            setattr(self, config_option, config_value)
