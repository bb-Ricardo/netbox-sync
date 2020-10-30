
import pprint

import json

from ipaddress import ip_address, ip_network, ip_interface, IPv6Network, IPv4Network, IPv4Address, IPv6Address


from module.netbox.object_classes import *
from module.common.logging import get_logger

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


        if data is None:
            return None

        if self.base_structure[object_type.name] is None:
            return None

        if not isinstance(data, dict):
            # ToDo:
            # * proper handling
            log.error("data is not dict")
            pprint.pprint(data)
            exit(0)

        # shortcut if data contains valid id
        data_id = data.get("id")
        if data_id is not None and data_id != 0:
            return self.get_by_id(object_type, id=data_id)

        # try to find by name
        if data.get(object_type.primary_key) is not None:
            object_name_to_find = None
            results = list()
            for object in self.base_structure[object_type.name]:

                # Todo:
                #   * try to compare second key if present.

                if object_name_to_find is None:
                    object_name_to_find = object.get_display_name(data)
                    #print(f"get_by_data(): Object Display Name: {object_name_to_find}")

                if object_name_to_find == object.get_display_name():
                    results.append(object)

            # found exactly one match
            # ToDo:
            # * add force secondary key if one object has a secondary key

            if len(results) == 1:
                #print(f"found exact match: {object_name_to_find}")
                return results[0]

            # compare secondary key
            elif len(results) > 1:

                object_name_to_find = None
                for object in results:

                    if object_name_to_find is None:
                        object_name_to_find = object.get_display_name(data, including_second_key=True)
                        #print(f"get_by_data(): Object Display Name: {object_name_to_find}")

                    if object_name_to_find == object.get_display_name(including_second_key=True):
                        return object

        # try to match all data attributes
        else:

            for object in self.base_structure[object_type.name]:
                all_items_match = True
                for attr_name, attr_value in data.items():

                    if object.data.get(attr_name) != attr_value:
                        all_items_match = False
                        break

                if all_items_match == True:
                    return object

                """
                if data.get(object_type.primary_key) is not None and \
                    object.resolve_attribute(object_type.primary_key) == object.resolve_attribute(object_type.primary_key, data=data):

                    # object type has a secondary key, lets check if it matches
                    if getattr(object_type, "secondary_key", None) is not None and data.get(object_type.secondary_key) is not None:

                        if object.resolve_attribute(object_type.secondary_key) == object.resolve_attribute(object_type.secondary_key, data=data):
                            return_data.append(object)

                    # object has no secondary key but the same name, add to list
                    else:
                        return_data.append(object)
                """
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
            log.debug("Updated %s object: %s" % (this_object.name, this_object.get_display_name()))

        return this_object

    def resolve_relations(self):

        log.debug("Start resolving relations")
        for object_type in NetBoxObject.__subclasses__():

            for object in self.base_structure.get(object_type.name, list()):

                object.resolve_relations()

        log.debug("Finished resolving relations")

    def get_all_items(self, object_type):

        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a sub class of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))

        return self.base_structure.get(object_type.name, list())


    def tag_all_the_things(self, sources, netbox_handler):

        # ToDo:
        # * DONE: add main tag to all objects retrieved from a source
        # * Done: add source tag all objects of that source
        # * check for orphaned objects
        #   * DONE: objects tagged by a source but not present in source anymore (add)
        #   * DONE: objects tagged as orphaned but are present again (remove)

        source_tags = [x.source_tag for x in sources]

        for object_type in NetBoxObject.__subclasses__():

            if self.base_structure[object_type.name] is None:
                continue

            for object in self.base_structure[object_type.name]:

                # if object was found in source
                if object.source is not None:
                    object.add_tags([netbox_handler.primary_tag, object.source.source_tag])

                    # if object was orphaned remove tag again
                    if netbox_handler.orphaned_tag in object.get_tags():
                        object.remove_tags(netbox_handler.orphaned_tag)

                # if object was tagged by a source in previous runs but is not present
                # anymore then add the orphaned tag
                else:
                    for source_tag in source_tags:
                        if source_tag in object.get_tags():
                            object.add_tags(netbox_handler.orphaned_tag)

    def update_all_ip_addresses(self):


        # ToDo:
        #   * get DNS names for ip addresses and set them as well

        def _return_longest_match(ip_to_match=None, list_of_prefixes=None):

            if ip_to_match is None or list_of_prefixes is None:
                return

            if not isinstance(ip_to_match, (IPv4Address, IPv6Address)):
                try:
                    ip_to_match = ip_address(ip_to_match)
                except ValueError:
                    return

            if not isinstance(list_of_prefixes, list):
                return

            sanatized_list_of_prefixes = list()
            for prefix in list_of_prefixes:

                if not isinstance(prefix, (IPv4Network, IPv6Network)):
                    try:
                        sanatized_list_of_prefixes.append(ip_network(prefix))
                    except ValueError:
                        return
                else:
                    sanatized_list_of_prefixes.append(prefix)

            current_longest_matching_prefix_length = 0
            current_longest_matching_prefix = None

            for prefix in sanatized_list_of_prefixes:

                if ip_to_match in prefix and \
                    prefix.prefixlen >= current_longest_matching_prefix_length:

                    current_longest_matching_prefix_length = prefix.prefixlen
                    current_longest_matching_prefix = prefix

            return current_longest_matching_prefix


        log.info("Trying to math IPs to existing prefixes")

        all_prefixes = self.get_all_items(NBPrefixes)
        all_addresses = self.get_all_items(NBIPAddresses)

        # prepare prefixes
        prefixes_per_site = dict()
        for this_prefix in all_prefixes:

            # name of the site or None (as string)
            prefix_site = str(grab(this_prefix, "data.site.data.name"))

            if prefixes_per_site.get(prefix_site) is None:
                prefixes_per_site[prefix_site] = list()

            prefixes_per_site[prefix_site].append(ip_network(grab(this_prefix, "data.prefix")))

        # iterate over all IP addresses
        for ip in all_addresses:

            if ip.source is None:
                continue

            if grab(ip, "data.assigned_object_id") is None:
                continue

            log.debug2("Trying to find prefix for IP: %s" % ip.get_display_name())

            object_site = "None"
            # name of the site or None (as string)
            # -> NBInterfaces -> NBDevices -> NBSites
            if grab(ip, "data.assigned_object_type") == "dcim.interface":
                object_site = str(grab(ip, "data.assigned_object_id.data.device.data.site.data.name"))
            # -> NBVMInterfaces -> NBVMs -> NBClusters -> NBSites
            elif grab(ip, "data.assigned_object_type") == "virtualization.vminterface":
                object_site = str(grab(ip, "data.assigned_object_id.data.virtual_machine.data.cluster.data.site.data.name"))

            log.debug2(f"Site name for this IP: {object_site}")

            ip_a = grab(ip, "data.address", fallback="").split("/")[0]

            # test site prefixes first
            matching_site_name = object_site
            matching_site_prefix = _return_longest_match(ip_a, prefixes_per_site.get(object_site))

            # nothing was found then check prefixes with no
            if matching_site_prefix is None:

                matching_site_name = "undefined"
                matching_site_prefix = _return_longest_match(ip_a, prefixes_per_site.get("None"))

            # no matching prefix found, give up
            if matching_site_prefix is None:
                continue

            log.debug2(f"Found IP '{ip_a}' matches prefix '{matching_site_prefix}' in site '{matching_site_name}'")

            for prefix in all_prefixes:

                this_prefix_site_name = grab(prefix, "data.site.data.name", fallback="undefined")
                matching_site_prefix = str(matching_site_prefix)

                # make sure we found the correct prefix object
                if this_prefix_site_name == matching_site_name and \
                    grab(prefix, "data.prefix") == matching_site_prefix:

                    data = dict()

                    vrf = grab(prefix, "data.vrf.id")
                    tenant = grab(prefix, "data.tenant.id")

                    if vrf is not None and str(vrf) != str(grab(ip, "data.vrf.id")):
                        data["vrf"] = vrf

                    # only overwrite tenant if not already defined
                    if tenant is not None and grab(ip, "data.tenant.id") is None and str(tenant) != str(grab(ip, "data.tenant.id")):
                        data["tenant"] = tenant

                    if len(data.keys()) > 0:
                        ip.update(data=data)
                    break


    def set_primary_ips(self):

        pass


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
