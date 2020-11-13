
import pprint

import json

from ipaddress import ip_address, ip_network, ip_interface, IPv6Network, IPv4Network, IPv4Address, IPv6Address


from module.netbox.object_classes import *
from module.common.logging import get_logger
from module.common.support import perform_ptr_lookups

log = get_logger()

class NetBoxInventory:

    base_structure = dict()
    resolved_dependencies = list()

    primary_tag = None

    def __init__(self):
        for object_type in NetBoxObject.__subclasses__():

            self.base_structure[object_type.name] = list()


    def get_by_id(self, object_type, id=None):

        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a sub class of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))

        if id is None or self.base_structure[object_type.name] is None:
            return None

        for object in self.base_structure[object_type.name]:

            if object.nb_id == id:
                return object


    def get_by_data(self, object_type, data=None):

        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a sub class of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))


        if data is None or len(self.get_all_items(object_type)) == 0:
            return

        if not isinstance(data, dict):
            raise ValueError(f"Attribute data must be type 'dict' got: {data}")

        # shortcut if data contains valid id
        data_id = data.get("id")
        if data_id is not None and data_id != 0:
            return self.get_by_id(object_type, id=data_id)

        # try to find by name
        if data.get(object_type.primary_key) is not None:
            object_name_to_find = None
            results = list()
            for object in self.get_all_items(object_type):

                if object_name_to_find is None:
                    object_name_to_find = object.get_display_name(data, including_second_key=True)

                if object_name_to_find == object.get_display_name(including_second_key=True):
                    return object

        # try to match all data attributes
        else:

            for object in self.get_all_items(object_type):
                all_items_match = True
                for attr_name, attr_value in data.items():

                    if object.data.get(attr_name) != attr_value:
                        all_items_match = False
                        break

                if all_items_match == True:
                    return object

        return None

    def add_item_from_netbox(self, object_type, data=None):
        """
        only to be used if data is read from NetBox and added to inventory
        """

        # create new object
        new_object = object_type(data, read_from_netbox=True, inventory=self)

        # add to inventory
        self.base_structure[object_type.name].append(new_object)

        return

    def add_update_object(self, object_type, data=None, read_from_netbox=False, source=None):

        if data is None:
            # ToDo:
            #   * proper error handling
            log.error("NO DATA")
            return

        this_object = self.get_by_data(object_type, data=data)

        if this_object is None:
            this_object = object_type(data, read_from_netbox=read_from_netbox, inventory=self, source=source)
            self.base_structure[object_type.name].append(this_object)
            if read_from_netbox is False:
                log.debug(f"Created new {this_object.name} object: {this_object.get_display_name()}")

        else:
            this_object.update(data, read_from_netbox=read_from_netbox, source=source)
            log.debug2("Updated %s object: %s" % (this_object.name, this_object.get_display_name()))

        return this_object

    def resolve_relations(self):

        log.debug("Start resolving relations")
        for object_type in NetBoxObject.__subclasses__():

            for object in self.get_all_items(object_type):

                object.resolve_relations()

        log.debug("Finished resolving relations")

    def get_all_items(self, object_type):

        if object_type not in NetBoxObject.__subclasses__():
            raise ValueError("'%s' object must be a sub class of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))

        return self.base_structure.get(object_type.name, list())

    def get_all_interfaces(self, object):

        if not isinstance(object, (NBVMs, NBDevices)):
            raise ValueError(f"Object must be a '{NBVMs.name}' or '{NBDevices.name}'.")

        interfaces = list()
        if isinstance(object, NBVMs):
            for int in self.get_all_items(NBVMInterfaces):
                if grab(int, "data.virtual_machine") == object:
                    interfaces.append(int)

        if isinstance(object, NBDevices):
            for int in self.get_all_items(NBInterfaces):
                if grab(int, "data.device") == object:
                    interfaces.append(int)

        return interfaces

    def tag_all_the_things(self, netbox_handler):

        # ToDo:
        # * DONE: add main tag to all objects retrieved from a source
        # * Done: add source tag all objects of that source
        # * check for orphaned objects
        #   * DONE: objects tagged by a source but not present in source anymore (add)
        #   * DONE: objects tagged as orphaned but are present again (remove)


        for object_type in NetBoxObject.__subclasses__():

            for object in self.get_all_items(object_type):

                # if object was found in source
                if object.source is not None:
                    object.add_tags([netbox_handler.primary_tag, object.source.source_tag])

                    # if object was orphaned remove tag again
                    if netbox_handler.orphaned_tag in object.get_tags():
                        object.remove_tags(netbox_handler.orphaned_tag)

                # if object was tagged by this program in previous runs but is not present
                # anymore then add the orphaned tag
                else:
                    if netbox_handler.primary_tag in object.get_tags():
                        object.add_tags(netbox_handler.orphaned_tag)

    def query_ptr_records_for_all_ips(self):

        log.debug("Starting to look up PTR records for IP addresses")

        # store IP addresses to look them up in bulk
        ip_lookup_dict = dict()

        # iterate over all IP addresses and try to match them to a prefix
        for ip in self.get_all_items(NBIPAddresses):

            # ignore IPs which are not handled by any source
            if ip.source is None:
                continue

            # get IP and prefix length
            ip_a = grab(ip, "data.address", fallback="").split("/")[0]

            # check if we meant to look up DNS host name for this IP
            if grab(ip, "source.dns_name_lookup", fallback=False) is True:

                if ip_lookup_dict.get(ip.source) is None:

                    ip_lookup_dict[ip.source] = {
                        "ips": list(),
                        "servers": grab(ip, "source.custom_dns_servers")
                    }

                ip_lookup_dict[ip.source].get("ips").append(ip_a)


        # now perform DNS requests to look up DNS names for IP addresses
        for source, data in ip_lookup_dict.items():

            if len(data.get("ips")) == 0:
                continue

            # get DNS names for IP addresses:
            records = perform_ptr_lookups(data.get("ips"), data.get("servers"))

            for ip in self.get_all_items(NBIPAddresses):

                if ip.source != source:
                    continue

                ip_a = grab(ip, "data.address", fallback="").split("/")[0]

                dns_name = records.get(ip_a)

                if dns_name is not None:

                    ip.update(data = {"dns_name": dns_name})

        log.debug("Finished to look up PTR records for IP addresses")

    def to_dict(self):

        output = dict()
        for nb_object_class in NetBoxObject.__subclasses__():

            output[nb_object_class.name] = list()

            for object in self.base_structure[nb_object_class.name]:
                output[nb_object_class.name].append(object.to_dict())

        return output

    def __str__(self):

        return json.dumps(self.to_dict(), sort_keys=True, indent=4)

# EOF
