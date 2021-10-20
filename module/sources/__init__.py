# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2021 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

# define all available sources here
from .vmware.connection import VMWareHandler
from .check_redfish.import_inventory import CheckRedfish

# list of valid sources
valid_sources = [VMWareHandler, CheckRedfish]

###############
from module.common.configuration import get_config
from module.common.logging import get_logger
from module.netbox.inventory import NetBoxInventory


def validate_source(source_class_object=None, state="pre"):
    """
    Validate class and object attributes of a $source_class_object
    Check if all needed attributes in class definition are present before initialization.
    Check if all needed attributes and their type in object are present
    after initialization

    Parameters
    ----------
    source_class_object: Source handler class/object
        class/object to investigate
    state: str
        pre if validating a class, post if validating a initialized object

    """

    necessary_attributes = {
        "dependent_netbox_objects": list,
        "init_successful": bool,
        "inventory": NetBoxInventory,
        "name": str,
        "settings": dict,
        "source_tag": str,
        "source_type": str,
    }

    for attr in necessary_attributes.keys():

        # raise exception if attribute not present
        getattr(source_class_object, attr)

    if state == "pre":
        return

    # post initialization validation
    for attr, value_type in necessary_attributes.items():

        value = getattr(source_class_object, attr)

        if not isinstance(value, value_type):
            raise ValueError(f"Value for attribute '{attr}' needs to be {value_type}")

        if value_type in [list, str] and len(value) == 0:
            raise ValueError(f"Value for attribute '{attr}' can't be empty.")


def instantiate_sources(config_handler=None, inventory=None):
    """
    Instantiate a source handler and add necessary attributes. Also
    validate source handler pre and post initialization.

    Parameters
    ----------
    config_handler: ConfigParser
        a config file handler to read config data from
    inventory: inventory object
        inventory to be passed to source handler

    Returns
    -------
    source handler object: instantiated source handler
    """

    log = get_logger()

    if config_handler is None:
        raise Exception("No config handler defined!")

    if inventory is None:
        raise Exception("No inventory defined!")

    # first validate all available sources
    for possible_source_class in valid_sources:
        validate_source(possible_source_class)

    sources = list()

    # iterate over sources and validate them
    for source_section in config_handler.sections():

        # a source section needs to start with "source/"
        if not source_section.startswith("source/"):
            continue

        # get type of source
        source_type = config_handler.get(source_section, "type", fallback=None)

        if source_type is None:
            log.error(f"Source {source_section} option 'type' is undefined")

        source_class = None
        for possible_source_class in valid_sources:
            validate_source(possible_source_class)
            source_class_type = getattr(possible_source_class, "source_type", None)
            if source_class_type is None:
                raise AttributeError("'%s' class attribute 'source_type' not defined." % source_class_type.__name__)

            if source_class_type == source_type:
                source_class = possible_source_class
                break

        if source_class is None:
            log.error(f"Unknown source type '{source_type}' defined for '{source_section}'")
            continue

        source_config = get_config(config_handler,
                                   section=source_section,
                                   valid_settings=source_class.settings,
                                   deprecated_settings=getattr(source_class, "deprecated_settings", None),
                                   removed_settings=getattr(source_class, "removed_settings", None)
                                   )

        source_handler = source_class(name=source_section.replace("source/", ""),
                                      inventory=inventory,
                                      settings=source_config)

        validate_source(source_handler, "post")

        # add to list of source handlers
        if source_handler.init_successful is True:
            sources.append(source_handler)
        elif getattr(source_handler, "enabled") is False:
            inventory.add_disabled_source_tag(source_handler.source_tag)

    return sources

# EOF
