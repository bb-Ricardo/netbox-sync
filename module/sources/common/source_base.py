# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import re

from ipaddress import ip_interface, ip_address, IPv6Address, IPv4Address, IPv6Network, IPv4Network
from packaging import version

from module.netbox import *
from module.common.logging import get_logger
from module.common.misc import grab

log = get_logger()


class SourceBase:
    """
    This is the base class for all import source classes. It provides some helpful common methods.
    """

    inventory = None
    source_tag = None
    settings = None
    init_successful = False
    name = None

    def set_source_tag(self):
        self.source_tag = f"Source: {self.name}"

    @classmethod
    def implements(cls, source_type):

        if getattr(cls, "source_type", None) == source_type:
            return True

        return False

    # stub function to implement a finish call for each source
    def finish(self):
        pass

    def map_object_interfaces_to_current_interfaces(self, device_vm_object, interface_data_dict=None,
                                                    append_unmatched_interfaces=False):
        """
        Try to match current object interfaces to discovered ones. This will be done
        by multiple approaches. Order as following listing whatever matches first will be chosen.

            by simple name:
                both interface names match exactly
            by MAC address separated by physical and virtual NICs:
                MAC address of interfaces match exactly, distinguish between physical and virtual interfaces
            by MAC regardless of interface type
                MAC address of interfaces match exactly, type of interface does not matter

            If there are interfaces which don't match at all then the unmatched interfaces will be
            matched 1:1. Sort both lists (unmatched current interfaces, unmatched new interfaces)
            by name and assign them each other.

                eth0 > vNIC 1
                eth1 > vNIC 2
                ens1 > vNIC 3
                ...  > ...

        Parameters
        ----------
        device_vm_object: (NBDevice, NBVM)
            object type to look for
        interface_data_dict: dict
            dictionary with interface data to compare to existing machine
        append_unmatched_interfaces: bool
            if True add unmatched interfaces as new interfaces instead of trying to assign to en unmatched on

        Returns
        -------
        dict: {"$interface_name": associated_interface_object}
            if no current interface was left to match "None" will be returned instead of
            a matching interface object
        """

        """
            trying multiple ways to match interfaces
        """

        if not isinstance(device_vm_object, (NBDevice, NBVM)):
            raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

        if not isinstance(interface_data_dict, dict):
            raise ValueError(f"Value for 'interface_data_dict' must be a dict, got: {interface_data_dict}")

        log.debug2("Trying to match current object interfaces in NetBox with discovered interfaces")

        current_object_interfaces = {
            "virtual": dict(),
            "physical": dict()
        }

        current_object_interface_names = list()

        return_data = dict()

        # grab current data
        for interface in self.inventory.get_all_interfaces(device_vm_object):
            int_mac = grab(interface, "data.mac_address")
            int_name = grab(interface, "data.name")
            int_type = "virtual"
            if "virtual" not in str(grab(interface, "data.type", fallback="virtual")):
                int_type = "physical"

            if int_mac is not None:
                current_object_interfaces[int_type][int_mac] = interface
                current_object_interfaces[int_mac] = interface

            if int_name is not None:
                current_object_interfaces[int_name] = interface
                current_object_interface_names.append(int_name)

        log.debug2("Found '%d' NICs in NetBox for '%s'" %
                   (len(current_object_interface_names), device_vm_object.get_display_name()))

        unmatched_interface_names = list()

        for int_name, int_data in interface_data_dict.items():

            return_data[int_name] = None

            int_mac = grab(int_data, "mac_address", fallback="XX:XX:YY:YY:ZZ:ZZ")
            int_type = "virtual"
            if "virtual" not in str(grab(int_data, "type", fallback="virtual")):
                int_type = "physical"

            # match simply by name
            matching_int = None
            if int_name in current_object_interface_names:
                log.debug2(f"Found 1:1 name match for NIC '{int_name}'")
                matching_int = current_object_interfaces.get(int_name)

            # match mac by interface type
            elif grab(current_object_interfaces, f"{int_type}.{int_mac}") is not None:
                log.debug2(f"Found 1:1 MAC address match for {int_type} NIC '{int_name}'")
                matching_int = grab(current_object_interfaces, f"{int_type}.{int_mac}")

            # match mac regardless of interface type
            elif current_object_interfaces.get(int_mac) is not None and \
                    current_object_interfaces.get(int_mac) not in return_data.values():
                log.debug2(f"Found 1:1 MAC address match for NIC '{int_name}' (ignoring interface type)")
                matching_int = current_object_interfaces.get(int_mac)

            if isinstance(matching_int, (NBInterface, NBVMInterface)):
                return_data[int_name] = matching_int
                # ToDo:
                # check why sometimes names are not present anymore and remove fails
                if grab(matching_int, "data.name") in current_object_interface_names:
                    current_object_interface_names.remove(grab(matching_int, "data.name"))

            # no match found, we match the leftovers just by #1 -> #1, #2 -> #2, ...
            else:
                unmatched_interface_names.append(int_name)

        current_object_interface_names.sort()
        unmatched_interface_names.sort()

        # Don't match to existing interfaces, just append additionally to list of interfaces
        if append_unmatched_interfaces is True:
            for int_name in unmatched_interface_names:
                return_data[int_name] = None
        else:
            matching_nics = dict(zip(unmatched_interface_names, current_object_interface_names))

            for new_int, current_int in matching_nics.items():
                current_int_object = current_object_interfaces.get(current_int)
                log.debug2(f"Matching '{new_int}' to NetBox Interface '{current_int_object.get_display_name()}'")
                return_data[new_int] = current_int_object

        return return_data

    def return_longest_matching_prefix_for_ip(self, ip_to_match=None, site_name=None) -> NBPrefix|None:
        """
        This is a lazy approach to find the longest matching prefix to an IP address.
        If site_name is set only IP prefixes from that site are matched.

        Parameters
        ----------
        ip_to_match: IPv4Address, IPv6Address
            IP address to find prefix for
        site_name: str
            name of the site the prefix needs to be in

        Returns
        -------
        (NBPrefix, None): longest matching IP prefix, or None if no matching prefix was found
        """

        if ip_to_match is None or self.inventory is None:
            return

        if not isinstance(ip_to_match, (IPv4Address, IPv6Address)):
            raise ValueError("Value of 'ip_to_match' needs to be an IPv4Address or IPv6Address object.")

        site_object = None
        if site_name is not None:
            site_object = self.inventory.get_by_data(NBSite, data={"name": site_name})

            if site_object is None:
                log.error(f"Unable to find site '{site_name}' for IP {ip_to_match}. "
                          "Skipping to find Prefix for this IP.")
                return

        current_longest_matching_prefix_length = 0
        current_longest_matching_prefix = None

        for prefix in self.inventory.get_all_items(NBPrefix):

            if not prefix.matches_site(site_object):
                continue

            prefix_network = grab(prefix, f"data.{NBPrefix.primary_key}")
            if prefix_network is None:
                continue

            if ip_to_match in prefix_network and \
                    prefix_network.prefixlen >= current_longest_matching_prefix_length:
                current_longest_matching_prefix_length = prefix_network.prefixlen
                current_longest_matching_prefix = prefix

        return current_longest_matching_prefix

    def add_update_interface(self, interface_object, device_object, interface_data, interface_ips=None,
                             vmware_object=None):
        """
        Adds/Updates an interface to/of a NBVM or NBDevice including IP addresses.
        Validates/enriches data in following order:
          * extract untagged_vlan from data
          * try to find tagged_vlan_objects
          * add/update interface
          * loop over list of IPs and add each IP to the Interface if
            * IP is valid
            * does not belong to another active interface
          * extract prefixes belonging to the IPs
          * use VLAN data from prefixes to match/add to the interface

        Parameters
        ----------
        interface_object: NBVMInterface | NBInterface | None
            object handle of the current interface (if existent, otherwise None)
        device_object: NBVM | NBDevice
            device object handle this interface belongs to
        interface_data: dict
            dictionary with interface attributes to add to this interface
        interface_ips: list
            a list of ip addresses which are assigned to this interface
        vmware_object: vim.HostSystem | vim.VirtualMachine
            object to add to list of objects to reevaluate

        Returns
        -------
        objects:
            tuple of NBVMInterface | NBInterface and list
            tuple with interface object that was added/updated and a list of ip address objects which were
            added to this interface
        """

        # handle change to mac_address object from NetBox 4.2 on
        interface_mac_address = None
        if version.parse(self.inventory.netbox_api_version) >= version.parse("4.2.0") and \
                interface_data.get("mac_address") is not None:
            interface_mac_address = interface_data.get("mac_address")
            del(interface_data["mac_address"])

        ip_tenant_inheritance_order = self.settings.ip_tenant_inheritance_order

        if not isinstance(interface_data, dict):
            log.error(f"Attribute 'interface_data' must be a dict() got {type(interface_data)}.")
            return None

        device_object_cluster = grab(device_object, "data.cluster")
        device_object_site = grab(device_object, "data.site")

        if type(device_object) == NBVM:
            interface_class = NBVMInterface
            site_name = device_object_cluster.get_site_name()
        elif type(device_object) == NBDevice:
            interface_class = NBInterface
            site_name = device_object.get_site_name()
        elif device_object is None:
            log.error(f"No device/VM object submitted to attach interface '{grab(interface_data, 'name')}' to.")
            return None
        else:
            log.error(f"Device object for interface '{grab(interface_data, 'name')}' must be a 'NBVM' or 'NBDevice'. "
                      f"Got {type(device_object)}")
            return None

        if interface_object is not None and not isinstance(interface_object, interface_class):
            log.error(f"Interface object '{grab(interface_data, 'name')}' must be a '{interface_class.name}'.")
            return None

        # get vlans from interface data and remove it for now from interface data dict
        # vlans get added later once we have the prefixes for the IP addresses
        untagged_vlan = interface_data.get("untagged_vlan")
        if untagged_vlan is not None:
            del interface_data["untagged_vlan"]

        tagged_vlans = interface_data.get("tagged_vlans") or list()
        if len(tagged_vlans) > 0:
            del interface_data["tagged_vlans"]

        # get device tenant
        device_tenant = grab(device_object, "data.tenant")

        # add object to interface
        interface_data[interface_class.secondary_key] = device_object

        # create or update interface with data
        if interface_object is None:
            interface_object = self.inventory.add_object(interface_class, data=interface_data, source=self)
        else:
            interface_object.update(data=interface_data, source=self)

        if version.parse(self.inventory.netbox_api_version) >= version.parse("4.2.0") and \
                interface_mac_address is not None:

            primary_mac_address_data = {
                "mac_address": interface_mac_address,
                "assigned_object_id": interface_object,
                "assigned_object_type": interface_class
            }

            primary_mac_address_object = None
            # check for associated MAC addresses on existing interface
            if interface_object.is_new is False:
                current_primary_mac_address_object = grab(interface_object, "data.primary_mac_address")
                if grab(current_primary_mac_address_object, "data.mac_address") == interface_mac_address:
                    primary_mac_address_object = current_primary_mac_address_object
                for mac_address_object in interface_object.get_mac_addresses():
                    if (primary_mac_address_object is None and
                            grab(mac_address_object, "data.mac_address") == interface_mac_address):
                        primary_mac_address_object = mac_address_object
                    if mac_address_object is not primary_mac_address_object:
                        mac_address_object.remove_interface_association()

            # if a new interface or not matching assigned MAC address, try to find an existing unassigned mac address
            if primary_mac_address_object is None:
                for mac_address_object in self.inventory.get_all_items(NBMACAddress):
                    if (grab(mac_address_object, "data.mac_address") == interface_mac_address and
                            grab(mac_address_object, "data.assigned_object_id") is None):
                        primary_mac_address_object = mac_address_object
                        break

            # of no existing mac address could be found, create a new one
            if primary_mac_address_object is None:
                primary_mac_address_object = self.inventory.add_object(NBMACAddress, data=primary_mac_address_data,
                                                                       source=self)
            else:
                primary_mac_address_object.update(data=primary_mac_address_data, source=self)

            interface_object.update(data={"primary_mac_address": primary_mac_address_object}, source=self)

        # skip handling of IPs for VMs with not installed/running guest tools
        skip_ip_handling = False
        if type(device_object) == NBVM and grab(vmware_object,'guest.toolsRunningStatus') != "guestToolsRunning":
            log.debug(f"VM '{device_object.name}' guest tool status is 'NotRunning', skipping IP handling")
            skip_ip_handling = True

        ip_address_objects = list()
        matching_ip_prefixes = list()
        # add all interface IPs
        for nic_ip in interface_ips or list():

            if skip_ip_handling is True:
                continue

            # get IP and prefix length
            try:
                if "/" in nic_ip:
                    ip_object = ip_interface(nic_ip)
                else:
                    ip_object = ip_address(nic_ip)
            except ValueError:
                log.error(f"IP '{nic_ip}' ({interface_object.get_display_name()}) does not appear "
                          "to be a valid IP address. Skipping!")
                continue

            log.debug2(f"Trying to find prefix for IP: {ip_object}")

            possible_ip_vrf = None
            prefix_tenant = None

            # test for site prefixes first
            matching_ip_prefix = self.return_longest_matching_prefix_for_ip(ip_object, site_name)

            # nothing was found then check prefixes without site name
            if matching_ip_prefix is None:
                matching_ip_prefix = self.return_longest_matching_prefix_for_ip(ip_object)

            # matching prefix found, get data from prefix
            if matching_ip_prefix is not None:

                this_prefix = grab(matching_ip_prefix, f"data.{NBPrefix.primary_key}")
                prefix_scope = matching_ip_prefix.get_scope_display_name()
                if prefix_scope is None:
                    log.debug2(f"Found IP '{ip_object}' matches global prefix '{this_prefix}'")
                else:
                    log.debug2(f"Found IP '{ip_object}' matches {prefix_scope} prefix "
                               f"'{this_prefix}'")

                # check if prefix net size and ip address prefix length match
                if not isinstance(ip_object, (IPv6Address, IPv4Address)) and \
                        this_prefix.prefixlen != ip_object.network.prefixlen:
                    log.warning(f"IP prefix length of '{ip_object}' ({interface_object.get_display_name()}) "
                                f"does not match network prefix length '{this_prefix}'!")

                possible_ip_vrf = grab(matching_ip_prefix, "data.vrf")
                prefix_tenant = grab(matching_ip_prefix, "data.tenant")

                matching_ip_prefixes.append(matching_ip_prefix)

            else:
                log_text = f"No matching NetBox prefix for '{ip_object}' found"

                # check if IP address is of type IP interface (includes prefix length)
                if type(ip_object) in [IPv6Address, IPv4Address]:
                    log.warning(f"{log_text}. Unable to add IP address to NetBox")
                    continue
                else:
                    log.debug2(log_text)

            # try to add prefix length to IP address if present
            if matching_ip_prefix is not None and type(ip_object) in [IPv6Address, IPv4Address]:
                this_prefix = grab(matching_ip_prefix, "data.prefix")
                if type(this_prefix) in [IPv4Network, IPv6Network]:
                    ip_object = ip_interface(f"{ip_object}/{this_prefix.prefixlen}")
                else:
                    log.warning(f"{matching_ip_prefix.name} got wrong format. Unable to add IP address to NetBox")
                    continue

            # try to find matching IP address object
            this_ip_object = None
            skip_this_ip = False
            for ip in self.inventory.get_all_items(NBIPAddress):

                # check if address matches (without prefix length)
                ip_address_string = grab(ip, "data.address", fallback="")

                # not a matching address
                if not ip_address_string.startswith(f"{ip_object.ip.compressed}/"):
                    continue

                current_ip_nic = ip.get_interface()
                current_ip_device = ip.get_device_vm()

                # is it our current ip interface?
                if current_ip_nic == interface_object:
                    this_ip_object = ip
                    break

                # check if IP has the same prefix
                # continue if
                #   * both are in global scope
                #   * both are part of the same vrf
                current_vrf = grab(ip, "data.vrf")
                if possible_ip_vrf != current_vrf:
                    possible_ip_vrf_str = possible_ip_vrf if not isinstance(possible_ip_vrf, NetBoxObject) \
                        else possible_ip_vrf.get_display_name()
                    current_vrf_str = current_vrf if not isinstance(current_vrf, NetBoxObject) \
                        else current_vrf.get_display_name()
                    current_ip_nic_str = "" if not isinstance(current_ip_nic, NetBoxObject) else \
                        " "+current_ip_nic.get_display_name()

                    log.warning(f"Possibly wrongly assigned VRF for{current_ip_nic_str} IP "
                                f"'{ip_address_string}'. Current VRF '{current_vrf_str}' and "
                                f"possible VRF '{possible_ip_vrf_str}'")
                    continue

                # IP address is not assigned to any interface
                if not isinstance(current_ip_nic, (NBInterface, NBVMInterface)):
                    this_ip_object = ip
                    break

                # IP address already belongs to the device but maybe to a different interface
                if device_object is current_ip_device:
                    this_ip_object = ip
                    break

                # get current IP interface status
                current_nic_enabled = grab(current_ip_nic, "data.enabled", fallback=True)
                this_nic_enabled = grab(interface_object, "data.enabled", fallback=True)

                # if device or VM is NOT active, set current nic status to disabled
                if "active" not in str(grab(current_ip_device, "data.status")):
                    current_nic_enabled = False

                if current_nic_enabled is True and this_nic_enabled is False:
                    log.debug(f"Current interface '{current_ip_nic.get_display_name()}' for IP '{ip_object}'"
                              f" is enabled and this one '{interface_object.get_display_name()}' is disabled. "
                              f"IP assignment skipped!")
                    skip_this_ip = True
                    break

                if current_nic_enabled is False and this_nic_enabled is True:
                    log.debug(f"Current interface '{current_ip_nic.get_display_name()}' for IP '{ip_object}'"
                              f" is disabled and this one '{interface_object.get_display_name()}' is enabled. "
                              f"IP will be assigned to this interface.")

                    this_ip_object = ip

                if grab(ip, "data.role.value") == "anycast":
                    log.debug(f"{ip.name} '{ip.get_display_name()}' is an Anycast address and "
                              f"can be assigned to multiple interfaces at the same time.")
                    skip_this_ip = True
                    break

                if current_nic_enabled == this_nic_enabled:

                    this_log_handler = log.warning
                    state = "enabled" if this_nic_enabled is True else "disabled"
                    log_msg = (f"Current interface '{current_ip_nic.get_display_name()}' for IP "
                               f"'{ip_object}' and this one '{interface_object.get_display_name()}' are "
                               f"both {state}.")

                    if hasattr(self, "objects_to_reevaluate") and vmware_object is not None and \
                            getattr(self, "parsing_objects_to_reevaluate", True) is False:
                        if vmware_object not in self.objects_to_reevaluate:
                            self.objects_to_reevaluate.append(vmware_object)
                        this_log_handler = log.debug
                        log_msg += f" The {device_object.name} will be checked later again to see if " \
                                   f"current interface status or association has changed"
                    else:
                        log_msg += " IP assignment skipped because it is unclear which one is the correct one!"

                    this_log_handler(log_msg)
                    skip_this_ip = True
                    break

            if skip_this_ip is True:
                continue

            nic_ip_data = {
                "address": ip_object.compressed,
                "assigned_object_id": interface_object,
            }

            # skip reassignment if IP is assigned to sub interface of a VM
            if (type(device_object) == NBVM and this_ip_object is not None and
                    grab(this_ip_object.get_interface(), "data.parent") is not None):
                current_ip_nic = this_ip_object.get_interface()
                current_ip_nic_parent = grab(current_ip_nic, "data.parent")
                if isinstance(current_ip_nic_parent, dict):
                    current_ip_nic_parent = self.inventory.get_by_id(NBVMInterface,
                                                                     nb_id=current_ip_nic_parent.get("id"))

                if current_ip_nic_parent == interface_object:
                    log.debug(f"{this_ip_object.name} '{this_ip_object.get_display_name()}' is assigned to sub interface "
                              f"'{current_ip_nic.get_display_name()}' of '{interface_object.get_display_name()}'. "
                              f"Not changing assignment")
                    nic_ip_data["assigned_object_id"] = current_ip_nic

            # grab tenant from device/vm if prefix didn't provide a tenant
            ip_tenant = None
            if isinstance(ip_tenant_inheritance_order, list) and "disabled" not in ip_tenant_inheritance_order:

                ip_tenant_inheritance_order_copy = ip_tenant_inheritance_order.copy()
                while len(ip_tenant_inheritance_order_copy) > 0:
                    ip_tenant_source = ip_tenant_inheritance_order_copy.pop(0)
                    if ip_tenant_source == "device" and device_tenant is not None:
                        ip_tenant = device_tenant
                        break
                    if ip_tenant_source == "prefix" and prefix_tenant is not None:
                        ip_tenant = prefix_tenant
                        break

            if possible_ip_vrf is not None:
                nic_ip_data["vrf"] = possible_ip_vrf
            if ip_tenant is not None:
                nic_ip_data["tenant"] = ip_tenant

            if not isinstance(this_ip_object, NBIPAddress):
                log.debug(f"No existing {NBIPAddress.name} object found. Creating a new one.")

                this_ip_object = self.inventory.add_object(NBIPAddress, data=nic_ip_data, source=self)

            # update IP address with additional data if not already present
            else:

                log.debug2(f"Found existing NetBox {NBIPAddress.name} object: {this_ip_object.get_display_name()}")

                this_ip_object.update(data=nic_ip_data, source=self)

            ip_address_objects.append(this_ip_object)

        for current_ip in interface_object.get_ip_addresses():

            if skip_ip_handling is True:
                continue

            if grab(current_ip, "data.role.value") == "anycast":
                log.debug2(f"{current_ip.name} '{current_ip.get_display_name()}' is an Anycast address and will "
                          f"NOT be deleted from interface")
                continue

            if current_ip not in ip_address_objects:
                log.info(f"{current_ip.name} is no longer assigned to {interface_object.get_display_name()} and "
                         f"therefore removed from this interface")
                current_ip.remove_interface_association()

        matching_untagged_vlan = None
        matching_tagged_vlans = dict()
        tagged_vlan_ids = list()
        compiled_tagged_vlans = list()

        # compile list of tagged VLAN IDs
        for tagged_vlan in tagged_vlans:
            if isinstance(tagged_vlan, NBVLAN):
                compiled_tagged_vlans.append(tagged_vlan)
            else:
                tagged_vlan_ids.append(grab(tagged_vlan, "vid"))

        # try to match prefix VLANs
        for matching_prefix in matching_ip_prefixes:

            prefix_vlan = grab(matching_prefix, "data.vlan")
            if prefix_vlan is None:
                continue

            # find untagged vlans
            if untagged_vlan is None or grab(prefix_vlan, "data.vid") == untagged_vlan.get("vid"):

                if matching_untagged_vlan is None:
                    matching_untagged_vlan = prefix_vlan

            # find tagged vlans
            if grab(prefix_vlan, "data.vid") in tagged_vlan_ids:
                matching_tagged_vlans[grab(prefix_vlan, "data.vid")] = prefix_vlan

        # try to find vlan object if no matching prefix VLAN was found
        vlan_interface_data = dict()
        if untagged_vlan is not None or (untagged_vlan is None and len(tagged_vlans) == 0):
            if matching_untagged_vlan is None and untagged_vlan is not None:
                matching_untagged_vlan = self.get_vlan_object_if_exists(untagged_vlan, device_object_site,
                                                                        device_object_cluster)

                # don't sync newly discovered VLANs to NetBox
                if self.add_vlan_object_to_netbox(matching_untagged_vlan, site_name) is False:
                    matching_untagged_vlan = None

            elif matching_untagged_vlan is not None:
                log.debug2(f"Found matching prefix VLAN {matching_untagged_vlan.get_display_name()} for "
                           f"untagged interface VLAN.")

            if matching_untagged_vlan is not None:
                vlan_interface_data["untagged_vlan"] = self.add_vlan_group(matching_untagged_vlan, site_name,
                                                                           device_object_cluster)
                if grab(interface_object, "data.mode") is None:
                    vlan_interface_data["mode"] = "access"

        # try to find tagged vlan prefixes
        for tagged_vlan in [x for x in tagged_vlans if not isinstance(tagged_vlans, NBVLAN)]:

            matching_tagged_vlan = matching_tagged_vlans.get(grab(tagged_vlan, "vid"))
            if matching_tagged_vlan is not None:
                log.debug2(f"Found matching prefix VLAN {matching_tagged_vlan.get_display_name()} for "
                           f"tagged interface VLAN.")
            else:
                matching_tagged_vlan = self.get_vlan_object_if_exists(tagged_vlan, device_object_site,
                                                                      device_object_cluster)

                # don't sync newly discovered VLANs to NetBox
                if self.add_vlan_object_to_netbox(matching_tagged_vlan, site_name) is False:
                    matching_tagged_vlan = None

            if matching_tagged_vlan is not None:
                compiled_tagged_vlans.append(self.add_vlan_group(matching_tagged_vlan, site_name,
                                                                 device_object_cluster))

        if len(compiled_tagged_vlans) > 0:
            vlan_interface_data["tagged_vlans"] = compiled_tagged_vlans

        if len(vlan_interface_data.keys()) > 0:
            interface_object.update(data=vlan_interface_data, source=self)

        return interface_object, ip_address_objects

    @staticmethod
    def patch_data(object_to_patch, data, overwrite=False):
        """
        Patch data to only fill currently unset parameters.

        Parameters
        ----------
        object_to_patch: NetBoxObject
            Source object to patch
        data: dict
            New data to be patched in existing data
        overwrite: bool
            If True no patching will be performed and the data dict will be returned

        Returns
        -------
        data_to_update: dict
            A dict with data to append/patch
        """

        if overwrite is True:
            return data

        # only append data
        data_to_update = dict()
        for key, value in data.items():
            current_value = grab(object_to_patch, f"data.{key}")
            if current_value is None or str(current_value) == "":
                data_to_update[key] = value

        return data_to_update

    def add_vlan_group(self, vlan_data, vlan_site, vlan_cluster) -> NBVLAN | dict:
        """
        This function will try to find a matching VLAN group according to the settings.
        Name matching will take precedence over ID matching. First match wins.

        If nothing matches the input data the submitted 'vlan_data' will be returned

        Parameters
        ----------
        vlan_data: dict | NBVLAN
            A dict or NBVLAN object
        vlan_site: NBSite | str | None
            name of site for the VLAN
        vlan_cluster: NBCluster | str | None

        Returns
        -------
        NBVLAN | dict: the input vlan_data enriched with VLAN group if a match was found

        """

        # get VLAN details
        if isinstance(vlan_data, NBVLAN):
            if vlan_data.is_new is False:
                return vlan_data

            vlan_name = grab(vlan_data, "data.name")
            vlan_id = grab(vlan_data, "data.vid")
            vlan_current_site = grab(vlan_data, "data.site")
            # vlan already has a group attached
            if grab(vlan_data, "data.group") is not None:
                return vlan_data

        elif isinstance(vlan_data, dict):
            vlan_name = vlan_data.get("name")
            vlan_id = vlan_data.get("vid")
            vlan_current_site = vlan_data.get("site")
        else:
            return vlan_data

        if isinstance(vlan_site, str):
            vlan_site = self.inventory.get_by_data(NBSite, data={"name": vlan_site})

        if isinstance(vlan_cluster, str):
            vlan_cluster = self.inventory.get_by_data(NBCluster, data={"name": vlan_cluster})

        if isinstance(vlan_current_site, dict):
            vlan_current_site = self.inventory.get_by_data(NBSite, data=vlan_current_site)

        log_text = f"Trying to find a matching VLAN Group based on the VLAN name '{vlan_name}'"
        if vlan_site is not None:
            log_text += f", site '{vlan_site.get_display_name()}'"
        if vlan_cluster is not None:
            log_text += f", cluster '{vlan_cluster.get_display_name()}'"
        log_text += f" and VLAN ID '{vlan_id}'"
        log.debug(log_text)

        vlan_group = None
        for vlan_filter, vlan_group_name in self.settings.vlan_group_relation_by_name or list():
            if vlan_filter.matches(vlan_name, vlan_site):
                for inventory_vlan_group in self.inventory.get_all_items(NBVLANGroup):

                    if grab(inventory_vlan_group, "data.name") != vlan_group_name:
                        continue

                    if inventory_vlan_group.matches_site_cluster(vlan_site, vlan_cluster):
                        vlan_group = inventory_vlan_group
                        break

        if vlan_group is None:
            for vlan_filter, vlan_group_name in self.settings.vlan_group_relation_by_id or list():
                if vlan_filter.matches(vlan_id, vlan_site):
                    for inventory_vlan_group in self.inventory.get_all_items(NBVLANGroup):

                        if grab(inventory_vlan_group, "data.name") != vlan_group_name:
                            continue

                        if inventory_vlan_group.matches_site_cluster(vlan_site, vlan_cluster):
                            vlan_group = inventory_vlan_group
                            break

        if vlan_group is not None:
            log.debug2(f"Found matching VLAN group '{vlan_group.get_display_name()}'")
            """
            If a VLAN group has been found we also need to check if the vlan site and the scope of the VLAN group are
            matching. If the VLAN group has a different scope then site, we need to remove the site from the VLAN.

            Mitigation for: https://github.com/netbox-community/netbox/issues/18706
            """
            if isinstance(vlan_data, NBVLAN):
                vlan_data.update(data={"group": vlan_group})
                if vlan_current_site is not vlan_group.data.get("scope_id"):
                    vlan_data.unset_attribute("site")
            elif isinstance(vlan_data, dict):
                vlan_data["group"] = vlan_group
                if vlan_current_site is not vlan_group.data.get("scope_id"):
                    del(vlan_data["site"])
        else:
            log.debug2("No matching VLAN group found")

        return vlan_data

    def get_vlan_object_if_exists(self, vlan_data=None, vlan_site=None, vlan_cluster=None):
        """
        This function will try to find a matching VLAN object based on 'vlan_data'
        Will return matching objects in following order:
            * exact match: VLAN ID and site or VLAN Group which matches site or cluster
            * global match: VLAN ID matches but the VLAN has no site assigned
        If nothing matches the input data from 'vlan_data' will be returned

        Parameters
        ----------
        vlan_data: dict
            A dict with NBVLAN data attributes
        vlan_site: NBSite
            site object
        vlan_cluster: NBCluster
            cluster object

        Returns
        -------
        (NBVLAN, dict, None): matching VLAN object, dict or None (content of vlan_data) if no match found

        """

        if vlan_data is None:
            return None

        if isinstance(vlan_data, NBVLAN):
            return vlan_data

        if not isinstance(vlan_data, dict):
            raise ValueError("Value of 'vlan_data' needs to be a dict.")

        # check existing Devices for matches
        log.debug2(f"Trying to find a {NBVLAN.name} based on the VLAN ID '{vlan_data.get('vid')}'")

        if vlan_data.get("vid") is None:
            log.debug("No VLAN ID set in vlan_data while trying to find matching VLAN.")
            return vlan_data

        if vlan_site is None:
            vlan_site = self.inventory.get_by_data(NBSite, data=vlan_data.get("site"))
        elif isinstance(vlan_site, str):
            vlan_site = self.inventory.get_by_data(NBSite, data={"name": vlan_site})

        if isinstance(vlan_cluster, str):
            vlan_cluster = self.inventory.get_by_data(NBCluster, data={"name": vlan_cluster})

        return_data = vlan_data
        vlan_object_by_site = None
        vlan_object_by_group = None
        vlan_object_global = None

        for vlan in self.inventory.get_all_items(NBVLAN):

            if grab(vlan, "data.vid") != vlan_data.get("vid"):
                continue

            # try finding matching VLAN by site
            if vlan_site is not None and grab(vlan, "data.site") == vlan_site:
                vlan_object_by_site = vlan
                break

            # try find matching VLAN by group
            if grab(vlan, "data.group") is not None:
                vlan_group = grab(vlan, "data.group")
                if vlan_group.matches_site_cluster(vlan_site, vlan_cluster):
                    vlan_object_by_group = vlan
                    break

            if grab(vlan, "data.site") is None and grab(vlan, "data.group") is None:
                vlan_object_global = vlan

        if isinstance(vlan_object_by_site, NetBoxObject):
            return_data = vlan_object_by_site
            log.debug2(f"Found a {return_data.name} object which matches the site '{vlan_site.get_display_name()}': %s"
                       % vlan_object_by_site.get_display_name(including_second_key=True))

        elif isinstance(vlan_object_by_group, NetBoxObject):
            return_data = vlan_object_by_group
            vlan_group_object = grab(vlan_object_by_group, "data.group")
            vlan_group_object_scope_object = grab(vlan_object_by_group, "data.scope_id")
            scope_details = ""
            if vlan_group_object_scope_object is not None:
                scope_details = (f" ({vlan_group_object_scope_object.name} "
                                 f"{vlan_group_object_scope_object.get_display_name()})")
            log.debug2(f"Found a {return_data.name} object which matches the {vlan_group_object.name} "
                       f"'{vlan_group_object.get_display_name()}'{scope_details}: %s" %
                       vlan_object_by_group.get_display_name(including_second_key=True))

        elif isinstance(vlan_object_global, NetBoxObject):
            return_data = vlan_object_global
            log.debug2(f"Found a global matching {return_data.name} object: %s" %
                       vlan_object_global.get_display_name(including_second_key=True))

        else:
            log.debug2("No matching existing VLAN found for this VLAN ID.")

        return return_data

    def add_vlan_object_to_netbox(self, vlan_data, site_name=None):
        """
        Determines if a newly discovered VLAN should be synced to NetBox or not

        Parameters
        ----------
        vlan_data: dict, NetBoxVLAN
            dict with NBVLAN data attributes
        site_name: str
            name of site the VLAN could be present

        Returns
        -------
        Bool: True or False based on config settings

        """

        # VLAN is already an existing NetBox VLAN, then it can be reused
        if isinstance(vlan_data, NetBoxObject):
            return True

        if vlan_data is None:
            return False

        if self.settings.disable_vlan_sync is True:
            return False

        # get VLAN details
        vlan_name = vlan_data.get("name")
        vlan_id = vlan_data.get("vid")

        if vlan_id == 4095:
            log.debug(f"Skipping sync of VLAN '{vlan_name}' ID: '{vlan_id}' (VMware 'Virtual Guest Tagging') to NetBox")
            return False

        if vlan_id >= 4096:
            log.warning(f"Skipping sync of invalid VLAN '{vlan_name}' ID: '{vlan_id}'")
            return False

        for excluded_vlan in self.settings.vlan_sync_exclude_by_name or list():
            if excluded_vlan.matches(vlan_name, site_name):
                return False

        for excluded_vlan in self.settings.vlan_sync_exclude_by_id or list():
            if excluded_vlan.matches(vlan_id, site_name):
                return False

        return True

    def add_update_custom_field(self, data) -> NBCustomField:
        """
        Adds/updates a NBCustomField object with data.
        Update will only update the 'object_types' attribute.

        Parameters
        ----------
        data: dict
            dictionary with NBCustomField attributes

        Returns
        -------
        custom_field: NBCustomField
            new or updated NBCustomField
        """

        # enforce NetBox name constrains
        data["name"] = NetBoxObject.format_slug(
            re.sub('-+', '-', data.get("name").replace("_", "-")).strip("-"), 100)[0:50].replace("-", "_")

        custom_field = self.inventory.get_by_data(NBCustomField, data={"name": data.get("name")})

        if custom_field is None:
            custom_field = self.inventory.add_object(NBCustomField, data=data, source=self)
        else:
            custom_field.update(data={"object_types": data.get("object_types")}, source=self)

        return custom_field

# EOF
