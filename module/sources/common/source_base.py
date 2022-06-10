# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import re

from ipaddress import ip_interface, ip_address, IPv6Address, IPv4Address, IPv6Network, IPv4Network

from module.netbox.inventory import (
    NetBoxObject,
    NBDevice,
    NBVM,
    NBInterface,
    NBVMInterface,
    NBSite,
    NBPrefix,
    NBIPAddress,
    NBVLAN,
    NBCustomField
)
from module.common.logging import get_logger
from module.common.misc import grab

log = get_logger()


class SourceBase:
    """
    This is the base class for all import source classes. It provides some helpful common methods.
    """

    inventory = None
    source_tag = None

    def map_object_interfaces_to_current_interfaces(self, device_vm_object, interface_data_dict=None):
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
            matched 1:1. Sort both lists (unmatched current interfaces, unmatched new new interfaces)
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

        Returns
        -------
        dict: {"$interface_name": associated_interface_object}
            if no current current interface was left to match "None" will be returned instead of
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

            # no match found, we match the left overs just by #1 -> #1, #2 -> #2, ...
            else:
                unmatched_interface_names.append(int_name)

        current_object_interface_names.sort()
        unmatched_interface_names.sort()

        matching_nics = dict(zip(unmatched_interface_names, current_object_interface_names))

        for new_int, current_int in matching_nics.items():
            current_int_object = current_object_interfaces.get(current_int)
            log.debug2(f"Matching '{new_int}' to NetBox Interface '{current_int_object.get_display_name()}'")
            return_data[new_int] = current_int_object

        return return_data

    def return_longest_matching_prefix_for_ip(self, ip_to_match=None, site_name=None):
        """
        This is a lazy approach to find longest matching prefix to an IP address.
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

            if grab(prefix, "data.site") != site_object:
                continue

            prefix_network = grab(prefix, f"data.{NBPrefix.primary_key}")
            if prefix_network is None:
                continue

            if ip_to_match in prefix_network and \
                    prefix_network.prefixlen >= current_longest_matching_prefix_length:
                current_longest_matching_prefix_length = prefix_network.prefixlen
                current_longest_matching_prefix = prefix

        return current_longest_matching_prefix

    def add_update_interface(self, interface_object, device_object, interface_data, interface_ips=None):
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
        interface_object: NBVMInterface, NBInterface, None
            object handle of the current interface (if existent, otherwise None)
        device_object: NBVM, NBDevice
            device object handle this interface belongs to
        interface_data: dict
            dictionary with interface attributes to add to this interface
        interface_ips: list
            list of ip addresses which are assigned to this interface

        Returns
        -------
        objects: tuple((NBVMInterface, NBInterface), list)
            tuple with interface object that was added/updated and a list of ip address objects which were
            added to this interface
        """

        if not isinstance(interface_data, dict):
            log.error(f"Attribute 'interface_data' must be a dict() got {type(interface_data)}.")
            return None

        if type(device_object) == NBVM:
            interface_class = NBVMInterface
            site_name = grab(device_object, "data.cluster.data.site.data.name")
        elif type(device_object) == NBDevice:
            interface_class = NBInterface
            site_name = grab(device_object, "data.site.data.name")
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
            del(interface_data["untagged_vlan"])

        tagged_vlans = interface_data.get("tagged_vlans") or list()
        if len(tagged_vlans) > 0:
            del (interface_data["tagged_vlans"])

        # add object to interface
        interface_data[interface_class.secondary_key] = device_object

        # create or update interface with data
        if interface_object is None:
            interface_object = self.inventory.add_object(interface_class, data=interface_data, source=self)
        else:
            interface_object.update(data=interface_data, source=self)

        ip_address_objects = list()
        matching_ip_prefixes = list()
        # add all interface IPs
        for nic_ip in interface_ips or list():

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
            possible_ip_tenant = None

            # test for site prefixes first
            matching_site_name = site_name
            matching_ip_prefix = self.return_longest_matching_prefix_for_ip(ip_object, matching_site_name)

            # nothing was found then check prefixes without site name
            if matching_ip_prefix is None:
                matching_site_name = None
                matching_ip_prefix = self.return_longest_matching_prefix_for_ip(ip_object)

            # matching prefix found, get data from prefix
            if matching_ip_prefix is not None:

                this_prefix = grab(matching_ip_prefix, f"data.{NBPrefix.primary_key}")
                if matching_site_name is None:
                    log.debug2(f"Found IP '{ip_object}' matches global prefix '{this_prefix}'")
                else:
                    log.debug2(f"Found IP '{ip_object}' matches site '{matching_site_name}' prefix "
                               f"'{this_prefix}'")

                # check if prefix net size and ip address prefix length match
                if not isinstance(ip_object, (IPv6Address, IPv4Address)) and \
                        this_prefix.prefixlen != ip_object.network.prefixlen:
                    log.warning(f"IP prefix length of '{ip_object}' ({interface_object.get_display_name()}) "
                                f"does not match network prefix length '{this_prefix}'!")

                possible_ip_vrf = grab(matching_ip_prefix, "data.vrf")
                possible_ip_tenant = grab(matching_ip_prefix, "data.tenant")

                matching_ip_prefixes.append(matching_ip_prefix)

            else:
                log_text = f"No matching NetBox prefix for '{ip_object}' found"

                # check if IP address is of type IP interface (includes prefix length)
                if type(ip_object) in [IPv6Address, IPv4Address]:
                    log.warning(f"{log_text}. Unable to add IP address to NetBox.")
                    continue
                else:
                    log.debug2(log_text)

            # try to add prefix length to IP address if present
            if matching_ip_prefix is not None and type(ip_object) in [IPv6Address, IPv4Address]:
                this_prefix = grab(matching_ip_prefix, "data.prefix")
                if type(this_prefix) in [IPv4Network, IPv6Network]:
                    ip_object = ip_interface(f"{ip_object}/{this_prefix.prefixlen}")
                else:
                    log.warning(f"{matching_ip_prefix.name} got wrong format. Unable to add IP to NetBox")
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

                current_nic = grab(ip, "data.assigned_object_id")

                # is it our current ip interface?
                if current_nic == interface_object:
                    this_ip_object = ip
                    break

                # check if IP has the same prefix
                # continue if
                #   * both are in global scope
                #   * both are part of the same vrf
                if possible_ip_vrf != grab(ip, "data.vrf"):
                    continue

                # IP address is not assigned to any interface
                if not isinstance(current_nic, (NBInterface, NBVMInterface)):
                    this_ip_object = ip
                    break

                # get current IP interface status
                current_nic_enabled = grab(current_nic, "data.enabled", fallback=True)
                this_nic_enabled = grab(interface_object, "data.enabled", fallback=True)

                if current_nic_enabled is True and this_nic_enabled is False:
                    log.debug(f"Current interface '{current_nic.get_display_name()}' for IP '{ip_object}'"
                              f" is enabled and this one '{interface_object.get_display_name()}' is disabled. "
                              f"IP assignment skipped!")
                    skip_this_ip = True
                    break

                if current_nic_enabled is False and this_nic_enabled is True:
                    log.debug(f"Current interface '{current_nic.get_display_name()}' for IP '{ip_object}'"
                              f" is disabled and this one '{interface_object.get_display_name()}' is enabled. "
                              f"IP will be assigned to this interface.")

                    this_ip_object = ip

                if current_nic_enabled == this_nic_enabled:
                    state = "enabled" if this_nic_enabled is True else "disabled"
                    log.warning(f"Current interface '{current_nic.get_display_name()}' for IP "
                                f"'{ip_object}' and this one '{interface_object.get_display_name()}' are "
                                f"both {state}. "
                                f"IP assignment skipped because it is unclear which one is the correct one!")
                    skip_this_ip = True
                    break

            if skip_this_ip is True:
                continue

            nic_ip_data = {
                "address": ip_object.compressed,
                "assigned_object_id": interface_object,
            }

            # grab tenant from device/vm if prefix didn't provide a tenant
            if possible_ip_tenant is None:
                possible_ip_tenant = grab(device_object, "data.tenant")

            if not isinstance(this_ip_object, NBIPAddress):
                log.debug(f"No existing {NBIPAddress.name} object found. Creating a new one.")

                if possible_ip_vrf is not None:
                    nic_ip_data["vrf"] = possible_ip_vrf
                if possible_ip_tenant is not None:
                    nic_ip_data["tenant"] = possible_ip_tenant

                this_ip_object = self.inventory.add_object(NBIPAddress, data=nic_ip_data, source=self)

            # update IP address with additional data if not already present
            else:

                log.debug2(f"Found existing NetBox {NBIPAddress.name} object: {this_ip_object.get_display_name()}")

                if grab(this_ip_object, "data.vrf") is None and possible_ip_vrf is not None:
                    nic_ip_data["vrf"] = possible_ip_vrf

                if grab(this_ip_object, "data.tenant") is None and possible_ip_tenant is not None:
                    nic_ip_data["tenant"] = possible_ip_tenant

                this_ip_object.update(data=nic_ip_data, source=self)

            ip_address_objects.append(this_ip_object)

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

        # try to find vlan object if no matching prefix VLAN ws found
        vlan_interface_data = dict()
        if untagged_vlan is not None or (untagged_vlan is None and len(tagged_vlans) == 0):
            if matching_untagged_vlan is None and untagged_vlan is not None:
                matching_untagged_vlan = self.get_vlan_object_if_exists(untagged_vlan, site_name)
            elif matching_untagged_vlan is not None:
                log.debug2(f"Found matching prefix VLAN {matching_untagged_vlan.get_display_name()} for "
                           f"untagged interface VLAN.")

            if matching_untagged_vlan is not None:
                vlan_interface_data["untagged_vlan"] = matching_untagged_vlan
                if grab(interface_object, "data.mode") is None:
                    vlan_interface_data["mode"] = "access"

        # try to find tagged vlan prefixes
        for tagged_vlan in [x for x in tagged_vlans if not isinstance(tagged_vlans, NBVLAN)]:

            matching_tagged_vlan = matching_tagged_vlans.get(grab(tagged_vlan, "vid"))
            if matching_tagged_vlan is not None:
                log.debug2(f"Found matching prefix VLAN {matching_tagged_vlan.get_display_name()} for "
                           f"tagged interface VLAN.")
            else:
                matching_tagged_vlan = self.get_vlan_object_if_exists(tagged_vlan, site_name)

            compiled_tagged_vlans.append(matching_tagged_vlan)

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
            dict with data to append/patch
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

    def get_vlan_object_if_exists(self, vlan_data=None, vlan_site=None):
        """
        This function will try to find a matching VLAN object based on 'vlan_data'
        Will return matching objects in following order:
            * exact match: VLAN id and site match
            * global match: VLAN id matches but the VLAN has no site assigned
        If nothing matches the input data from 'vlan_data' will be returned

        Parameters
        ----------
        vlan_data: dict
            dict with NBVLAN data attributes
        vlan_site: str
            name of site the VLAN could be present

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
        log.debug2(f"Trying to find a {NBVLAN.name} based on the VLAN id '{vlan_data.get('vid')}'")

        if vlan_data.get("vid") is None:
            log.debug("No VLAN id set in vlan_data while trying to find matching VLAN.")
            return vlan_data

        if vlan_site is None:
            vlan_site = self.inventory.get_by_data(NBSite, data=vlan_data.get("site"))
        elif isinstance(vlan_site, str):
            vlan_site = self.inventory.get_by_data(NBSite, data={"name": vlan_site})

        return_data = vlan_data
        vlan_object_including_site = None
        vlan_object_without_site = None

        for vlan in self.inventory.get_all_items(NBVLAN):

            if grab(vlan, "data.vid") != vlan_data.get("vid"):
                continue

            if vlan_site is not None and grab(vlan, "data.site") == vlan_site:
                vlan_object_including_site = vlan

            if grab(vlan, "data.site") is None:
                vlan_object_without_site = vlan

        if isinstance(vlan_object_including_site, NetBoxObject):
            return_data = vlan_object_including_site
            log.debug2("Found a exact matching %s object: %s" %
                       (vlan_object_including_site.name,
                        vlan_object_including_site.get_display_name(including_second_key=True)))

        elif isinstance(vlan_object_without_site, NetBoxObject):
            return_data = vlan_object_without_site
            log.debug2("Found a global matching %s object: %s" %
                       (vlan_object_without_site.name,
                        vlan_object_without_site.get_display_name(including_second_key=True)))

        else:
            log.debug2("No matching existing VLAN found for this VLAN id.")

        return return_data

    def add_update_custom_field(self, data):
        """
        Adds/updates a NBCustomField object with data.
        Update will only update the 'content_types' attribute.

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
            custom_field.update(data={"content_types": data.get("content_types")}, source=self)

        return custom_field

# EOF
