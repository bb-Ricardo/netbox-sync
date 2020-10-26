#!/usr/bin/env python3
self_description = \
"""
Sync objects from various sources to Netbox
"""


from datetime import date, datetime

from module.common.misc import grab
from module.common.cli_parser import parse_command_line
from module.common.logging import setup_logging
from module.common.configuration import get_config_file, open_config_file, get_config
from module.netbox.connection import NetBoxHandler
from module.netbox.inventory import NetBoxInventory
#from module.netbox.object_classes import *

from module.sources import *


import pprint

__version__ = "0.0.1"
__version_date__ = "2020-10-01"
__author__ = "Ricardo Bartels <ricardo.bartels@telekom.de>"
__description__ = "NetBox Sync"
__license__ = "MIT"
__url__ = "https://github.com/bb-Ricardo/unknown"


default_log_level = "WARNING"
default_config_file_path = "./settings.ini"


"""
ToDo:
* host "Management" interface is Primary
* return more then one object if found more then one and add somehow to returned objects. Maybe related?
* Add purge option
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
    if grab(args, "log_level") is not None:
        log_level = grab(args, "log_level")

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
    NB_handler = NetBoxHandler(cli_args=args, settings=netbox_settings, inventory=inventory)

    # instantiate source handlers and get attributes
    sources = instanciate_sources(config_handler, inventory)

    # all sources are unavailable
    if len(sources) == 0:
        do_error_exit("No working sources found. Exit.")

    # retrieve all dependent object classes
    netbox_objects_to_query = list()
    for source in sources:
        netbox_objects_to_query.extend(source.dependend_netbox_objects)

    # request NetBox data
    NB_handler.query_current_data(list(set(netbox_objects_to_query)))

    # resolve object relations within the initial inventory
    inventory.resolve_relations()

    # initialize basic data needed for syncing
    NB_handler.inizialize_basic_data()

   # for object in inventory.get_all_items(NBIPAddresses):
   #     pprint.pprint(object.__dict__)
   #     exit(0)
    # loop over sources and patch netbox data
    for source in sources:
        source.apply()

    # add/remove tags to/from all inventory items
    inventory.tag_all_the_things(sources, NB_handler)

    #for object in inventory.get_all_items(NBVMs):
    #    print(object.get_display_name())
    """
    nb.set_primary_ips()
    # Optional tasks
    if settings.POPULATE_DNS_NAME:
        nb.set_dns_names()
    """

    # update data in NetBox
    NB_handler.update_instance()

    # finish

    log.info("Completed NetBox Sync! Total execution time %s." % (datetime.now() - start_time))


if __name__ == "__main__":
    main()
