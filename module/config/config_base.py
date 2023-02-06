# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.common.misc import grab
from module.common.logging import get_logger
from module.config.config_parser import ConfigParser
from module.config.config_option import ConfigOption

log = get_logger()


class ConfigBase:
    """
        Base class to parse config data
    """

    section_name = None

    options = list()

    def __init__(self):

        config = ConfigParser()
        if config.parsing_finished is True:
            self.config_content = config.content

    def parse(self, do_log: bool = True):

        if self.section_name is None:
            raise KeyError(f"Class '{self.__class__.__name__}' is missing 'section_name' attribute")

        for config_object in self.options:

            if not isinstance(config_object, ConfigOption):
                continue

            config_value = grab(self.config_content, f"{self.section_name}.{config_object.key}")

            alt_key_used = False
            if config_value is None and config_object.alt_key is not None:
                alt_key_used = True
                config_value = grab(self.config_content, f"{self.section_name}.{config_object.alt_key}")

            # check for deprecated settings
            if config_object.deprecated is True:
                log_text = f"Setting '{config_object.key}' is deprecated and will be removed soon."
                if len(config_object.deprecation_message) > 0:
                    log_text += " " + config_object.deprecation_message
                if do_log:
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
                if do_log:
                    log.warning(log_text)
                continue

            # set value
            config_object.set_value(config_value)

        options = dict()
        for config_object in self.options:
            if isinstance(config_object, ConfigOption) and config_object.removed is False:
                if do_log:
                    log.debug(f"Config: {self.section_name}.{config_object.key} = {config_object.sensitive_value}")
                options[config_object.key] = config_object.value

        for option_key in grab(self.config_content, self.section_name, fallback=dict()).keys():
            if option_key not in options:
                if do_log:
                    log.warning(f"Found unknown config option '{option_key}' for '{self.section_name}' config")

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
