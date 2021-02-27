# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2021 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import atexit
import pprint
import re
from ipaddress import ip_address, ip_network, ip_interface, IPv4Address, IPv6Address
from socket import gaierror

from pyVim.connect import SmartConnectNoSSL, Disconnect
from pyVmomi import vim

from module.common.logging import get_logger, DEBUG3
from module.common.misc import grab, dump, get_string_or_none, plural
from module.common.support import normalize_mac_address, ip_valid_to_add_to_netbox
from module.netbox.object_classes import *

log = get_logger()


# noinspection PyTypeChecker
class VMWareHandler:
    """
    Source class to import data from a vCenter instance and add/update NetBox objects based on gathered information
    """

    dependent_netbox_objects = [
        NBTag,
        NBManufacturer,
        NBDeviceType,
        NBPlatform,
        NBClusterType,
        NBClusterGroup,
        NBDeviceRole,
        NBSite,
        NBCluster,
        NBDevice,
        NBVM,
        NBVMInterface,
        NBInterface,
        NBIPAddress,
        NBPrefix,
        NBTenant,
        NBVRF,
        NBVLAN
    ]

    settings = {
        "enabled": True,
        "host_fqdn": None,
        "port": 443,
        "username": None,
        "password": None,
        "cluster_exclude_filter": None,
        "cluster_include_filter": None,
        "host_exclude_filter": None,
        "host_include_filter": None,
        "vm_exclude_filter": None,
        "vm_include_filter": None,
        "netbox_host_device_role": "Server",
        "netbox_vm_device_role": "Server",
        "permitted_subnets": None,
        "collect_hardware_asset_tag": True,
        "cluster_site_relation": None,
        "host_site_relation": None,
        "vm_tenant_relation": None,
        "vm_platform_relation": None,
        "dns_name_lookup": False,
        "custom_dns_servers": None,
        "set_primary_ip": "when-undefined",
        "skip_vm_comments": False,
        "skip_vm_templates": True,
        "strip_host_domain_name": False,
        "strip_vm_domain_name": False
    }

    init_successful = False
    inventory = None
    name = None
    source_tag = None
    source_type = "vmware"

    # internal vars
    session = None

    site_name = None

    network_data = {
        "vswitch": dict(),
        "pswitch": dict(),
        "host_pgroup": dict(),
        "dpgroup": dict(),
        "dpgroup_ports": dict()
    }

    permitted_clusters = dict()

    processed_host_names = dict()
    processed_vm_names = dict()
    processed_vm_uuid = list()

    parsing_vms_the_first_time = True

    def __init__(self, name=None, settings=None, inventory=None):

        if name is None:
            raise ValueError(f"Invalid value for attribute 'name': '{name}'.")

        self.inventory = inventory
        self.name = name

        self.parse_config_settings(settings)

        self.source_tag = f"Source: {name}"
        self.site_name = f"vCenter: {name}"

        if self.enabled is False:
            log.info(f"Source '{name}' is currently disabled. Skipping")
            return

        self.create_session()

        if self.session is None:
            log.info(f"Source '{name}' is currently unavailable. Skipping")
            return

        self.init_successful = True

    def parse_config_settings(self, config_settings):
        """
        Validate parsed settings from config file

        Parameters
        ----------
        config_settings: dict
            dict of config settings

        """

        validation_failed = False
        for setting in ["host_fqdn", "port", "username", "password"]:
            if config_settings.get(setting) is None:
                log.error(f"Config option '{setting}' in 'source/{self.name}' can't be empty/undefined")
                validation_failed = True

        # check permitted ip subnets
        if config_settings.get("permitted_subnets") is None:
            log.info(f"Config option 'permitted_subnets' in 'source/{self.name}' is undefined. "
                     f"No IP addresses will be populated to Netbox!")
        else:
            config_settings["permitted_subnets"] = \
                [x.strip() for x in config_settings.get("permitted_subnets").split(",") if x.strip() != ""]

            permitted_subnets = list()
            for permitted_subnet in config_settings["permitted_subnets"]:
                try:
                    permitted_subnets.append(ip_network(permitted_subnet))
                except Exception as e:
                    log.error(f"Problem parsing permitted subnet: {e}")
                    validation_failed = True

            config_settings["permitted_subnets"] = permitted_subnets

        # check include and exclude filter expressions
        for setting in [x for x in config_settings.keys() if "filter" in x]:
            if config_settings.get(setting) is None or config_settings.get(setting).strip() == "":
                continue

            re_compiled = None
            try:
                re_compiled = re.compile(config_settings.get(setting))
            except Exception as e:
                log.error(f"Problem parsing regular expression for '{setting}': {e}")
                validation_failed = True

            config_settings[setting] = re_compiled

        for relation_option in ["cluster_site_relation", "host_site_relation",
                                "vm_tenant_relation", "vm_platform_relation"]:

            if config_settings.get(relation_option) is None:
                continue

            relation_data = list()

            relation_type = relation_option.split("_")[1]

            # obey quotations to be able to add names including a comma
            # thanks to: https://stackoverflow.com/a/64333329
            for relation in re.split(r",(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)",
                                     config_settings.get(relation_option)):

                object_name = relation.split("=")[0].strip(' "')
                relation_name = relation.split("=")[1].strip(' "')

                if len(object_name) == 0 or len(relation_name) == 0:
                    log.error(f"Config option '{relation}' malformed got '{object_name}' for "
                              f"object name and '{relation_name}' for {relation_type} name.")
                    validation_failed = True

                try:
                    re_compiled = re.compile(object_name)
                except Exception as e:
                    log.error(f"Problem parsing regular expression '{object_name}' for '{relation}': {e}")
                    validation_failed = True
                    continue

                relation_data.append({
                    "object_regex": re_compiled,
                    f"{relation_type}_name": relation_name
                })

            config_settings[relation_option] = relation_data

        if config_settings.get("dns_name_lookup") is True and config_settings.get("custom_dns_servers") is not None:

            custom_dns_servers = \
                [x.strip() for x in config_settings.get("custom_dns_servers").split(",") if x.strip() != ""]

            tested_custom_dns_servers = list()
            for custom_dns_server in custom_dns_servers:
                try:
                    tested_custom_dns_servers.append(str(ip_address(custom_dns_server)))
                except ValueError:
                    log.error(f"Config option 'custom_dns_servers' value '{custom_dns_server}' "
                              f"does not appear to be an IP address.")
                    validation_failed = True

            config_settings["custom_dns_servers"] = tested_custom_dns_servers

        if validation_failed is True:
            log.error("Config validation failed. Exit!")
            exit(1)

        for setting in self.settings.keys():
            setattr(self, setting, config_settings.get(setting))

    def create_session(self):
        """
        Initialize session with vCenter

        Returns
        -------
        bool: if initialization was successful or not
        """

        if self.session is not None:
            return True

        log.debug(f"Starting vCenter connection to '{self.host_fqdn}'")

        try:
            instance = SmartConnectNoSSL(
                host=self.host_fqdn,
                port=self.port,
                user=self.username,
                pwd=self.password
            )
            atexit.register(Disconnect, instance)
            self.session = instance.RetrieveContent()

        except (gaierror, OSError) as e:
            log.error(
                f"Unable to connect to vCenter instance '{self.host_fqdn}' on port {self.port}. "
                f"Reason: {e}"
            )
            return False
        except vim.fault.InvalidLogin as e:
            log.error(f"Unable to connect to vCenter instance '{self.host_fqdn}' on port {self.port}. {e.msg}")
            return False

        log.info(f"Successfully connected to vCenter '{self.host_fqdn}'")

        return True

    def apply(self):
        """
        Main source handler method. This method is called for each source from "main" program
        to retrieve data from it source and apply it to the netBox inventory.

        Every update of new/existing objects fot this source has to happen here.
        """

        log.info(f"Query data from vCenter: '{self.host_fqdn}'")

        """
        Mapping of object type keywords to view types and handlers

        iterate over all VMs twice.

        To handle VMs with the same name in a cluster we first
        iterate over all VMs and look only at the active ones
        and sync these first.
        Then we iterate a second time to catch the rest.

        This has been implemented to support migration scenarios
        where you create the same machines with a different setup
        like a new version or something. This way NetBox will be
        updated primarily with the actual active VM data.

        # disabled, no useful information at this moment
            "virtual switch": {
                "view_type": vim.DistributedVirtualSwitch,
                "view_handler": self.add_virtual_switch
            },

        """
        object_mapping = {
            "datacenter": {
                "view_type": vim.Datacenter,
                "view_handler": self.add_datacenter
            },
            "cluster": {
                "view_type": vim.ClusterComputeResource,
                "view_handler": self.add_cluster
            },
            "network": {
                "view_type": vim.dvs.DistributedVirtualPortgroup,
                "view_handler": self.add_port_group
            },
            "host": {
                "view_type": vim.HostSystem,
                "view_handler": self.add_host
            },
            "virtual machine": {
                "view_type": vim.VirtualMachine,
                "view_handler": self.add_virtual_machine
            },
            "offline virtual machine": {
                "view_type": vim.VirtualMachine,
                "view_handler": self.add_virtual_machine
            }
        }

        for view_name, view_details in object_mapping.items():

            if self.session is None:
                log.info("No existing vCenter session found.")
                self.create_session()

            view_data = {
                "container": self.session.rootFolder,
                "type": [view_details.get("view_type")],
                "recursive": True
            }

            try:
                container_view = self.session.viewManager.CreateContainerView(**view_data)
            except Exception as e:
                log.error(f"Problem creating vCenter view for '{view_name}s': {e}")
                continue

            view_objects = grab(container_view, "view")

            if view_objects is None:
                log.error(f"Creating vCenter view for '{view_name}s' failed!")
                continue

            if view_name != "offline virtual machine":
                log.debug("vCenter returned '%d' %s%s" % (len(view_objects), view_name, plural(len(view_objects))))
            else:
                self.parsing_vms_the_first_time = False
                log.debug("Iterating over all virtual machines a second time ")

            for obj in view_objects:

                if log.level == DEBUG3:
                    try:
                        dump(obj)
                    except Exception as e:
                        log.error(e)

                view_details.get("view_handler")(obj)

            container_view.Destroy()

        self.update_basic_data()

    @staticmethod
    def passes_filter(name, include_filter, exclude_filter):
        """
        checks if object name passes a defined object filter.

        Parameters
        ----------
        name: str
            name of the object to check
        include_filter: regex object
            regex object of include filter
        exclude_filter: regex object
            regex object of exclude filter

        Returns
        -------
        bool: True if all filter passed, otherwise False
        """

        # first includes
        if include_filter is not None and not include_filter.match(name):
            log.debug(f"Object '{name}' did not match include filter '{include_filter.pattern}'. Skipping")
            return False

        # second excludes
        if exclude_filter is not None and exclude_filter.match(name):
            log.debug(f"Object '{name}' matched exclude filter '{exclude_filter.pattern}'. Skipping")
            return False

        return True

    def get_site_name(self, object_type, object_name, cluster_name=""):
        """
        Return a site name for a NBCluster or NBDevice depending on config options
        host_site_relation and cluster_site_relation

        Parameters
        ----------
        object_type: (NBCluster, NBDevice)
            object type to check site relation for
        object_name: str
            object name to check site relation for
        cluster_name: str
            cluster name of NBDevice to check for site name

        Returns
        -------
        str: site name if a relation was found
        """

        if object_type not in [NBCluster, NBDevice]:
            raise ValueError(f"Object must be a '{NBCluster.name}' or '{NBDevice.name}'.")

        log.debug2(f"Trying to find site name for {object_type.name} '{object_name}'")

        site_name = None

        # check if site was provided in config
        config_name = "host_site_relation" if object_type == NBDevice else "cluster_site_relation"

        site_relations = grab(self, config_name, fallback=list())

        for site_relation in site_relations:
            object_regex = site_relation.get("object_regex")
            if object_regex.match(object_name):
                site_name = site_relation.get("site_name")
                log.debug2(f"Found a match ({object_regex.pattern}) for {object_name}, using site '{site_name}'")
                break

        if object_type == NBDevice and site_name is None:
            site_name = self.permitted_clusters.get(cluster_name) or \
                        self.get_site_name(NBCluster, object_name, cluster_name)
            log.debug2(f"Found a matching cluster site for {object_name}, using site '{site_name}'")

        # set default site name
        if site_name is None:
            site_name = self.site_name
            log.debug(f"No site relation for '{object_name}' found, using default site '{site_name}'")

        return site_name

    def get_object_based_on_macs(self, object_type, mac_list=None):
        """
        Try to find a NetBox object based on list of MAC addresses.

        Iterate over all interfaces of this object type and compare MAC address with list of desired MAC
        addresses. If match was found store related machine object and count every correct match.

        If exactly one machine with matching interfaces was found then this one will be returned.

        If two or more machines with matching MACs are found compare the two machines with
        the highest amount of matching interfaces. If the ration of matching interfaces
        exceeds 2.0 then the top matching machine is chosen as desired object.

        If the ration is below 2.0 then None will be returned. The probability is to low that
        this one is the correct one.

        None will also be returned if no machine was found at all.

        Parameters
        ----------
        object_type: (NBDevice, NBVM)
            type of NetBox device to find in inventory
        mac_list: list
            list of MAC addresses to compare against NetBox interface objects

        Returns
        -------
        (NBDevice, NBVM, None): object instance of found device, otherwise None
        """

        object_to_return = None

        if object_type not in [NBDevice, NBVM]:
            raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

        if mac_list is None or not isinstance(mac_list, list) or len(mac_list) == 0:
            return

        interface_typ = NBInterface if object_type == NBDevice else NBVMInterface

        objects_with_matching_macs = dict()
        matching_object = None

        for interface in self.inventory.get_all_items(interface_typ):

            if grab(interface, "data.mac_address") in mac_list:

                matching_object = grab(interface, f"data.{interface.secondary_key}")
                if not isinstance(matching_object, (NBDevice, NBVM)):
                    continue

                log.debug2("Found matching MAC '%s' on %s '%s'" %
                           (grab(interface, "data.mac_address"), object_type.name,
                            matching_object.get_display_name(including_second_key=True)))

                if objects_with_matching_macs.get(matching_object) is None:
                    objects_with_matching_macs[matching_object] = 1
                else:
                    objects_with_matching_macs[matching_object] += 1

        # try to find object based on amount of matching MAC addresses
        num_devices_witch_matching_macs = len(objects_with_matching_macs.keys())

        if num_devices_witch_matching_macs == 1:

            log.debug2("Found one %s '%s' based on MAC addresses and using it" %
                       (object_type.name, matching_object.get_display_name(including_second_key=True)))

            object_to_return = list(objects_with_matching_macs.keys())[0]

        elif num_devices_witch_matching_macs > 1:

            log.debug2(f"Found {num_devices_witch_matching_macs} {object_type.name}s with matching MAC addresses")

            # now select the two top matches
            first_choice, second_choice = \
                sorted(objects_with_matching_macs, key=objects_with_matching_macs.get, reverse=True)[0:2]

            first_choice_matches = objects_with_matching_macs.get(first_choice)
            second_choice_matches = objects_with_matching_macs.get(second_choice)

            log.debug2(f"The top candidate {first_choice.get_display_name()} with {first_choice_matches} matches")
            log.debug2(f"The second candidate {second_choice.get_display_name()} with {second_choice_matches} matches")

            # get ratio between
            matching_ration = first_choice_matches / second_choice_matches

            # only pick the first one if the ration exceeds 2
            if matching_ration >= 2.0:
                log.debug2(f"The matching ratio of {matching_ration} is high enough "
                           f"to select {first_choice.get_display_name()} as desired {object_type.name}")
                object_to_return = first_choice
            else:
                log.debug2("Both candidates have a similar amount of "
                           "matching interface MAC addresses. Using NONE of them!")

        return object_to_return

    def get_object_based_on_primary_ip(self, object_type, primary_ip4=None, primary_ip6=None):
        """
        Try to find a NBDevice or NBVM based on the primary IP address. If an exact
        match was found the device/vm object will be returned immediately without
        checking of the other primary IP address (if defined).

        Parameters
        ----------
        object_type: (NBDevice, NBVM)
            object type to look for
        primary_ip4: str
            primary IPv4 address of object to find
        primary_ip6: str
            primary IPv6 address of object to find

        Returns
        -------

        """

        def _matches_device_primary_ip(device_primary_ip, ip_needle):

            ip = None
            if device_primary_ip is not None and ip_needle is not None:
                if isinstance(device_primary_ip, dict):
                    ip = grab(device_primary_ip, "address")

                elif isinstance(device_primary_ip, int):
                    ip = self.inventory.get_by_id(NBIPAddress, nb_id=device_primary_ip)
                    ip = grab(ip, "data.address")

                if ip is not None and ip.split("/")[0] == ip_needle:
                    return True

            return False

        if object_type not in [NBDevice, NBVM]:
            raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

        if primary_ip4 is None and primary_ip6 is None:
            return

        if primary_ip4 is not None:
            primary_ip4 = str(primary_ip4).split("/")[0]

        if primary_ip6 is not None:
            primary_ip6 = str(primary_ip6).split("/")[0]

        for device in self.inventory.get_all_items(object_type):

            if _matches_device_primary_ip(grab(device, "data.primary_ip4"), primary_ip4) is True:
                log.debug2(f"Found existing host '{device.get_display_name()}' "
                           f"based on the primary IPv4 '{primary_ip4}'")
                return device

            if _matches_device_primary_ip(grab(device, "data.primary_ip6"), primary_ip6) is True:
                log.debug2(f"Found existing host '{device.get_display_name()}' "
                           f"based on the primary IPv6 '{primary_ip6}'")
                return device

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

        log.debug2("Found '%d' NICs in Netbox for '%s'" %
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

            if matching_int is not None:
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
        ip_to_match: (IPv4Address, IPv6Address)
            IP address to find prefix for
        site_name: str
            name of the site the prefix needs to be in

        Returns
        -------
        (NBPrefix, None): longest matching IP prefix, or None if no matching prefix was found
        """

        if ip_to_match is None:
            return

        if not isinstance(ip_to_match, (IPv4Address, IPv6Address)):
            raise ValueError("Value of 'ip_to_match' needs to be an IPv4Address or IPv6Address this_object.")

        site_object = None
        if site_name is not None:
            site_object = self.inventory.get_by_data(NBSite, data={"name": site_name})

            if site_object is None:
                log.error(f"Unable to find site '{site_name}' for IP {ip_to_match}. "
                          "Skipping to find Prefix for this IP.")

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

    def get_vlan_object_if_exists(self, vlan_data=None):
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

        Returns
        -------
        (NBVLAN, dict, None): matching VLAN object, dict or None (content of vlan_data) if no match found

        """

        if vlan_data is None:
            return None

        if not isinstance(vlan_data, dict):
            raise ValueError("Value of 'vlan_data' needs to be a dict.")

        # check existing Devices for matches
        log.debug2(f"Trying to find a {NBVLAN.name} based on the VLAN id '{vlan_data.get('vid')}'")

        if vlan_data.get("vid") is None:
            log.debug("No VLAN id set in vlan_data while trying to find matching VLAN.")
            return vlan_data

        vlan_site = self.inventory.get_by_data(NBSite, data=vlan_data.get("site"))

        return_data = vlan_data
        vlan_object_including_site = None
        vlan_object_without_site = None

        for vlan in self.inventory.get_all_items(NBVLAN):

            if grab(vlan, "data.vid") != vlan_data.get("vid"):
                continue

            if vlan_site is not None and grab(vlan, "data.site") == vlan_site:
                vlan_object_including_site = vlan

            if grab(vlan, "data.site") == None:
                vlan_object_without_site = vlan

        if vlan_object_including_site is not None:
            return_data = vlan_object_including_site
            log.debug2("Found a exact matching %s object: %s" %
                       (vlan_object_including_site.name,
                        vlan_object_including_site.get_display_name(including_second_key=True)))

        elif vlan_object_without_site is not None:
            return_data = vlan_object_without_site
            log.debug2("Found a global matching %s object: %s" %
                       (vlan_object_without_site.name,
                        vlan_object_without_site.get_display_name(including_second_key=True)))

        else:
            log.debug2("No matching existing VLAN found for this VLAN id.")

        return return_data

    def add_device_vm_to_inventory(self, object_type, object_data, site_name, pnic_data=None, vnic_data=None,
                                   nic_ips=None, p_ipv4=None, p_ipv6=None):
        """
        Add/update device/VM object in inventory based on gathered data.

        Try to find object first based on the object data, interface MAC addresses and primary IPs.
            1. try to find by name and cluster/site
            2. try to find by mac addresses interfaces
            3. try to find by serial number (1st) or asset tag (2nd) (ESXi host)
            4. try to find by primary IP

        IP addresses for each interface are added here as well. First they will be checked and added
        if all checks pass. For each IP address a matching IP prefix will be searched for. First we
        look for longest matching IP Prefix in the same site. If this failed we try to find the longest
        matching global IP Prefix.

        If a IP Prefix was found then we try to get the VRF and VLAN for this prefix. Now we compare
        if interface VLAN and prefix VLAN match up and warn if they don't. Then we try to add data to
        the IP address if not already set:

            add prefix VRF if VRF for this IP is undefined
            add tenant if tenant for this IP is undefined
                1. try prefix tenant
                2. if prefix tenant is undefined try VLAN tenant

        And we also set primary IP4/6 for this object depending on the "set_primary_ip" setting.

        If a IP address is set as primary IP for another device then using this IP on another
        device will be rejected by NetBox.

        Setting "always":
            check all NBDevice and NBVM objects if this IP address is set as primary IP to any
            other object then this one. If we found another object, then we unset the primary_ip*
            for the found object and assign it to this object.

            This setting will also reset the primary IP if it has been changed in NetBox

        Setting "when-undefined":
            Will set the primary IP for this object if primary_ip4/6 is undefined. Will cause a
            NetBox error if IP has been assigned to a different object as well

        Setting "never":
            Well, the attribute primary_ip4/6 will never be touched/changed.

        Parameters
        ----------
        object_type: (NBDevice, NBVM)
            NetBoxObject sub class of object to add
        object_data: dict
            data of object to add/update
        site_name: str
            site name this object is part of
        pnic_data: dict
            data of physical interfaces of this object, interface name as key
        vnic_data: dict
            data of virtual interfaces of this object, interface name as key
        nic_ips: dict
            dict of ips per interface of this object, interface name as key
        p_ipv4: str
            primary IPv4 as string including netmask/prefix
        p_ipv6: str
            primary IPv6 as string including netmask/prefix

        """

        if object_type not in [NBDevice, NBVM]:
            raise ValueError(f"Object must be a '{NBVM.name}' or '{NBDevice.name}'.")

        if log.level == DEBUG3:

            log.debug3("function: add_device_vm_to_inventory")
            log.debug3(f"Object type {object_type}")
            pprint.pprint(object_data)
            pprint.pprint(pnic_data)
            pprint.pprint(vnic_data)
            pprint.pprint(nic_ips)
            pprint.pprint(p_ipv4)
            pprint.pprint(p_ipv6)

        # check existing Devices for matches
        log.debug2(f"Trying to find a {object_type.name} based on the collected name, cluster, IP and MAC addresses")

        device_vm_object = self.inventory.get_by_data(object_type, data=object_data)

        if device_vm_object is not None:
            log.debug2("Found a exact matching %s object: %s" %
                       (object_type.name, device_vm_object.get_display_name(including_second_key=True)))

        # keep searching if no exact match was found
        else:

            log.debug2(f"No exact match found. Trying to find {object_type.name} based on MAC addresses")

            # on VMs vnic data is used, on physical devices pnic data is used
            mac_source_data = vnic_data if object_type == NBVM else pnic_data

            nic_macs = [x.get("mac_address") for x in mac_source_data.values()]

            device_vm_object = self.get_object_based_on_macs(object_type, nic_macs)

        # look for devices with same serial or asset tag
        if object_type == NBDevice:

            if device_vm_object is None and object_data.get("serial") is not None:
                log.debug2(f"No match found. Trying to find {object_type.name} based on serial number")

                device_vm_object = self.inventory.get_by_data(object_type, data={"serial": object_data.get("serial")})

            if device_vm_object is None and object_data.get("asset_tag") is not None:
                log.debug2(f"No match found. Trying to find {object_type.name} based on asset tag")

                device_vm_object = self.inventory.get_by_data(object_type,
                                                              data={"asset_tag": object_data.get("asset_tag")})

        if device_vm_object is not None:
            log.debug2("Found a matching %s object: %s" %
                       (object_type.name, device_vm_object.get_display_name(including_second_key=True)))

        # keep looking for devices with the same primary IP
        else:

            log.debug2(f"No match found. Trying to find {object_type.name} based on primary IP addresses")

            device_vm_object = self.get_object_based_on_primary_ip(object_type, p_ipv4, p_ipv6)

        if device_vm_object is None:
            object_name = object_data.get(object_type.primary_key)
            log.debug(f"No existing {object_type.name} object for {object_name}. Creating a new {object_type.name}.")
            device_vm_object = self.inventory.add_object(object_type, data=object_data, source=self)
        else:
            device_vm_object.update(data=object_data, source=self)

        # add role if undefined
        if object_type == NBDevice and grab(device_vm_object, "data.device_role") is None:
            device_vm_object.update(data={"device_role": {"name": self.netbox_host_device_role}})
        if object_type == NBVM and grab(device_vm_object, "data.role") is None:
            device_vm_object.update(data={"role": {"name": self.netbox_vm_device_role}})

        # compile all nic data into one dictionary
        if object_type == NBVM:
            nic_data = vnic_data
            interface_class = NBVMInterface
        else:
            nic_data = {**pnic_data, **vnic_data}
            interface_class = NBInterface

        # map interfaces of existing object with discovered interfaces
        nic_object_dict = self.map_object_interfaces_to_current_interfaces(device_vm_object, nic_data)

        for int_name, int_data in nic_data.items():

            # add object to interface
            int_data[interface_class.secondary_key] = device_vm_object

            # get current object for this interface if it exists
            nic_object = nic_object_dict.get(int_name)

            # create or update interface with data
            if nic_object is None:
                nic_object = self.inventory.add_object(interface_class, data=int_data, source=self)
            else:
                nic_object.update(data=int_data, source=self)

            # add all interface IPs
            for nic_ip in nic_ips.get(int_name, list()):

                # get IP and prefix length
                try:
                    ip_interface_object = ip_interface(nic_ip)
                except ValueError:
                    log.error(f"IP '{nic_ip}' (nic_object.get_display_name()) does not appear "
                              "to be a valid IP address. Skipping!")
                    continue

                log.debug2(f"Trying to find prefix for IP: {ip_interface_object}")

                possible_ip_vrf = None
                possible_ip_tenant = None

                # test for site prefixes first
                matching_site_name = site_name
                matching_ip_prefix = self.return_longest_matching_prefix_for_ip(ip_interface_object, matching_site_name)

                # nothing was found then check prefixes with site name
                if matching_ip_prefix is None:

                    matching_site_name = None
                    matching_ip_prefix = self.return_longest_matching_prefix_for_ip(ip_interface_object)

                # matching prefix found, get data from prefix
                if matching_ip_prefix is not None:

                    this_prefix = grab(matching_ip_prefix, f"data.{NBPrefix.primary_key}")
                    if matching_site_name is None:
                        log.debug2(f"Found IP '{ip_interface_object}' matches global prefix '{this_prefix}'")
                    else:
                        log.debug2(f"Found IP '{ip_interface_object}' matches site '{matching_site_name}' prefix "
                                   f"'{this_prefix}'")

                    # check if prefix net size and ip address prefix length match
                    if this_prefix.prefixlen != ip_interface_object.network.prefixlen:
                        log.warning(f"IP prefix length of '{ip_interface_object}' ({nic_object.get_display_name()}) "
                                    f"does not match network prefix length '{this_prefix}'!")

                    # get prefix data
                    possible_ip_vrf = grab(matching_ip_prefix, "data.vrf")
                    prefix_tenant = grab(matching_ip_prefix, "data.tenant")
                    prefix_vlan = grab(matching_ip_prefix, "data.vlan")

                    # get NIC VLAN data
                    nic_vlan = grab(nic_object, "data.untagged_vlan")
                    nic_vlan_tenant = None
                    if nic_vlan is not None:
                        nic_vlan_tenant = grab(nic_vlan, "data.tenant")

                    # check if interface VLAN matches prefix VLAN for IP address
                    if nic_vlan is not None and prefix_vlan is not None and nic_vlan != prefix_vlan:
                        log.warning(f"Prefix vlan '{prefix_vlan.get_display_name()}' does not match interface vlan "
                                    f"'{nic_vlan.get_display_name()}' for '{nic_object.get_display_name()}")

                    if prefix_tenant is not None:
                        possible_ip_tenant = prefix_tenant
                    elif nic_vlan_tenant is not None:
                        possible_ip_tenant = nic_vlan_tenant

                else:
                    log.debug2(f"No matching prefix found for '{ip_interface_object}'")

                # try to find matching IP address object
                ip_object = None
                skip_this_ip = False
                for ip in self.inventory.get_all_items(NBIPAddress):

                    # check if address matches (without prefix length)
                    ip_address_string = grab(ip, "data.address", fallback="")

                    # not a matching address
                    if not ip_address_string.startswith(f"{ip_interface_object.ip.compressed}/"):
                        continue

                    current_nic = grab(ip, "data.assigned_object_id")

                    # is it our current ip interface?
                    if current_nic == nic_object:
                        ip_object = ip
                        break

                    # check if IP has the same prefix
                    # continue if
                    #   * both are in global scope
                    #   * both ara part of the same vrf
                    if possible_ip_vrf != grab(ip, "data.vrf"):
                        continue

                    # IP address is not assigned to any interface
                    if current_nic is None:
                        ip_object = ip
                        break

                    # get current IP interface status
                    current_nic_enabled = grab(current_nic, "data.enabled", fallback=True)
                    this_nic_enabled = grab(nic_object, "data.enabled", fallback=True)

                    if current_nic_enabled is True and this_nic_enabled is False:
                        log.debug(f"Current interface '{current_nic.get_display_name()}' for IP '{ip_interface_object}'"
                                  f" is enabled and this one '{nic_object.get_display_name()}' is disabled. "
                                  f"IP assignment skipped!")
                        skip_this_ip = True
                        break

                    if current_nic_enabled is False and this_nic_enabled is True:
                        log.debug(f"Current interface '{current_nic.get_display_name()}' for IP '{ip_interface_object}'"
                                  f" is disabled and this one '{nic_object.get_display_name()}' is enabled. "
                                  f"IP will be assigned to this interface.")

                        ip_object = ip

                    if current_nic_enabled == this_nic_enabled:
                        state = "enabled" if this_nic_enabled is True else "disabled"
                        log.warning(f"Current interface '{current_nic.get_display_name()}' for IP "
                                    f"'{ip_interface_object}' and this one '{nic_object.get_display_name()}' are "
                                    f"both {state}. "
                                    f"IP assignment skipped because it is unclear which one is the correct one!")
                        skip_this_ip = True
                        break

                if skip_this_ip is True:
                    continue

                nic_ip_data = {
                    "address": ip_interface_object.compressed,
                    "assigned_object_id": nic_object,
                }

                if ip_object is None:
                    log.debug(f"No existing {NBIPAddress.name} object found. Creating a new one.")

                    if possible_ip_vrf is not None:
                        nic_ip_data["vrf"] = possible_ip_vrf
                    if possible_ip_tenant is not None:
                        nic_ip_data["tenant"] = possible_ip_tenant

                    ip_object = self.inventory.add_object(NBIPAddress, data=nic_ip_data, source=self)

                # update IP address with additional data if not already present
                else:

                    log.debug2(f"Found existing NetBox {NBIPAddress.name} object: {ip_object.get_display_name()}")

                    if grab(ip_object, "data.vrf") is None and possible_ip_vrf is not None:
                        nic_ip_data["vrf"] = possible_ip_vrf

                    if grab(ip_object, "data.tenant") is None and possible_ip_tenant is not None:
                        nic_ip_data["tenant"] = possible_ip_tenant

                    ip_object.update(data=nic_ip_data, source=self)

                # continue if address is not a primary IP
                if nic_ip not in [p_ipv4, p_ipv6]:
                    continue

                # set/update/remove primary IP addresses
                set_this_primary_ip = False
                ip_version = ip_interface_object.ip.version
                if self.set_primary_ip == "always":

                    for object_type in [NBDevice, NBVM]:

                        # new IPs don't need to be removed from other devices/VMs
                        if ip_object.is_new is True:
                            break

                        for devices_vms in self.inventory.get_all_items(object_type):

                            # device has no primary IP of this version
                            this_primary_ip = grab(devices_vms, f"data.primary_ip{ip_version}")

                            # we found this exact object
                            if devices_vms == device_vm_object:
                                continue

                            # device has the same object assigned
                            if this_primary_ip == ip_object:
                                devices_vms.unset_attribute(f"primary_ip{ip_version}")

                    set_this_primary_ip = True

                elif self.set_primary_ip != "never" and grab(device_vm_object, f"data.primary_ip{ip_version}") is None:
                    set_this_primary_ip = True

                if set_this_primary_ip is True:

                    log.debug(f"Setting IP '{nic_ip}' as primary IPv{ip_version} for "
                              f"'{device_vm_object.get_display_name()}")
                    device_vm_object.update(data={f"primary_ip{ip_version}": ip_object})

        return

    def add_datacenter(self, obj):
        """
        Add a vCenter datacenter as a NBClusterGroup to NetBox

        Parameters
        ----------
        obj: vim.Datacenter
            datacenter object

        """

        name = get_string_or_none(grab(obj, "name"))

        if name is None:
            return

        log.debug2(f"Parsing vCenter datacenter: {name}")

        self.inventory.add_update_object(NBClusterGroup, data={"name": name}, source=self)

    def add_cluster(self, obj):
        """
        Add a vCenter cluster as a NBCluster to NetBox. Cluster name is checked against
        cluster_include_filter and cluster_exclude_filter config setting. Also adds
        cluster and site_name to "self.permitted_clusters" so hosts and VMs can be
        checked if they are part of a permitted cluster.

        Parameters
        ----------
        obj: vim.ClusterComputeResource
            cluster to add
        """

        name = get_string_or_none(grab(obj, "name"))
        group = get_string_or_none(grab(obj, "parent.parent.name"))

        if name is None or group is None:
            return

        log.debug2(f"Parsing vCenter cluster: {name}")

        if self.passes_filter(name, self.cluster_include_filter, self.cluster_exclude_filter) is False:
            return

        site_name = self.get_site_name(NBCluster, name)

        data = {
            "name": name,
            "type": {"name": "VMware ESXi"},
            "group": {"name": group},
            "site": {"name": site_name}
        }

        self.inventory.add_update_object(NBCluster, data=data, source=self)

        self.permitted_clusters[name] = site_name

    def add_virtual_switch(self, obj):
        """
        CURRENTLY UNUSED

        Parses port data of each distributed virtual switch.

        Parameters
        ----------
        obj: vim.DistributedVirtualSwitch
            dvs to retrieve port data from
        """

        uuid = get_string_or_none(grab(obj, "uuid"))
        name = get_string_or_none(grab(obj, "name"))

        if uuid is None or name is None:
            return

        log.debug2(f"Parsing vCenter virtual switch: {name}")

        # add ports
        self.network_data["dpgroup_ports"][uuid] = dict()

        criteria = vim.dvs.PortCriteria()
        ports = obj.FetchDVPorts(criteria)

        log.debug2(f"Found {len(ports)} vCenter virtual switch ports")

        for port in ports:
            self.network_data["dpgroup_ports"][uuid][port.key] = port

    def add_port_group(self, obj):
        """
        Parse distributed virtual port group to extract VLAN IDs from each port group

        Parameters
        ----------
        obj: vim.dvs.DistributedVirtualPortgroup
            portgroup to parse
        """

        key = get_string_or_none(grab(obj, "key"))
        name = get_string_or_none(grab(obj, "name"))
        private = False
        vlan_ids = list()
        vlan_id_ranges = list()

        if key is None or name is None:
            return

        log.debug2(f"Parsing vCenter port group: {name}")

        vlan_info = grab(obj, "config.defaultPortConfig.vlan")

        if isinstance(vlan_info, vim.dvs.VmwareDistributedVirtualSwitch.TrunkVlanSpec):
            for item in grab(vlan_info, "vlanId", fallback=list()):
                if item.start == item.end:
                    vlan_ids.append(item.start)
                    vlan_id_ranges.append(str(item.start))
                elif item.start == 0 and item.end == 4094:
                    vlan_ids.append(4095)
                    vlan_id_ranges.append(f"{item.start}-{item.end}")
                else:
                    vlan_ids.extend(range(item.start, item.end+1))
                    vlan_id_ranges.append(f"{item.start}-{item.end}")

        elif isinstance(vlan_info, vim.dvs.VmwareDistributedVirtualSwitch.PvlanSpec):
            vlan_ids.append(grab(vlan_info, "pvlanId"))
            private = True
        else:
            vlan_ids.append(grab(vlan_info, "vlanId"))

        self.network_data["dpgroup"][key] = {
            "name": name,
            "vlan_ids": vlan_ids,
            "vlan_id_ranges": vlan_id_ranges,
            "private": private
        }

    def add_host(self, obj):
        """
        Parse a vCenter host (ESXi) add to NetBox once all data is gathered.

        First host is filtered:
             host has a cluster and is it permitted
             was host with same name and site already parsed
             does the host pass the host_include_filter and host_exclude_filter

        Then all necessary host data will be collected.
            host model, manufacturer, serial, physical interfaces, virtual interfaces,
            virtual switches, proxy switches, host port groups, interface VLANs, IP addresses

        Primary IPv4/6 will be determined by
            1. if the interface port group name contains
                "management" or "mngt"
            2. interface is the default route of this host

        Parameters
        ----------
        obj: vim.HostSystem
            host object to parse
        """

        name = get_string_or_none(grab(obj, "name"))

        if name is not None and self.strip_host_domain_name is True:
            name = name.split(".")[0]

        # parse data
        log.debug2(f"Parsing vCenter host: {name}")

        #
        # Filtering
        #

        # manage site and cluster
        cluster_name = get_string_or_none(grab(obj, "parent.name"))

        if cluster_name is None:
            log.error(f"Requesting cluster for host '{name}' failed. Skipping.")
            return

        if log.level == DEBUG3:
            try:
                log.info("Cluster data")
                dump(grab(obj, "parent"))
            except Exception as e:
                log.error(e)

        # handle standalone hosts
        if cluster_name == name:
            log.debug2(f"Host name and cluster name are equal '{cluster_name}'. "
                       f"Assuming this host is a 'standalone' host.")

        elif self.permitted_clusters.get(cluster_name) is None:
            log.debug(f"Host '{name}' is not part of a permitted cluster. Skipping")
            return

        # get a site for this host
        site_name = self.get_site_name(NBDevice, name, cluster_name)

        if name in self.processed_host_names.get(site_name, list()):
            log.warning(f"Host '{name}' for site '{site_name}' already parsed. "
                        "Make sure to use unique host names. Skipping")
            return

        # add host to processed list
        if self.processed_host_names.get(site_name) is None:
            self.processed_host_names[site_name] = list()

        self.processed_host_names[site_name].append(name)

        # filter hosts by name
        if self.passes_filter(name, self.host_include_filter, self.host_exclude_filter) is False:
            return

        # add host as single cluster to cluster list
        if cluster_name == name:
            self.permitted_clusters[cluster_name] = site_name
            # add cluster to NetBox
            cluster_data = {
                "name": cluster_name,
                "type": {
                    "name": "VMware ESXi"
                },
                "site": {
                    "name": site_name
                }
            }
            self.inventory.add_update_object(NBCluster, data=cluster_data, source=self)

        #
        # Collecting data
        #

        # collect all necessary data
        manufacturer = get_string_or_none(grab(obj, "summary.hardware.vendor"))
        model = get_string_or_none(grab(obj, "summary.hardware.model"))
        product_name = get_string_or_none(grab(obj, "summary.config.product.name"))
        product_version = get_string_or_none(grab(obj, "summary.config.product.version"))
        platform = f"{product_name} {product_version}"

        # if the device vendor/model cannot be retrieved (due to problem on the host),
        # set a dummy value so the host still gets synced
        if manufacturer is None:
            manufacturer = "Generic Vendor"
        if model is None:
            model = "Generic Model"

        # get status
        status = "offline"
        if get_string_or_none(grab(obj, "summary.runtime.connectionState")) == "connected":
            status = "active"

        # prepare identifiers to find asset tag and serial number
        identifiers = grab(obj, "summary.hardware.otherIdentifyingInfo", fallback=list())
        identifier_dict = dict()
        for item in identifiers:
            value = grab(item, "identifierValue", fallback="")
            if len(str(value).strip()) > 0:
                identifier_dict[grab(item, "identifierType.key")] = str(value).strip()

        # try to find serial
        serial = None

        for serial_num_key in ["SerialNumberTag", "ServiceTag", "EnclosureSerialNumberTag"]:
            if serial_num_key in identifier_dict.keys():
                serial = get_string_or_none(identifier_dict.get(serial_num_key))
                break

        # add asset tag if desired and present
        asset_tag = None

        if bool(self.collect_hardware_asset_tag) is True and "AssetTag" in identifier_dict.keys():

            banned_tags = ["Default string", "NA", "N/A", "None", "Null", "oem", "o.e.m",
                           "to be filled by o.e.m.", "Unknown"]

            this_asset_tag = identifier_dict.get("AssetTag")

            if this_asset_tag.lower() not in [x.lower() for x in banned_tags]:
                asset_tag = this_asset_tag

        # prepare host data model
        host_data = {
            "name": name,
            "device_type": {
                "model": model,
                "manufacturer": {
                    "name": manufacturer
                }
            },
            "site": {"name": site_name},
            "cluster": {"name": cluster_name},
            "status": status
        }

        # add data if present
        if serial is not None:
            host_data["serial"] = serial
        if asset_tag is not None:
            host_data["asset_tag"] = asset_tag
        if platform is not None:
            host_data["platform"] = {"name": platform}

        # iterate over hosts virtual switches, needed to enrich data on physical interfaces
        self.network_data["vswitch"][name] = dict()
        for vswitch in grab(obj, "config.network.vswitch", fallback=list()):

            vswitch_name = grab(vswitch, "name")

            vswitch_pnics = [str(x) for x in grab(vswitch, "pnic", fallback=list())]

            if vswitch_name is not None:

                log.debug2(f"Found host vSwitch {vswitch_name}")

                self.network_data["vswitch"][name][vswitch_name] = {
                    "mtu": grab(vswitch, "mtu"),
                    "pnics": vswitch_pnics
                }

        # iterate over hosts proxy switches, needed to enrich data on physical interfaces
        # also stores data on proxy switch configured mtu which is used for VM interfaces
        self.network_data["pswitch"][name] = dict()
        for pswitch in grab(obj, "config.network.proxySwitch", fallback=list()):

            pswitch_uuid = grab(pswitch, "dvsUuid")
            pswitch_name = grab(pswitch, "dvsName")
            pswitch_pnics = [str(x) for x in grab(pswitch, "pnic", fallback=list())]

            if pswitch_uuid is not None:

                log.debug2(f"Found host proxySwitch {pswitch_name}")

                self.network_data["pswitch"][name][pswitch_uuid] = {
                    "name": pswitch_name,
                    "mtu": grab(pswitch, "mtu"),
                    "pnics": pswitch_pnics
                }

        # iterate over hosts port groups, needed to enrich data on physical interfaces
        self.network_data["host_pgroup"][name] = dict()
        for pgroup in grab(obj, "config.network.portgroup", fallback=list()):

            pgroup_name = grab(pgroup, "spec.name")

            if pgroup_name is not None:

                log.debug2(f"Found host portGroup {pgroup_name}")

                nic_order = grab(pgroup, "computedPolicy.nicTeaming.nicOrder")
                pgroup_nics = nic_order.activeNic + nic_order.standbyNic

                self.network_data["host_pgroup"][name][pgroup_name] = {
                    "vlan_id": grab(pgroup, "spec.vlanId"),
                    "vswitch": grab(pgroup, "spec.vswitchName"),
                    "nics": pgroup_nics
                }

        # now iterate over all physical interfaces and collect data
        pnic_data_dict = dict()
        for pnic in grab(obj, "config.network.pnic", fallback=list()):

            pnic_name = grab(pnic, "device")
            pnic_key = grab(pnic, "key")

            log.debug2("Parsing {}: {}".format(grab(pnic, "_wsdlName"), pnic_name))

            pnic_link_speed = grab(pnic, "linkSpeed.speedMb")
            if pnic_link_speed is None:
                pnic_link_speed = grab(pnic, "spec.linkSpeed.speedMb")
            if pnic_link_speed is None:
                pnic_link_speed = grab(pnic, "validLinkSpecification.0.speedMb")

            # determine link speed text
            pnic_description = ""
            if pnic_link_speed is not None:
                if pnic_link_speed >= 1000:
                    pnic_description = "%iGb/s " % int(pnic_link_speed / 1000)
                else:
                    pnic_description = f"{pnic_link_speed}Mb/s "

            pnic_description = f"{pnic_description} pNIC"

            pnic_mtu = None

            pnic_mode = None

            # check virtual switches for interface data
            for vs_name, vs_data in self.network_data["vswitch"][name].items():

                if pnic_key in vs_data.get("pnics", list()):
                    pnic_description = f"{pnic_description} ({vs_name})"
                    pnic_mtu = vs_data.get("mtu")

            # check proxy switches for interface data
            for ps_uuid, ps_data in self.network_data["pswitch"][name].items():

                if pnic_key in ps_data.get("pnics", list()):
                    ps_name = ps_data.get("name")
                    pnic_description = f"{pnic_description} ({ps_name})"
                    pnic_mtu = ps_data.get("mtu")

                    pnic_mode = "tagged-all"

            # check vlans on this pnic
            pnic_vlans = list()

            for pg_name, pg_data in self.network_data["host_pgroup"][name].items():

                if pnic_name in pg_data.get("nics", list()):
                    pnic_vlans.append({
                        "name": pg_name,
                        "vid": pg_data.get("vlan_id")
                    })

            pnic_speed_type_mapping = {
                100: "100base-tx",
                1000: "1000base-t",
                10000: "10gbase-t",
                25000: "25gbase-x-sfp28",
                40000: "40gbase-x-qsfpp"
            }

            pnic_data = {
                "name": pnic_name,
                "device": None,     # will be set once we found the correct device
                "mac_address": normalize_mac_address(grab(pnic, "mac")),
                "enabled": bool(grab(pnic, "linkSpeed")),
                "description": pnic_description,
                "type": pnic_speed_type_mapping.get(pnic_link_speed, "other")
            }

            if pnic_mtu is not None:
                pnic_data["mtu"] = pnic_mtu
            if pnic_mode is not None:
                pnic_data["mode"] = pnic_mode

            # determine interface mode for non VM traffic NICs
            if len(pnic_vlans) > 0:
                vlan_ids = list(set([x.get("vid") for x in pnic_vlans]))
                if len(vlan_ids) == 1 and vlan_ids[0] == 0:
                    pnic_data["mode"] = "access"
                elif 4095 in vlan_ids:
                    pnic_data["mode"] = "tagged-all"
                else:
                    pnic_data["mode"] = "tagged"

                tagged_vlan_list = list()
                for pnic_vlan in pnic_vlans:

                    # only add VLANs if port is tagged
                    if pnic_data.get("mode") != "tagged":
                        break

                    # ignore VLAN ID 0
                    if pnic_vlan.get("vid") == 0:
                        continue

                    tagged_vlan_list.append(self.get_vlan_object_if_exists({
                        "name": pnic_vlan.get("name"),
                        "vid": pnic_vlan.get("vid"),
                        "site": {
                            "name": site_name
                        }
                    }))

                if len(tagged_vlan_list) > 0:
                    pnic_data["tagged_vlans"] = tagged_vlan_list

            pnic_data_dict[pnic_name] = pnic_data

        host_primary_ip4 = None
        host_primary_ip6 = None

        # now iterate over all virtual interfaces and collect data
        vnic_data_dict = dict()
        vnic_ips = dict()
        for vnic in grab(obj, "config.network.vnic", fallback=list()):

            vnic_name = grab(vnic, "device")

            log.debug2("Parsing {}: {}".format(grab(vnic, "_wsdlName"), vnic_name))

            vnic_portgroup = grab(vnic, "portgroup")
            vnic_portgroup_data = self.network_data["host_pgroup"][name].get(vnic_portgroup)
            vnic_portgroup_vlan_id = 0

            vnic_dv_portgroup_key = grab(vnic, "spec.distributedVirtualPort.portgroupKey")
            vnic_dv_portgroup_data = self.network_data["dpgroup"].get(vnic_dv_portgroup_key)
            vnic_dv_portgroup_data_vlan_ids = list()

            vnic_description = None
            vnic_mode = None

            # get data from local port group
            if vnic_portgroup_data is not None:

                vnic_portgroup_vlan_id = vnic_portgroup_data.get("vlan_id")
                vnic_vswitch = vnic_portgroup_data.get("vswitch")
                vnic_description = f"{vnic_portgroup} ({vnic_vswitch}, vlan ID: {vnic_portgroup_vlan_id})"
                vnic_mode = "access"

            # get data from distributed port group
            elif vnic_dv_portgroup_data is not None:

                vnic_description = vnic_dv_portgroup_data.get("name")
                vnic_dv_portgroup_data_vlan_ids = vnic_dv_portgroup_data.get("vlan_ids")

                if len(vnic_dv_portgroup_data_vlan_ids) == 1 and vnic_dv_portgroup_data_vlan_ids[0] == 4095:
                    vlan_description = "all vlans"
                    vnic_mode = "tagged-all"
                else:
                    if len(vnic_dv_portgroup_data.get("vlan_id_ranges")) > 0:
                        vlan_description = "vlan IDs: %s" % ", ".join(vnic_dv_portgroup_data.get("vlan_id_ranges"))
                    else:
                        vlan_description = f"vlan ID: {vnic_dv_portgroup_data_vlan_ids[0]}"

                    if len(vnic_dv_portgroup_data_vlan_ids) == 1 and vnic_dv_portgroup_data_vlan_ids[0] == 0:
                        vnic_mode = "access"
                    else:
                        vnic_mode = "tagged"

                vnic_dv_portgroup_dswitch_uuid = grab(vnic, "spec.distributedVirtualPort.switchUuid", fallback="NONE")
                vnic_vswitch = grab(self.network_data, f"pswitch|{name}|{vnic_dv_portgroup_dswitch_uuid}|name",
                                    separator="|")

                if vnic_vswitch is not None:
                    vnic_description = f"{vnic_description} ({vnic_vswitch}, {vlan_description})"

            # add data
            vnic_data = {
                "name": vnic_name,
                "device": None,     # will be set once we found the correct device
                "mac_address": normalize_mac_address(grab(vnic, "spec.mac")),
                "enabled": True,    # ESXi vmk interface is enabled by default
                "mtu": grab(vnic, "spec.mtu"),
                "type": "virtual"
            }

            if vnic_mode is not None:
                vnic_data["mode"] = vnic_mode

            if vnic_description is not None:
                vnic_data["description"] = vnic_description
            else:
                vnic_description = ""

            if vnic_portgroup_data is not None and vnic_portgroup_vlan_id != 0:

                vnic_data["untagged_vlan"] = self.get_vlan_object_if_exists({
                    "name": f"ESXi {vnic_portgroup} (ID: {vnic_portgroup_vlan_id}) ({site_name})",
                    "vid": vnic_portgroup_vlan_id,
                    "site": {
                        "name": site_name
                    }
                })

            elif vnic_dv_portgroup_data is not None:

                tagged_vlan_list = list()
                for vnic_dv_portgroup_data_vlan_id in vnic_dv_portgroup_data_vlan_ids:

                    if vnic_mode != "tagged":
                        break

                    if vnic_dv_portgroup_data_vlan_id == 0:
                        continue

                    tagged_vlan_list.append(self.get_vlan_object_if_exists({
                        "name": f"{vnic_dv_portgroup_data.get('name')}-{vnic_dv_portgroup_data_vlan_id}",
                        "vid": vnic_dv_portgroup_data_vlan_id,
                        "site": {
                            "name": site_name
                        }
                    }))

                if len(tagged_vlan_list) > 0:
                    vnic_data["tagged_vlans"] = tagged_vlan_list

            vnic_data_dict[vnic_name] = vnic_data

            # check if interface has the default route or is described as management interface
            vnic_is_primary = False
            if "management" in vnic_description.lower() or \
               "mgmt" in vnic_description.lower() or \
               grab(vnic, "spec.ipRouteSpec") is not None:

                vnic_is_primary = True

            if vnic_ips.get(vnic_name) is None:
                vnic_ips[vnic_name] = list()

            int_v4 = "{}/{}".format(grab(vnic, "spec.ip.ipAddress"), grab(vnic, "spec.ip.subnetMask"))

            if ip_valid_to_add_to_netbox(int_v4, self.permitted_subnets, vnic_name) is True:
                vnic_ips[vnic_name].append(int_v4)

                if vnic_is_primary is True and host_primary_ip4 is None:
                    host_primary_ip4 = int_v4

            for ipv6_entry in grab(vnic, "spec.ip.ipV6Config.ipV6Address", fallback=list()):

                int_v6 = "{}/{}".format(grab(ipv6_entry, "ipAddress"), grab(ipv6_entry, "prefixLength"))

                if ip_valid_to_add_to_netbox(int_v6, self.permitted_subnets, vnic_name) is True:
                    vnic_ips[vnic_name].append(int_v6)

                    # set first valid IPv6 address as primary IPv6
                    # not the best way but maybe we can find more information in "spec.ipRouteSpec"
                    # about default route and we could use that to determine the correct IPv6 address
                    if vnic_is_primary is True and host_primary_ip6 is None:
                        host_primary_ip6 = int_v6

        # add host to inventory
        self.add_device_vm_to_inventory(NBDevice, object_data=host_data, site_name=site_name, pnic_data=pnic_data_dict,
                                        vnic_data=vnic_data_dict, nic_ips=vnic_ips,
                                        p_ipv4=host_primary_ip4, p_ipv6=host_primary_ip6)

        return

    def add_virtual_machine(self, obj):
        """
        Parse a vCenter VM  add to NetBox once all data is gathered.

        VMs are parsed twice. First only "online" VMs are parsed and added. In the second
        round also "offline" VMs will be parsed. This helps of VMs are cloned and used
        for upgrades but then have the same name.

        First VM is filtered:
             VM has a cluster and is it permitted
             was VM with same name and cluster already parsed
             does the VM pass the vm_include_filter and vm_exclude_filter

        Then all necessary VM data will be collected.
            platform, virtual interfaces, virtual cpu/disk/memory interface VLANs, IP addresses

        Primary IPv4/6 will be determined by interface that provides the default route for this VM

        Note:
            IP address information can only be extracted if guest tools are installed and running.

        Parameters
        ----------
        obj: vim.VirtualMachine
            virtual machine object to parse
        """

        name = get_string_or_none(grab(obj, "name"))

        if name is not None and self.strip_vm_domain_name is True:
            name = name.split(".")[0]

        #
        # Filtering
        #

        # get VM UUID
        vm_uuid = grab(obj, "config.uuid")

        if vm_uuid is None or vm_uuid in self.processed_vm_uuid:
            return

        log.debug2(f"Parsing vCenter VM: {name}")

        # get VM power state
        status = "active" if get_string_or_none(grab(obj, "runtime.powerState")) == "poweredOn" else "offline"

        # check if vm is template
        template = grab(obj, "config.template")
        if bool(self.skip_vm_templates) is True and template is True:
            log.debug2(f"VM '{name}' is a template. Skipping")
            return

        # ignore offline VMs during first run
        if self.parsing_vms_the_first_time is True and status == "offline":
            log.debug2(f"Ignoring {status} VM '{name}' on first run")
            return

        # add to processed VMs
        self.processed_vm_uuid.append(vm_uuid)

        parent_name = get_string_or_none(grab(obj, "runtime.host.name"))

        # check VM cluster
        cluster_name = get_string_or_none(grab(obj, "runtime.host.parent.name"))
        if cluster_name is None:
            log.error(f"Requesting cluster for Virtual Machine '{name}' failed. Skipping.")
            return

        elif self.permitted_clusters.get(cluster_name) is None:
            log.debug(f"Virtual machine '{name}' is not part of a permitted cluster. Skipping")
            return

        if name in self.processed_vm_names.get(cluster_name, list()):
            log.warning(f"Virtual machine '{name}' for cluster '{cluster_name}' already parsed. "
                        "Make sure to use unique VM names. Skipping")
            return

        # add host to processed list
        if self.processed_vm_names.get(cluster_name) is None:
            self.processed_vm_names[cluster_name] = list()

        self.processed_vm_names[cluster_name].append(name)

        # filter VMs by name
        if self.passes_filter(name, self.vm_include_filter, self.vm_exclude_filter) is False:
            return

        #
        # Collect data
        #

        # check if cluster is a Standalone ESXi
        site_name = self.permitted_clusters.get(cluster_name)
        if site_name is None:
            site_name = self.get_site_name(NBCluster, cluster_name)

        # first check against vm_platform_relation
        platform = grab(obj, "config.guestFullName")
        platform = get_string_or_none(grab(obj, "guest.guestFullName", fallback=platform))

        for platform_relation in grab(self, "vm_platform_relation", fallback=list()):

            if platform is None:
                break

            object_regex = platform_relation.get("object_regex")
            if object_regex.match(platform):
                platform = platform_relation.get("platform_name")
                log.debug2(f"Found a match ({object_regex.pattern}) for {platform}, using mapped platform '{platform}'")
                break

        hardware_devices = grab(obj, "config.hardware.device", fallback=list())

        disk = int(sum([getattr(comp, "capacityInKB", 0) for comp in hardware_devices
                       if isinstance(comp, vim.vm.device.VirtualDisk)
                        ]) / 1024 / 1024)

        annotation = None
        if bool(self.skip_vm_comments) is False:
            annotation = get_string_or_none(grab(obj, "config.annotation"))

        # assign vm_tenant_relation
        tenant_name = None
        for tenant_relation in grab(self, "vm_tenant_relation", fallback=list()):
            object_regex = tenant_relation.get("object_regex")
            if object_regex.match(name):
                tenant_name = tenant_relation.get("tenant_name")
                log.debug2(f"Found a match ({object_regex.pattern}) for {name}, using tenant '{tenant_name}'")
                break

        vm_data = {
            "name": name,
            "cluster": {"name": cluster_name},
            "status": status,
            "memory": grab(obj, "config.hardware.memoryMB"),
            "vcpus": grab(obj, "config.hardware.numCPU"),
            "disk": disk
        }

        if platform is not None:
            vm_data["platform"] = {"name": platform}
        if annotation is not None:
            vm_data["comments"] = annotation
        if tenant_name is not None:
            vm_data["tenant"] = {"name": tenant_name}

        vm_primary_ip4 = None
        vm_primary_ip6 = None
        vm_default_gateway_ip4 = None
        vm_default_gateway_ip6 = None

        # check vm routing to determine which is the default interface for each IP version
        for route in grab(obj, "guest.ipStack.0.ipRouteConfig.ipRoute", fallback=list()):

            # we found a default route
            if grab(route, "prefixLength") == 0:

                try:
                    ip_a = ip_address(grab(route, "network"))
                except ValueError:
                    continue

                try:
                    gateway_ip_address = ip_address(grab(route, "gateway.ipAddress"))
                except ValueError:
                    continue

                if ip_a.version == 4 and gateway_ip_address is not None:
                    log.debug2(f"Found default IPv4 gateway {gateway_ip_address}")
                    vm_default_gateway_ip4 = gateway_ip_address
                elif ip_a.version == 6 and gateway_ip_address is not None:
                    log.debug2(f"Found default IPv6 gateway {gateway_ip_address}")
                    vm_default_gateway_ip6 = gateway_ip_address

        nic_data = dict()
        nic_ips = dict()

        # get VM interfaces
        for vm_device in hardware_devices:

            # sample: https://github.com/vmware/pyvmomi-community-samples/blob/master/samples/getvnicinfo.py

            # not a network interface
            if not isinstance(vm_device, vim.vm.device.VirtualEthernetCard):
                continue

            int_mac = normalize_mac_address(grab(vm_device, "macAddress"))

            device_class = grab(vm_device, "_wsdlName")

            log.debug2(f"Parsing device {device_class}: {int_mac}")

            device_backing = grab(vm_device, "backing")

            # set defaults
            int_mtu = None
            int_mode = None
            int_network_vlan_ids = None
            int_network_vlan_id_ranges = None
            int_network_name = None
            int_network_private = False

            # get info from local vSwitches
            if isinstance(device_backing, vim.vm.device.VirtualEthernetCard.NetworkBackingInfo):

                int_network_name = get_string_or_none(grab(device_backing, "deviceName"))
                int_host_pgroup = grab(self.network_data, f"host_pgroup|{parent_name}|{int_network_name}",
                                       separator="|")

                if int_host_pgroup is not None:
                    int_network_vlan_ids = [int_host_pgroup.get("vlan_id")]
                    int_network_vlan_id_ranges = [str(int_host_pgroup.get("vlan_id"))]

                    int_vswitch_name = int_host_pgroup.get("vswitch")
                    int_vswitch_data = grab(self.network_data, f"vswitch|{parent_name}|{int_vswitch_name}",
                                            separator="|")

                    if int_vswitch_data is not None:
                        int_mtu = int_vswitch_data.get("mtu")

            # get info from distributed port group
            else:

                dvs_portgroup_key = grab(device_backing, "port.portgroupKey", fallback="None")
                int_portgroup_data = grab(self.network_data, f"dpgroup|{dvs_portgroup_key}", separator="|")

                if int_portgroup_data is not None:
                    int_network_name = grab(int_portgroup_data, "name")
                    int_network_vlan_ids = grab(int_portgroup_data, "vlan_ids")
                    if len(grab(int_portgroup_data, "vlan_id_ranges")) > 0:
                        int_network_vlan_id_ranges = grab(int_portgroup_data, "vlan_id_ranges")
                    else:
                        int_network_vlan_id_ranges = [str(int_network_vlan_ids[0])]
                    int_network_private = grab(int_portgroup_data, "private")

                int_dvswitch_uuid = grab(device_backing, "port.switchUuid")
                int_dvswitch_data = grab(self.network_data, f"pswitch|{parent_name}|{int_dvswitch_uuid}", separator="|")

                if int_dvswitch_data is not None:
                    int_mtu = int_dvswitch_data.get("mtu")

            int_connected = grab(vm_device, "connectable.connected", fallback=False)
            int_label = grab(vm_device, "deviceInfo.label", fallback="")

            int_name = "vNIC {}".format(int_label.split(" ")[-1])

            int_full_name = int_name
            if int_network_name is not None:
                int_full_name = f"{int_full_name} ({int_network_name})"

            int_description = f"{int_label} ({device_class})"
            if int_network_vlan_ids is not None:

                if len(int_network_vlan_ids) == 1 and int_network_vlan_ids[0] == 4095:
                    vlan_description = "all vlans"
                    int_mode = "tagged-all"
                else:
                    vlan_description = "vlan ID: %s" % ", ".join(int_network_vlan_id_ranges)

                    if len(int_network_vlan_ids) == 1:
                        int_mode = "access"
                    else:
                        int_mode = "tagged"

                if int_network_private is True:
                    vlan_description = f"{vlan_description} (private)"

                int_description = f"{int_description} ({vlan_description})"

            # find corresponding guest NIC and get IP addresses and connected status
            for guest_nic in grab(obj, "guest.net", fallback=list()):

                # get matching guest NIC
                if int_mac != normalize_mac_address(grab(guest_nic, "macAddress")):
                    continue

                int_connected = grab(guest_nic, "connected", fallback=int_connected)

                if nic_ips.get(int_full_name) is None:
                    nic_ips[int_full_name] = list()

                # grab all valid interface IP addresses
                for int_ip in grab(guest_nic, "ipConfig.ipAddress", fallback=list()):

                    int_ip_address = f"{int_ip.ipAddress}/{int_ip.prefixLength}"

                    if ip_valid_to_add_to_netbox(int_ip_address, self.permitted_subnets, int_full_name) is False:
                        continue

                    nic_ips[int_full_name].append(int_ip_address)

                    # check if primary gateways are in the subnet of this IP address
                    # if it matches IP gets chosen as primary IP
                    if vm_default_gateway_ip4 is not None and \
                            vm_default_gateway_ip4 in ip_interface(int_ip_address).network and \
                            vm_primary_ip4 is None:

                        vm_primary_ip4 = int_ip_address

                    if vm_default_gateway_ip6 is not None and \
                            vm_default_gateway_ip6 in ip_interface(int_ip_address).network and \
                            vm_primary_ip6 is None:

                        vm_primary_ip6 = int_ip_address

            vm_nic_data = {
                "name": int_full_name,
                "virtual_machine": None,
                "mac_address": int_mac,
                "description": int_description,
                "enabled": int_connected,
            }

            if int_mtu is not None:
                vm_nic_data["mtu"] = int_mtu
            if int_mode is not None:
                vm_nic_data["mode"] = int_mode

            if int_network_vlan_ids is not None and int_mode != "tagged-all":

                if len(int_network_vlan_ids) == 1 and int_network_vlan_ids[0] != 0:

                    vm_nic_data["untagged_vlan"] = self.get_vlan_object_if_exists({
                        "name": int_network_name,
                        "vid": int_network_vlan_ids[0],
                        "site": {
                            "name": site_name
                        }
                    })
                else:
                    tagged_vlan_list = list()
                    for int_network_vlan_id in int_network_vlan_ids:

                        if int_network_vlan_id == 0:
                            continue

                        tagged_vlan_list.append(self.get_vlan_object_if_exists({
                            "name": f"{int_network_name}-{int_network_vlan_id}",
                            "vid": int_network_vlan_id,
                            "site": {
                                "name": site_name
                            }
                        }))

                    if len(tagged_vlan_list) > 0:
                        vm_nic_data["tagged_vlans"] = tagged_vlan_list

            nic_data[int_full_name] = vm_nic_data

        # add VM to inventory
        self.add_device_vm_to_inventory(NBVM, object_data=vm_data, site_name=site_name, vnic_data=nic_data,
                                        nic_ips=nic_ips, p_ipv4=vm_primary_ip4, p_ipv6=vm_primary_ip6)

        return

    def update_basic_data(self):
        """

        Returns
        -------

        """

        # add source identification tag
        self.inventory.add_update_object(NBTag, data={
            "name": self.source_tag,
            "description": f"Marks sources synced from vCenter {self.name} "
                           f"({self.host_fqdn}) to this NetBox Instance."
        })

        # update virtual site if present
        this_site_object = self.inventory.get_by_data(NBSite, data={"name": self.site_name})

        if this_site_object is not None:
            this_site_object.update(data={
                "name": self.site_name,
                "comments": f"A default virtual site created to house objects "
                            "that have been synced from this vCenter instance "
                            "and have no predefined site assigned."
            })

        server_role_object = self.inventory.get_by_data(NBDeviceRole, data={"name": "Server"})

        if server_role_object is not None:
            server_role_object.update(data={
                "name": "Server",
                "color": "9e9e9e",
                "vm_role": True
            })


# EOF
