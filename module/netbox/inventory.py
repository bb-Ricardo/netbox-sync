# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import json
import asyncio
from ipaddress import IPv4Address, IPv6Address, IPv4Network, IPv6Network, IPv4Interface, IPv6Interface

from module.netbox import *
from module.common.misc import grab
from module.common.logging import get_logger
from module.common.support import perform_ptr_lookups

log = get_logger()

"""
Indexes of the inventory

Most lookups in the inventory used to compare every object of a type with the value to find, which makes
syncing N objects cost N^2. The indexes below map an attribute value to the objects carrying it, so a
lookup only needs to look at a handful of candidates.

An index is only ever used to pick candidates. Every lookup still applies its original comparison to the
candidates, in the order the objects were added to the inventory, so the result is exactly the one the
full scan returned. For that to hold an index has to list every object which could match, which is
guaranteed by these invariants:

  * an index key is derived only from the data of the object itself (never from objects it references),
  * every write to NetBoxObject.data and NetBoxObject.nb_id is reported to the inventory (see NBDataDict and
    NetBoxObject.__setattr__) and the object is re-keyed straight away,
  * a value which can't be turned into a reliable key is filed as "volatile" and returned for every lookup,
  * a value which can never match a lookup (i.e. an interface parent which isn't an object yet) isn't indexed.

The index of an object type is built on its first lookup. It is rebuilt whenever its list in
'base_structure' was replaced or changed behind the inventory's back (tests do that).
"""

# key of the bucket holding objects which have to be considered for every lookup
_VOLATILE = object()

# key for values which can never match a lookup, objects with this key are not indexed
_SKIP = object()

# primary key values whose string representation is stable and can be used as key
_STABLE_PRIMARY_KEY_TYPES = (str, int, float, bool, IPv4Address, IPv6Address, IPv4Network, IPv6Network,
                             IPv4Interface, IPv6Interface)


def _display_name_key(value):
    """
    Key of the primary key index. The display name of every object type starts with the string of its
    primary key value, followed by nothing or " (" and its secondary key, so the lower-cased primary key
    string is a prefix of every lower-cased display name this object can produce.
    """
    if value is None or isinstance(value, _STABLE_PRIMARY_KEY_TYPES):
        return f"{value}".lower()
    # i.e. a NetBoxObject as primary key value, its string changes with the referenced object
    return _VOLATILE


def _value_key(value):
    """key for plain equality lookups"""
    try:
        hash(value)
    except TypeError:
        return _VOLATILE
    return value


def _reference_key(value):
    """key for lookups by referenced object, only objects can be equal to an object"""
    if isinstance(value, NetBoxObject):
        return value
    return _SKIP


def _ip_address_key(value):
    """key for IP address strings, the address without prefix length"""
    if isinstance(value, str):
        return value.split("/")[0]
    return _SKIP


def _ip_network_key(value):
    """key for IP prefixes, an IP can be matched against every possible network of all prefix lengths"""
    if isinstance(value, (IPv4Network, IPv6Network)):
        return value.version, value.prefixlen, int(value.network_address)
    if value is None:
        return _SKIP
    return _VOLATILE


def _primary_ip_key(value):
    """key for primary IPs of devices and VMs, they can be an object, a NetBox dict or an ID"""
    if isinstance(value, NBIPAddress):
        return value
    if isinstance(value, (dict, int)):
        return _VOLATILE
    return _SKIP


class _IndexSpec:
    """
    describes one index of an object type

    name: name of the index
    watch: name of the data key which changes the key of this index ('__nb_id__' for the NetBox ID)
    key_function: turns the watched value into an index key
    """

    __slots__ = ("name", "watch", "key_function")

    nb_id_key = "__nb_id__"

    def __init__(self, name, watch, key_function):
        self.name = name
        self.watch = watch
        self.key_function = key_function

    def key_of(self, this_object):

        if self.watch == self.nb_id_key:
            return self.key_function(this_object.nb_id)

        data = this_object.data
        if not isinstance(data, dict):
            return _SKIP

        return self.key_function(data.get(self.watch))


# additional indexes on top of NetBox ID and primary key, data key as index name
_type_index_definitions = {
    NBVM: {"serial": _value_key, "primary_ip4": _primary_ip_key, "primary_ip6": _primary_ip_key},
    NBDevice: {"serial": _value_key, "asset_tag": _value_key,
               "primary_ip4": _primary_ip_key, "primary_ip6": _primary_ip_key},
    NBVMInterface: {"virtual_machine": _reference_key, "mac_address": _value_key},
    NBInterface: {"device": _reference_key, "mac_address": _value_key},
    NBIPAddress: {"address": _ip_address_key, "assigned_object_id": _reference_key},
    NBMACAddress: {"mac_address": _value_key, "assigned_object_id": _reference_key},
    NBPrefix: {"prefix": _ip_network_key},
    NBVirtualDisk: {"virtual_machine": _reference_key}
}

_index_spec_cache = dict()


def _get_index_specs(object_type):

    specs = _index_spec_cache.get(object_type)
    if specs is None:
        specs = [
            _IndexSpec("__nb_id__", _IndexSpec.nb_id_key, _value_key),
            _IndexSpec("__primary_key__", object_type.primary_key, _display_name_key)
        ]
        for data_key, key_function in _type_index_definitions.get(object_type, dict()).items():
            specs.append(_IndexSpec(data_key, data_key, key_function))

        _index_spec_cache[object_type] = specs

    return specs


class _TypeIndex:
    """
    All indexes of one object type.

    position: inventory position of every indexed object, used to return candidates in inventory order
    keys: index name -> object -> key of this object
    buckets: index name -> key -> objects with this key (dict used as ordered set)
    """

    def __init__(self, object_type, items):

        self.items = items
        self.specs = _get_index_specs(object_type)
        self.watch = dict()
        for spec in self.specs:
            self.watch.setdefault(spec.watch, list()).append(spec)

        self.position = dict()
        self.keys = {spec.name: dict() for spec in self.specs}
        self.buckets = {spec.name: dict() for spec in self.specs}

        for position, this_object in enumerate(items):
            self.add(this_object, position)

    def add(self, this_object, position):

        # first occurrence defines the position, like a scan would
        if this_object in self.position:
            return

        self.position[this_object] = position
        for spec in self.specs:
            self._file(this_object, spec, spec.key_of(this_object))

    def _file(self, this_object, spec, key):

        self.keys[spec.name][this_object] = key
        if key is not _SKIP:
            self.buckets[spec.name].setdefault(key, dict())[this_object] = None

    def update(self, this_object, watch_key=None):

        if this_object not in self.position:
            return

        specs = self.specs if watch_key is None else self.watch.get(watch_key)
        if not specs:
            return

        for spec in specs:
            old_key = self.keys[spec.name].get(this_object, _SKIP)
            new_key = spec.key_of(this_object)

            if old_key is new_key or (type(old_key) is type(new_key) and old_key == new_key):
                continue

            if old_key is not _SKIP:
                bucket = self.buckets[spec.name].get(old_key)
                if bucket is not None:
                    bucket.pop(this_object, None)
                    if len(bucket) == 0:
                        del self.buckets[spec.name][old_key]

            self._file(this_object, spec, new_key)

    def candidates(self, index_name, keys):
        """
        return all objects filed under one of the keys plus all volatile ones in inventory order.
        Returns None if a key can't be looked up, the caller needs to scan all objects then.
        """

        buckets = self.buckets[index_name]

        result = dict()
        for key in keys:
            try:
                bucket = buckets.get(key)
            except TypeError:
                return None
            if bucket:
                result.update(bucket)

        volatile = buckets.get(_VOLATILE)
        if volatile:
            result.update(volatile)

        if len(result) <= 1:
            return list(result)

        return sorted(result, key=self.position.__getitem__)


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

        # object types read from NetBox in this run
        self.loaded_types = set()

        # indexes per object type name, built on first lookup
        self._indexes = dict()

        for object_type in NetBoxObject.__subclasses__():

            self.base_structure[object_type.name] = list()

    def _get_index(self, object_type):
        """
        return the index of an object type, (re)build it if it doesn't match the current list of objects

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to return the index for

        Returns
        -------
        _TypeIndex: index of this object type
        """

        items = self.base_structure.get(object_type.name)
        if items is None:
            return _TypeIndex(object_type, list())

        indexes = self.__dict__.get("_indexes")
        if indexes is None:
            indexes = self._indexes = dict()

        type_index = indexes.get(object_type.name)
        if type_index is None or type_index.items is not items or len(type_index.position) != len(items):
            type_index = indexes[object_type.name] = _TypeIndex(object_type, items)

        return type_index

    def object_data_changed(self, this_object, data_key=None):
        """
        Called by a NetBoxObject whenever its data or its NetBox ID changes, keeps the indexes up to date.

        Parameters
        ----------
        this_object: NetBoxObject
            the changed object
        data_key: str
            the changed data key, None if the whole data set changed
        """

        indexes = self.__dict__.get("_indexes")
        if not indexes:
            return

        type_index = indexes.get(this_object.name)
        if type_index is not None:
            type_index.update(this_object, data_key)

    def get_index_candidates(self, object_type, index_name, values):
        """
        Return all objects of $object_type which possibly carry one of $values in the indexed
        attribute $index_name, in the order of the inventory. The result can contain objects which don't
        match, callers have to apply their own comparison to each object.

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to find
        index_name: str
            name of the index, the data key of one of the indexes in '_type_index_definitions'
        values: list, tuple
            values to look for

        Returns
        -------
        list: of candidate objects
        """

        type_index = self._get_index(object_type)

        key_function = None
        for spec in type_index.specs:
            if spec.name == index_name:
                key_function = spec.key_function
                break

        if key_function is None:
            raise ValueError(f"No index '{index_name}' defined for {object_type.name} objects")

        keys = list()
        for value in values:
            key = key_function(value)
            if key is _VOLATILE:
                return list(type_index.items)
            if key is not _SKIP:
                keys.append(key)

        candidates = type_index.candidates(index_name, keys)
        if candidates is None:
            return list(type_index.items)

        return candidates

    def get_prefix_candidates(self, ip_to_match):
        """
        Return all prefixes which possibly contain $ip_to_match, in the order of the inventory.
        Callers have to apply their own comparison to each prefix.

        Parameters
        ----------
        ip_to_match: IPv4Address, IPv6Address
            IP address to find prefixes for

        Returns
        -------
        list: of candidate NBPrefix objects
        """

        if not isinstance(ip_to_match, (IPv4Address, IPv6Address)):
            return list(self.get_all_items(NBPrefix))

        max_prefix_length = ip_to_match.max_prefixlen
        all_ones = (1 << max_prefix_length) - 1
        ip_int = int(ip_to_match)

        keys = list()
        for prefix_length in range(max_prefix_length + 1):
            netmask = all_ones ^ (all_ones >> prefix_length)
            keys.append((ip_to_match.version, prefix_length, ip_int & netmask))

        candidates = self._get_index(NBPrefix).candidates("prefix", keys)
        if candidates is None:
            return list(self.get_all_items(NBPrefix))

        return candidates

    def get_primary_ip_candidates(self, object_type, ip_addresses):
        """
        Return all objects of $object_type which possibly use one of $ip_addresses (without prefix length)
        as primary IPv4 or IPv6 address, in the order of the inventory. Callers have to apply their own
        comparison to each object.

        Parameters
        ----------
        object_type: NBDevice, NBVM
            object type to find
        ip_addresses: list
            IP address strings without prefix length

        Returns
        -------
        list: of candidate objects
        """

        ip_objects = self.get_index_candidates(NBIPAddress, "address", [x for x in ip_addresses if x is not None])

        result = dict()
        for index_name in ["primary_ip4", "primary_ip6"]:
            for this_object in self.get_index_candidates(object_type, index_name, ip_objects):
                result[this_object] = None

        position = self._get_index(object_type).position
        return sorted(result, key=position.__getitem__)

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

        candidates = self._get_index(object_type).candidates("__nb_id__", [_value_key(nb_id)])
        if candidates is None:
            candidates = self.base_structure[object_type.name]

        for this_object in candidates:

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
            if object_type in (NBVMInterface, NBVirtualDisk) and isinstance(data.get("virtual_machine"), NBVM):
                # VM names are only unique within a cluster; use the resolved parent object.
                for this_object in self.get_index_candidates(object_type, "virtual_machine",
                                                             [data["virtual_machine"]]):
                    if this_object.data.get("virtual_machine") is data["virtual_machine"] and \
                            str(this_object.data.get(object_type.primary_key)).lower() == \
                            str(data[object_type.primary_key]).lower():
                        return this_object
                return None
            object_name_to_find = self.get_all_items(object_type)[0].get_display_name(data, including_second_key=True)
            name_to_find = f"{object_name_to_find}".lower()

            for this_object in self._get_display_name_candidates(object_type, name_to_find):

                # compare lower key
                if name_to_find == f"{this_object.get_display_name(including_second_key=True)}".lower():

                    return this_object

        # try to match all data attributes
        else:

            candidates = None
            for attr_name, attr_value in data.items():
                if attr_name in ("serial", "asset_tag") and \
                        attr_name in _type_index_definitions.get(object_type, dict()).keys():
                    candidates = self.get_index_candidates(object_type, attr_name, [attr_value])
                    break

            for this_object in candidates if candidates is not None else self.get_all_items(object_type):
                all_items_match = True
                for attr_name, attr_value in data.items():

                    if this_object.data.get(attr_name) != attr_value:
                        all_items_match = False
                        break

                if all_items_match is True:
                    return this_object

        return None

    def _get_display_name_candidates(self, object_type, name_to_find):
        """
        Return all objects of $object_type whose lower-cased display name (including second key) can be
        $name_to_find, in the order of the inventory.

        A display name is the primary key value, optionally followed by " (" and the secondary key. So the
        primary key of a matching object is either the whole name or the part in front of one of the " ("
        in it.

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type to find
        name_to_find: str
            lower-cased display name to find

        Returns
        -------
        list: of candidate objects
        """

        keys = [name_to_find]
        separator_position = name_to_find.find(" (")
        while separator_position != -1:
            keys.append(name_to_find[:separator_position])
            separator_position = name_to_find.find(" (", separator_position + 1)

        candidates = self._get_index(object_type).candidates("__primary_key__", keys)
        if candidates is None:
            return list(self.get_all_items(object_type))

        return candidates

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
        items = self.base_structure[object_type.name]
        items.append(new_object)

        # add to index if one exists, otherwise it gets built with the next lookup
        indexes = self.__dict__.get("_indexes")
        type_index = indexes.get(object_type.name) if indexes else None
        if type_index is not None:
            if type_index.items is items and len(type_index.position) == len(items) - 1:
                type_index.add(new_object, len(items) - 1)
            else:
                del indexes[object_type.name]

        if read_from_netbox is False:
            log.info(f"Created new {new_object.name} object: {new_object.get_display_name()}")

        return new_object

    def load_from_netbox(self, object_type, objects_data):
        """
        Adds all objects of $object_type read from NetBox to the inventory.

        Parameters
        ----------
        object_type: NetBoxObject subclass
            object type which was read from NetBox
        objects_data: list
            data of every object of this type NetBox returned
        """

        for object_data in objects_data:
            self.add_object(object_type, data=object_data, read_from_netbox=True)

        self.loaded_types.add(object_type)

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
            for interface in self.get_index_candidates(NBVMInterface, "virtual_machine", [this_object]):
                if grab(interface, "data.virtual_machine") == this_object:
                    interfaces.append(interface)

        if isinstance(this_object, NBDevice):
            for interface in self.get_index_candidates(NBInterface, "device", [this_object]):
                if grab(interface, "data.device") == this_object:
                    interfaces.append(interface)

        return interfaces

    @staticmethod
    def get_orphaned_status(this_object, netbox_handler):
        """
        Return the status this program assigns to $this_object while it is orphaned.

        Only devices and virtual machines have such an option and only if the user
        configured one. An undefined option also means that this program never touches
        the status of these objects.

        Parameters
        ----------
        this_object: NetBoxObject
            the object to return the configured orphaned status for
        netbox_handler: NetBoxHandler
            the object instance of a NetBox handler to get the settings from

        Returns
        -------
        (str, None): the configured status, None if unconfigured or unsupported object type
        """

        if isinstance(this_object, NBDevice):
            return netbox_handler.settings.orphaned_device_status

        if isinstance(this_object, NBVM):
            return netbox_handler.settings.orphaned_vm_status

        return None

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

                        # set the status back to active, but only if this program parked this
                        # object at the configured orphaned status. Any other status was set
                        # by a user or reported by the source and has to be left alone.
                        orphaned_status = self.get_orphaned_status(this_object, netbox_handler)
                        if orphaned_status is not None:
                            current_status = grab(this_object, "data.status")
                            if isinstance(current_status, dict):
                                current_status = current_status.get("value")
                            if current_status == orphaned_status:
                                this_object.update(data={"status": "active"})

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

                    # set orphaned status on devices/VMs if configured
                    # and pruning is enabled (if a source was unavailable,
                    # pruning is disabled to prevent false orphaning)
                    if netbox_handler.settings.prune_enabled is True:
                        orphaned_status = self.get_orphaned_status(this_object, netbox_handler)
                        if orphaned_status is not None:
                            this_object.update(data={"status": orphaned_status})

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
            records = asyncio.run(perform_ptr_lookups(data.get("ips"), data.get("servers")))

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
