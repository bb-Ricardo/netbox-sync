# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import json

from module.netbox import *
from module.common.misc import grab
from module.common.logging import get_logger
from module.common.support import perform_ptr_lookups

log = get_logger()


class NetBoxInventory:
    """
    Singleton class to manage an inventory of NetBoxObject objects
    """

    base_structure = dict()

    source_list = list()

    # track NetBox API version and provided it for all sources
    netbox_api_version = "0.0.0"

    def __new__(cls):
        it = cls.__dict__.get("__it__")
        if it is not None:
            return it
        cls.__it__ = it = object.__new__(cls)
        it.init()
        return it

    def init(self):

        for object_type in NetBoxObject.__subclasses__():

            self.base_structure[object_type.name] = list()

    def add_source(self, source_handler=None):
        """
        adds $source_tag to list of disabled sources

        Parameters
        ----------
        source_handler: object
            source handler object

        """
        if source_handler is not None:
            self.source_list.append(source_handler)

    def get_by_id(self, object_type, nb_id=None):
        """
        Try to find an object of $object_type with ID $id in inventory

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to find
        nb_id: int
            NetBox ID of object

        Returns
        -------
        (NetBoxObject subclass, None): return object instance if object was found, None otherwise
        """

        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a subclass of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))

        if nb_id is None or self.base_structure[object_type.name] is None:
            return None

        for this_object in self.base_structure[object_type.name]:

            if this_object.nb_id == nb_id:
                return this_object

    def get_by_data(self, object_type, data=None):
        """
        Try to find an object of $object_type which match params defined in $data

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to find
        data: dict
            params of object to match

        Returns
        -------
        (NetBoxObject subclass, None): return object instance if object was found, None otherwise
        """

        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a subclass of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))

        if data is None or len(self.get_all_items(object_type)) == 0:
            return

        if not isinstance(data, dict):
            raise ValueError(f"Attribute data must be type 'dict' got: {data}")

        # shortcut if data contains valid id
        data_id = data.get("id")
        if data_id is not None and data_id != 0:
            return self.get_by_id(object_type, nb_id=data_id)

        # try to find object by slug
        if "slug" in object_type.data_model.keys() and data.get("name") is not None:
            object_slug = NetBoxObject.format_slug(data.get("name"))
            for this_object in self.get_all_items(object_type):
                if this_object.data.get("slug") == object_slug:
                    return this_object

        # try to find by primary/secondary key
        elif data.get(object_type.primary_key) is not None:
            object_name_to_find = None
            for this_object in self.get_all_items(object_type):

                if object_name_to_find is None:
                    object_name_to_find = this_object.get_display_name(data, including_second_key=True)

                # compare lower key
                if f"{object_name_to_find}".lower() == \
                        f"{this_object.get_display_name(including_second_key=True)}".lower():

                    return this_object

        # try to match all data attributes
        else:

            for this_object in self.get_all_items(object_type):
                all_items_match = True
                for attr_name, attr_value in data.items():

                    if this_object.data.get(attr_name) != attr_value:
                        all_items_match = False
                        break

                if all_items_match is True:
                    return this_object

        return None

    def slug_used(self, object_type: NetBoxObject, slug: str) -> bool:
        """
        Determine if a slug for an object_tpe is already used

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to search
        slug: str
            slug which needs to be found

        Returns
        -------
        (bool): if slug has been used for this object type
        """

        if object_type not in NetBoxObject.__subclasses__():
            raise AttributeError("'%s' object must be a subclass of '%s'." %
                                 (object_type.__name__, NetBoxObject.__name__))

        if "slug" in object_type.data_model.keys():
            for this_object in self.get_all_items(object_type):
                if this_object.data.get("slug") == slug:
                    return True

        return False

    def add_object(self, object_type, data=None, read_from_netbox=False, source=None):
        """
        Adds a new object to the inventory.

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to add
        data: dict
            Object data to add to the inventory
        read_from_netbox: bool
            True if data was read directly from NetBox
        source: object handler of source
            the object source which should be added to the object

        Returns
        -------
        NetBoxObject child object: of the created object
        """

        # create new object
        new_object = object_type(data, read_from_netbox=read_from_netbox, inventory=self, source=source)

        # add to inventory
        self.base_structure[object_type.name].append(new_object)

        if read_from_netbox is False:
            log.info(f"Created new {new_object.name} object: {new_object.get_display_name()}")

        return new_object

    def add_update_object(self, object_type, data=None, read_from_netbox=False, source=None):
        """
        Adds new object or updates existing object with data, based on the content of data.

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to add/update
        data: dict
            data used to create a new object or update an existing object
        read_from_netbox: bool
            True if data was read directly from NetBox
        source: object handler of source
            the object source which should be added to the object

        Returns
        -------
        NetBoxObject child object: of the created/updated object
        """

        if data is None:
            log.error(f"Unable to find {object_type.name} object, parameter 'data' is 'None'")
            return None

        # try to find exiting object based on submitted data
        this_object = self.get_by_data(object_type, data=data)

        if this_object is None:
            this_object = self.add_object(object_type, data=data, read_from_netbox=read_from_netbox, source=source)

        else:
            this_object.update(data, read_from_netbox=read_from_netbox, source=source)

        return this_object

    def resolve_relations(self):
        """
        Resolve relations of all objects in the inventory. Used after data is read from NetBox.
        """

        log.debug("Start resolving relations")
        for object_type in NetBoxObject.__subclasses__():

            for this_object in self.get_all_items(object_type):

                this_object.resolve_relations()

        log.debug("Finished resolving relations")

    def get_all_items(self, object_type):
        """
        Returns list of all $object_type items inventory.

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to find

        Returns
        -------
        list: of all $object_type items
        """

        if object_type not in NetBoxObject.__subclasses__():
            raise ValueError(f"'{object_type.__name__}' object must be a subclass of '{NetBoxObject.__name__}'.")

        return self.base_structure.get(object_type.name, list())

    def get_all_interfaces(self, this_object: (NBVM, NBDevice)):
        """
        Return all interfaces items for a NBVM, NBDevice object

        Parameters
        ----------
        this_object: NBVM, NBDevice
            object instance to return interfaces for

        Returns
        -------
        list: of all interfaces found for this object
        """

        if not isinstance(this_object, (NBVM, NBDevice)):
            raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

        interfaces = list()
        if isinstance(this_object, NBVM):
            for interface in self.get_all_items(NBVMInterface):
                if grab(interface, "data.virtual_machine") == this_object:
                    interfaces.append(interface)

        if isinstance(this_object, NBDevice):
            for interface in self.get_all_items(NBInterface):
                if grab(interface, "data.device") == this_object:
                    interfaces.append(interface)

        return interfaces

    def tag_all_the_things(self, netbox_handler):
        """
        Tag all items which have been created/updated/inherited by this program
        * add main tag (NetBox: Synced) to all objects retrieved from a source
        * add source tag (source: $name) all objects of that source
        * check for orphaned objects
            * objects tagged by main tag but not present in source anymore (add)
            * objects tagged as orphaned but are present again (remove)

        Parameters
        ----------
        netbox_handler: NetBoxHandler
            the object instance of a NetBox handler to get the tag names from
        """

        all_sources_tags = [x.source_tag for x in self.source_list]
        disabled_sources_tags = \
            [x.source_tag for x in self.source_list if grab(x, "settings.enabled", fallback=False) is False]

        for object_type in NetBoxObject.__subclasses__():

            for this_object in self.get_all_items(object_type):

                this_object_tags = this_object.get_tags()

                # if object was found in source
                if this_object.source is not None:
                    # skip tagging of VLANs if vlan sync is disabled
                    if object_type == NBVLAN and \
                          grab(this_object.source, "settings.disable_vlan_sync", fallback=False) is True:
                        continue

                    this_object.add_tags([netbox_handler.primary_tag, this_object.source.source_tag])

                    # if object was orphaned remove tag again
                    if netbox_handler.orphaned_tag in this_object_tags:
                        this_object.remove_tags(netbox_handler.orphaned_tag)

                # if object was tagged by this program in previous runs but is not present
                # anymore then add the orphaned tag except it originated from a disabled source
                else:

                    if bool(set(this_object_tags).intersection(disabled_sources_tags)) is True:
                        log.debug2(f"Object {this_object.__class__.name} '{this_object.get_display_name()}' was added "
                                   f"from a currently disabled source. Skipping orphaned tagging.")
                        continue

                    # test for different conditions.
                    if netbox_handler.primary_tag not in this_object_tags:
                        continue

                    if bool(set(this_object_tags).intersection(all_sources_tags)) is True:
                        for source_tag in all_sources_tags:
                            this_object.remove_tags(source_tag)
                    elif netbox_handler.settings.ignore_unknown_source_object_pruning is True:
                        continue

                    if getattr(this_object, "prune", False) is False:
                        # or just remove primary tag if pruning is disabled
                        this_object.remove_tags(netbox_handler.orphaned_tag)
                        continue

                    # don't mark IPs as orphaned if vm/device is only switched off
                    if isinstance(this_object, NBIPAddress):
                        device_vm_object = this_object.get_device_vm()

                        if device_vm_object is not None and \
                                grab(device_vm_object, "data.status") is not None and \
                                "active" not in str(grab(device_vm_object, "data.status")):

                            if netbox_handler.orphaned_tag in this_object.get_tags():
                                this_object.remove_tags(netbox_handler.orphaned_tag)

                            log.debug2(f"{device_vm_object.name} '{device_vm_object.get_display_name()}' has IP "
                                       f"'{this_object.get_display_name()}' assigned but is in status "
                                       f"{grab(device_vm_object, 'data.status')}. "
                                       f"IP address will not marked as orphaned.")
                            continue

                    this_object.add_tags(netbox_handler.orphaned_tag)

    def query_ptr_records_for_all_ips(self):
        """
        Perform a DNS lookup for all IP address of a certain source if desired.
        """

        log.debug("Starting to look up PTR records for IP addresses")

        # store IP addresses to look them up in bulk
        ip_lookup_dict = dict()

        # iterate over all IP addresses
        for ip in self.get_all_items(NBIPAddress):

            # ignore IPs which are not handled by any source
            if ip.source is None:
                continue

            # get IP without prefix length
            ip_a = grab(ip, "data.address", fallback="").split("/")[0]

            # check if we meant to look up DNS host name for this IP
            if grab(ip, "source.settings.dns_name_lookup", fallback=False) is True:

                if ip_lookup_dict.get(ip.source) is None:

                    ip_lookup_dict[ip.source] = {
                        "ips": list(),
                        "servers": grab(ip, "source.settings.custom_dns_servers")
                    }

                ip_lookup_dict[ip.source].get("ips").append(ip_a)

        # now perform DNS requests to look up DNS names for IP addresses
        for source, data in ip_lookup_dict.items():

            if len(data.get("ips")) == 0:
                continue

            # get DNS names for IP addresses:
            records = perform_ptr_lookups(data.get("ips"), data.get("servers"))

            for ip in self.get_all_items(NBIPAddress):

                if ip.source != source:
                    continue

                ip_a = grab(ip, "data.address", fallback="").split("/")[0]

                dns_name = records.get(ip_a)

                if dns_name is not None:

                    ip.update(data={"dns_name": dns_name})

        log.debug("Finished to look up PTR records for IP addresses")

    def to_dict(self):
        """
        Return the whole inventory as one dictionary

        Returns
        -------
        dict: of all items in inventory
        """

        output = dict()
        for nb_object_class in NetBoxObject.__subclasses__():

            output[nb_object_class.name] = list()

            for this_object in self.base_structure[nb_object_class.name]:
                output[nb_object_class.name].append(this_object.to_dict())

        return output

    def __str__(self):
        """
        Return a dictionary of whole inventory as JSON formatted string

        Returns
        -------
        str: JSON formatted string of the whole inventory
        """

        return json.dumps(self.to_dict(), sort_keys=True, indent=4)

# EOF
