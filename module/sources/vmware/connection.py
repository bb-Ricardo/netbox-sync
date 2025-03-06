# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import datetime
import pprint
import ssl
from ipaddress import ip_address, ip_interface
from urllib.parse import unquote
from itertools import zip_longest

import urllib3
import requests
import http
# noinspection PyUnresolvedReferences
from packaging import version
# noinspection PyUnresolvedReferences
from pyVim import connect
# noinspection PyUnresolvedReferences
from pyVmomi import vim
# noinspection PyUnresolvedReferences
from pyVmomi.VmomiSupport import VmomiJSONEncoder

from module.sources.common.source_base import SourceBase
from module.sources.vmware.config import VMWareConfig
from module.common.logging import get_logger, DEBUG3
from module.common.misc import grab, dump, get_string_or_none, plural, quoted_split
from module.common.support import normalize_mac_address
from module.netbox.inventory import NetBoxInventory
from module.netbox import *

vsphere_automation_sdk_available = True
try:
    # noinspection PyUnresolvedReferences
    from com.vmware.vapi.std_client import DynamicID
    # noinspection PyUnresolvedReferences
    from vmware.vapi.vsphere.client import create_vsphere_client
except ImportError:
    vsphere_automation_sdk_available = False

log = get_logger()


# noinspection PyTypeChecker
class VMWareHandler(SourceBase):
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
        NBSiteGroup,
        NBCluster,
        NBDevice,
        NBVM,
        NBVMInterface,
        NBInterface,
        NBIPAddress,
        NBPrefix,
        NBTenant,
        NBVRF,
        NBVLAN,
        NBVLANGroup,
        NBCustomField,
        NBVirtualDisk,
        NBMACAddress
    ]

    source_type = "vmware"

    recursion_level = 0

    # internal vars
    session = None
    tag_session = None

    site_name = None

    def __init__(self, name=None):

        if name is None:
            raise ValueError(f"Invalid value for attribute 'name': '{name}'.")

        self.inventory = NetBoxInventory()
        self.name = name

        # parse settings
        settings_handler = VMWareConfig()
        settings_handler.source_name = self.name
        self.settings = settings_handler.parse()

        self.set_source_tag()
        self.site_name = f"vCenter: {name}"

        if self.settings.enabled is False:
            log.info(f"Source '{name}' is currently disabled. Skipping")
            return

        self._sdk_instance = None
        self.create_sdk_session()

        if self.session is None:
            log.info(f"Source '{name}' is currently unavailable. Skipping")
            return

        self.create_api_session()

        self.init_successful = True

        # instantiate source specific vars
        self.network_data = {
            "vswitch": dict(),
            "pswitch": dict(),
            "host_pgroup": dict(),
            "dpgroup": dict(),
            "dpgroup_ports": dict()
        }
        self.processed_host_names = dict()
        self.processed_vm_names = dict()
        self.processed_vm_uuid = list()
        self.object_cache = dict()
        self.parsing_vms_the_first_time = True
        self.objects_to_reevaluate = list()
        self.parsing_objects_to_reevaluate = False

    def create_sdk_session(self):
        """
        Initialize SDK session with vCenter

        Returns
        -------
        bool: if initialization was successful or not
        """

        if self.session is not None:
            return True

        log.debug(f"Starting vCenter SDK connection to '{self.settings.host_fqdn}'")

        ssl_context = ssl.create_default_context()
        if self.settings.validate_tls_certs is False:
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

        connection_params = dict(
            host=self.settings.host_fqdn,
            port=self.settings.port,
            sslContext=ssl_context
        )

        # uses connect.SmartStubAdapter
        if self.settings.proxy_host is not None and self.settings.proxy_port is not None:
            connection_params.update(
                httpProxyHost=self.settings.proxy_host,
                httpProxyPort=self.settings.proxy_port,
            )

        # uses connect.SmartConnect
        else:
            connection_params.update(
                user=self.settings.username,
                pwd=self.settings.password,
            )

        def_exception_text = f"Unable to connect to vCenter instance " \
                             f"'{self.settings.host_fqdn}' on port {self.settings.port}."

        try:
            if self.settings.proxy_host is not None and self.settings.proxy_port is not None:
                smart_stub = connect.SmartStubAdapter(**connection_params)
                self._sdk_instance = vim.ServiceInstance('ServiceInstance', smart_stub)
                content = self._sdk_instance.RetrieveContent()
                content.sessionManager.Login(self.settings.username, self.settings.password, None)
            else:

                self._sdk_instance = connect.SmartConnect(**connection_params)

            self.session = self._sdk_instance.RetrieveContent()

        except vim.fault.InvalidLogin as e:
            log.error(f"{def_exception_text} {e.msg}")
            return False
        except vim.fault.NoPermission as e:
            log.error(f"{def_exception_text} User {self.settings.username} does not have required permission. {e.msg}")
            return False
        except Exception as e:
            log.error(f"{def_exception_text} Reason: {e}")
            return False

        log.info(f"Successfully connected to vCenter SDK '{self.settings.host_fqdn}'")

        return True

    def create_api_session(self):
        """
        Initialize API session with vCenter

        Returns
        -------
        bool: if initialization was successful or not
        """

        if self.tag_session is not None:
            return True

        source_tag_settings_list = [
            self.settings.cluster_tag_source,
            self.settings.host_tag_source,
            self.settings.vm_tag_source
        ]

        # check if vm tag syncing is configured
        if source_tag_settings_list.count(None) == len(source_tag_settings_list):
            return False

        if vsphere_automation_sdk_available is False:
            log.warning(f"Unable to import Python 'vsphere-automation-sdk'. Tag syncing will be disabled.")
            return False

        log.debug(f"Starting vCenter API connection to '{self.settings.host_fqdn}'")

        # create a requests session to enable/disable TLS verification
        session = requests.session()
        session.verify = self.settings.validate_tls_certs

        # disable TLS insecure warnings if user explicitly switched off validation
        if self.settings.validate_tls_certs is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # adds proxy to the session
        if self.settings.proxy_host is not None and self.settings.proxy_port is not None:
            session.proxies.update({
                "http": f"http://{self.settings.proxy_host}:{self.settings.proxy_port}",
                "https": f"http://{self.settings.proxy_host}:{self.settings.proxy_port}",
            })

        try:
            self.tag_session = create_vsphere_client(
                server=f"{self.settings.host_fqdn}:{self.settings.port}",
                username=self.settings.username,
                password=self.settings.password,
                session=session)

        except Exception as e:
            log.warning(f"Unable to connect to vCenter API instance "
                        f"'{self.settings.host_fqdn}' on port {self.settings.port}: {e}")
            log.warning("Tag syncing will be disabled.")
            return False

        log.info(f"Successfully connected to vCenter API '{self.settings.host_fqdn}'")

        return True

    def finish(self):

        # closing tag session
        if self._sdk_instance is not None:
            try:
                connect.Disconnect(self._sdk_instance)
            except Exception as e:
                log.error(f"unable to close vCenter SDK connection: {e}")

        # closing SDK session
        if self.tag_session is not None:
            try:
                del self.tag_session
            except Exception as e:
                log.error(f"unable to close vCenter API instance connection: {e}")

    def apply(self):
        """
        Main source handler method. This method is called for each source from "main" program
        to retrieve data from it source and apply it to the NetBox inventory.

        Every update of new/existing objects fot this source has to happen here.
        """

        log.info(f"Query data from vCenter: '{self.settings.host_fqdn}'")

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
            "single host cluster": {
                "view_type": vim.ComputeResource,
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

        # skip virtual machines which are reported offline
        if self.settings.skip_offline_vms is True:
            log.info("Skipping offline VMs")
            del object_mapping["offline virtual machine"]

        for view_name, view_details in object_mapping.items():

            # test if session is still alive
            try:
                self.session.sessionManager.currentSession.key
            except (vim.fault.NotAuthenticated, AttributeError, http.client.RemoteDisconnected):
                log.info("No existing vCenter session found.")
                self.session = None
                self.tag_session = None
                self.create_sdk_session()
                self.create_api_session()

            if self.session is None:
                log.error("Recreating session failed")
                break

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

                # noinspection PyArgumentList
                view_details.get("view_handler")(obj)

            container_view.Destroy()

        self.parsing_objects_to_reevaluate = True
        log.info("Parsing objects which were marked to be reevaluated")

        for obj in self.objects_to_reevaluate:

            if isinstance(obj, vim.HostSystem):
                self.add_host(obj)
            elif isinstance(obj, vim.VirtualMachine):
                self.add_virtual_machine(obj)
            else:
                log.error(f"Unable to handle reevaluation of {obj} (type: {type(obj)})")

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
            A regex object of include filter
        exclude_filter: regex object
            A regex object of exclude filter

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

        # check if site was provided in config
        relation_name = "host_site_relation" if object_type == NBDevice else "cluster_site_relation"

        site_name = self.get_object_relation(object_name, relation_name)

        if object_type == NBDevice and site_name is None:
            site_name = self.get_site_name(NBCluster, cluster_name)
            if site_name is not None:
                log.debug2(f"Found a matching cluster site for {object_name}, using site '{site_name}'")

        # set default site name
        if site_name is None:
            site_name = self.site_name
            log.debug(f"No site relation for '{object_name}' found, using default site '{site_name}'")

        # set the site for cluster to None if None-keyword ("<NONE>") is set via cluster_site_relation
        if object_type == NBCluster and site_name == "<NONE>":
            site_name = None
            log.debug2(f"Site relation for '{object_name}' set to None")

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

        If the ration is below 2.0 then None will be returned. The probability is too low that
        this one is the correct one.

        None will also be returned if no machine was found at all.

        Parameters
        ----------
        object_type: (NBDevice, NBVM)
            type of NetBox device to find in inventory
        mac_list: list
            a list of MAC addresses to compare against NetBox interface objects

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

        if num_devices_witch_matching_macs == 1 and isinstance(matching_object, (NBDevice, NBVM)):

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

    def get_vmware_object_tags(self, obj):
        """
        Get tags from vCenter for submitted object.

        Parameters
        ----------
        obj
            pyvmomi object to retrieve tags for

        Returns
        -------
        tag_list: list
            list of NBTag objets retrieved from vCenter for this object
        """
        if obj is None:
            return

        tag_list = list()
        if self.tag_session is not None:

            # noinspection PyBroadException
            try:
                object_tag_ids = self.tag_session.tagging.TagAssociation.list_attached_tags(
                    DynamicID(type=grab(obj, "_wsdlName"), id=grab(obj, "_moId")))
            except Exception as e:
                log.error(f"Unable to retrieve vCenter tags for '{obj.name}': {e}")
                return

            for tag_id in object_tag_ids:

                # noinspection PyBroadException
                try:
                    tag_name = self.tag_session.tagging.Tag.get(tag_id).name
                    tag_description = self.tag_session.tagging.Tag.get(tag_id).description
                except Exception as e:
                    log.error(f"Unable to retrieve vCenter tag '{tag_id}' for '{obj.name}': {e}")
                    continue

                if tag_name is not None:

                    if tag_description is not None and len(f"{tag_description}") > 0:
                        tag_description = f"{primary_tag_name}: {tag_description}"
                    else:
                        tag_description = primary_tag_name

                    tag_list.append(self.inventory.add_update_object(NBTag, data={
                        "name": tag_name,
                        "description": tag_description
                    }))

        return tag_list

    def collect_object_tags(self, obj):
        """
        collect tags from object based on the config settings

        Parameters
        ----------
        obj
            pyvmomi object to retrieve tags for

        Returns
        -------
        tag_list: list
            a list of NBTag objets retrieved from vCenter for this object
        """

        if obj is None:
            return

        tag_list = list()

        if isinstance(obj, (vim.ClusterComputeResource, vim.ComputeResource)):
            tag_source = self.settings.cluster_tag_source
        elif isinstance(obj, vim.HostSystem):
            tag_source = self.settings.host_tag_source
        elif isinstance(obj, vim.VirtualMachine):
            tag_source = self.settings.vm_tag_source
        else:
            raise ValueError(f"Tags for '{grab(obj, '_wsdlName')}' are not supported")

        if tag_source is None or self.tag_session is None:
            return tag_list

        log.debug2(f"Collecting tags for {obj.name}")

        if "object" in tag_source:
            tag_list.extend(self.get_vmware_object_tags(obj))
        if "parent_folder_1" in tag_source or "parent_folder_2" in tag_source:
            parent_folder_1 = self.get_parent_object_by_class(obj, vim.Folder)
            if parent_folder_1 is not None:
                if "parent_folder_1" in tag_source:
                    tag_list.extend(self.get_vmware_object_tags(parent_folder_1))
                if "parent_folder_2" in tag_source:
                    parent_folder_2 = self.get_parent_object_by_class(obj, vim.Folder)
                    if parent_folder_2 is not None:
                        tag_list.extend(self.get_vmware_object_tags(parent_folder_2))
        if not isinstance(obj, (vim.ClusterComputeResource, vim.ComputeResource)) and "cluster" in tag_source:
            cluster = self.get_parent_object_by_class(obj, vim.ClusterComputeResource)
            if cluster is not None:
                tag_list.extend(self.get_vmware_object_tags(cluster))
            single_cluster = self.get_parent_object_by_class(obj, vim.ComputeResource)
            if single_cluster is not None:
                tag_list.extend(self.get_vmware_object_tags(single_cluster))
        if "datacenter" in tag_source:
            datacenter = self.get_parent_object_by_class(obj, vim.Datacenter)
            if datacenter is not None:
                tag_list.extend(self.get_vmware_object_tags(datacenter))

        return tag_list

    def get_object_custom_fields(self, obj):
        """
        Get custom attributes from vCenter for submitted object and as NetBox custom fields

        Parameters
        ----------
        obj
            pyvmomi object to retrieve custom attributes from

        Returns
        -------
        custom_fields: dict
            dictionary with assigned custom fields
        """

        return_custom_fields = dict()

        custom_value = list()
        if self.settings.sync_custom_attributes is True:
            custom_value = grab(obj, "customValue", fallback=list())

        if grab(obj, "_wsdlName") == "VirtualMachine":
            object_type = "virtualization.virtualmachine"
            custom_object_attributes = self.settings.vm_custom_object_attributes or list()
            object_attribute_prefix = "vm"
        else:
            object_type = "dcim.device"
            custom_object_attributes = self.settings.host_custom_object_attributes or list()
            object_attribute_prefix = "host"

        # add basic host data to device
        if object_type == "dcim.device":
            num_cpu_cores = grab(obj, "summary.hardware.numCpuCores")
            cpu_model = grab(obj, "summary.hardware.cpuModel")
            memory_size = grab(obj, "summary.hardware.memorySize")

            if num_cpu_cores is not None:
                custom_field = self.add_update_custom_field({
                    "name": "host_cpu_cores",
                    "label": "Physical CPU Cores",
                    "object_types": [object_type],
                    "type": "text",
                    "description": f"Reported Host CPU cores"
                })

                return_custom_fields[grab(custom_field, "data.name")] = f"{num_cpu_cores} {cpu_model}"

            if isinstance(memory_size, int):
                custom_field = self.add_update_custom_field({
                    "name": "host_memory",
                    "label": "Memory",
                    "object_types": [object_type],
                    "type": "text",
                    "description": f"Reported size of Memory"
                })

                memory_size = round(memory_size / 1024 ** 3)
                memory_unit = "GB"

                if memory_size >= 1024:
                    memory_size = memory_size / 1024
                    memory_unit = "TB"

                return_custom_fields[grab(custom_field, "data.name")] = f"{memory_size} {memory_unit}"

        field_definition = {grab(k, "key"): grab(k, "name") for k in grab(obj, "availableField", fallback=list())}

        for obj_custom_field in custom_value:
            key = grab(obj_custom_field, "key")
            value = grab(obj_custom_field, "value")

            if key is None or value is None:
                continue

            label = field_definition.get(key)

            if label is None:
                continue

            label = label.strip('"')

            if self.settings.custom_attribute_exclude is not None and \
                    label in self.settings.custom_attribute_exclude:
                log.debug(f"Custom attribute '{label}' excluded from sync. Skipping")
                continue

            custom_field = self.add_update_custom_field({
                "name": f"vcsa_{label}",
                "label": label,
                "object_types": [object_type],
                "type": "text",
                "description": f"vCenter '{self.name}' synced custom attribute '{label}'"
            })

            return_custom_fields[grab(custom_field, "data.name")] = value

        for custom_object_attribute in custom_object_attributes:

            attribute_data = grab(obj, custom_object_attribute, fallback="NOT FOUND")

            if attribute_data == "NOT FOUND":
                log.warning(f"This object has no attribute '{custom_object_attribute}' or attribute is undefined.")
                continue

            if isinstance(attribute_data, datetime.datetime):
                custom_field_type = "text"
                attribute_data = attribute_data.strftime("%Y-%m-%dT%H:%M:%S%z")
            elif isinstance(attribute_data, bool):
                custom_field_type = "boolean"
            elif isinstance(attribute_data, int):
                custom_field_type = "integer"
            elif isinstance(attribute_data, str):
                custom_field_type = "text"
            else:
                import json
                # noinspection PyBroadException
                try:
                    attribute_data = json.loads(json.dumps(attribute_data, cls=VmomiJSONEncoder, sort_keys=True))
                except Exception:
                    attribute_data = json.loads(json.dumps(str(attribute_data)))

                custom_field_type = "json"

            custom_field = self.add_update_custom_field({
                "name": f"vcsa_{object_attribute_prefix}_{custom_object_attribute}",
                "label": custom_object_attribute,
                "object_types": [object_type],
                "type": custom_field_type,
                "description": f"vCenter '{self.name}' synced object attribute '{custom_object_attribute}'"
            })

            return_custom_fields[grab(custom_field, "data.name")] = attribute_data

        return return_custom_fields

    def get_object_relation(self, name, relation, fallback=None):
        """

        Parameters
        ----------
        name: str
            name of the object to find a relation for
        relation: str
            name of the config variable relation (i.e: vm_tag_relation)
        fallback: str
            fallback string if no relation matched

        Returns
        -------
        data: str, list, None
            string of matching relation or list of matching tags
        """

        resolved_list = list()
        for single_relation in grab(self.settings, relation, fallback=list()):
            object_regex = single_relation.get("object_regex")
            match_found = False
            if object_regex.match(name):
                resolved_name = single_relation.get("assigned_name")
                log.debug2(f"Found a matching {relation} '{resolved_name}' ({object_regex.pattern}) for {name}")
                resolved_list.append(resolved_name)
                match_found = True

            # special cluster condition
            if match_found is False and grab(f"{relation}".split("_"), "0") == "cluster":

                stripped_name = "/".join(name.split("/")[1:])
                if object_regex.match(stripped_name):

                    resolved_name = single_relation.get("assigned_name")
                    log.debug2(f"Found a matching {relation} '{resolved_name}' ({object_regex.pattern}) "
                               f"for {stripped_name}")
                    resolved_list.append(resolved_name)

        if grab(f"{relation}".split("_"), "1") == "tag":
            return resolved_list

        else:
            resolved_name = fallback
            if len(resolved_list) >= 1:
                resolved_name = resolved_list[0]
                if len(resolved_list) > 1:
                    log.debug(f"Found {len(resolved_list)} matches for {name} in {relation}."
                              f" Using first on: {resolved_name}")

            return resolved_name

    def add_device_vm_to_inventory(self, object_type, object_data, pnic_data=None, vnic_data=None,
                                   nic_ips=None, p_ipv4=None, p_ipv6=None, vmware_object=None, disk_data=None):
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

        If an IP Prefix was found then we try to get the VRF and VLAN for this prefix. Now we compare
        if interface VLAN and prefix VLAN match up and warn if they don't. Then we try to add data to
        the IP address if not already set:

            add prefix VRF if VRF for this IP is undefined
            add tenant if tenant for this IP is undefined
                1. try prefix tenant
                2. if prefix tenant is undefined try VLAN tenant

        And we also set primary IP4/6 for this object depending on the "set_primary_ip" setting.

        If an IP address is set as primary IP for another device then using this IP on another
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
            NetBoxObject subclass of object to add
        object_data: dict
            data of object to add/update
        pnic_data: dict
            data of physical interfaces of this object, interface name as key
        vnic_data: dict
            data of virtual interfaces of this object, interface name as key
        nic_ips: dict
            a dict of ips per interface of this object, interface name as key
        p_ipv4: str
            primary IPv4 as string including netmask/prefix
        p_ipv6: str
            primary IPv6 as string including netmask/prefix
        vmware_object: (vim.HostSystem, vim.VirtualMachine)
            vmware object to pass on to 'add_update_interface' method to set up reevaluation
        disk_data: list
            data of discs which belong to a VM

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
            pprint.pprint(disk_data)

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

            if device_vm_object is None and object_data.get("serial") is not None and \
                    self.settings.match_host_by_serial is True:
                log.debug2(f"No match found. Trying to find {object_type.name} based on serial number")

                device_vm_object = self.inventory.get_by_data(object_type, data={"serial": object_data.get("serial")})

            if device_vm_object is None and object_data.get("asset_tag") is not None:
                log.debug2(f"No match found. Trying to find {object_type.name} based on asset tag")

                device_vm_object = self.inventory.get_by_data(object_type,
                                                              data={"asset_tag": object_data.get("asset_tag")})

        # look for VMs with same serial
        if object_type == NBVM and device_vm_object is None and object_data.get("serial") is not None:
            log.debug2(f"No match found. Trying to find {object_type.name} based on serial number")
            device_vm_object = self.inventory.get_by_data(object_type, data={"serial": object_data.get("serial")})

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

            if object_type == NBVM and self.settings.overwrite_vm_platform is False and \
                    object_data.get("platform") is not None:
                del object_data["platform"]

            if object_type == NBDevice and self.settings.overwrite_device_platform is False and \
                    object_data.get("platform") is not None:
                del object_data["platform"]

            device_vm_object.update(data=object_data, source=self)

        # add object to cache
        self.add_object_to_cache(vmware_object, device_vm_object)

        # update role according to config settings
        object_name = object_data.get(object_type.primary_key)
        role_name = self.get_object_relation(object_name,
                                             "host_role_relation" if object_type == NBDevice else "vm_role_relation")

        # take care of object role in NetBox
        if object_type == NBDevice:
            if role_name is None:
                role_name = "Server"
            device_vm_object.update(data={"device_role": {"name": role_name}})
        if object_type == NBVM and role_name is not None:
            device_vm_object.update(data={"role": {"name": role_name}})

        # verify if source tags have been removed from object.
        new_object_tags = list(map(NetBoxObject.extract_tag_name, object_data.get("tags", list())))

        for object_tag in device_vm_object.data.get("tags", list()):

            if not f'{object_tag.data.get("description")}'.startswith(primary_tag_name):
                continue

            if NetBoxObject.extract_tag_name(object_tag) not in new_object_tags:
                device_vm_object.remove_tags(object_tag)

        # update VM disk data information
        if version.parse(self.inventory.netbox_api_version) >= version.parse("3.7.0") and \
                object_type == NBVM and disk_data is not None and len(disk_data) > 0:

            # create pairs of existing and discovered disks.
            # currently these disks are only used within the VM model. that's why we use this simple approach and
            # just rewrite disk as they appear in order.
            # otherwise we would need to implement a matching function like matching interfaces.
            disk_zip_list = zip_longest(
                sorted(device_vm_object.get_virtual_disks(), key=lambda x: grab(x, "data.name")),
                sorted(disk_data, key=lambda x: x.get("name")),
                fillvalue="X")

            for existing, discovered in disk_zip_list:
                if existing == "X":
                    self.inventory.add_object(NBVirtualDisk, source=self,
                                              data={**discovered, **{"virtual_machine": device_vm_object}}, )
                elif discovered == "X":
                    log.info(f"{existing.name} '{existing.get_display_name(including_second_key=True)}' has been deleted")
                    existing.deleted = True
                else:
                    existing.update(data=discovered, source=self)

        # compile all nic data into one dictionary
        if object_type == NBVM:
            nic_data = vnic_data
        else:
            nic_data = {**pnic_data, **vnic_data}

        # map interfaces of existing object with discovered interfaces
        nic_object_dict = self.map_object_interfaces_to_current_interfaces(device_vm_object, nic_data)

        if object_data.get("status", "") == "active" and (nic_ips is None or len(nic_ips.keys()) == 0):
            log.debug(f"No IP addresses for '{object_name}' found!")

        primary_ipv4_object = None
        primary_ipv6_object = None

        if p_ipv4 is not None:
            try:
                primary_ipv4_object = ip_interface(p_ipv4)
            except ValueError:
                log.error(f"Primary IPv4 ({p_ipv4}) does not appear to be a valid IP address (needs included suffix).")

        if p_ipv6 is not None:
            try:
                primary_ipv6_object = ip_interface(p_ipv6)
            except ValueError:
                log.error(f"Primary IPv6 ({p_ipv6}) does not appear to be a valid IP address (needs included suffix).")

        for int_name, int_data in nic_data.items():

            if nic_object_dict.get(int_name) is not None:
                if object_type == NBDevice and self.settings.overwrite_device_interface_name is False:
                    del int_data["name"]
                if object_type == NBVM and self.settings.overwrite_vm_interface_name is False:
                    del int_data["name"]

            # add/update interface with retrieved data
            nic_object, ip_address_objects = self.add_update_interface(nic_object_dict.get(int_name), device_vm_object,
                                                                       int_data, nic_ips.get(int_name, list()),
                                                                       vmware_object=vmware_object)

            # add all interface IPs
            for ip_object in ip_address_objects:

                if ip_object is None:
                    continue

                ip_interface_object = ip_interface(grab(ip_object, "data.address"))

                # continue if address is not a primary IP
                if ip_interface_object not in [primary_ipv4_object, primary_ipv6_object]:
                    continue

                # set/update/remove primary IP addresses
                set_this_primary_ip = False
                ip_version = ip_interface_object.ip.version
                if self.settings.set_primary_ip == "always":

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

                elif self.settings.set_primary_ip != "never" and \
                        grab(device_vm_object, f"data.primary_ip{ip_version}") is None:
                    set_this_primary_ip = True

                if set_this_primary_ip is True:

                    log.debug(f"Setting IP '{grab(ip_object, 'data.address')}' as primary IPv{ip_version} for "
                              f"'{device_vm_object.get_display_name()}'")
                    device_vm_object.update(data={f"primary_ip{ip_version}": ip_object})

        return

    def get_parent_object_by_class(self, obj, object_class_to_find):

        if obj is None or object_class_to_find is None:
            return

        if isinstance(obj, object_class_to_find):
            self.recursion_level = 0
            return obj

        max_recursion = 20
        while True:
            if self.recursion_level >= max_recursion:
                self.recursion_level = 0
                return None

            # noinspection PyBroadException
            try:
                parent = obj.parent
            except Exception:
                self.recursion_level = 0
                return None

            if isinstance(parent, object_class_to_find):
                self.recursion_level = 0
                return parent

            self.recursion_level += 1
            return self.get_parent_object_by_class(parent, object_class_to_find)

    def add_object_to_cache(self, vm_object, netbox_object):

        if None in [vm_object, netbox_object]:
            return

        # noinspection PyBroadException
        try:
            vm_class_name = vm_object.__class__.__name__
            # noinspection PyProtectedMember
            vm_object_id = vm_object._GetMoId()
        except Exception:
            return

        if self.object_cache.get(vm_class_name) is None:
            self.object_cache[vm_class_name] = dict()

        self.object_cache[vm_class_name][vm_object_id] = netbox_object

    def get_object_from_cache(self, vm_object):

        if vm_object is None:
            return

        # noinspection PyBroadException
        try:
            vm_class_name = vm_object.__class__.__name__
            # noinspection PyProtectedMember
            vm_object_id = vm_object._GetMoId()
        except Exception:
            return

        if self.object_cache.get(vm_class_name) is None:
            return

        return self.object_cache[vm_class_name].get(vm_object_id)

    def add_datacenter(self, obj):
        """
        Add a vCenter datacenter as a NBClusterGroup to NetBox

        Parameters
        ----------
        obj: vim.Datacenter
            a datacenter object

        """
        if self.settings.set_source_name_as_cluster_group is True:
            name = self.name
        else:
            name = get_string_or_none(grab(obj, "name"))

        if name is None:
            return

        log.debug(f"Parsing vCenter datacenter: {name}")

        object_data = {"name": name}

        if self.settings.set_source_name_as_cluster_group is True:
            label = "Datacenter Name"
            custom_field = self.add_update_custom_field({
                "name": f"vcsa_{label}",
                "label": label,
                "object_types": ["virtualization.clustergroup"],
                "type": "text",
                "description": f"vCenter '{self.name}' synced custom attribute '{label}'"
            })

            object_data["custom_fields"] = {
                grab(custom_field, "data.name"): get_string_or_none(grab(obj, "name"))
            }

        self.add_object_to_cache(obj, self.inventory.add_update_object(NBClusterGroup, data=object_data, source=self))

    def add_cluster(self, obj):
        """
        Add a vCenter cluster as a NBCluster to NetBox. Cluster name is checked against
        cluster_include_filter and cluster_exclude_filter config setting.

        Parameters
        ----------
        obj: vim.ClusterComputeResource
            cluster to add
        """

        name = get_string_or_none(grab(obj, "name"))
        if self.settings.set_source_name_as_cluster_group is True:
            group = self.inventory.get_by_data(NBClusterGroup, data={"name": self.name})
        else:
            group = self.get_object_from_cache(self.get_parent_object_by_class(obj, vim.Datacenter))

        if name is None or group is None:
            return

        # if we're parsing a single host "cluster" and the hosts domain name should be stripped,
        # then the ComputeResources domain name gets stripped as well
        if isinstance(obj, vim.ComputeResource) and self.settings.strip_host_domain_name is True:
            name = name.split(".")[0]

        group_name = grab(group, "data.name")
        full_cluster_name = f"{group_name}/{name}"

        log.debug(f"Parsing vCenter cluster: {full_cluster_name}")

        # check for full name and then for cluster name only
        if self.passes_filter(full_cluster_name,
                              self.settings.cluster_include_filter,
                              self.settings.cluster_exclude_filter) is False \
                or self.passes_filter(name,
                                      self.settings.cluster_include_filter,
                                      self.settings.cluster_exclude_filter) is False:
            return

        site_name = self.get_site_name(NBCluster, full_cluster_name)

        data = {
            "name": name,
            "type": {"name": "VMware ESXi"},
            "group": group
        }

        if version.parse(self.inventory.netbox_api_version) >= version.parse("4.2.0"):
            data["scope_id"] = {"name": site_name}
            data["scope_type"] = "dcim.site"
        else:
            data["site"] = {"name": site_name}

        tenant_name = self.get_object_relation(full_cluster_name, "cluster_tenant_relation")
        if tenant_name is not None:
            data["tenant"] = {"name": tenant_name}

        cluster_tags = self.get_object_relation(full_cluster_name, "cluster_tag_relation")
        cluster_tags.extend(self.collect_object_tags(obj))
        if len(cluster_tags) > 0:
            data["tags"] = cluster_tags

        # try to find cluster including cluster group
        log.debug2("Trying to find a matching existing cluster")
        cluster_object = None
        fallback_cluster_object = None
        for cluster_candidate in self.inventory.get_all_items(NBCluster):
            if grab(cluster_candidate, "data.name") != name:
                continue

            # try to find a cluster with matching site
            if cluster_candidate.get_site_name() == site_name:
                cluster_object = cluster_candidate
                log.debug2("Found an existing cluster where 'name' and 'site' are matching")
                break

            if grab(cluster_candidate, "data.group") is not None and \
                    grab(cluster_candidate, "data.group.data.name") == group_name:
                cluster_object = cluster_candidate
                log.debug2("Found an existing cluster where 'name' and 'cluster group' are matching")
                break

            if grab(cluster_candidate, "data.tenant") is not None and \
                    tenant_name is not None and \
                    grab(cluster_candidate, "data.tenant.data.name") == tenant_name:
                cluster_object = cluster_candidate
                log.debug2("Found an existing cluster where 'name' and 'tenant' are matching")
                break

            # if only the name matches and there are multiple cluster with the same name we choose the first
            # cluster returned from netbox. This needs to be done to not ignore possible matches in one of
            # the next iterations
            if fallback_cluster_object is None:
                fallback_cluster_object = cluster_candidate

        if cluster_object is None and fallback_cluster_object is not None:
            log.debug2(f"Found an existing cluster where 'name' "
                       f"matches (NetBox id: {fallback_cluster_object.get_nb_reference()})")
            cluster_object = fallback_cluster_object

        if cluster_object is not None:
            cluster_object.update(data=data, source=self)
        else:
            cluster_object = self.inventory.add_update_object(NBCluster, data=data, source=self)

        self.add_object_to_cache(obj, cluster_object)

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

        log.debug(f"Parsing vCenter virtual switch: {name}")

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

        log.debug(f"Parsing vCenter port group: {name}")

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

        if name is not None and self.settings.strip_host_domain_name is True:
            name = name.split(".")[0]

        # parse data
        log.debug(f"Parsing vCenter host: {name}")

        #
        # Filtering
        #

        # manage site and cluster
        cluster_object = self.get_parent_object_by_class(obj, vim.ClusterComputeResource)

        if cluster_object is None:
            cluster_object = self.get_parent_object_by_class(obj, vim.ComputeResource)

        if cluster_object is None:
            log.error(f"Requesting cluster for host '{name}' failed. Skipping.")
            return

        if log.level == DEBUG3:
            try:
                log.info("Cluster data")
                dump(cluster_object)
            except Exception as e:
                log.error(e)

        # get cluster object
        nb_cluster_object = self.get_object_from_cache(cluster_object)

        if nb_cluster_object is None:
            log.debug(f"Host '{name}' is not part of a permitted cluster. Skipping")
            return

        cluster_name = get_string_or_none(grab(nb_cluster_object, "data.name"))

        # get a site for this host
        if self.settings.set_source_name_as_cluster_group is True:
            group = self.inventory.get_by_data(NBClusterGroup, data={"name": self.name})
        else:
            group = self.get_object_from_cache(self.get_parent_object_by_class(obj, vim.Datacenter))
        group_name = grab(group, "data.name")
        site_name = self.get_site_name(NBDevice, name, f"{group_name}/{cluster_name}")

        if name in self.processed_host_names.get(site_name, list()) and obj not in self.objects_to_reevaluate:
            log.warning(f"Host '{name}' for site '{site_name}' already parsed. "
                        "Make sure to use unique host names. Skipping")
            return

        # add host to processed list
        if self.processed_host_names.get(site_name) is None:
            self.processed_host_names[site_name] = list()

        self.processed_host_names[site_name].append(name)

        # filter hosts by name
        if self.passes_filter(name, self.settings.host_include_filter, self.settings.host_exclude_filter) is False:
            return

        #
        # Collecting data
        #

        # collect all necessary data
        manufacturer = get_string_or_none(grab(obj, "summary.hardware.vendor"))
        model = get_string_or_none(grab(obj, "summary.hardware.model"))
        product_name = get_string_or_none(grab(obj, "summary.config.product.name"))
        product_version = get_string_or_none(grab(obj, "summary.config.product.version"))

        # collect platform
        platform = f"{product_name} {product_version}"
        platform = self.get_object_relation(platform, "host_platform_relation", fallback=platform)

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
            if serial_num_key in identifier_dict.keys() and self.settings.collect_hardware_serial is True:
                log.debug2(f"Found {serial_num_key}: {get_string_or_none(identifier_dict.get(serial_num_key))}")
                if serial is None:
                    serial = get_string_or_none(identifier_dict.get(serial_num_key))

        # add asset tag if desired and present
        asset_tag = None

        if self.settings.collect_hardware_asset_tag is True and "AssetTag" in identifier_dict.keys():

            banned_tags = ["Default string", "NA", "N/A", "None", "Null", "oem", "o.e.m",
                           "to be filled by o.e.m.", "Unknown"]

            this_asset_tag = identifier_dict.get("AssetTag")

            if this_asset_tag.lower() not in [x.lower() for x in banned_tags]:
                asset_tag = this_asset_tag

        # get host_tenant_relation
        tenant_name = self.get_object_relation(name, "host_tenant_relation")

        # get host_tag_relation
        host_tags = self.get_object_relation(name, "host_tag_relation")

        # get vCenter tags
        host_tags.extend(self.collect_object_tags(obj))

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
            "cluster": nb_cluster_object,
            "status": status
        }

        # add data if present
        if serial is not None:
            host_data["serial"] = serial
        if asset_tag is not None:
            host_data["asset_tag"] = asset_tag
        if platform is not None:
            host_data["platform"] = {"name": platform}
        if tenant_name is not None:
            host_data["tenant"] = {"name": tenant_name}
        if len(host_tags) > 0:
            host_data["tags"] = host_tags

        # add custom fields if present and configured
        host_custom_fields = self.get_object_custom_fields(obj)
        if len(host_custom_fields) > 0:
            host_data["custom_fields"] = host_custom_fields

        if self.settings.skip_host_nics is True:
            return

        # iterate over hosts virtual switches, needed to enrich data on physical interfaces
        self.network_data["vswitch"][name] = dict()
        for vswitch in grab(obj, "config.network.vswitch", fallback=list()):

            vswitch_name = unquote(grab(vswitch, "name"))

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
            pswitch_name = unquote(grab(pswitch, "dvsName"))
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
                pgroup_nics = list()
                if grab(nic_order, "activeNic") is not None:
                    pgroup_nics += nic_order.activeNic
                if grab(nic_order, "standbyNic") is not None:
                    pgroup_nics += nic_order.standbyNic

                self.network_data["host_pgroup"][name][pgroup_name] = {
                    "vlan_id": grab(pgroup, "spec.vlanId"),
                    "vswitch": unquote(grab(pgroup, "spec.vswitchName")),
                    "nics": pgroup_nics
                }

        # now iterate over all physical interfaces and collect data
        pnic_data_dict = dict()
        pnic_hints = dict()
        # noinspection PyBroadException
        try:
            for hint in obj.configManager.networkSystem.QueryNetworkHint(""):
                pnic_hints[hint.device] = hint
        except Exception:
            pass

        for pnic in grab(obj, "config.network.pnic", fallback=list()):

            pnic_name = grab(pnic, "device")
            pnic_key = grab(pnic, "key")

            log.debug2("Parsing {}: {}".format(grab(pnic, "_wsdlName"), pnic_name))

            pnic_link_speed = grab(pnic, "linkSpeed.speedMb")
            if pnic_link_speed is None:
                pnic_link_speed = grab(pnic, "spec.linkSpeed.speedMb")
            if pnic_link_speed is None:
                pnic_link_speed = grab(pnic, "validLinkSpecification.0.speedMb")

            pnic_link_duplex = grab(pnic, "linkSpeed.duplex")
            if pnic_link_duplex is None:
                pnic_link_duplex = grab(pnic, "spec.linkSpeed.duplex")
            if pnic_link_duplex is None:
                pnic_link_duplex = grab(pnic, "validLinkSpecification.0.duplex")

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

            pnic_mac_address = normalize_mac_address(grab(pnic, "mac"))

            if pnic_hints.get(pnic_name) is not None:
                pnic_switch_port = grab(pnic_hints.get(pnic_name), 'connectedSwitchPort')
                if pnic_switch_port is not None:
                    pnic_sp_sys_name = grab(pnic_switch_port, 'systemName')
                    if pnic_sp_sys_name is None:
                        pnic_sp_sys_name = grab(pnic_switch_port, 'devId')
                    if pnic_sp_sys_name is not None:
                        pnic_description += f" (conn: {pnic_sp_sys_name} - {grab(pnic_switch_port, 'portId')})"

            if self.settings.host_nic_exclude_by_mac_list is not None and \
                    pnic_mac_address in self.settings.host_nic_exclude_by_mac_list:
                log.debug2(f"Host NIC with MAC '{pnic_mac_address}' excluded from sync. Skipping")
                continue

            pnic_data = {
                "name": unquote(pnic_name),
                "device": None,     # will be set once we found the correct device
                "mac_address": pnic_mac_address,
                "enabled": bool(grab(pnic, "linkSpeed")),
                "description": unquote(pnic_description),
                "type": NetBoxInterfaceType(pnic_link_speed).get_this_netbox_type()
            }

            if pnic_mtu is not None:
                pnic_data["mtu"] = pnic_mtu
            if pnic_mode is not None:
                pnic_data["mode"] = pnic_mode

            # add link speed and duplex attributes
            if version.parse(self.inventory.netbox_api_version) >= version.parse("3.2.0"):
                if pnic_link_speed is not None:
                    pnic_data["speed"] = pnic_link_speed * 1000
                if pnic_link_duplex is not None:
                    pnic_data["duplex"] = "full" if pnic_link_duplex is True else "half"

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

                    tagged_vlan_list.append({
                        "name": pnic_vlan.get("name"),
                        "vid": pnic_vlan.get("vid"),
                        "site": {
                            "name": site_name
                        }
                    })

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
                "name": unquote(vnic_name),
                "device": None,     # will be set once we found the correct device
                "mac_address": normalize_mac_address(grab(vnic, "spec.mac")),
                "enabled": True,    # ESXi vmk interface is enabled by default
                "mtu": grab(vnic, "spec.mtu"),
                "type": "virtual"
            }

            if vnic_mode is not None:
                vnic_data["mode"] = vnic_mode

            if vnic_description is not None:
                vnic_data["description"] = unquote(vnic_description)
            else:
                vnic_description = ""

            if vnic_portgroup_data is not None and vnic_portgroup_vlan_id != 0:

                vnic_data["untagged_vlan"] = {
                    "name": unquote(f"ESXi {vnic_portgroup} (ID: {vnic_portgroup_vlan_id}) ({site_name})"),
                    "vid": vnic_portgroup_vlan_id,
                    "site": {
                        "name": site_name
                    }
                }

            elif vnic_dv_portgroup_data is not None:

                tagged_vlan_list = list()
                for vnic_dv_portgroup_data_vlan_id in vnic_dv_portgroup_data_vlan_ids:

                    if vnic_mode != "tagged":
                        break

                    if vnic_dv_portgroup_data_vlan_id == 0:
                        continue

                    tagged_vlan_list.append({
                        "name": unquote(f"{vnic_dv_portgroup_data.get('name')}-{vnic_dv_portgroup_data_vlan_id}"),
                        "vid": vnic_dv_portgroup_data_vlan_id,
                        "site": {
                            "name": site_name
                        }
                    })

                if len(tagged_vlan_list) > 0:
                    vnic_data["tagged_vlans"] = tagged_vlan_list

            vnic_data_dict[vnic_name] = vnic_data

            # check if interface has the default route or is described as management interface
            vnic_is_primary = False
            for management_match in self.settings.host_management_interface_match:
                if management_match in vnic_description.lower():
                    vnic_is_primary = True

            if grab(vnic, "spec.ipRouteSpec") is not None:

                vnic_is_primary = True

            if vnic_ips.get(vnic_name) is None:
                vnic_ips[vnic_name] = list()

            int_v4 = "{}/{}".format(grab(vnic, "spec.ip.ipAddress"), grab(vnic, "spec.ip.subnetMask"))

            if self.settings.permitted_subnets.permitted(int_v4, interface_name=vnic_name) is True:
                vnic_ips[vnic_name].append(int_v4)

                if vnic_is_primary is True and host_primary_ip4 is None:
                    host_primary_ip4 = int_v4

            for ipv6_entry in grab(vnic, "spec.ip.ipV6Config.ipV6Address", fallback=list()):

                int_v6 = "{}/{}".format(grab(ipv6_entry, "ipAddress"), grab(ipv6_entry, "prefixLength"))

                if self.settings.permitted_subnets.permitted(int_v6, interface_name=vnic_name) is True:
                    vnic_ips[vnic_name].append(int_v6)

                    # set first valid IPv6 address as primary IPv6
                    # not the best way, but maybe we can find more information in "spec.ipRouteSpec"
                    # about default route, and we could use that to determine the correct IPv6 address
                    if vnic_is_primary is True and host_primary_ip6 is None:
                        host_primary_ip6 = int_v6

        # add host to inventory
        self.add_device_vm_to_inventory(NBDevice, object_data=host_data, pnic_data=pnic_data_dict,
                                        vnic_data=vnic_data_dict, nic_ips=vnic_ips,
                                        p_ipv4=host_primary_ip4, p_ipv6=host_primary_ip6, vmware_object=obj)

        return

    def add_virtual_machine(self, obj):
        """
        Parse a vCenter VM  add to NetBox once all data is gathered.

        VMs are parsed twice. First only "online" VMs are parsed and added. In the second
        round also "offline" VMs will be parsed. This helps if VMs are cloned and used
        for upgrades but then have the same name.

        First VM will be filtered:
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

        if name is not None and self.settings.strip_vm_domain_name is True:
            name = name.split(".")[0]

        #
        # Filtering
        #

        # get VM UUID
        vm_uuid = grab(obj, "config.instanceUuid")

        if vm_uuid is None or vm_uuid in self.processed_vm_uuid and obj not in self.objects_to_reevaluate:
            return

        log.debug(f"Parsing vCenter VM: {name}")

        # get VM power state
        status = "active" if get_string_or_none(grab(obj, "runtime.powerState")) == "poweredOn" else "offline"

        # check if vm is template
        template = grab(obj, "config.template")
        if bool(self.settings.skip_vm_templates) is True and template is True:
            log.debug2(f"VM '{name}' is a template. Skipping")
            return

        if bool(self.settings.skip_srm_placeholder_vms) is True \
                and f"{grab(obj, 'config.managedBy.extensionKey')}".startswith("com.vmware.vcDr"):
            log.debug2(f"VM '{name}' is a SRM placeholder VM. Skipping")
            return

        # ignore offline VMs during first run
        if self.parsing_vms_the_first_time is True and status == "offline":
            log.debug2(f"Ignoring {status} VM '{name}' on first run")
            return

        # add to processed VMs
        self.processed_vm_uuid.append(vm_uuid)

        parent_host = self.get_parent_object_by_class(grab(obj, "runtime.host"), vim.HostSystem)
        cluster_object = self.get_parent_object_by_class(parent_host, vim.ClusterComputeResource)

        # get single host 'cluster' if VM runs on one
        if cluster_object is None:
            cluster_object = self.get_parent_object_by_class(parent_host, vim.ComputeResource)

        if self.settings.set_source_name_as_cluster_group is True:
            group = self.inventory.get_by_data(NBClusterGroup, data={"name": self.name})
        else:
            group = self.get_parent_object_by_class(cluster_object, vim.Datacenter)

        if None in [parent_host, cluster_object, group]:
            log.error(f"Requesting host or cluster for Virtual Machine '{name}' failed. Skipping.")
            return

        nb_cluster_object = self.get_object_from_cache(cluster_object)

        # check VM cluster
        if nb_cluster_object is None:
            log.debug(f"Virtual machine '{name}' is not part of a permitted cluster. Skipping")
            return

        parent_name = grab(parent_host, "name")
        cluster_name = grab(nb_cluster_object, "data.name")
        cluster_full_name = f"{group.name}/{cluster_name}"

        if name in self.processed_vm_names.get(cluster_full_name, list()) and obj not in self.objects_to_reevaluate:
            log.warning(f"Virtual machine '{name}' for cluster '{cluster_full_name}' already parsed. "
                        "Make sure to use unique VM names. Skipping")
            return

        # add vm to processed list
        if self.processed_vm_names.get(cluster_full_name) is None:
            self.processed_vm_names[cluster_full_name] = list()

        self.processed_vm_names[cluster_full_name].append(name)

        # filter VMs by name
        if self.passes_filter(name, self.settings.vm_include_filter, self.settings.vm_exclude_filter) is False:
            return

        #
        # Collect data
        #

        # check if cluster is a Standalone ESXi
        site_name = nb_cluster_object.get_site_name()
        if site_name is None:
            site_name = self.get_site_name(NBCluster, cluster_full_name)

        # first check against vm_platform_relation
        platform = get_string_or_none(grab(obj, "config.guestFullName"))
        platform = get_string_or_none(grab(obj, "guest.guestFullName", fallback=platform))

        # extract prettyName from extraConfig exposed by guest tools
        extra_config = [x.value for x in grab(obj, "config.extraConfig", fallback=[])
                        if x.key == "guestOS.detailed.data"]
        if len(extra_config) > 0:
            pretty_name = [x for x in quoted_split(extra_config[0].replace("' ", "', ")) if x.startswith("prettyName")]
            if len(pretty_name) > 0:
                platform = pretty_name[0].replace("prettyName='","")

        if platform is not None:
            platform = self.get_object_relation(platform, "vm_platform_relation", fallback=platform)

        hardware_devices = grab(obj, "config.hardware.device", fallback=list())

        annotation = None
        if self.settings.skip_vm_comments is False:
            annotation = get_string_or_none(grab(obj, "config.annotation"))

        # assign vm_tenant_relation
        tenant_name = self.get_object_relation(name, "vm_tenant_relation")

        # assign vm_tag_relation
        vm_tags = self.get_object_relation(name, "vm_tag_relation")

        # get vCenter tags
        vcenter_tags = self.collect_object_tags(obj)

        # check if VM tag excludes VM from being synced to NetBox
        for sync_exclude_tag in self.settings.vm_exclude_by_tag_filter or list():
            if sync_exclude_tag in vcenter_tags:
                log.debug(f"Virtual machine vCenter tag '{sync_exclude_tag}' in matches 'vm_exclude_by_tag_filter'. "
                          f"Skipping")
                return

        vm_tags.extend(vcenter_tags)

        # vm memory depending on setting
        vm_memory = grab(obj, "config.hardware.memoryMB", fallback=0)

        if self.settings.vm_disk_and_ram_in_decimal is True:
            vm_memory = int(vm_memory / 1024 * 1000)

        vm_data = {
            "name": name,
            "cluster": nb_cluster_object,
            "status": status,
            "memory": vm_memory,
            "vcpus": grab(obj, "config.hardware.numCPU")
        }

        # Add adaption for change in NetBox 3.3.0 VM model
        # issue: https://github.com/netbox-community/netbox/issues/10131#issuecomment-1225783758
        if version.parse(self.inventory.netbox_api_version) >= version.parse("3.3.0"):
            vm_data["site"] = {"name": site_name}

            if self.settings.track_vm_host:
                vm_data["device"] = self.get_object_from_cache(parent_host)

        # Add adaption for added virtual disks in NetBox 3.7.0
        if version.parse(self.inventory.netbox_api_version) < version.parse("3.7.0"):
            vm_data["disk"] = int(sum([getattr(comp, "capacityInKB", 0) for comp in hardware_devices
                                       if isinstance(comp, vim.vm.device.VirtualDisk)
                                       ]) / 1024 / 1024)

        # Add adaptation for the new 'serial' field in NetBox 4.1.0 VM model
        if version.parse(self.inventory.netbox_api_version) >= version.parse("4.1.0"):
            vm_data["serial"] = vm_uuid

        if platform is not None:
            vm_data["platform"] = {"name": platform}
        if annotation is not None:
            vm_data["comments"] = annotation
        if tenant_name is not None:
            vm_data["tenant"] = {"name": tenant_name}
        if len(vm_tags) > 0:
            vm_data["tags"] = vm_tags

        # add custom fields if present and configured
        vm_custom_fields = self.get_object_custom_fields(obj)
        if len(vm_custom_fields) > 0:
            vm_data["custom_fields"] = vm_custom_fields

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
        disk_data = list()

        # track MAC addresses in order add dummy guest interfaces
        processed_interface_macs = list()

        # get VM interfaces
        for vm_device in hardware_devices:

            if isinstance(vm_device, vim.vm.device.VirtualDisk):

                vm_device_backing = vm_device.backing
                while grab(vm_device_backing, "parent") is not None:
                    vm_device_backing = vm_device_backing.parent

                vm_device_description = list()
                if grab(vm_device, 'backing.diskMode') is not None:
                    vm_device_description.append(
                        str(grab(vm_device, 'backing.diskMode')).capitalize().replace("_", "-"))

                if grab(vm_device, 'backing.thinProvisioned') is True:
                    vm_device_description.append("ThinProvisioned")
                else:
                    vm_device_description.append("ThickProvisioned")

                if grab(vm_device_backing, "fileName") is not None:
                    vm_device_description.append(grab(vm_device_backing, "fileName"))

                disk_size_in_kb = grab(vm_device, "capacityInKB", fallback=0)
                if version.parse(self.inventory.netbox_api_version) < version.parse("4.1.0"):
                    disk_size = int(disk_size_in_kb / 1024 / 1024)
                    if disk_size < 1:
                        vm_device_description.append(f"Size: {int(disk_size_in_kb / 1024)} MB")
                        disk_size = 1
                # since NetBox 4.1.0 disk size is represented in MB
                else:
                    disk_size = int(disk_size_in_kb / 1024)
                    if self.settings.vm_disk_and_ram_in_decimal:
                        disk_size = int(disk_size / 1024 * 1000)

                disk_data.append({
                    "name": grab(vm_device, "deviceInfo.label"),
                    "size": disk_size,
                    "description": " / ".join(vm_device_description)
                })

                continue

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

            processed_interface_macs.append(int_mac)

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

                    if self.settings.permitted_subnets.permitted(int_ip_address, interface_name=int_full_name) is False:
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
                "name": unquote(int_full_name),
                "virtual_machine": None,
                "mac_address": int_mac,
                "description": unquote(int_description),
                "enabled": int_connected,
            }

            if int_mtu is not None and self.settings.sync_vm_interface_mtu is True:
                vm_nic_data["mtu"] = int_mtu
            if int_mode is not None:
                vm_nic_data["mode"] = int_mode

            if int_network_vlan_ids is not None and int_mode != "tagged-all":

                if len(int_network_vlan_ids) == 1 and int_network_vlan_ids[0] != 0:

                    vm_nic_data["untagged_vlan"] = {
                        "name": unquote(int_network_name),
                        "vid": int_network_vlan_ids[0],
                        "site": {
                            "name": site_name
                        }
                    }
                else:
                    tagged_vlan_list = list()
                    for int_network_vlan_id in int_network_vlan_ids:

                        if int_network_vlan_id == 0:
                            continue

                        tagged_vlan_list.append({
                            "name": unquote(f"{int_network_name}-{int_network_vlan_id}"),
                            "vid": int_network_vlan_id,
                            "site": {
                                "name": site_name
                            }
                        })

                    if len(tagged_vlan_list) > 0:
                        vm_nic_data["tagged_vlans"] = tagged_vlan_list

            nic_data[int_full_name] = vm_nic_data

        # find dummy guest NIC interfaces
        if self.settings.sync_vm_dummy_interfaces is True:
            for guest_nic in grab(obj, "guest.net", fallback=list()):

                # get matching guest NIC MAC
                guest_nic_mac = normalize_mac_address(grab(guest_nic, "macAddress"))

                # skip interfaces of MAC addresses for already known interfaces
                if guest_nic_mac is None or guest_nic_mac in processed_interface_macs:
                    continue

                processed_interface_macs.append(guest_nic_mac)

                int_full_name = "vNIC Dummy-{}".format("".join(guest_nic_mac.split(":")[-2:]))

                log.debug2(f"Parsing dummy network device: {guest_nic_mac}")

                if nic_ips.get(int_full_name) is None:
                    nic_ips[int_full_name] = list()

                # grab all valid interface IP addresses
                for int_ip in grab(guest_nic, "ipConfig.ipAddress", fallback=list()):

                    int_ip_address = f"{int_ip.ipAddress}/{int_ip.prefixLength}"

                    if self.settings.permitted_subnets.permitted(int_ip_address, interface_name=int_full_name) is True:
                        nic_ips[int_full_name].append(int_ip_address)

                vm_nic_data = {
                    "name": int_full_name,
                    "virtual_machine": None,
                    "mac_address": guest_nic_mac,
                    "enabled": grab(guest_nic, "connected", fallback=False),
                }

                if len(nic_ips.get(int_full_name, list())) == 0:
                    log.debug(f"Dummy network interface '{int_full_name}' has no IP addresses assigned. Skipping")
                    continue

                nic_data[int_full_name] = vm_nic_data

        # if VM has only one IPv6 on all interfaces, use it as primary IPv6 address
        if vm_primary_ip6 is None or True:
            all_ips = [y for xs in nic_ips.values() for y in xs]
            potential_primary_ipv6_list = list()

            for ip in all_ips:
                # noinspection PyBroadException
                try:
                    ip_address_object = ip_interface(ip)
                except Exception:
                    continue

                if ip_address_object.version == 6:
                    potential_primary_ipv6_list.append(ip_address_object)

            if len(potential_primary_ipv6_list) == 1:
                log.debug(f"Found one IPv6 '{potential_primary_ipv6_list[0]}' address on all interfaces of "
                          f"VM '{name}', using it as primary IPv6.")
                vm_primary_ip6 = potential_primary_ipv6_list[0]

        # add VM to inventory
        self.add_device_vm_to_inventory(NBVM, object_data=vm_data, vnic_data=nic_data,
                                        nic_ips=nic_ips, p_ipv4=vm_primary_ip4, p_ipv6=vm_primary_ip6,
                                        vmware_object=obj, disk_data=disk_data)

        return

    def update_basic_data(self):
        """

        Returns
        -------

        """

        # add source identification tag
        self.inventory.add_update_object(NBTag, data={
            "name": self.source_tag,
            "description": f"Marks objects synced from vCenter '{self.name}' "
                           f"({self.settings.host_fqdn}) to this NetBox Instance."
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
            role_data = {"name": "Server", "vm_role": True}
            if server_role_object.is_new is True:
                role_data["color"] = "9e9e9e"

            server_role_object.update(data=role_data)

# EOF
