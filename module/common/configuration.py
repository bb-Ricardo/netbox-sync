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


def get_config(config_handler=None, section=None, valid_settings=None, deprecated_settings=None, removed_settings=None):
    """
    read config items from a defined section

    Parameters
    ----------
    config_handler: ConfigParser
        a config file handler to read config data from
    section: str
        name of the section to read
    valid_settings: dict
        a dictionary with valid config items to read from this section.
        key: is the config item name
        value: default value if config option is undefined
    deprecated_settings: dict
        a dictionary of deprecated config settings
        key: name of deprecated setting
        value: name of superseding setting or None if no substitution applies
    removed_settings: dict
        a dictionary of removed setting names
        key: name of removed setting
        value: name of superseding setting or None if no substitution applies

    Returns
    -------
    dict:   parsed config items from defined $section

    """

    def get_config_option(this_section, item, default=None):

        if isinstance(default, bool):
            value = config_handler.getboolean(this_section, item, fallback=default)
        elif isinstance(default, int):
            value = config_handler.getint(this_section, item, fallback=default)
        else:
            value = config_handler.get(this_section, item, fallback=default)

        if value == "":
            value = None

        config_dict[item] = value

        # take care of logging sensitive data
        for sensitive_item in ["token", "pass"]:

            if sensitive_item.lower() in item.lower():
                value = value[0:3] + "***"

        log.debug(f"Config: {this_section}.{item} = {value}")

    config_dict = {}

    if valid_settings is None:
        log.error("No valid settings passed to config parser!")

    # read specified section section
    if section is None:
        return config_dict

    if section not in config_handler.sections():
        log.error(f"Section '{section}' not found in config_file")
        return config_dict

    for config_item, default_value in valid_settings.items():
        get_config_option(section, config_item, default=default_value)

    # check for deprecated settings
    if isinstance(deprecated_settings, dict):
        for deprecated_setting, alternative_setting in deprecated_settings.items():
            if config_handler.get(section, deprecated_setting, fallback=None) is not None:
                log_text = f"Setting '{deprecated_setting}' is deprecated and will be removed soon."
                if alternative_setting is not None:
                    log_text += f" Consider changing your config to use the '{alternative_setting}' setting."
                log.warning(log_text)

    # check for removed settings
    if isinstance(removed_settings, dict):
        for removed_setting, alternative_setting in removed_settings.items():
            if config_handler.get(section, removed_setting, fallback=None) is not None:
                log_text = f"Setting '{removed_setting}' has been removed " \
                           f"but is still defined in config section '{section}'."
                if alternative_setting is not None:
                    log_text += f" You need to switch to '{alternative_setting}' setting."
                log.warning(log_text)

    return config_dict

# EOF
