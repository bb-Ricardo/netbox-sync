
import atexit
from socket import gaierror
from ipaddress import ip_address, ip_network, ip_interface
import re

import pprint

from pyVim.connect import SmartConnectNoSSL, Disconnect
from pyVmomi import vim

from module.netbox.object_classes import *
from module.common.misc import grab, do_error_exit, dump
from module.common.support import normalize_mac_address, format_ip
from module import plural
from module.common.logging import get_logger

log = get_logger()

class VMWareHandler():

    dependend_netbox_objects = [
        NBTags,
        NBManufacturers,
        NBDeviceTypes,
        NBPlatforms,
        NBClusterTypes,
        NBClusterGroups,
        NBDeviceRoles,
        NBSites,
        NBClusters,
    #]
    #"""
        NBDevices,
        NBVMs,
        NBVMInterfaces,
        NBInterfaces,
        NBIPAddresses,

    ] #"""

    session = None
    inventory = None

    init_successfull = False

    source_type = "vmware"

    source_tag = None
    site_name = None

    networks = dict()
    standalone_hosts = list()

    settings = {
        "host_fqdn": None,
        "port": 443,
        "username": None,
        "password": None,
        "host_exclude_filter": None,
        "host_include_filter": None,
        "vm_exclude_filter": None,
        "vm_include_filter": None,
        "netbox_host_device_role": "Server",
        "netbox_vm_device_role": "Server",
        "permitted_subnets": None,
        "collect_hardware_asset_tag": True
    }

    def __init__(self, name=None, config=None, inventory=None):

        if name is None:
            raise ValueError("Invalid value for attribute 'name': '{name}'.")

        self.inventory = inventory
        self.name = name

        self.parse_config(config)

        self.create_session()

        self.source_tag = f"Source: {name}"
        self.site_name = f"vCenter: {name}"

        if self.session is not None:
            self.init_successfull = True


    def parse_config(self, config):

        validation_failed = False
        for setting in ["host_fqdn", "port", "username", "password" ]:
            if config.get(setting) is None:
                log.error(f"Config option '{setting}' in 'source/{self.name}' can't be empty/undefined")
                validation_failed = True

        # check permitted ip subnets
        if config.get("permitted_subnets") is None:
            log.info(f"Config option 'permitted_subnets' in 'source/{self.name}' is undefined. No IP addresses will be populated to Netbox!")
        else:
            config["permitted_subnets"] = [x.strip() for x in config.get("permitted_subnets").split(",") if x.strip() != ""]

            permitted_subnets = list()
            for permitted_subnet in config["permitted_subnets"]:
                try:
                    permitted_subnets.append(ip_network(permitted_subnet))
                except Exception as e:
                    log.error(f"Problem parsing permitted subnet: {e}")
                    validation_failed = True

            config["permitted_subnets"] = permitted_subnets
            
        # check include and exclude filter expressions
        for setting in [x for x in config.keys() if "filter" in x]:
            if config.get(setting) is None or config.get(setting).strip() == "":
                continue
            
            re_compiled = None
            try:
                re_compiled = re.compile(config.get(setting))
            except Exception as e:
                log.error(f"Problem parsing parsing regular expression for '{setting}': {e}")
                validation_failed = True
        
            config[setting] = re_compiled
            
        if validation_failed is True:
            do_error_exit("Config validation failed. Exit!")

        for setting in self.settings.keys():
            setattr(self, setting, config.get(setting))

    def create_session(self):

        if self.session is not None:
            return True

        log.info(f"Starting vCenter connection to '{self.host_fqdn}'")

        try:
            instance = SmartConnectNoSSL(
                host=self.host_fqdn,
                port=self.port,
                user=self.username,
                pwd=self.password
            )
            atexit.register(Disconnect, instance)
            self.session = instance.RetrieveContent()

        except (gaierror, vim.fault.InvalidLogin, OSError) as e:

            log.error(
                f"Unable to connect to vCenter instance '{self.host_fqdn}' on port {self.port}. "
                f"Reason: {e}"
            )

            return False

        log.info(f"Successfully connected to vCenter '{self.host_fqdn}'")

        return True

    def apply(self):

        self.inizialize_basic_data()

        log.info(f"Query data from vCenter: '{self.host_fqdn}'")

        # Mapping of object type keywords to view types and handlers
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
                "view_type": vim.Network,
                "view_handler": self.add_network
            },
            "host": {
                "view_type": vim.HostSystem,
                "view_handler": self.add_host
            },
            "virtual machine": {
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

            log.info("vCenter returned '%d' %s%s" % (len(view_objects), view_name, plural(len(view_objects))))

            for obj in view_objects:

                view_details.get("view_handler")(obj)

            container_view.Destroy()

    def add_datacenter(self, obj):

        if grab(obj, "name") is None:
            return

        self.inventory.add_update_object(NBClusterGroups,
                                         data = { "name": obj.name }, source=self)

    def add_cluster(self, obj):

        if grab(obj, "name") is None or grab(obj, "parent.parent.name") is None:
            return

        self.inventory.add_update_object(NBClusters,
                                         data = {
                                            "name": obj.name,
                                            "type": { "name": "VMware ESXi" },
                                            "group": { "name": obj.parent.parent.name }
                                         },
                                         source=self)

    def add_network(self, obj):

        if grab(obj, "key") is None or grab(obj, "name") is None:
            return

        self.networks[obj.key] = obj.name

    def add_host(self, obj):

        # ToDo:
        # * find Host based on device mac addresses

        name = grab(obj, "name")
         
        # parse data
        log.debug2(f"Parsing vCenter host: {name}")
       
        # filter hosts
        # first includes
        if self.host_include_filter is not None:
            if not self.host_include_filter.match(name):
                log.debug(f"Host '{name}' did not match include filter '{self.host_include_filter.pattern}'. Skipping")
                return
                
        # second excludes
        if self.host_exclude_filter is not None:
            if self.host_exclude_filter.match(name):
                log.debug(f"Host '{name}' matched exclude filter '{self.host_exclude_filter.pattern}'. Skipping")
                return

        manufacturer = grab(obj, "summary.hardware.vendor")
        model = grab(obj, "summary.hardware.model")
        platform = "{} {}".format(grab(obj, "config.product.name"), grab(obj, "config.product.version"))
        
        cluster = grab(obj, "parent.name")
        status = "active" if grab(obj, "summary.runtime.connectionState") == "connected" else "offline"
        
        # prepare identifiers
        identifiers = grab(obj, "summary.hardware.otherIdentifyingInfo")
        identifier_dict = dict()
        if identifiers is not None:
            for item in identifiers:
                value = grab(item, "identifierValue", fallback="")
                if len(str(value).strip()) > 0:
                    identifier_dict[grab(item, "identifierType.key")] = str(value).strip()

        # try to find serial
        serial = None

        for serial_num_key in [ "EnclosureSerialNumberTag", "SerialNumberTag", "ServiceTag"]:
            if serial_num_key in identifier_dict.keys():
                serial = identifier_dict.get(serial_num_key)
                break

        # add asset tag if desired and present
        asset_tag = None

        if bool(self.collect_hardware_asset_tag) is True and "AssetTag" in identifier_dict.keys():

            banned_tags = [ "Default string", "NA", "N/A", "None", "Null", "oem", "o.e.m",
                            "to be filled by o.e.m.", "Unknown" ]

            this_asset_tag = identifier_dict.get("AssetTag")

            if this_asset_tag.lower() not in [x.lower() for x in banned_tags]:
                asset_tag = this_asset_tag

        # handle standalone hosts
        if cluster == name:
            # Store the host so that we can check VMs against it
            self.standalone_hosts.append(cluster)
            cluster = "Standalone ESXi Host"

        data={
            "name": name,
            "device_role": {"name": self.netbox_host_device_role},
            "device_type": {
                "model": model,
                "manufacturer": {
                    "name": manufacturer
                }
            },
            "platform": {"name": platform},
            "site": {"name": self.site_name},
            "cluster": {"name": cluster},
            "status": status
        }

        if serial is not None:
            data["serial"]: serial
        if asset_tag is not None:
            data["asset_tag"]: asset_tag

        host_object = self.inventory.add_update_object(NBDevices, data=data, source=self)

        for pnic in grab(obj, "config.network.pnic", fallback=list()):

            log.debug2("Parsing {}: {}".format(grab(pnic, "_wsdlName"), grab(pnic, "device")))

            pnic_link_speed = grab(pnic, "linkSpeed.speedMb")
            if pnic_link_speed is None:
                pnic_link_speed = grab(pnic, "spec.linkSpeed.speedMb")
            if pnic_link_speed is None:
                pnic_link_speed = grab(pnic, "validLinkSpecification.0.speedMb")

            pnic_link_speed_text = f"{pnic_link_speed}Mbps " if pnic_link_speed is not None else ""

            pnic_speed_type_mapping = {
                100: "100base-tx",
                1000: "1000base-t",
                10000: "10gbase-t",
                25000: "25gbase-x-sfp28",
                40000: "40gbase-x-qsfpp"
            }

            pnic_data = {
                "name": grab(pnic, "device"),
                "device": host_object,
                "mac_address": normalize_mac_address(grab(pnic, "mac")),
                "enabled": bool(grab(pnic, "spec.linkSpeed")),
                "description": f"{pnic_link_speed_text}Physical Interface",
                "type": pnic_speed_type_mapping.get(pnic_link_speed, "other")
            }

            self.inventory.add_update_object(NBInterfaces, data=pnic_data, source=self)

            
        for vnic in grab(obj, "config.network.vnic", fallback=list()):

            log.debug2("Parsing {}: {}".format(grab(vnic, "_wsdlName"), grab(vnic, "device")))
            
            vnic_data = {
                "name": grab(vnic, "device"),
                "device": host_object,
                "mac_address": normalize_mac_address(grab(vnic, "spec.mac")),
                "mtu": grab(vnic, "spec.mtu"),
                "description": grab(vnic, "portgroup"),
                "type": "virtual"
            }

            vnic_object = self.inventory.add_update_object(NBInterfaces, data=vnic_data, source=self)

            vnic_ip = "{}/{}".format(grab(vnic, "spec.ip.ipAddress"), grab(vnic, "spec.ip.subnetMask"))
            
            if format_ip(vnic_ip) is None:
                logging.error(f"IP address '{vnic_ip}' for {vnic_object.get_display_name()} invalid!")
                continue
            
            ip_permitted = False

            ip_address_object = ip_address(grab(vnic, "spec.ip.ipAddress"))
            for permitted_subnet in self.permitted_subnets:
                if ip_address_object in permitted_subnet:
                    ip_permitted = True
                    break

            if ip_permitted is False:
                log.debug(f"IP address {vnic_ip} not part of any permitted subnet. Skipping.")
                continue
                
            vnic_ip_data = {
                "address": format_ip(vnic_ip),
                "assigned_object_id": vnic_object.nb_id,
                "assigned_object_type": "dcim.interface"
            }

            self.inventory.add_update_object(NBIPAddresses, data=vnic_ip_data, source=self)


    def add_virtual_machine(self, obj):

        # ToDo:
        # * find VM based on device mac addresses
        
        name = grab(obj, "name")
        
        log.debug2(f"Parsing vCenter host: {name}")
        
        # filter VMs
        # first includes
        if self.vm_include_filter is not None:
            if not self.vm_include_filter.match(name):
                log.debug(f"Virtual machine '{name}' did not match include filter '{self.vm_include_filter.pattern}'. Skipping")
                return
                
        # second excludes
        if self.vm_exclude_filter is not None:
            if self.vm_exclude_filter.match(name):
                log.debug(f"Virtual Machine '{name}' matched exclude filter '{self.vm_exclude_filter.pattern}'. Skipping")
                return

        cluster = grab(obj, "runtime.host.parent.name")
        if cluster in self.standalone_hosts:
            cluster = "Standalone ESXi Host"

        platform = grab(obj, "config.guestFullName")
        platform = grab(obj, "guest.guestFullName", fallback=platform)

        status = "active" if grab(obj, "runtime.powerState") == "poweredOn" else "offline"

        hardware_devices = grab(obj, "config.hardware.device", fallback=list())

        disk = int(sum([ getattr(comp, "capacityInKB", 0) for comp in hardware_devices
                       if isinstance(comp, vim.vm.device.VirtualDisk)
                            ]) / 1024 / 1024)

        data = {
            "name": grab(obj, "name"),
            "role": {"name": self.settings.get("netbox_vm_device_role")},
            "status": status,
            "memory": grab(obj, "config.hardware.memoryMB"),
            "vcpus": grab(obj, "config.hardware.numCPU"),
            "disk": disk,
            "comments": grab(obj, "config.annotation")
        }
        
        if cluster is not None:
            data["cluster"] = {"name": cluster}
        if platform is not None:
            data["platform"] = {"name": platform}

        vm_object = self.inventory.add_update_object(NBVMs, data=data, source=self)

        # ToDo:
        # * get current interfaces and compare description (primary key in vCenter)
        
        # get vm interfaces
        for vm_device in hardware_devices:

            int_mac = normalize_mac_address(grab(vm_device, "macAddress"))

            # not a network interface
            if int_mac is None:
                continue

            log.debug2("Parsing device {}: {}".format(grab(vm_device, "_wsdlName"), grab(vm_device, "macAddress")))

            int_network_name = self.networks.get(grab(vm_device, "backing.port.portgroupKey"))
            int_connected = grab(vm_device, "connectable.connected")
            int_label = grab(vm_device, "deviceInfo.label", fallback="")

            int_name = "vNIC {}".format(int_label.split(" ")[-1])
            
            int_ip_addresses = list()

            for guest_nic in grab(obj, "guest.net", fallback=list()):

                if int_mac != normalize_mac_address(grab(guest_nic, "macAddress")):
                    continue

                int_network_name = grab(guest_nic, "network", fallback=int_network_name)
                int_connected = grab(guest_nic, "connected", fallback=int_connected)

                for ip in grab(guest_nic, "ipConfig.ipAddress", fallback=list()):
                    int_ip_addresses.append(f"{ip.ipAddress}/{ip.prefixLength}")


            if int_network_name is not None:
                int_name = f"{int_name} ({int_network_name})"

            vm_nic_data = {
                "name": int_name,
                "virtual_machine": vm_object,
                "mac_address": int_mac,
                "description": int_label,
                "enabled": int_connected,
            }

            vm_nic_object = self.inventory.get_by_data(NBVMInterfaces, data={"mac_address": int_mac})
            
            if vm_nic_object is not None:
                vm_nic_object.update(data=vm_nic_data, source=self)
            else:
                vm_nic_object = self.inventory.add_update_object(NBVMInterfaces, data=vm_nic_data, source=self)

            for int_ip_address in int_ip_addresses:
                        
                if format_ip(int_ip_address) is None:
                    logging.error(f"IP address '{int_ip_address}' for {vm_nic_object.get_display_name()} invalid!")
                    continue
                
                ip_permitted = False

                ip_address_object = ip_address(int_ip_address.split("/")[0])
                for permitted_subnet in self.permitted_subnets:
                    if ip_address_object in permitted_subnet:
                        ip_permitted = True
                        break

                if ip_permitted is False:
                    log.debug(f"IP address {int_ip_address} not part of any permitted subnet. Skipping.")
                    continue
                
                # apply ip filter
                vm_nic_ip_data = {
                    "address": format_ip(int_ip_address),
                    "assigned_object_id": vm_nic_object.nb_id,
                    "assigned_object_type": "virtualization.vminterface"
                }

                self.inventory.add_update_object(NBIPAddresses, data=vm_nic_ip_data, source=self)

    def inizialize_basic_data(self):

        # add source identification tag
        self.inventory.add_update_object(NBTags, data={
            "name": self.source_tag,
            "description": f"Marks sources synced from vCenter {self.name} "
                           f"({self.host_fqdn}) to this NetBox Instance."
        })

        self.inventory.add_update_object(NBSites, data={
            "name": self.site_name,
            "comments": f"A default virtual site created to house objects "
                        "that have been synced from this vCenter instance."
        })

        self.inventory.add_update_object(NBClusters, data={
            "name": "Standalone ESXi Host",
            "type": {"name": "VMware ESXi"},
            "comments": "A default cluster created to house standalone "
                        "ESXi hosts and VMs that have been synced from "
                        "vCenter."
        })

        self.inventory.add_update_object(NBDeviceRoles, data={
            "name": "Server",
            "color": "9e9e9e",
            "vm_role": True
        })


# EOF
