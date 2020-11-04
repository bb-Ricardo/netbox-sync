

import json
import logging

import pprint

from module.common.misc import grab, do_error_exit, dump
from module.common.logging import get_logger

log = get_logger()

class NetBoxObject():
    default_attributes = {
        "data": None,
        "is_new": True,
        "nb_id": 0,
        "updated_items": list(),
        "source": None,
    }

    # keep handle to inventory instance to append objects on demand
    inventory = None

    def __init__(self, data=None, read_from_netbox=False, inventory=None, source=None):

        # inherit and create default attributes from parent
        for attr_key, attr_value in self.default_attributes.items():
            if isinstance(attr_value, (list, dict, set)):
                setattr(self, attr_key, attr_value.copy())
            else:
                setattr(self, attr_key, attr_value)

        # store provided inventory handle
        self.inventory = inventory

        # store source handle
        if source is not None:
            self.source = source

        self.update(data=data, read_from_netbox=read_from_netbox)

    def __repr__(self):
        return "<%s instance '%s' at %s>" % (self.__class__.__name__, self.get_display_name(), id(self))

    def to_dict(self):

        out = dict()

        for key in dir(self):
            value = getattr(self, key)
            if "__" in key:
                continue
            if callable(value) is True:
                continue
            if key in ["inventory", "default_attributes", "data_model_relation"]:
                continue
            if key == "source":
                value = getattr(value, "name", None)

            if key == "data_model":

                data_model = dict()
                for dkey, dvalue in value.items():
                    if isinstance(dvalue, list):
                        new_dvalue = list()
                        for possible_option in dvalue:
                            if possible_option in NetBoxObject.__subclasses__():
                                new_dvalue.append(str(possible_option))
                            else:
                                new_dvalue.append(possible_option)

                        dvalue = new_dvalue

                    # if value is class name then print class name
                    if type(dvalue) == type:
                        dvalue = str(dvalue)

                    data_model[dkey] = dvalue

                value = data_model

            if key == "data":

                data = dict()
                for dkey, dvalue in value.items():
                    # if value is class name then print class name
                    if isinstance(dvalue, NetBoxObject):
                        dvalue = repr(dvalue)

                    if dkey == "tags":
                        dvalue = [x.get_display_name() for x in dvalue]

                    data[dkey] = dvalue

                value = data

            out[key] = value

        return out

    def __str__(self):
        return json.dumps(self.to_dict(), sort_keys=True, indent=4)

    def __iter__(self):
        for key, value in self.to_dict():
            yield (key, value)

    @staticmethod
    def format_slug(text=None, max_len=50):
        """
        Format string to comply to NetBox slug acceptable pattern and max length.

        :param text: Text to be formatted into an acceptable slug
        :type text: str
        :return: Slug of allowed characters [-a-zA-Z0-9_] with max length of 50
        :rtype: str
        """

        if text is None or len(text) == 0:
            raise AttributeError("Argument 'text' can't be None or empty!")

        permitted_chars = (
            "abcdefghijklmnopqrstuvwxyz" # alphabet
            "0123456789" # numbers
            "_-" # symbols
        )

        # Replace separators with dash
        for sep in [" ", ",", "."]:
            text = text.replace(sep, "-")

        # Strip unacceptable characters
        text = "".join([c for c in text.lower() if c in permitted_chars])

        # Enforce max length
        return text[0:max_len]

    def update(self, data=None, read_from_netbox=False, source=None):

        if data is None:
            return

        if not isinstance(data, dict):
            raise AttributeError("Argument 'data' needs to be a dict!")

        if data.get("id") is not None:
            self.nb_id = data.get("id")

        if read_from_netbox is True:
            self.is_new = False
            self.data = data
            self.updated_items = list()

            return

        if source is not None:
            self.source = source

        display_name = self.get_display_name(data)

        if display_name is None:
            display_name = self.get_display_name()

        log.debug2(f"Parsing '{self.name}' data structure: {display_name}")

        parsed_data = dict()
        for key, value in data.items():

            if key not in self.data_model.keys():
                log.error(f"Found undefined data model key '{key}' for object '{self.__class__.__name__}'")
                continue

            # skip unset values
            if value is None:
                log.info(f"Found unset key '{key}' while parsing {display_name}. Skipping This key")
                continue

            # check data model to see how we have to parse the value
            defined_value_type = self.data_model.get(key)

            # value must be a string witch a certain max length
            if isinstance(defined_value_type, int):
                if not isinstance(value, str):
                    log.error(f"Invalid data type for '{self.__class__.__name__}.{key}' (must be str), got: '{value}'")
                    continue

                value = value[0:defined_value_type]

                if key == "slug":
                    value = self.format_slug(text=value, max_len=defined_value_type)
                else:
                    value = value[0:defined_value_type]

            if isinstance(defined_value_type, list):

                if isinstance(value, NetBoxObject):

                    if type(value) not in defined_value_type:
                        log.error(f"Invalid data type for '{key}' (must be one of {defined_value_type}), got: '{type(value)}'")
                        continue

                elif value not in defined_value_type:
                    log.error(f"Invalid data type for '{key}' (must be one of {defined_value_type}), got: '{value}'")
                    continue

            # just check the type of the value
            type_check_faild = False
            for valid_type in [bool, str, int]:

                if defined_value_type == valid_type and not isinstance(value, valid_type):
                    log.error(f"Invalid data type for '{key}' (must be {valid_type.__name__}), got: '{value}'")
                    type_check_faild = True
                    break

            if type_check_faild is True:
                continue

            # this is meant to be reference to a different object
            if defined_value_type in NetBoxObject.__subclasses__():

                # tags need to be treated as list of dictionaries
                if defined_value_type == NBTags:
                    self.add_tags(value)
                    continue

                if not isinstance(value, NetBoxObject):
                    # try to find object.
                    value = self.inventory.add_update_object(defined_value_type, data=value)

            # add to parsed data dict
            parsed_data[key] = value

        # add/update slug
        # if data model contains a slug we need to handle it
        if "slug" in self.data_model.keys() and parsed_data.get("slug") is None and parsed_data.get(self.primary_key) is not None:

            parsed_data["slug"] = self.format_slug(text=parsed_data.get(self.primary_key), max_len=self.data_model.get("slug"))

        # this is a new set of data
        if self.data is None:
            self.data = parsed_data

            # add empty tag list if not tags were provided
            if "tags" in self.data_model.keys() and data.get("tags") is None:
                self.data["tags"] = list()

        # see if data just got updated and mark it as such.
        else:

            for key, new_value in parsed_data.items():

                # nothing changed, continue with next key
                current_value = self.data.get(key)
                if current_value == new_value:
                    continue

                # get current value str
                if isinstance(current_value, NetBoxObject):
                    current_value_str = str(current_value.get_display_name())

                # if data model is a list then we need to read the netbox data value
                elif isinstance(self.data_model.get(key), list) and isinstance(current_value, dict):
                    current_value_str = str(current_value.get("value"))

                else:
                    current_value_str = str(current_value).replace("\r","")

                # get new value str
                if isinstance(new_value, NetBoxObject):
                    new_value_str = str(new_value.get_display_name())
                else:
                    new_value_str = str(new_value).replace("\r","")

                # just check again if values might match now
                if current_value_str == new_value_str:
                    continue

                self.data[key] = new_value
                self.updated_items.append(key)

                log.debug(f"{self.name.capitalize()} '{display_name}' attribute '{key}' changed from '{current_value_str}' to '{new_value_str}'")

            self.resolve_relations()


    def get_display_name(self, data=None, including_second_key=False):

        this_data_set = data
        if data is None:
            this_data_set = self.data

        if this_data_set is None:
            return None

        my_name = this_data_set.get(self.primary_key)

        secondary_key = getattr(self, "secondary_key", None)
        enforce_secondary_key = getattr(self, "enforce_secondary_key", False)

        if secondary_key is not None and (enforce_secondary_key is True or including_second_key is True):

            secondary_key_value = this_data_set.get(secondary_key)

            if isinstance(secondary_key_value, NetBoxObject):
                secondary_key_value = secondary_key_value.get_display_name()

            if isinstance(secondary_key_value, dict):
                secondary_key_value = self.get_display_name(data=secondary_key_value)

            if secondary_key_value is None:
                raise ValueError(f"Unable to determine second key for {this_data_set.get(secondary_key)}")

            my_name = f"{my_name} ({secondary_key_value})"

        return my_name


    def resolve_relations(self):

        for key, value in self.data_model.items():

            if self.data.get(key) is None:
                continue

            # continue if value is not an NetBox object
            if value not in NetBoxObject.__subclasses__():
                continue

            data_value = self.data.get(key)

            resolved_data = None
            if value == NBTags:

                resolved_tag_list = list()
                for tag in data_value:

                    if isinstance(tag, NetBoxObject):
                        tag_object = tag
                    else:
                        tag_object = self.inventory.get_by_data(value, data=tag)

                    if tag_object is not None:
                        resolved_tag_list.append(tag_object)

                resolved_data = resolved_tag_list
            else:
                if data_value is None:
                    continue

                if isinstance(data_value, NetBoxObject):
                    resolved_data = data_value
                else:
                    data_to_find = None
                    if isinstance(data_value, int):
                        data_to_find = {"id": data_value}
                    elif isinstance(data_value, dict):
                        data_to_find = data_value

                    resolved_data = self.inventory.get_by_data(value, data=data_to_find)

            if resolved_data is not None:
                self.data[key] = resolved_data
            else:
                log.error(f"Problems resolving relation '{key}' for object '%s' and value '%s'" % (self.get_display_name(), data_value))

    def raw(self):

        return self.data


    def get_dependencies(self):

        return [x for x in self.data_model.values() if x in NetBoxObject.__subclasses__()]

    def get_tags(self):

        return [x.get_display_name() for x in self.data.get("tags", list())]

    def update_tags(self, tags, remove=False):

        if tags is None or NBTags not in self.data_model.values():
            return

        action = "Adding" if remove is False else "Removing"

        log.debug2(f"{action} Tags: {tags}")
        new_tags = list()

        def extract_tags(this_tags):
            if isinstance(this_tags, str):
                new_tags.append(this_tags)
            elif isinstance(this_tags, dict) and this_tags.get("name") is not None:
                new_tags.append(this_tags.get("name"))

        if isinstance(tags, list):
            for tag in tags:
                extract_tags(tag)
        else:
            extract_tags(tags)


        log.debug2(f"Parsed tag list: {new_tags}")

        current_tags = self.get_tags()

        tag_has_changed = False
        for tag_name in new_tags:

            if tag_name not in current_tags and remove == False:
                # add tag

                tag = self.inventory.add_update_object(NBTags, data={"name": tag_name})

                self.data["tags"].append(tag)
                if self.is_new is False:
                    self.updated_items.append("tags")

                tag_has_changed = True

            if tag_name in current_tags and remove == True:

                tag = self.inventory.get_by_data(NBTags, data={"name": tag_name})

                self.data["tags"].remove(tag)
                if self.is_new is False:
                    self.updated_items.append("tags")

                tag_has_changed = True

        new_tags = self.get_tags()

        if tag_has_changed is True:
            log.debug(f"{self.name.capitalize()} '{self.get_display_name()}' attribute 'tags' changed from '{current_tags}' to '{new_tags}'")

    def add_tags(self, tags_to_add):
        self.update_tags(tags_to_add)

    def remove_tags(self, tags_to_remove):
        self.update_tags(tags_to_remove, remove=True)


    def get_nb_reference(self):
        """
        Default class to return reference of how this object is usually referenced.

        default: return NetBox ID
        """

        """
            FIXME
            does this work?
        """
        if self.nb_id == 0:
            return self

        return self.nb_id


class NBTags(NetBoxObject):
    name = "tag"
    api_path = "extras/tags"
    primary_key = "name"
    data_model = {
        "name": 100,
        "slug": 100,
        "color": 6,
        "description": 200
    }


class NBManufacturers(NetBoxObject):
    name = "manufacturer"
    api_path = "dcim/manufacturers"
    primary_key = "name"
    data_model = {
        "name": 50,
        "slug": 50,
        "description": 200
    }


class NBDeviceTypes(NetBoxObject):
    name ="device type"
    api_path = "dcim/device-types"
    primary_key = "model"
    data_model = {
        "model": 50,
        "slug": 50,
        "part_number": 50,
        "description": 200,
        "manufacturer": NBManufacturers,
        "tags": NBTags
    }

class NBPlatforms(NetBoxObject):
    name = "platform"
    api_path = "dcim/platforms"
    primary_key = "name"
    data_model = {
        "name": 100,
        "slug": 100,
        "manufacturer": NBManufacturers,
        "description": 200
    }

class NBClusterTypes(NetBoxObject):
    name = "cluster type"
    api_path = "virtualization/cluster-types"
    primary_key = "name"
    data_model = {
        "name": 50,
        "slug": 50,
        "description": 200
    }

class NBClusterGroups(NetBoxObject):
    name = "cluster group"
    api_path = "virtualization/cluster-groups"
    primary_key = "name"
    data_model = {
        "name": 50,
        "slug": 50,
        "description": 200
    }

class NBDeviceRoles(NetBoxObject):
    name = "device role"
    api_path = "dcim/device-roles"
    primary_key = "name"
    data_model = {
        "name": 50,
        "slug": 50,
        "color": 6,
        "description": 200,
        "vm_role": bool
    }

class NBSites(NetBoxObject):
    name = "site"
    api_path = "dcim/sites"
    primary_key = "name"
    data_model = {
        "name": 50,
        "slug": 50,
        "comments": str,
        "tags": NBTags
    }


class NBClusters(NetBoxObject):
    name = "cluster"
    api_path = "virtualization/clusters"
    primary_key = "name"
    data_model = {
        "name": 100,
        "comments": str,
        "type": NBClusterTypes,
        "group": NBClusterGroups,
        "site": NBSites,
        "tags": NBTags
    }


class NBDevices(NetBoxObject):
    name = "device"
    api_path = "dcim/devices"
    primary_key = "name"
    secondary_key = "site"
    data_model = {
        "name": 64,
        "device_type": NBDeviceTypes,
        "device_role": NBDeviceRoles,
        "platform": NBPlatforms,
        "serial": 50,
        "site": NBSites,
        "status": [ "offline", "active", "planned", "staged", "failed", "inventory", "decommissioning" ],
        "cluster": NBClusters,
        "asset_tag": 50,
        "primary_ip4": int,
        "primary_ip6": int,
        "tags": NBTags
    }


class NBVMs(NetBoxObject):
    name = "virtual machine"
    api_path = "virtualization/virtual-machines"
    primary_key = "name"
    secondary_key = "cluster"
    data_model = {
        "name": 64,
        "status": [ "offline", "active", "planned", "staged", "failed", "decommissioning" ],
        "cluster": NBClusters,
        "role": NBDeviceRoles,
        "platform": NBPlatforms,
        "vcpus": int,
        "memory": int,
        "disk": int,
        "comments": str,
        "primary_ip4": int,
        "primary_ip6": int,
        "tags": NBTags
    }

class NBVMInterfaces(NetBoxObject):
    name = "virtual machine interface"
    api_path = "virtualization/interfaces"
    primary_key = "name"
    secondary_key = "virtual_machine"
    enforce_secondary_key = True
    is_primary = False
    data_model = {
        "name": 64,
        "virtual_machine": NBVMs,
        "enabled": bool,
        "mac_address": str,
        "description": 200,
        "tags": NBTags
    }

class NBInterfaces(NetBoxObject):
    name = "interface"
    api_path = "dcim/interfaces"
    primary_key = "name"
    secondary_key = "device"
    enforce_secondary_key = True
    is_primary = False
    data_model = {
        "name": 64,
        "device": NBDevices,
        "label": 64,
        "type": [ "virtual", "100base-tx", "1000base-t", "10gbase-t", "25gbase-x-sfp28", "40gbase-x-qsfpp", "other" ],
        "enabled": bool,
        "mac_address": str,
        "mgmt_only": bool,
        "mtu": int,
        "description": 200,
        "connection_status": bool,
        "tags": NBTags
    }


class NBIPAddresses(NetBoxObject):
    name = "IP address"
    api_path = "ipam/ip-addresses"
    primary_key = "address"
    data_model = {
        "address": str,
        "assigned_object_type": ["dcim.interface", "virtualization.vminterface"],
        "assigned_object_id": [ NBInterfaces, NBVMInterfaces ],
        "description": 200,
        "dns_name": 255,
        "tags": NBTags,
        "tenant": int,
        "vrf": int
    }
    # add relation between two attributes
    data_model_relation = {
        "dcim.interface": NBInterfaces,
        "virtualization.vminterface": NBVMInterfaces,
        NBInterfaces: "dcim.interface",
        NBVMInterfaces: "virtualization.vminterface"
    }

    def resolve_relations(self):

        o_id = self.data.get("assigned_object_id")
        o_type = self.data.get("assigned_object_type")

        # this needs special treatment as the object type depends on a second model key
        if o_type is not None and o_type not in self.data_model.get("assigned_object_type"):

            log.error("Attribute 'assigned_object_type' for '%s' invalid: %s" % \
                (self.get_display_name(), o_type))
            do_error_exit("Error while resolving relations for %s" % self.get_display_name())


        if isinstance(o_id, int):
            self.data["assigned_object_id"] = self.inventory.get_by_id(self.data_model_relation.get(o_type), id=o_id)

        super().resolve_relations()


    def update(self, data=None, read_from_netbox=False, source=None):

        object_type = data.get("assigned_object_type")
        object = data.get("assigned_object_id")

        # we got an object data structure where we have to find the object
        if read_from_netbox is False and object is not None:

            if not isinstance(object, NetBoxObject):

                data["assigned_object_id"] = \
                    self.inventory.add_update_object(self.data_model_relation.get(object_type), data=object)

            else:
                data["assigned_object_type"] = self.data_model_relation.get(type(object))

        super().update(data=data, read_from_netbox=read_from_netbox, source=source)

        # we need to tell NetBox which object type this is meant to be
        if "assigned_object_id" in self.updated_items:
            self.updated_items.append("assigned_object_type")

    def get_dependencies(self):
        """
            This is hard coded in here. Updated if data_model attribute changes!!!!
        """

        return [ NBInterfaces, NBVMInterfaces, NBTags ]


class NBPrefixes(NetBoxObject):
    name = "IP prefix"
    api_path = "ipam/prefixes"
    primary_key = "prefix"
    data_model = {
        "prefix": str,
        "site": NBSites,
        "description": 200,
        "tags": NBTags
    }

# EOF
