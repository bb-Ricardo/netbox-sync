# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import os
import glob
import json

from packaging import version

from module.sources.common.source_base import SourceBase
from module.sources.check_redfish.config import CheckRedfishConfig
from module.common.logging import get_logger
from module.common.misc import grab, get_string_or_none
from module.common.support import normalize_mac_address
from module.netbox.inventory import NetBoxInventory
from module.netbox import *

log = get_logger()


class CheckRedfish(SourceBase):
    """
    Source class to import check_redfish inventory files
    """

    # minimum check_redfish inventory version
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
        NBSiteGroup,
        NBCluster,
        NBDevice,
        NBInterface,
        NBMACAddress,
        NBIPAddress,
        NBPrefix,
        NBTenant,
        NBVRF,
        NBVLAN,
        NBVLANGroup,
        NBPowerPort,
        NBInventoryItem,
        NBCustomField
    ]

    source_type = "check_redfish"

    device_object = None
    inventory_file_content = None
    manager_name = None

    def __init__(self, name=None):

        if name is None:
            raise ValueError(f"Invalid value for attribute 'name': '{name}'.")

        self.inventory = NetBoxInventory()
        self.name = name

        # parse settings
        settings_handler = CheckRedfishConfig()
        settings_handler.source_name = self.name
        self.settings = settings_handler.parse()

        self.set_source_tag()

        if self.settings.enabled is False:
            log.info(f"Source '{name}' is currently disabled. Skipping")
            return

        self.init_successful = True

        self.interface_adapter_type_dict = dict()

    def apply(self):
        """
        Main source handler method. This method is called for each source from "main" program
        to retrieve data from it source and apply it to the NetBox inventory.

        Every update of new/existing objects fot this source has to happen here.

        First try to find and iterate over each inventory file.
        Then parse the system data first and then all components.
        """

        # first add all custom fields we need for this source
        self.add_necessary_base_objects()

        for filename in glob.glob(f"{self.settings.inventory_file_path}/*.json"):

            self.reset_inventory_state()

            if self.read_inventory_file_content(filename) is False:
                continue

            # try to get device by supplied NetBox id
            inventory_id = grab(self.inventory_file_content, "meta.inventory_id")

            # parse inventory id to int as all NetBox ids are type integer
            try:
                inventory_id = int(inventory_id)
            except (ValueError, TypeError):
                log.warning(f"Value for meta.inventory_id '{inventory_id}' must be an integer. "
                            f"Cannot use inventory_id to match device in NetBox.")

            self.device_object = self.inventory.get_by_id(NBDevice, inventory_id)

            if self.device_object is not None:
                log.debug2("Found a matching %s object '%s' based on inventory id '%d'" %
                           (self.device_object.name,
                            self.device_object.get_display_name(including_second_key=True),
                            inventory_id))

            else:
                # try to find device by serial of first system in inventory
                device_serial = grab(self.inventory_file_content, "inventory.system.0.serial")
                if self.device_object is None:
                    self.device_object = self.inventory.get_by_data(NBDevice, data={
                        "serial": device_serial
                    })

                if self.device_object is None:
                    log.error(f"Unable to find {NBDevice.name} with id '{inventory_id}' or "
                              f"serial '{device_serial}' in NetBox inventory from inventory file {filename}")
                    continue
                else:
                    log.debug2("Found a matching %s object '%s' based on serial '%s'" %
                               (self.device_object.name,
                                self.device_object.get_display_name(including_second_key=True),
                                device_serial))

            # parse all components
            self.update_device()
            self.update_power_supply()
            self.update_fan()
            self.update_memory()
            self.update_proc()
            self.update_manager()               # reads manager name to set it via update_network_interface for BMC
            self.update_physical_drive()
            self.update_storage_controller()
            self.update_storage_enclosure()
            self.update_network_adapter()
            self.update_network_interface()

    def reset_inventory_state(self):
        """
        reset attributes to make sure not using data from a previous inventory file
        """

        self.inventory_file_content = None
        self.device_object = None
        self.manager_name = None

        # reset interface types
        self.interface_adapter_type_dict = dict()

    def read_inventory_file_content(self, filename: str) -> bool:
        """
        open an inventory file, parse content to json and compare layout version.

        Parameters
        ----------
        filename: str
            path ot the file to parse

        Returns
        -------
        success: bool
            True if reading file content was successful otherwise False
        """

        if not os.path.isfile(filename):
            log.error(f"Inventory file {filename} seems to be not a regular file")
            return False

        with open(filename) as json_file:
            try:
                file_content = json.load(json_file)
            except json.decoder.JSONDecodeError as e:
                log.error(f"Inventory file {filename} contains invalid json: {e}")
                return False

        log.debug(f"Parsing inventory file {filename}")

        # get inventory_layout_version
        inventory_layout_version = grab(file_content, "meta.inventory_layout_version", fallback=0)

        if version.parse(inventory_layout_version) < version.parse(self.minimum_check_redfish_version):
            log.error(f"Inventory layout version '{inventory_layout_version}' of file {filename} not supported. "
                      f"Minimum layout version {self.minimum_check_redfish_version} required.")

            return False

        self.inventory_file_content = file_content

        return True

    def update_device(self):

        system = grab(self.inventory_file_content, "inventory.system.0")

        if system is None:
            log.error(f"No system data found for '{self.device_object.get_display_name()}' in inventory file.")
            return

        serial = get_string_or_none(grab(system, "serial"))
        name = get_string_or_none(grab(system, "host_name"))
        manufacturer = get_string_or_none(grab(system, "manufacturer"))

        device_data = {
            "device_type": {
                "model": get_string_or_none(grab(system, "model")),
                "manufacturer": {
                    "name": manufacturer
                },
            },
            "custom_fields": {
                "health": get_string_or_none(grab(system, "health_status")),
                "power_state": get_string_or_none(grab(system, "power_state"))
            }
        }

        if serial is not None:
            device_data["serial"] = serial
        if name is not None and self.settings.overwrite_host_name is True:
            device_data["name"] = name
        if "dell" in str(manufacturer).lower():
            chassi = grab(self.inventory_file_content, "inventory.chassi.0")
            if chassi and "sku" in chassi:

                # add ServiceTag
                self.add_update_custom_field({
                    "name": "service_tag",
                    "label": "Service Tag",
                    "object_types": [
                        "dcim.device"
                    ],
                    "type": "text",
                    "description": "Dell Service Tag"
                })

                device_data["custom_fields"]["service_tag"] = chassi.get("sku")
            else:
                log.warning(f"No chassi or sku data found for "
                            f"'{self.device_object.get_display_name()}' in inventory file.")

        self.device_object.update(data=device_data, source=self)

    def update_power_supply(self):

        # get power supplies
        current_ps = list()
        for ps in self.inventory.get_all_items(NBPowerPort):
            if grab(ps, "data.device") == self.device_object:
                current_ps.append(ps)

        current_ps.sort(key=lambda x: grab(x, "data.name") or "")

        ps_index = 1
        ps_items = list()
        for ps in grab(self.inventory_file_content, "inventory.power_supply", fallback=list()):

            if grab(ps, "operation_status") in ["NotPresent", "Absent"]:
                continue

            ps_name = get_string_or_none(grab(ps, "name"))
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
                "device": self.device_object,
                "description": ", ".join(description)
            }

            if capacity_in_watt is not None:
                ps_data["maximum_draw"] = capacity_in_watt
            if firmware is not None:
                ps_data["custom_fields"] = {"firmware": firmware, "health": health_status}

            # add/update power supply data
            ps_object = None
            for current_ps_item in current_ps:
                current_ps_item_name = grab(current_ps_item, "data.name", fallback="")
                if ps_name.lower() in current_ps_item_name.lower():
                    ps_object = current_ps_item
                    break

                if str(ps_index) in current_ps_item_name.split(" "):
                    ps_object = current_ps_item
                    break

            if ps_object is None:
                self.inventory.add_object(NBPowerPort, data=ps_data, source=self)
            else:
                if self.settings.overwrite_power_supply_name is False:
                    del(ps_data["name"])

                data_to_update = self.patch_data(ps_object, ps_data, self.settings.overwrite_power_supply_attributes)
                ps_object.update(data=data_to_update, source=self)
                current_ps.remove(ps_object)

            ps_index += 1

        self.update_all_items(ps_items)

    def update_fan(self):

        items = list()
        for fan in grab(self.inventory_file_content, "inventory.fan", fallback=list()):

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
                "description": description,
                "full_name": f"{fan_name} (ID: {fan_id})",
                "health": health_status,
                "speed": speed
            })

        self.update_all_items(items)

    def update_memory(self):

        items = list()
        memory_size_total = 0
        for memory in grab(self.inventory_file_content, "inventory.memory", fallback=list()):

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

            memory_size_total += size_in_mb

            name_details = list()
            if dimm_type is not None:
                name_details.append(f"{dimm_type}")

            if len(name_details) > 0:
                name += f" ({' '.join(name_details)})"

            description = list()
            if socket is not None:
                description.append(f"Socket: {socket}")
            if channel is not None:
                description.append(f"Channel: {channel}")
            if slot is not None:
                description.append(f"Slot: {slot}")

            if speed is not None:
                speed = f"{speed}MHz"

            items.append({
                "inventory_type": "DIMM",
                "description": description,
                "full_name": name or "None",
                "serial": get_string_or_none(grab(memory, "serial")),
                "manufacturer": get_string_or_none(grab(memory, "manufacturer")),
                "part_number": get_string_or_none(grab(memory, "part_number")),
                "health": health_status,
                "size": f"{size_in_mb / 1024}GB",
                "speed": speed,
            })

        self.update_all_items(items)

        if memory_size_total > 0:
            memory_size_total = memory_size_total / 1024
            memory_size_unit = "GB"
            if memory_size_total >= 1024:
                memory_size_total = memory_size_total / 1024
                memory_size_unit = "TB"

            custom_fields_data = {"custom_fields": {"host_memory": f"{memory_size_total} {memory_size_unit}"}}
            self.device_object.update(data=custom_fields_data, source=self)

    def update_proc(self):

        items = list()
        num_cores = 0
        cpu_name = ""
        for processor in grab(self.inventory_file_content, "inventory.processor", fallback=list()):

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
            cpu_name = model

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
                num_cores += int(cores)
            if threads is not None:
                description.append(f"Threads: {threads}")

            items.append({
                "inventory_type": "CPU",
                "description": description,
                "manufacturer": get_string_or_none(grab(processor, "manufacturer")),
                "full_name": name,
                "serial": get_string_or_none(grab(processor, "serial")),
                "health": health_status,
                "size": size,
                "speed": current_speed
            })

        self.update_all_items(items)

        if num_cores > 0:
            custom_fields_data = {"custom_fields": {"host_cpu_cores": f"{num_cores} {cpu_name}"}}
            self.device_object.update(data=custom_fields_data, source=self)

    def update_physical_drive(self):

        items = list()
        for pd in grab(self.inventory_file_content, "inventory.physical_drive", fallback=list()):

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
                "description": description,
                "manufacturer": get_string_or_none(grab(pd, "manufacturer")),
                "full_name": name or "None",
                "serial": serial,
                "part_number": get_string_or_none(grab(pd, "part_number")),
                "firmware": firmware,
                "health": health_status,
                "size": size,
                "speed": speed
            })

        self.update_all_items(items)

    def update_storage_controller(self):

        items = list()
        for sc in grab(self.inventory_file_content, "inventory.storage_controller", fallback=list()):

            if grab(sc, "operation_status") in ["NotPresent", "Absent"]:
                continue

            name = get_string_or_none(grab(sc, "name"))
            model = get_string_or_none(grab(sc, "model"))
            location = get_string_or_none(grab(sc, "location"))
            logical_drive_ids = grab(sc, "logical_drive_ids", fallback=list())
            physical_drive_ids = grab(sc, "physical_drive_ids", fallback=list())
            cache_size_in_mb = grab(sc, "cache_size_in_mb")

            if name.lower().startswith("hp") and model is not None:
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
                "description": description,
                "manufacturer": get_string_or_none(grab(sc, "manufacturer")),
                "full_name": name or "None",
                "serial": get_string_or_none(grab(sc, "serial")),
                "firmware": get_string_or_none(grab(sc, "firmware")),
                "health": get_string_or_none(grab(sc, "health_status")),
                "size": size
            })

        self.update_all_items(items)

    def update_storage_enclosure(self):

        items = list()
        for se in grab(self.inventory_file_content, "inventory.storage_enclosure", fallback=list()):

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
                "manufacturer": get_string_or_none(grab(se, "manufacturer")),
                "full_name": name or "None",
                "serial": get_string_or_none(grab(se, "serial")),
                "firmware": get_string_or_none(grab(se, "firmware")),
                "health": get_string_or_none(grab(se, "health_status")),
                "size": size
            })

        self.update_all_items(items)

    def update_network_adapter(self):

        items = list()
        for adapter in grab(self.inventory_file_content, "inventory.network_adapter", fallback=list()):

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
            if adapter_name is not None:
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

            name = adapter_name or "None"
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

    def update_network_interface(self):

        port_data_dict = dict()
        nic_ips = dict()
        discovered_int_list = list()

        for nic_port in grab(self.inventory_file_content, "inventory.network_port", fallback=list()):

            if grab(nic_port, "operation_status") in ["Disabled"]:
                continue

            port_name = get_string_or_none(grab(nic_port, "name"))
            port_id = get_string_or_none(grab(nic_port, "id"))
            interface_addresses = grab(nic_port, "addresses", fallback=list())
            link_status = get_string_or_none(grab(nic_port, "link_status"))
            manager_ids = grab(nic_port, "manager_ids", fallback=list())
            hostname = get_string_or_none(grab(nic_port, "hostname"))
            health_status = get_string_or_none(grab(nic_port, "health_status"))
            adapter_id = get_string_or_none(grab(nic_port, "adapter_id"))
            link_speed = grab(nic_port, "capable_speed") or grab(nic_port, "current_speed") or 0
            link_duplex = grab(nic_port, "full_duplex")

            mac_address = None
            wwn = None
            if isinstance(interface_addresses, list):
                for interface_address in interface_addresses:
                    interface_address = normalize_mac_address(interface_address)

                    # get 1. mac address
                    if mac_address is None and len(interface_address.split(":")) == 6:
                        mac_address = interface_address

                    if wwn is None and len(interface_address.split(":")) == 8:
                        wwn = interface_address

            if mac_address in discovered_int_list or wwn in discovered_int_list:
                continue

            if mac_address is not None:
                discovered_int_list.append(mac_address)

            if wwn is not None:
                discovered_int_list.append(wwn)

            if port_name is not None:
                port_name += f" ({port_id})"
            else:
                port_name = port_id

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

            # set BMC interface to manager name
            if mgmt_only is True and self.manager_name is not None:
                port_name = f"{self.manager_name} ({port_id})"

            port_data_dict[port_name] = {
                "inventory_type": "NIC Port",
                "name": port_name,
                "mac_address": mac_address,
                "wwn": wwn,
                "enabled": enabled,
                "type": link_type.get_this_netbox_type(),
                "mgmt_only": mgmt_only,
                "health": health_status
            }

            if len(description) > 0:
                port_data_dict[port_name]["description"] = ", ".join(description)
            if mgmt_only is True:
                port_data_dict[port_name]["mode"] = "access"

            # add link speed and duplex attributes
            if version.parse(self.inventory.netbox_api_version) >= version.parse("3.2.0"):
                if link_speed > 0:
                    port_data_dict[port_name]["speed"] = link_speed * 1000
                if link_duplex is not None:
                    port_data_dict[port_name]["duplex"] = "full" if link_duplex is True else "half"

            # collect ip addresses
            nic_ips[port_name] = list()
            for ipv4_address in grab(nic_port, "ipv4_addresses", fallback=list()):
                if self.settings.permitted_subnets.permitted(ipv4_address, interface_name=port_name) is False:
                    continue

                nic_ips[port_name].append(ipv4_address)

            for ipv6_address in grab(nic_port, "ipv6_addresses", fallback=list()):
                if self.settings.permitted_subnets.permitted(ipv6_address, interface_name=port_name) is False:
                    continue

                nic_ips[port_name].append(ipv6_address)

        data = self.map_object_interfaces_to_current_interfaces(self.device_object, port_data_dict, True)

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

            # del empty wwn attribute
            if port_data.get("wwn") is None:
                del (port_data["wwn"])

            # create or update interface with data
            if nic_object is not None:
                if self.settings.overwrite_interface_name is False and port_data.get("name") is not None:
                    del(port_data["name"])

                this_link_type = port_data.get("type")
                mgmt_only = port_data.get("mgmt_only")
                mac_address = port_data.get("mac_address")
                data_to_update = self.patch_data(nic_object, port_data, self.settings.overwrite_interface_attributes)

                # always overwrite nic type if discovered
                if port_data.get("type") != "other":
                    data_to_update["type"] = this_link_type

                data_to_update["mgmt_only"] = mgmt_only

                if mac_address is not None:
                    data_to_update["mac_address"] = mac_address

                port_data = data_to_update

            self.add_update_interface(nic_object, self.device_object, port_data, nic_ips.get(port_name, list()))

    def update_manager(self):

        items = list()
        for manager in grab(self.inventory_file_content, "inventory.manager", fallback=list()):

            name = get_string_or_none(grab(manager, "name"))
            model = get_string_or_none(grab(manager, "model"))
            licenses = grab(manager, "licenses", fallback=list())

            if name == "Manager" and model is not None:
                name = model

            if model is not None and model not in name:
                name += f" {model}"

            if self.manager_name is None:
                self.manager_name = name

            description = None
            if len(licenses) > 0:
                description = f"Licenses: %s" % (", ".join(licenses))

            items.append({
                "inventory_type": "Manager",
                "description": description,
                "full_name": name,
                "manufacturer": grab(self.device_object, "data.device_type.data.manufacturer.data.name"),
                "firmware": get_string_or_none(grab(manager, "firmware")),
                "health": get_string_or_none(grab(manager, "health_status"))
            })

        self.update_all_items(items)

    def update_all_items(self, items):
        """
        Updates all inventory items of a certain type. Both (current and supplied list of items) will
        be sorted by name and matched 1:1.

        Parameters
        ----------
        items: list
            a list of items to update

        Returns
        -------
        None
        """

        if not isinstance(items, list):
            raise ValueError(f"Value for 'items' must be type 'list' got: {items}")

        if len(items) == 0:
            return

        # get device
        inventory_type = grab(items, "0.inventory_type")

        if inventory_type is None:
            log.error(f"Unable to find inventory type for inventory item {items[0]}")
            return

        # get current inventory items for this device and type
        current_inventory_items = dict()
        for item in self.inventory.get_all_items(NBInventoryItem):
            if grab(item, "data.device") == self.device_object and \
                    grab(item, "data.custom_fields.inventory_type") == inventory_type:

                current_inventory_items[grab(item, "data.name")] = item

        # sort items by display name
        current_inventory_items = dict(sorted(current_inventory_items.items()))

        # dict
        #   key: NB inventory object
        #   value: parsed data matching the exact name
        matched_inventory = dict()
        unmatched_inventory_items = list()

        # try to match names to existing inventory
        for item in items:

            current_item = current_inventory_items.get(item.get("full_name"))
            if current_item is not None:
                # log.debug2(f"Found 1:1 name match for inventory item '{item.get('full_name')}'")
                matched_inventory[current_item] = item
            else:
                # log.debug2(f"No current NetBox inventory item found for '{item.get('full_name')}'")
                unmatched_inventory_items.append(item)

        # sort unmatched items by full_name
        unmatched_inventory_items.sort(key=lambda x: x.get("full_name") or "")

        # iterate over current NetBox inventory items
        # if name did not match try to assign unmatched items in alphabetical order
        for nb_inventory_item in current_inventory_items.values():

            if nb_inventory_item not in matched_inventory.keys():
                if len(unmatched_inventory_items) > 0:
                    matched_inventory[nb_inventory_item] = unmatched_inventory_items.pop(0)

                # set item health to absent if item can't be found in redfish inventory anymore
                elif grab(nb_inventory_item, "data.custom_fields.health") != "Absent":
                    nb_inventory_item.update(data={"custom_fields": {"health": "Absent"}}, source=self)

        # update items with matching NetBox inventory item
        for inventory_object, inventory_data in matched_inventory.items():
            self.update_item(inventory_data, inventory_object)

        # create new inventory item in NetBox
        for unmatched_inventory_item in unmatched_inventory_items:
            self.update_item(unmatched_inventory_item)

    def update_item(self, item_data: dict, inventory_object: NBInventoryItem = None):
        """
        Updates a single inventory item with the supplied data.
        If no item is provided a new one will be created.

        Parameters
        ----------
        item_data: dict
            a dict with data for item to update
        inventory_object: NBInventoryItem, None
            the NetBox inventory item to update.

        Returns
        -------
        None
        """

        full_name = item_data.get("full_name")
        label = item_data.get("label")
        manufacturer = item_data.get("manufacturer")
        part_number = item_data.get("part_number")
        serial = item_data.get("serial")
        description = item_data.get("description")

        # compile inventory item data
        inventory_data = {
            "device": self.device_object,
            "discovered": True,
            "custom_fields": {
                "firmware": item_data.get("firmware"),
                "health": item_data.get("health"),
                "inventory_type": item_data.get("inventory_type"),
                "inventory_size": item_data.get("size"),
                "inventory_speed": item_data.get("speed")
            }
        }

        if isinstance(description, list):
            description = ", ".join(description)

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

        if inventory_object is None:
            self.inventory.add_object(NBInventoryItem, data=inventory_data, source=self)
        else:
            inventory_object.update(data=inventory_data, source=self)

        return

    def add_necessary_base_objects(self):
        """
        Adds/updates source tag and all custom fields necessary for this source.
        """

        # add source identification tag
        self.inventory.add_update_object(NBTag, data={
            "name": self.source_tag,
            "description": f"Marks objects synced from check_redfish inventory '{self.name}' to this NetBox Instance."
        })

        self.add_update_custom_field({
            "name": "host_cpu_cores",
            "label": "Physical CPU Cores",
            "object_types": [
                "dcim.device"
            ],
            "type": "text",
            "description": f"Reported Host CPU cores"
        })

        self.add_update_custom_field({
            "name": "host_memory",
            "label": "Memory",
            "object_types": [
                "dcim.device"
            ],
            "type": "text",
            "description": f"Reported size of Memory"
        })

        self.add_update_custom_field({
            "name": "power_state",
            "label": "Power State",
            "object_types": [
                "dcim.device"
            ],
            "type": "text",
            "description": "Device power state"
        })

        # add Firmware
        self.add_update_custom_field({
            "name": "firmware",
            "label": "Firmware",
            "object_types": [
                "dcim.inventoryitem",
                "dcim.powerport"
            ],
            "type": "text",
            "description": "Item firmware version"
        })

        # add inventory item type
        self.add_update_custom_field({
            "name": "inventory_type",
            "label": "Type",
            "object_types": ["dcim.inventoryitem"],
            "type": "text",
            "description": "Describes the type of inventory item"
        })

        # add inventory item size
        self.add_update_custom_field({
            "name": "inventory_size",
            "label": "Size",
            "object_types": ["dcim.inventoryitem"],
            "type": "text",
            "description": "Describes the size of the inventory item if applicable"
        })

        # add inventory item speed
        self.add_update_custom_field({
            "name": "inventory_speed",
            "label": "Speed",
            "object_types": ["dcim.inventoryitem"],
            "type": "text",
            "description": "Describes the speed of the inventory item if applicable"
        })

        # add health status
        self.add_update_custom_field({
            "name": "health",
            "label": "Health",
            "object_types": [
                "dcim.inventoryitem",
                "dcim.powerport",
                "dcim.device"
            ],
            "type": "text",
            "description": "Shows the currently discovered health status"
        })

# EOF
