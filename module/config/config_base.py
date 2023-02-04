# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import os

from module.common.misc import do_error_exit, grab
from module.common.logging import get_logger
from module.config.config_files import ConfigFilesParser
from module.config.config_option import ConfigOption

log = get_logger()


class ConfigBase:
    """
        Base class to parse config data
    """

    env_var_prefix = "NBS"
    section_name = None

    options = list()

    def __init__(self, config_file_handler: ConfigFilesParser):

        if not isinstance(config_file_handler, ConfigFilesParser):
            do_error_exit("config data is not a config parser object")

        self._parse_config_data(config_file_handler.content)

    def _parse_config_data(self, config_data):
        """
            generic method to parse config data and also takes care of reading equivalent env var
        """

        if self.section_name is None:
            raise KeyError(f"Class '{self.__class__.__name__}' is missing 'section_name' attribute")

        for config_object in self.options:

            if not isinstance(config_object, ConfigOption):
                continue

            config_value = grab(config_data, f"{self.section_name}.{config_object.key}")

            alt_key_used = False
            if config_value is None and config_object.alt_key is not None:
                alt_key_used = True
                config_value = grab(config_data, f"{self.section_name}.{config_object.alt_key}")

            # check for deprecated settings
            if config_object.deprecated is True:
                log_text = f"Setting '{config_object.key}' is deprecated and will be removed soon."
                if len(config_object.deprecation_message) > 0:
                    log_text += " " + config_object.deprecation_message
                log.warning(log_text)

            # check for removed settings
            if config_value is not None and config_object.removed is True:
                object_key = config_object.key
                if alt_key_used is True:
                    object_key = config_object.alt_key
                log_text = f"Setting '{object_key}' has been removed " \
                           f"but is still defined in config section '{self.section_name}'."
                if len(config_object.deprecation_message) > 0:
                    log_text += " " + config_object.deprecation_message
                log.warning(log_text)
                continue

            # parse env
            env_var_name = f"{self.env_var_prefix}_{self.section_name}_{config_object.key}".upper()
            config_value = os.environ.get(env_var_name, config_value)

            # set value
            config_object.set_value(config_value)

    def parse(self):

        options = dict()
        for config_object in self.options:
            if isinstance(config_object, ConfigOption) and config_object.removed is False:
                log.debug(f"Config: {self.section_name}.{config_object.key} = {config_object.sensitive_value}")
                options[config_object.key] = config_object.value

        return ConfigOptions(**options)


class ConfigOptions:

    def __init__(self, **kwargs):
        for name in kwargs:
            setattr(self, name, kwargs[name])

    def __eq__(self, other):
        if not isinstance(other, ConfigOptions):
            return NotImplemented
        return vars(self) == vars(other)

    def __contains__(self, key):
        return key in self.__dict__
