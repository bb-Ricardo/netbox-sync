# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.common.misc import grab
from module.common.logging import get_logger
from module.config.parser import ConfigParser
from module.config.option import ConfigOption
from module.config.group import ConfigOptionGroup

log = get_logger()


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

    def __getattr__(self, item):
        if item in self:
            return getattr(self, item)
        return None

class ConfigBase:
    """
        Base class to parse config data
    """

    section_name = None
    _parsing_failed = False

    options = list()
    config_content = dict()

    def __init__(self):

        config = ConfigParser()
        if config.parsing_finished is True:
            self.config_content = config.content

    # stub function, needs to implemented in each config class
    def validate_options(self):
        pass

    def set_validation_failed(self):
        self._parsing_failed = True

    def get_option_by_name(self, name: str) -> ConfigOption:
        for option in self.options:
            if option.key == name:
                return option

    def parse(self, do_log: bool = True):

        def _log(handler, message):
            if do_log is True:
                handler(message)

        def get_value(key: str = None):

            separator = "|"
            path = [self.section_name]
            source_name = getattr(self, "source_name", None)
            if source_name is not None:
                path.append(source_name)
            if key is not None:
                path.append(key)

            return grab(self.config_content, separator.join(path), separator=separator)

        if self.section_name is None:
            raise KeyError(f"Class '{self.__class__.__name__}' is missing 'section_name' attribute")

        config_option_location = self.section_name
        if hasattr(self, "source_name"):
            config_option_location += f".{self.source_name}"

        options_returned = list()

        input_options = list()
        for config_object in self.options:

            if isinstance(config_object, ConfigOption):
                input_options.append(config_object)
            elif isinstance(config_object, ConfigOptionGroup):
                input_options.extend(config_object.options)

        for config_object in input_options:

            if not isinstance(config_object, ConfigOption):
                continue

            config_value = get_value(config_object.key)

            alt_key_used = False
            if config_value is None and config_object.alt_key is not None:
                alt_key_used = True
                config_value = get_value(config_object.alt_key)

            # check for deprecated settings
            if config_value is not None and config_object.deprecated is True:
                log_text = f"Setting '{config_object.key}' in '{config_option_location}' is deprecated " \
                           "and will be removed soon."
                if len(config_object.deprecation_message) > 0:
                    log_text += " " + config_object.deprecation_message
                _log(log.warning, log_text)

            # check for removed settings
            if config_value is not None and config_object.removed is True:
                object_key = config_object.key
                if alt_key_used is True:
                    object_key = config_object.alt_key
                log_text = f"Setting '{object_key}' has been removed " \
                           f"but is still defined in config section '{config_option_location}'."
                if len(config_object.deprecation_message) > 0:
                    log_text += " " + config_object.deprecation_message
                _log(log.warning, log_text)
                continue

            if config_object.removed is True:
                continue

            # set value
            config_object.set_value(config_value)

            _log(log.debug, f"Config: {config_option_location}.{config_object.key} = {config_object.sensitive_value}")

            if config_object.mandatory is True and config_object.value is None:
                self._parsing_failed = True
                _log(log.error, f"Config option '{config_object.key}' in "
                                f"'{config_option_location}' can't be empty/undefined")

            if config_object.parsing_failed is True:
                self._parsing_failed = True

            options_returned.append(config_object)

        self.options = options_returned

        # check for unknown config options
        config_options = get_value()
        if not isinstance(config_options, dict):
            config_options = dict()

        for option_key in config_options.keys():
            if option_key not in [x.key for x in input_options]:
                _log(log.warning, f"Found unknown config option '{option_key}' for '{config_option_location}' config")

        # validate parsed config
        self.validate_options()

        if self._parsing_failed is True:
            log.error("Config validation failed. Exit!")
            exit(1)

        return ConfigOptions(**{x.key: x.value for x in self.options})
