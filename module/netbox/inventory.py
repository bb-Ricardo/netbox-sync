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

from module.netbox import *
from module.common.misc import grab
from module.common.logging import get_logger
from module.common.support import perform_ptr_lookups

log = get_logger()


class OwnerResolution:
    """How the source that owns a VM, device, or IP was identified."""

    def __init__(self, status, source=None, tags=None, sources=None):
        self.status = status
        self.source = source
        self.sources = list(sources if sources is not None else ([source] if source is not None else []))
        self.tags = list(tags or [])


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
        self._ambiguous_owner_reports = set()

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
            if object_type in (NBVMInterface, NBVirtualDisk) and isinstance(data.get("virtual_machine"), NBVM):
                # VM names are only unique within a cluster; use the resolved parent object.
                for this_object in self.get_all_items(object_type):
                    if this_object.data.get("virtual_machine") is data["virtual_machine"] and \
                            str(this_object.data.get(object_type.primary_key)).lower() == \
                            str(data[object_type.primary_key]).lower():
                        return this_object
                return None
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
            for interface in self.get_all_items(NBVMInterface):
                if grab(interface, "data.virtual_machine") == this_object:
                    interfaces.append(interface)

        if isinstance(this_object, NBDevice):
            for interface in self.get_all_items(NBInterface):
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

    def _configured_sources_by_tag(self):
        sources = {}
        for source in self.source_list:
            tag = getattr(source, "source_tag", None)
            if tag:
                sources[tag] = source
        return sources

    @staticmethod
    def _source_tag_names(obj):
        if obj is None:
            return []

        names = []
        for tag in obj.get_tags():
            if isinstance(tag, str) and tag.startswith("Source:"):
                names.append(tag)
        return names

    def _resolve_parent_owner(self, parent):
        """
        The owner is the single known Source tag. Several known tags on one
        device are shared provenance, not a conflict. An unknown Source tag is
        ambiguous. No tag is absent. Processing order is not a tie-break.
        """

        configured = self._configured_sources_by_tag()
        tags = self._source_tag_names(parent)
        unknown_tags = [tag for tag in tags if tag not in configured]
        known_sources = []
        for tag in tags:
            source = configured.get(tag)
            if source is not None and source not in known_sources:
                known_sources.append(source)

        if unknown_tags:
            return OwnerResolution("ambiguous", None, tags)

        # Several configured sources on one device are provenance. VMware and
        # Redfish both tag an ESXi host; that is not an ownership conflict.
        if len(known_sources) > 1:
            return OwnerResolution("shared", None, tags, sources=known_sources)

        if len(known_sources) == 1:
            return OwnerResolution("owner", known_sources[0], tags)

        observations = getattr(parent, "guest_ip_observations", None) or {}
        resolvers = []
        for tag in observations:
            source = configured.get(tag)
            if source is not None and source not in resolvers:
                resolvers.append(source)

        if len(resolvers) == 1 and len(observations) == 1:
            return OwnerResolution("owner", resolvers[0], tags)

        if observations:
            return OwnerResolution("ambiguous", None, list(observations.keys()))

        return OwnerResolution("none", None, tags)

    def resolve_owner(self, obj):
        """
        Identify the source that owns obj.

        An IP is decided by its own Source tags. No source tag on the IP uses
        its parent. Several known tags on the IP, or a Source tag that matches
        no configured source, is ambiguous. Several tags on the parent are not.
        """

        if isinstance(obj, NBIPAddress):
            parent = obj.get_device_vm()
            parent_decision = self._resolve_parent_owner(parent) if parent is not None \
                else OwnerResolution("none", None, [])
            configured = self._configured_sources_by_tag()
            ip_tags = self._source_tag_names(obj)
            unknown_tags = [tag for tag in ip_tags if tag not in configured]
            known_sources = []
            for tag in ip_tags:
                source = configured.get(tag)
                if source is not None and source not in known_sources:
                    known_sources.append(source)

            # The IP's own tags decide. A parent with several source tags does
            # not make this address ambiguous, and it is not copied into the warning.
            if unknown_tags or len(known_sources) > 1:
                return OwnerResolution("ambiguous", None, ip_tags)

            if len(known_sources) == 1:
                return OwnerResolution("owner", known_sources[0], ip_tags)

            return parent_decision

        if isinstance(obj, (NBVMInterface, NBInterface)):
            parent = obj.data.get("virtual_machine") if isinstance(obj, NBVMInterface) else obj.data.get("device")
            if parent is not None:
                return self._resolve_parent_owner(parent)

        return self._resolve_parent_owner(obj)

    def report_ambiguous_owner(self, obj, decision):
        if not hasattr(self, "_ambiguous_owner_reports"):
            self._ambiguous_owner_reports = set()

        if id(obj) in self._ambiguous_owner_reports:
            return

        self._ambiguous_owner_reports.add(id(obj))
        tag_list = ", ".join(decision.tags) if decision.tags else "(no source tag)"
        log.warning(f"Unresolved source metadata for {obj.name} '{obj.get_display_name()}'; "
                    f"tags: {tag_list}. Left the current record unchanged.")

    def assignment_hold(self, ip, acting_source_tag=None):
        """
        Why the current assignment of ip must stay where it is.

        'unknown' means the source responsible for that assignment recorded an
        unknown guest-IP observation. 'undetermined' means that source cannot
        be identified. None means a move may follow the existing rules.
        """

        configured = set(self._configured_sources_by_tag())
        if acting_source_tag:
            configured.add(acting_source_tag)

        def classify(tags):
            ordered = []
            for tag in tags:
                if tag not in ordered:
                    ordered.append(tag)
            unknown = [tag for tag in ordered if tag not in configured]
            known = [tag for tag in ordered if tag in configured]
            return unknown, known

        ip_unknown, ip_known = classify(self._source_tag_names(ip))
        parent = ip.get_device_vm()
        observations = getattr(parent, "guest_ip_observations", None) or {} if parent is not None else {}

        if ip_unknown or len(ip_known) > 1:
            return "undetermined"

        if len(ip_known) == 1:
            if observations.get(ip_known[0]) == "unknown":
                return "unknown"
            return None

        if parent is None:
            return None

        parent_unknown, parent_known = classify(self._source_tag_names(parent))
        if parent_unknown:
            return "undetermined"
        if len(parent_known) > 1:
            if any(observations.get(tag) == "unknown" for tag in parent_known):
                return "undetermined"
            return None
        if len(parent_known) == 1 and observations.get(parent_known[0]) == "unknown":
            return "unknown"
        return None

    def _owner_observation_is_unknown(self, parent):
        if parent is None:
            return False

        observations = getattr(parent, "guest_ip_observations", None) or {}
        if "unknown" not in observations.values():
            return False

        decision = self._resolve_parent_owner(parent)
        if decision.status == "owner" and decision.source is not None:
            return observations.get(decision.source.source_tag) == "unknown"
        if decision.status == "shared":
            return any(observations.get(source.source_tag) == "unknown" for source in decision.sources)
        return True

    def _has_tag(self, obj, tag_name):
        return tag_name in obj.get_tags()

    def retention_reason(self, obj, _seen=None):
        """
        Why orphaning and pruning must leave obj alone this run.

        'unknown' is a guest-IP observation and clears a stale orphan tag.
        'ambiguous' and 'incomplete' block a new orphan tag and any delete.
        None means the ordinary lifecycle applies.

        Returns
        -------
        str, None
        """

        if obj is None:
            return None

        if _seen is None:
            _seen = set()
        if id(obj) in _seen:
            return None
        _seen.add(id(obj))

        if not isinstance(obj, (NBVM, NBDevice, NBIPAddress, NBVMInterface, NBInterface)):
            return None

        if getattr(obj, "unverified_retain", False) is True:
            return "unknown"

        if isinstance(obj, NBIPAddress):
            hold = self.assignment_hold(obj)
            if hold == "unknown":
                return "unknown"
            if hold == "undetermined":
                self.report_ambiguous_owner(obj, OwnerResolution("undetermined", None, self._source_tag_names(obj)))
                return "ambiguous"

        if isinstance(obj, NBIPAddress) and self.has_unresolved_claim(obj):
            return "unresolved"

        if getattr(obj, "seen_this_run", False) is True:
            return "seen"

        if isinstance(obj, (NBVMInterface, NBInterface)):
            parent = obj.data.get("virtual_machine") if isinstance(obj, NBVMInterface) else obj.data.get("device")
            if self._owner_observation_is_unknown(parent):
                for ip in obj.get_ip_addresses():
                    if ip.get_device_vm() is parent:
                        return "unknown"
            for ip in obj.get_ip_addresses():
                reason = self.retention_reason(ip, _seen)
                if reason is not None:
                    return reason

        decision = self.resolve_owner(obj)
        if decision.status == "ambiguous":
            self.report_ambiguous_owner(obj, decision)
            return "ambiguous"

        if decision.status == "owner" and decision.source is not None and \
                getattr(decision.source, "inventory_complete", True) is False:
            return "incomplete"

        if decision.status == "shared" and any(getattr(source, "inventory_complete", True) is False
                                                for source in decision.sources):
            return "incomplete"

        # Normal orphaning removes the source tag. A later incomplete listing
        # then cannot tell which source owned the object, so it must not treat
        # that earlier absence as confirmed and delete it. A following complete
        # listing can prune it again.
        if decision.status == "none" and self._has_tag(obj, f"{primary_tag_name}: Orphaned") and \
                self._any_source_listing_incomplete():
            return "incomplete"

        if isinstance(obj, NBIPAddress) and decision.status == "none" and \
                not self._source_tag_names(obj) and self._has_tag(obj, primary_tag_name) and \
                isinstance(obj.get_device_vm(), NBVM) and obj.get_device_vm().source is None and \
                self._has_tag(obj.get_device_vm(), primary_tag_name):
            if self._any_enabled_vmware_listing_incomplete():
                return "incomplete"
            if self._absent_vm_orphan_eligible(obj.get_device_vm()):
                return None

        if decision.status == "none" and isinstance(obj, (NBVM, NBDevice, NBIPAddress)) and \
                self._has_tag(obj, primary_tag_name) and \
                self._has_tag(obj, f"{primary_tag_name}: Orphaned") is False:
            self.report_ambiguous_owner(obj, decision)
            return "ambiguous"

        return None

    def _absent_vm_orphan_eligible(self, vm):
        """
        Whether vm is an absent sync-managed VM that already carries the orphan
        tag, or would receive it from the same rules as VM tagging.

        Computed directly. An orphan tag this pass has not written yet is not
        required, and a retention result of None is not eligibility on its own.
        """

        if not isinstance(vm, NBVM):
            return False
        if vm.source is not None:
            return False
        if not self._has_tag(vm, primary_tag_name):
            return False
        if self._tagged_by_disabled_source(vm):
            return False
        if getattr(vm, "prune", False) is not True:
            return False
        if self.retention_reason(vm) is not None:
            return False
        source_tags = self._source_tag_names(vm)
        if not source_tags and self._ignore_unknown_source_pruning():
            return False
        if self._has_tag(vm, f"{primary_tag_name}: Orphaned"):
            return True
        return True

    def _tagged_by_disabled_source(self, obj):
        disabled = []
        for source in self.source_list:
            if grab(source, "settings.enabled", fallback=True) is False and getattr(source, "source_tag", None):
                disabled.append(source.source_tag)
        return bool(set(obj.get_tags()).intersection(disabled))

    def _ignore_unknown_source_pruning(self):
        handler = getattr(self, "_tagging_handler", None)
        if handler is None:
            return False
        return grab(handler, "settings.ignore_unknown_source_object_pruning", fallback=False) is True

    def _any_enabled_vmware_listing_incomplete(self):
        for source in self.source_list:
            if grab(source, "settings.enabled", fallback=True) is False:
                continue
            if getattr(source, "source_type", None) != "vmware":
                continue
            if getattr(source, "inventory_complete", True) is False:
                return True
        return False

    def note_unresolved_claim(self, ip, interface):
        """Remember that this interface reported ip and the run did not assign it there."""

        if ip is None or interface is None:
            return
        claims = getattr(ip, "unresolved_claims", None)
        if not isinstance(claims, dict):
            claims = {}
            ip.unresolved_claims = claims
        claims[id(interface)] = interface

    def resolve_unresolved_claim(self, ip, interface):
        """Drop only this interface's skipped claim. Another interface's claim stays."""

        claims = getattr(ip, "unresolved_claims", None)
        if isinstance(claims, dict) and interface is not None:
            claims.pop(id(interface), None)

    def has_unresolved_claim(self, ip):
        claims = getattr(ip, "unresolved_claims", None)
        return isinstance(claims, dict) and len(claims) > 0

    def _any_source_listing_incomplete(self):
        for source in self.source_list:
            if grab(source, "settings.enabled", fallback=True) is False:
                continue
            if getattr(source, "inventory_complete", True) is False:
                return True
        return False

    def _keep_current_assignment(self, ip):
        """Drop a queued unassignment and the primary clear that went with it."""

        for attribute in ("assigned_object_id", "assigned_object_type"):
            if attribute in ip.unset_items:
                ip.unset_items.remove(attribute)
        device = ip.get_device_vm()
        if device is None:
            return
        for version in (4, 6):
            attribute = f"primary_ip{version}"
            if grab(device, f"data.{attribute}") is ip and attribute in device.unset_items:
                device.unset_items.remove(attribute)

    def _pending_removal_still_applies(self, ip, removal):
        """A later confirmation of this assignment by the same source retires the hold."""

        source = removal.get("source")
        interface = removal.get("interface")
        if source is None or interface is None or ip.get_interface() is not interface:
            return False
        sequence = removal.get("sequence", 0)
        tag = getattr(source, "source_tag", None)
        for mark in getattr(ip, "assignment_confirmations", None) or []:
            if mark.get("interface") is not interface or mark.get("sequence", 0) <= sequence:
                continue
            if mark.get("source") is source or (tag is not None and mark.get("tag") == tag):
                return False
        device = removal.get("device")
        if device is None:
            device = interface.data.get("virtual_machine") or interface.data.get("device")
        return source.may_remove_ip_assignments(device, ip=ip) is True

    def _apply_pending_ip_removal(self, ip, removals):
        applicable = [item for item in removals if self._pending_removal_still_applies(ip, item)]
        if not applicable:
            return
        removal = applicable[-1]
        source = removal.get("source")
        interface = removal.get("interface")
        log.info(f"{ip.name} is no longer assigned to {interface.get_display_name()} and "
                 f"therefore removed from this interface")
        source.release_ip_retention(ip)
        ip.remove_interface_association()

    def settle_deferred_ip_moves(self):
        """
        Resolve every competing report from this run, then the old assignment.

        A VMware move is not applied while another interface may still report
        the same address. The old interface's removal is held with those
        reports. One eligible interface receives the address. Several eligible
        interfaces leave the current assignment and its primary in place, with
        nothing queued for the write.
        """

        for ip in list(self.get_all_items(NBIPAddress)):
            requests = getattr(ip, "deferred_moves", None)
            removals = getattr(ip, "pending_removals", None)
            if not isinstance(requests, list):
                requests = []
            if not isinstance(removals, list):
                removals = []
            if len(requests) == 0 and len(removals) == 0:
                continue

            grouped = []
            for request in requests:
                interface = request.get("interface")
                if interface is None:
                    continue
                group = next((item for item in grouped if item["interface"] is interface), None)
                if group is None:
                    grouped.append({"interface": interface, "requests": [request]})
                else:
                    group["requests"].append(request)

            try:
                self._settle_one_ip(ip, requests, removals, grouped)
            finally:
                # Clear after the decision. Move eligibility reads the held
                # removal, and clearing it first makes the earlier confirmation
                # look current again.
                ip.deferred_moves = None
                ip.pending_removals = None

    def _settle_one_ip(self, ip, requests, removals, grouped):
        reporter = next((request.get("source") for request in requests if request.get("source") is not None), None)
        if len(grouped) > 1:
            for group in grouped:
                self.note_unresolved_claim(ip, group["interface"])
            if reporter is not None:
                reporter._report_left_assignment(ip, "more than one interface requested it")
            else:
                log.warning(f"Left IP address '{ip.get_display_name()}' on its current assignment: "
                            f"more than one interface requested it.")
            self._keep_current_assignment(ip)
            return

        if len(grouped) == 0:
            self._apply_pending_ip_removal(ip, removals)
            return

        group = grouped[0]
        request = group["requests"][0]
        source = request.get("source")
        interface = group["interface"]
        if source is None:
            self._keep_current_assignment(ip)
            return

        device = request.get("device")
        if device is None:
            device = interface.data.get("virtual_machine") or interface.data.get("device")
        decision = source._may_move_assigned_ip(ip, device, settle=True)
        if decision != "allow":
            # The report did not become an assignment. Keep it visible after
            # deferred_moves is cleared so tagging does not treat it as unclaimed.
            self.note_unresolved_claim(ip, interface)
            self._keep_current_assignment(ip)
            if decision == "defer":
                source._report_left_assignment(
                    ip, "its current source has not reported whether the address is still assigned")
            return

        # The source that held the address has now omitted it, so its
        # earlier unverified mark no longer applies. The move replaces the
        # held removal; that removal must not also be written.
        tag = source._responsible_guest_tag(ip)
        retained = getattr(ip, "retained_by", None)
        if isinstance(retained, set) and tag:
            retained.discard(tag)
            ip.unverified_retain = bool(retained)
        source.release_ip_retention(ip)
        ip.update(data={
            "address": request.get("address") or grab(ip, "data.address"),
            "assigned_object_id": interface,
        }, source=source)
        self.resolve_unresolved_claim(ip, interface)

        version = next((item.get("primary_version") for item in group["requests"]
                        if item.get("primary_version") in (4, 6)), None)
        primary_source = next((item.get("source") for item in group["requests"]
                               if item.get("primary_version") in (4, 6) and item.get("source") is not None),
                              source)
        if version is not None and primary_source is not None:
            primary_source.assign_primary_ip(ip.get_device_vm() or device, ip, version)

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

        self._tagging_handler = netbox_handler
        all_sources_tags = [x.source_tag for x in self.source_list]
        disabled_sources_tags = \
            [x.source_tag for x in self.source_list if grab(x, "settings.enabled", fallback=False) is False]

        # Decide retention before any source tag is removed. Prune reads the same
        # flag, including when a later tag write does not succeed.
        for object_type in NetBoxObject.__subclasses__():
            for this_object in self.get_all_items(object_type):
                this_object.retention_reason = self.retention_reason(this_object)
                this_object.retain_from_prune = this_object.retention_reason is not None

        for object_type in NetBoxObject.__subclasses__():

            for this_object in self.get_all_items(object_type):

                this_object_tags = this_object.get_tags()

                # if object was found in source
                if this_object.source is not None:
                    # source or seen_this_run is not a completed move while any
                    # interface's claim is still unresolved. Resolving one
                    # interface does not retire a different one.
                    if isinstance(this_object, NBIPAddress) and self.has_unresolved_claim(this_object):
                        continue

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

                    # Retention is decided from the source tags still stored on the
                    # object. Removing those tags first makes the next run, loaded
                    # from NetBox, forget why the object was protected.
                    if getattr(this_object, "retain_from_prune", False) is True:
                        if this_object.retention_reason in ("unknown", "seen") and \
                                netbox_handler.orphaned_tag in this_object.get_tags():
                            this_object.remove_tags(netbox_handler.orphaned_tag)
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

                    # A VM that was seen this run and is not active is powered off.
                    # An absent VM is not. decommissioning alone is not the distinction:
                    # that status can be set by hand, and the sync also sets it when
                    # it orphans a VM that is gone.
                    if isinstance(this_object, NBIPAddress):
                        device_vm_object = this_object.get_device_vm()

                        if isinstance(device_vm_object, NBVM) and device_vm_object.source is not None and \
                                grab(device_vm_object, "data.status") is not None and \
                                "active" not in str(grab(device_vm_object, "data.status")):
                            if netbox_handler.orphaned_tag in this_object.get_tags():
                                this_object.remove_tags(netbox_handler.orphaned_tag)
                            log.debug2(f"{device_vm_object.name} '{device_vm_object.get_display_name()}' has IP "
                                       f"'{this_object.get_display_name()}' assigned but is in status "
                                       f"{grab(device_vm_object, 'data.status')}. "
                                       f"IP address will not marked as orphaned.")
                            continue

                        if device_vm_object is not None and not isinstance(device_vm_object, NBVM) and \
                                grab(device_vm_object, "data.status") is not None and \
                                "active" not in str(grab(device_vm_object, "data.status")):
                            if netbox_handler.orphaned_tag in this_object.get_tags():
                                this_object.remove_tags(netbox_handler.orphaned_tag)
                            continue

                    already_orphaned = netbox_handler.orphaned_tag in this_object.get_tags()
                    this_object.add_tags(netbox_handler.orphaned_tag)
                    if already_orphaned is False:
                        this_object.orphan_tag_added_this_run = True
                        this_object.orphan_tag_last_updated_at_add = grab(this_object, "data.last_updated")

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
