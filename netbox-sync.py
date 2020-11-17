#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#  Copyright (c) 2020 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

self_description = """
Sync objects from various sources to Netbox
"""


from datetime import datetime

from module.common.misc import grab, get_relative_time
from module.common.cli_parser import parse_command_line
from module.common.logging import setup_logging
from module.common.configuration import get_config_file, open_config_file, get_config
from module.netbox.connection import NetBoxHandler
from module.netbox.inventory import NetBoxInventory
from module.netbox.object_classes import *
from module.sources import instantiate_sources


import pprint

__version__ = "0.0.1"
__version_date__ = "2020-10-01"
__author__ = "Ricardo Bartels <ricardo.bartels@telekom.de>"
__description__ = "NetBox Sync"
__license__ = "MIT"
__url__ = "https://github.com/bb-ricardo/netbox-sync"


default_log_level = "INFO"
default_config_file_path = "./settings.ini"


"""
ToDo:
* documentation
    * describe migration (rename tags)
    * proper naming to assign sites to clusters
    * connection details
    * installation
    * Standalone Host declaration
    * source module structure
    * how a vm is picked
    * how interfaces are named
    * how objects are abducted (taken over by this program)
    * thanks to original Owner of ideas
    * ensure NTP is set up properly between all instances (pruning delay)
* test all log levels
* check/fix ToDo statements
"""


def main():

    start_time = datetime.now()

    # parse command line
    args = parse_command_line(self_description=self_description,
                              version=__version__,
                              version_date=__version_date__,
                              default_config_file_path=default_config_file_path)

    # get config file path
    config_file = get_config_file(args.config_file)

    # get config handler
    config_handler = open_config_file(config_file)

    # get logging configuration

    # set log level
    log_level = default_log_level
    # config overwrites default
    log_level = config_handler.get("common", "log_level", fallback=log_level)
    # cli option overwrites config file
    log_level = grab(args, "log_level", fallback=log_level)

    log_file = None
    if bool(config_handler.getboolean("common", "log_to_file", fallback=False)) is True:
        log_file = config_handler.get("common", "log_file", fallback=None)

    # setup logging
    log = setup_logging(log_level, log_file)

    # now we are ready to go
    log.info("Starting " + __description__)
    log.debug(f"Using config file: {config_file}")

    # initialize an empty inventory which will be used to hold and reference all objects
    inventory = NetBoxInventory()

    # get config for NetBox handler
    netbox_settings = get_config(config_handler, section="netbox", valid_settings=NetBoxHandler.settings)

    # establish NetBox connection
    nb_handler = NetBoxHandler(settings=netbox_settings, inventory=inventory)

    # if purge was selected we go ahead and remove all items which were managed by this tools
    if args.purge is True:

        if args.dry_run is True:
            do_error_exit("Purge not available with option 'dry_run'")

        nb_handler.just_delete_all_the_things()

        # that's it, we are done here
        exit(0)

    # instantiate source handlers and get attributes
    log.info("Initializing sources")
    sources = instantiate_sources(config_handler, inventory)

    # all sources are unavailable
    if len(sources) == 0:
        log.error("No working sources found. Exit.")
        exit(1)

    # collect all dependent object classes
    log.info("Querying necessary objects from Netbox. This might take a while.")
    for source in sources:
        nb_handler.query_current_data(source.dependent_netbox_objects)

    log.info("Finished querying necessary objects from Netbox")

    # resolve object relations within the initial inventory
    inventory.resolve_relations()

    # initialize basic data needed for syncing
    nb_handler.initialize_basic_data()

    # loop over sources and patch netbox data
    for source in sources:
        source.apply()

    # add/remove tags to/from all inventory items
    inventory.tag_all_the_things(nb_handler)

    # update all IP addresses
    inventory.query_ptr_records_for_all_ips()

    if args.dry_run is True:
        log.info("This is a dry run and we stop here. Running time: %s" %
                 get_relative_time(datetime.now() - start_time))
        exit(0)

    # update data in NetBox
    nb_handler.update_instance()

    # prune orphaned objects from NetBox
    nb_handler.prune_data()

    # finish
    log.info("Completed NetBox Sync in %s" % get_relative_time(datetime.now() - start_time))


if __name__ == "__main__":
    main()

# EOF
