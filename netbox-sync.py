#!/usr/bin/env python3
self_description = \
"""
Sync objects from various sources to Netbox
"""


from datetime import datetime

from module.common.misc import grab, get_relative_time
from module.common.cli_parser import parse_command_line
from module.common.logging import setup_logging
from module.common.configuration import get_config_file, open_config_file, get_config
from module.netbox.connection import NetBoxHandler
from module.netbox.inventory import NetBoxInventory
from module.netbox.object_classes import NBPrefixes
from module.sources import instanciate_sources


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
* primary IP assignment
* test all log levels
* check for ToDo/Fixme/pprint statements
"""

def main():

    start_time = datetime.now()

    sources = list()

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

    # get config for netbox handler
    netbox_settings = get_config(config_handler, section="netbox", valid_settings=NetBoxHandler.settings)

    # establish NetBox connection
    NB_handler = NetBoxHandler(settings=netbox_settings, inventory=inventory)

    # if purge was selected we go ahead and remove all items which were managed by this tools
    if args.purge is True:
        NB_handler.just_delete_all_the_things()

        # that's it, we are done here
        exit(0)

    # instantiate source handlers and get attributes
    log.info("Initializing sources")
    sources = instanciate_sources(config_handler, inventory)

    # all sources are unavailable
    if len(sources) == 0:
        do_error_exit("No working sources found. Exit.")

    # collect all dependent object classes
    netbox_objects_to_query = list()
    for source in sources:
        netbox_objects_to_query.extend(source.dependend_netbox_objects)

    # we need to collect prefixes as well to so which IP belongs to which prefix
    netbox_objects_to_query.append(NBPrefixes)

    # request NetBox data
    log.info("Querying necessary objects from Netbox. This might take a while.")
    NB_handler.query_current_data(list(set(netbox_objects_to_query)))
    log.info("Finished querying necessary objects from Netbox")

    # resolve object relations within the initial inventory
    inventory.resolve_relations()

    # initialize basic data needed for syncing
    NB_handler.inizialize_basic_data()

    # loop over sources and patch netbox data
    for source in sources:
        source.apply()

    # add/remove tags to/from all inventory items
    inventory.tag_all_the_things(NB_handler)

    # update all IP addresses
    inventory.update_all_ip_addresses()

    # update data in NetBox
    NB_handler.update_instance()

    # now see where we can update primary IPs
    inventory.set_primary_ips()

    # update data in NetBox again
    NB_handler.update_instance()

    # prune orphaned objects from NetBox
    NB_handler.prune_data()

    # finish
    log.info("Completed NetBox Sync in %s" % get_relative_time(datetime.now() - start_time))


if __name__ == "__main__":
    main()

# EOF
