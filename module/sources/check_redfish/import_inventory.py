# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2021 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from ipaddress import ip_network
import os
import glob
import json

from packaging import version

from module.sources.common.source_base import SourceBase
from module.common.logging import get_logger
from module.common.misc import grab, get_string_or_none
from module.common.support import normalize_mac_address, ip_valid_to_add_to_netbox
from module.netbox.object_classes import (
    NetBoxInterfaceType,
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
    NBInterface,
    NBIPAddress,
    NBPrefix,
    NBTenant,
    NBVRF,
    NBVLAN,
    NBPowerPort,
    NBInventoryItem,
    NBCustomField
)

log = get_logger()


class CheckRedfish(SourceBase):

    minimum_check_redfish_version = "1.2.0"

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
        NBInterface,
        NBIPAddress,
        NBPrefix,
        NBTenant,
        NBVRF,
        NBVLAN,
        NBPowerPort,
        NBInventoryItem,
        NBCustomField
    ]

    settings = {
        "enabled": True,
        "inventory_file_path": None,
        "permitted_subnets": None,
        "overwrite_host_name": False,
        "overwrite_power_supply_name": False,
        "overwrite_power_supply_attributes": True,
        "overwrite_interface_name": False,
        "overwrite_interface_attributes": True,
    }

    init_successful = False
    inventory = None
    name = None
    source_tag = None
    source_type = "check_redfish"
    enabled = False
    inventory_file_path = None
    interface_adapter_type_dict = dict()

    def __init__(self, name=None, settings=None, inventory=None):

        if name is None:
            raise ValueError(f"Invalid value for attribute 'name': '{name}'.")

        self.inventory = inventory
        self.name = name

        self.parse_config_settings(settings)

        self.source_tag = f"Source: {name}"

        if self.enabled is False:
            log.info(f"Source '{name}' is currently disabled. Skipping")
            return

        self.init_successful = True

    def parse_config_settings(self, config_settings):

        validation_failed = False

        for setting in ["inventory_file_path"]:
            if config_settings.get(setting) is None:
                log.error(f"Config option '{setting}' in 'source/{self.name}' can't be empty/undefined")
                validation_failed = True

        inv_path = config_settings.get("inventory_file_path")
        if not os.path.exists(inv_path):
            log.error(f"Inventory file path '{inv_path}' not found.")
            validation_failed = True

        if os.path.isfile(inv_path):
            log.error(f"Inventory file path '{inv_path}' needs to be a directory.")
            validation_failed = True

        if not os.access(inv_path, os.X_OK | os.R_OK):
            log.error(f"Inventory file path '{inv_path}' not readable.")
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

        if validation_failed is True:
            log.error("Config validation failed. Exit!")
            exit(1)

        for setting in self.settings.keys():
            setattr(self, setting, config_settings.get(setting))

    def apply(self):

        self.add_necessary_custom_fields()

        for filename in glob.glob(f"{self.inventory_file_path}/*.json"):

            if not os.path.isfile(filename):
                continue

            with open(filename) as json_file:
                try:
                    file_content = json.load(json_file)
                except json.decoder.JSONDecodeError as e:
                    log.error(f"Inventory file {filename} contains invalid json: {e}")
                    continue

            log.debug(f"Parsing inventory file {filename}")

            # get inventory_layout_version
            inventory_layout_version = grab(file_content, "meta.inventory_layout_version", fallback=0)

            if version.parse(inventory_layout_version) < version.parse(self.minimum_check_redfish_version):
                log.error(f"Inventory layout version '{inventory_layout_version}' of file {filename} not supported. "
                          f"Minimum layout version {self.minimum_check_redfish_version} required.")

                continue

            # try to get device by supplied NetBox id
            inventory_id = grab(file_content, "meta.inventory_id")
            device_object = self.inventory.get_by_id(NBDevice, inventory_id)

            # try to find device by serial of first system in inventory
            device_serial = grab(file_content, "inventory.system.0.serial")
            if device_object is None:
                device_object = self.inventory.get_by_data(NBDevice, data={
                    "serial": device_serial
                })

            if device_object is None:
                log.error(f"Unable to find {NBDevice.name} with id '{inventory_id}' or "
                          f"serial '{device_serial}' in NetBox inventory from inventory file {filename}")
                continue

            for system in grab(file_content, "inventory.system", fallback=list()):

                # get status
                status = "offline"
                if get_string_or_none(grab(system, "power_state")) == "On":
                    status = "active"

                serial = get_string_or_none(grab(system, "serial"))
                name = get_string_or_none(grab(system, "host_name"))
                device_data = {
                    "device_type": {
                        "model": get_string_or_none(grab(system, "model")),
                        "manufacturer": {
                            "name": get_string_or_none(grab(system, "manufacturer"))
                        },
                    },
                    "status": status
                }

                if serial is not None:
                    device_data["serial"] = serial
                if name is not None and self.overwrite_host_name is True:
                    device_data["name"] = name

                device_object.update(data=device_data, source=self)

                # reset interface types
                self.interface_adapter_type_dict = dict()

                # parse all components
                self.update_power_supply(device_object, file_content)
                self.update_fan(device_object, file_content)
                self.update_memory(device_object, file_content)
                self.update_proc(device_object, file_content)
                self.update_physical_drive(device_object, file_content)
                self.update_storage_controller(device_object, file_content)
                self.update_storage_enclosure(device_object, file_content)
                self.update_network_adapter(device_object, file_content)
                self.update_network_interface(device_object, file_content)
                self.update_manager(device_object, file_content)

    def update_power_supply(self, device_object, inventory_data):

        # get power supplies
        current_ps = list()
        for ps in self.inventory.get_all_items(NBPowerPort):
            if grab(ps, "data.device") == device_object:
                current_ps.append(ps)

        current_ps.sort(key=lambda x: grab(x, "data.name"))

        ps_index = 0
        ps_items = list()
        for ps in grab(inventory_data, "inventory.power_supply", fallback=list()):

            if grab(ps, "operation_status") in ["NotPresent", "Absent"]:
                continue

            ps_name = get_string_or_none(grab(ps, "name"))
            input_voltage = get_string_or_none(grab(ps, "input_voltage"))
            ps_type = get_string_or_none(grab(ps, "type"))
            bay = get_string_or_none(grab(ps, "bay"))
            capacity_in_watt = grab(ps, "capacity_in_watt")
            firmware = get_string_or_none(grab(ps, "firmware"))
            health_status = get_string_or_none(grab(ps, "health_status"))
            model = get_string_or_none(grab(ps, "model"))

            # set name
            if ps_name.lower().startswith("hp"):
                ps_name = "Power Supply"

            if bay is not None and f"{bay}" not in ps_name:
                ps_name += f" {bay}"

            name_details = list()
            if input_voltage is not None:
                name_details.append(f"{input_voltage}V")
            if ps_type is not None:
                name_details.append(f"{ps_type}")

            name = ps_name
            if len(name_details) > 0:
                name += f" ({' '.join(name_details)})"

            # set description
            description = list()
            if model is not None:
                description.append(f"Model: {model}")

            size = None
            if capacity_in_watt is not None:
                size = f"{capacity_in_watt}W"

            # compile inventory item data
            ps_items.append({
                "inventory_type": "Power Supply",
                "health": health_status,
                "device": device_object,
                "description": description,
                "full_name": name,
                "serial": get_string_or_none(grab(ps, "serial")),
                "manufacturer": get_string_or_none(grab(ps, "vendor")),
                "part_number": get_string_or_none(grab(ps, "part_number")),
                "firmware": firmware,
                "size": size
            })

            # compile power supply data
            ps_data = {
                "name": name,
                "device": device_object,
                "description": ", ".join(description)
            }

            if capacity_in_watt is not None:
                ps_data["maximum_draw"] = capacity_in_watt
            if firmware is not None:
                ps_data["custom_fields"] = {"firmware": firmware, "health": health_status}

            # add/update power supply data
            ps_object = grab(current_ps, f"{ps_index}")
            if ps_object is None:
                self.inventory.add_object(NBPowerPort, data=ps_data, source=self)
            else:
                if self.overwrite_power_supply_name is False:
                    del(ps_data["name"])

                data_to_update = self.patch_data(ps_object, ps_data, self.overwrite_power_supply_attributes)
                ps_object.update(data=data_to_update, source=self)

            ps_index += 1

        self.update_all_items(ps_items)

    def update_fan(self, device_object, inventory_data):

        items = list()
        for fan in grab(inventory_data, "inventory.fan", fallback=list()):

            if grab(fan, "operation_status") in ["NotPresent", "Absent"]:
                continue

            fan_name = get_string_or_none(grab(fan, "name"))
            health_status = get_string_or_none(grab(fan, "health_status"))
            physical_context = get_string_or_none(grab(fan, "physical_context"))
            fan_id = get_string_or_none(grab(fan, "id"))
            reading = get_string_or_none(grab(fan, "reading"))
            reading_unit = get_string_or_none(grab(fan, "reading_unit"))

            description = list()
            speed = None
            if physical_context is not None:
                description.append(f"Context: {physical_context}")

            if reading is not None and reading_unit is not None:
                reading_unit = "%" if reading_unit.lower() == "percent" else reading_unit
                speed = f"{reading}{reading_unit}"

            items.append({
                "inventory_type": "Fan",
                "device": device_object,
                "description": description,
                "full_name": f"{fan_name} (ID: {fan_id})",
                "health": health_status,
                "speed": speed
            })

        self.update_all_items(items)

    def update_memory(self, device_object, inventory_data):

        items = list()
        for memory in grab(inventory_data, "inventory.memory", fallback=list()):

            if grab(memory, "operation_status") in ["NotPresent", "Absent"]:
                continue

            name = get_string_or_none(grab(memory, "name"))
            health_status = get_string_or_none(grab(memory, "health_status"))
            size_in_mb = grab(memory, "size_in_mb", fallback=0)
            channel = get_string_or_none(grab(memory, "channel"))
            slot = get_string_or_none(grab(memory, "slot"))
            socket = get_string_or_none(grab(memory, "socket"))
            speed = get_string_or_none(grab(memory, "speed"))
            dimm_type = get_string_or_none(grab(memory, "type"))

            if size_in_mb == 0 or (health_status is None and grab(memory, "operation_status") != "GoodInUse"):
                continue

            name_details = list()
            if dimm_type is not None:
                name_details.append(f"{dimm_type}")

            name += f" ({' '.join(name_details)})"

            description = list()
            if socket is not None:
                description.append(f"Socket: {socket}")
            if channel is not None:
                description.append(f"Channel: {channel}")
            if slot is not None:
                description.append(f"Slot: {slot}")

            items.append({
                "inventory_type": "DIMM",
                "device": device_object,
                "description": description,
                "full_name": name,
                "serial": get_string_or_none(grab(memory, "serial")),
                "manufacturer": get_string_or_none(grab(memory, "manufacturer")),
                "part_number": get_string_or_none(grab(memory, "part_number")),
                "health": health_status,
                "size": f"{size_in_mb / 1024}GB",
                "speed": f"{speed}MHz",
            })

        self.update_all_items(items)

    def update_proc(self, device_object, inventory_data):

        items = list()
        for processor in grab(inventory_data, "inventory.processor", fallback=list()):

            if grab(processor, "operation_status") in ["NotPresent", "Absent"]:
                continue

            instruction_set = get_string_or_none(grab(processor, "instruction_set"))
            current_speed = grab(processor, "current_speed")
            model = get_string_or_none(grab(processor, "model"))
            cores = get_string_or_none(grab(processor, "cores"))
            threads = get_string_or_none(grab(processor, "threads"))
            socket = get_string_or_none(grab(processor, "socket"))
            health_status = get_string_or_none(grab(processor, "health_status"))

            name = f"{socket} ({model})"

            if current_speed is not None:
                current_speed = f"{current_speed / 1000}GHz"
            size = None
            if cores is not None and threads is not None:
                size = f"{cores}/{threads}"

            description = list()
            if instruction_set is not None:
                description.append(f"{instruction_set}")
            if cores is not None:
                description.append(f"Cores: {cores}")
            if threads is not None:
                description.append(f"Threads: {threads}")

            items.append({
                "inventory_type": "CPU",
                "device": device_object,
                "description": description,
                "manufacturer": get_string_or_none(grab(processor, "manufacturer")),
                "full_name": name,
                "serial": get_string_or_none(grab(processor, "serial")),
                "health": health_status,
                "size": size,
                "speed": current_speed
            })

        self.update_all_items(items)

    def update_physical_drive(self, device_object, inventory_data):

        items = list()
        for pd in grab(inventory_data, "inventory.physical_drive", fallback=list()):

            if grab(pd, "operation_status") in ["NotPresent", "Absent"]:
                continue

            pd_name = get_string_or_none(grab(pd, "name"))
            firmware = get_string_or_none(grab(pd, "firmware"))
            interface_type = get_string_or_none(grab(pd, "interface_type"))
            health_status = get_string_or_none(grab(pd, "health_status"))
            size_in_byte = grab(pd, "size_in_byte", fallback=0)
            model = get_string_or_none(grab(pd, "model"))
            speed_in_rpm = grab(pd, "speed_in_rpm")
            location = get_string_or_none(grab(pd, "location"))
            bay = get_string_or_none(grab(pd, "bay"))
            pd_type = get_string_or_none(grab(pd, "type"))
            serial = get_string_or_none(grab(pd, "serial"))
            pd_id = get_string_or_none(grab(pd, "id"))

            if serial is not None and serial in [x.get("serial") for x in items]:
                continue

            if pd_name.lower().startswith("hp"):
                pd_name = "Physical Drive"

            if location is not None and location not in pd_name:
                pd_name += f" {location}"
            elif bay is not None and bay not in pd_name:
                pd_name += f" {bay}"
            else:
                pd_name += f" {pd_id}"

            name = pd_name

            name_details = list()
            if pd_type is not None:
                name_details.append(pd_type)
            if model is not None and model not in name:
                name_details.append(model)

            name += f" ({' '.join(name_details)})"

            description = list()
            if interface_type is not None:
                description.append(f"Interface: {interface_type}")

            size = None
            speed = None
            if size_in_byte is not None and size_in_byte != 0:
                size = "%dGB" % (size_in_byte / 1000 ** 3)
            if speed_in_rpm is not None and speed_in_rpm != 0:
                speed = f"{speed_in_rpm}RPM"

            items.append({
                "inventory_type": "Physical Drive",
                "device": device_object,
                "description": description,
                "manufacturer": get_string_or_none(grab(pd, "manufacturer")),
                "full_name": name,
                "serial": serial,
                "part_number": get_string_or_none(grab(pd, "part_number")),
                "firmware": firmware,
                "health": health_status,
                "size": size,
                "speed": speed
            })

        self.update_all_items(items)

    def update_storage_controller(self, device_object, inventory_data):

        items = list()
        for sc in grab(inventory_data, "inventory.storage_controller", fallback=list()):

            if grab(sc, "operation_status") in ["NotPresent", "Absent"]:
                continue

            name = get_string_or_none(grab(sc, "name"))
            model = get_string_or_none(grab(sc, "model"))
            location = get_string_or_none(grab(sc, "location"))
            logical_drive_ids = grab(sc, "logical_drive_ids", fallback=list())
            physical_drive_ids = grab(sc, "physical_drive_ids", fallback=list())
            cache_size_in_mb = grab(sc, "cache_size_in_mb")

            if name.lower().startswith("hp"):
                name = model

            if location is not None and location not in name:
                name += f" {location}"

            description = list()
            if len(logical_drive_ids) > 0:
                description.append(f"LDs: {len(logical_drive_ids)}")
            if len(physical_drive_ids) > 0:
                description.append(f"PDs: {len(physical_drive_ids)}")

            size = None
            if cache_size_in_mb is not None and cache_size_in_mb != 0:
                size = f"{cache_size_in_mb}MB"

            items.append({
                "inventory_type": "Storage Controller",
                "device": device_object,
                "description": description,
                "manufacturer": get_string_or_none(grab(sc, "manufacturer")),
                "full_name": name,
                "serial": get_string_or_none(grab(sc, "serial")),
                "firmware": get_string_or_none(grab(sc, "firmware")),
                "health": get_string_or_none(grab(sc, "health_status")),
                "size": size
            })

        self.update_all_items(items)

    def update_storage_enclosure(self, device_object, inventory_data):

        items = list()
        for se in grab(inventory_data, "inventory.storage_enclosure", fallback=list()):

            if grab(se, "operation_status") in ["NotPresent", "Absent"]:
                continue

            name = get_string_or_none(grab(se, "name"))
            model = get_string_or_none(grab(se, "model"))
            location = get_string_or_none(grab(se, "location"))
            num_bays = get_string_or_none(grab(se, "num_bays"))

            if name.lower().startswith("hp") and model is not None:
                name = model

            if location is not None and location not in name:
                name += f" {location}"

            size = None
            if num_bays is not None:
                size = f"Bays: {num_bays}"

            items.append({
                "inventory_type": "Storage Enclosure",
                "device": device_object,
                "manufacturer": get_string_or_none(grab(se, "manufacturer")),
                "full_name": name,
                "serial": get_string_or_none(grab(se, "serial")),
                "firmware": get_string_or_none(grab(se, "firmware")),
                "health": get_string_or_none(grab(se, "health_status")),
                "size": size
            })

        self.update_all_items(items)

    def update_network_adapter(self, device_object, inventory_data):

        items = list()
        for adapter in grab(inventory_data, "inventory.network_adapter", fallback=list()):

            if grab(adapter, "operation_status") in ["NotPresent", "Absent"]:
                continue

            adapter_name = get_string_or_none(grab(adapter, "name"))
            adapter_id = get_string_or_none(grab(adapter, "id"))
            model = get_string_or_none(grab(adapter, "model"))
            firmware = get_string_or_none(grab(adapter, "firmware"))
            health_status = get_string_or_none(grab(adapter, "health_status"))
            serial = get_string_or_none(grab(adapter, "serial"))
            num_ports = get_string_or_none(grab(adapter, "num_ports"))
            manufacturer = get_string_or_none(grab(adapter, "manufacturer"))

            if adapter_name.startswith("Network Adapter View"):
                adapter_name = adapter_name.replace("Network Adapter View", "")
            if adapter_name.startswith("Network Adapter"):
                adapter_name = adapter_name.replace("Network Adapter", "")
            adapter_name = adapter_name.strip()

            if adapter_id != adapter_name:
                if len(adapter_name) == 0:
                    adapter_name = adapter_id
                else:
                    adapter_name = f"{adapter_name} ({adapter_id})"

            if manufacturer is None:
                if adapter_name.startswith("HPE"):
                    manufacturer = "HPE"
                elif adapter_name.startswith("HP"):
                    manufacturer = "HP"

            name = adapter_name
            size = None
            if model is not None:
                name += f" - {model}"
            if num_ports is not None:
                size = f"{num_ports} Ports"

            nic_type = NetBoxInterfaceType(name)

            if adapter_id is not None:
                self.interface_adapter_type_dict[adapter_id] = nic_type

            items.append({
                "inventory_type": "NIC",
                "device": device_object,
                "manufacturer": manufacturer,
                "full_name": name,
                "serial": serial,
                "part_number": get_string_or_none(grab(adapter, "part_number")),
                "firmware": firmware,
                "health": health_status,
                "size": size,
                "speed": nic_type.get_speed_human()
            })

        self.update_all_items(items)

    def update_network_interface(self, device_object, inventory_data):

        port_data_dict = dict()
        nic_ips = dict()
        discovered_mac_list = list()

        for nic_port in grab(inventory_data, "inventory.network_port", fallback=list()):

            if grab(nic_port, "operation_status") in ["Disabled"]:
                continue

            port_name = get_string_or_none(grab(nic_port, "name"))
            port_id = get_string_or_none(grab(nic_port, "id"))
            mac_address = get_string_or_none(grab(nic_port, "addresses.0"))  # get 1st mac address
            link_status = get_string_or_none(grab(nic_port, "link_status"))
            manager_ids = grab(nic_port, "manager_ids", fallback=list())
            hostname = get_string_or_none(grab(nic_port, "hostname"))
            health_status = get_string_or_none(grab(nic_port, "health_status"))
            adapter_id = get_string_or_none(grab(nic_port, "adapter_id"))

            mac_address = normalize_mac_address(mac_address)

            if mac_address in discovered_mac_list:
                continue

            if mac_address is not None:
                discovered_mac_list.append(mac_address)

            if port_name is not None:
                port_name += f" ({port_id})"
            else:
                port_name = port_id

            # get port speed
            link_speed = grab(nic_port, "capable_speed") or grab(nic_port, "current_speed") or 0

            if link_speed == 0 and adapter_id is not None:
                link_type = self.interface_adapter_type_dict.get(adapter_id)
            else:
                link_type = NetBoxInterfaceType(link_speed)

            description = list()
            if hostname is not None:
                description.append(f"Hostname: {hostname}")

            mgmt_only = False
            # if number of managers belonging to this port is not 0 then it's a BMC port
            if len(manager_ids) > 0:
                mgmt_only = True

            # get enabled state
            enabled = False
            # assume that a mgmt_only interface is always enabled as we retrieved data via redfish
            if "up" in f"{link_status}".lower() or mgmt_only is True:
                enabled = True

            port_data_dict[port_name] = {
                "inventory_type": "NIC Port",
                "name": port_name,
                "device": device_object,
                "mac_address": mac_address,
                "enabled": enabled,
                "description": ", ".join(description),
                "type": link_type.get_this_netbox_type(),
                "mgmt_only": mgmt_only,
                "health": health_status
            }

            # collect ip addresses
            nic_ips[port_name] = list()
            nic_ips[port_name].extend(grab(nic_port, "ipv4_addresses", fallback=list()))
            nic_ips[port_name].extend(grab(nic_port, "ipv6_addresses", fallback=list()))

        data = self.map_object_interfaces_to_current_interfaces(device_object, port_data_dict)

        for port_name, port_data in port_data_dict.items():

            # get current object for this interface if it exists
            nic_object = data.get(port_name)

            # unset "illegal" attributes
            for attribute in ["inventory_type", "health"]:
                if attribute in port_data:
                    del(port_data[attribute])

            # del empty mac address attribute
            if port_data.get("mac_address") is None:
                del (port_data["mac_address"])

            # create or update interface with data
            if nic_object is None:
                nic_object = self.inventory.add_object(NBInterface, data=port_data, source=self)
            else:
                if self.overwrite_interface_name is False and port_data.get("name") is not None:
                    del(port_data["name"])

                data_to_update = self.patch_data(nic_object, port_data, self.overwrite_interface_attributes)
                nic_object.update(data=data_to_update, source=self)

            # check for interface ips
            for nic_ip in nic_ips.get(port_name, list()):

                if ip_valid_to_add_to_netbox(nic_ip, self.permitted_subnets, port_name) is False:
                    continue

                self.add_ip_address(nic_ip, nic_object, grab(device_object, "data.site.data.name"))

    def update_manager(self, device_object, inventory_data):

        items = list()
        for manager in grab(inventory_data, "inventory.manager", fallback=list()):

            name = get_string_or_none(grab(manager, "name"))
            model = get_string_or_none(grab(manager, "model"))
            licenses = grab(manager, "licenses", fallback=list())

            if name == "Manager" and model is not None:
                name = model

            if model is not None and model not in name:
                name += f" {model}"

            description = None
            if len(licenses) > 0:
                description = f"Licenses: %s" % (", ".join(licenses))

            items.append({
                "inventory_type": "Manager",
                "device": device_object,
                "description": description,
                "full_name": name,
                "manufacturer": grab(device_object, "data.device_type.data.manufacturer.data.name"),
                "firmware": get_string_or_none(grab(manager, "firmware")),
                "health": get_string_or_none(grab(manager, "health_status"))
            })

        self.update_all_items(items)

    def update_all_items(self, items):

        if not isinstance(items, list):
            raise ValueError(f"Value for 'items' must be type 'list' got: {items}")

        if len(items) == 0:
            return

        # get device
        device = grab(items, "0.device")
        inventory_type = grab(items, "0.inventory_type")

        if device is None or inventory_type is None:
            return

        # sort items by full_name
        items.sort(key=lambda x: x.get("full_name"))

        # get current inventory items for
        current_inventory_items = list()
        for item in self.inventory.get_all_items(NBInventoryItem):
            if grab(item, "data.device") == device and \
                    grab(item, "data.custom_fields.inventory-type") == inventory_type:

                current_inventory_items.append(item)

        # sort items by display name
        current_inventory_items.sort(key=lambda x: x.get_display_name())

        mapped_items = dict(zip([x.get("full_name") for x in items], current_inventory_items))

        for item in items:

            self.update_item(item, mapped_items.get(item.get("full_name")))

        for current_inventory_item in current_inventory_items:

            if current_inventory_item not in mapped_items.values():
                current_inventory_item.deleted = True

    def update_item(self, item_data: dict, inventory_object: NBInventoryItem = None):

        device = item_data.get("device")
        full_name = item_data.get("full_name")
        label = item_data.get("label")
        manufacturer = item_data.get("manufacturer")
        part_number = item_data.get("part_number")
        serial = item_data.get("serial")
        description = item_data.get("description")
        firmware = item_data.get("firmware")
        health = item_data.get("health")
        inventory_type = item_data.get("inventory_type")
        size = item_data.get("size")
        speed = item_data.get("speed")

        if device is None:
            return False

        if isinstance(description, list):
            description = ", ".join(description)

        # compile inventory item data
        inventory_data = {
            "device": device,
            "discovered": True
        }

        if full_name is not None:
            inventory_data["name"] = full_name
        if description is not None and len(description) > 0:
            inventory_data["description"] = description
        if serial is not None:
            inventory_data["serial"] = serial
        if manufacturer is not None:
            inventory_data["manufacturer"] = {"name": manufacturer}
        if part_number is not None:
            inventory_data["part_id"] = part_number
        if label is not None:
            inventory_data["label"] = label

        # custom fields
        custom_fields = dict()
        if firmware is not None:
            custom_fields["firmware"] = firmware
        if health is not None:
            custom_fields["health"] = health
        if inventory_type is not None:
            custom_fields["inventory-type"] = inventory_type
        if size is not None:
            custom_fields["inventory-size"] = size
        if speed is not None:
            custom_fields["inventory-speed"] = speed

        if len(custom_fields.keys()) > 0:
            inventory_data["custom_fields"] = custom_fields

        if inventory_object is None:
            self.inventory.add_object(NBInventoryItem, data=inventory_data, source=self)
        else:
            inventory_object.update(data=inventory_data, source=self)

        return True

    def add_update_custom_field(self, data):

        custom_field = self.inventory.get_by_data(NBCustomField, data={"name": data.get("name")})

        if custom_field is None:
            self.inventory.add_object(NBCustomField, data=data, source=self)
        else:
            custom_field.update(data={"content_types": data.get("content_types")}, source=self)

    def add_necessary_custom_fields(self):

        # add Firmware
        self.add_update_custom_field({
            "name": "firmware",
            "label": "Firmware",
            "content_types": [
                "dcim.inventoryitem",
                "dcim.powerport"
            ],
            "type": "text",
            "description": "Item Firmware"
        })

        # add inventory type
        self.add_update_custom_field({
            "name": "inventory-type",
            "label": "Type",
            "content_types": ["dcim.inventoryitem"],
            "type": "text",
            "description": "Describes the type of inventory item"
        })

        # add inventory size
        self.add_update_custom_field({
            "name": "inventory-size",
            "label": "Size",
            "content_types": ["dcim.inventoryitem"],
            "type": "text",
            "description": "Describes the size of the inventory item if applicable"
        })

        # add inventory speed
        self.add_update_custom_field({
            "name": "inventory-speed",
            "label": "Speed",
            "content_types": ["dcim.inventoryitem"],
            "type": "text",
            "description": "Describes the size of the inventory item if applicable"
        })

        # add health status
        self.add_update_custom_field({
            "name": "health",
            "label": "Health",
            "content_types": [
                "dcim.inventoryitem",
                "dcim.powerport",
                "dcim.device"
            ],
            "type": "text",
            "description": "Shows the currently discovered health status"
        })

# EOF
