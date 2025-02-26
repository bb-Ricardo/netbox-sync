# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

# define all available sources here
from module.sources.vmware.connection import VMWareHandler
from module.sources.check_redfish.import_inventory import CheckRedfish

from module.common.logging import get_logger
from module.netbox.inventory import NetBoxInventory
from module.config.parser import ConfigParser
from module.config.base import ConfigOptions
from module.config import source_config_section_name

# list of valid sources
valid_sources = [VMWareHandler, CheckRedfish]


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
        pre if validating a class, post if validating an initialized object

    """

    necessary_attributes = {
        "dependent_netbox_objects": list,
        "init_successful": bool,
        "inventory": NetBoxInventory,
        "name": str,
        "settings": ConfigOptions,
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

        value = getattr(source_class_object, attr, None)

        if not isinstance(value, value_type):
            raise ValueError(f"Value for attribute '{attr}' needs to be {value_type}")

        if value_type in [list, str] and len(value) == 0:
            raise ValueError(f"Value for attribute '{attr}' can't be empty.")


def instantiate_sources():
    """
    Instantiate a source handler and add necessary attributes. Also
    validate source handler on pre- and post-initialization.

    Returns
    -------
    source handler object: instantiated source handler
    """

    config = ConfigParser()
    inventory = NetBoxInventory()

    log = get_logger()

    # first validate all available sources
    for possible_source_class in valid_sources:
        validate_source(possible_source_class)

    sources = list()

    source_config = dict()
    if isinstance(config.content, dict):
        source_config = config.content.get(source_config_section_name)

    for source_name, source_config in source_config.items():

        source_config_type = source_config.get("type")
        if source_config_type is None:
            log.error(f"Source {source_name} option 'type' is undefined")
            continue

        source_class = None
        for possible_source_class in valid_sources:
            validate_source(possible_source_class)

            if possible_source_class.implements(source_config_type):
                source_class = possible_source_class
                break

        if source_class is None:
            log.error(f"Unknown source type '{source_config_type}' defined for '{source_name}'")
            continue

        source_handler = source_class(name=source_name)

        validate_source(source_handler, "post")

        # add to list of source handlers
        if source_handler.init_successful is True:
            sources.append(source_handler)

        inventory.add_source(source_handler)

    return sources

# EOF
