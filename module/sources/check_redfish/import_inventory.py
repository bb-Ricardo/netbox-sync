# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2021 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

# from ipaddress import ip_address, ip_network, ip_interface, IPv4Address, IPv6Address

import os
import glob

from packaging import version

from module.common.logging import get_logger, DEBUG3
from module.common.misc import grab, dump, get_string_or_none, plural
from module.common.support import normalize_mac_address, ip_valid_to_add_to_netbox, map_object_interfaces_to_current_interfaces
from module.netbox.object_classes import *
from module.netbox.inventory import interface_speed_type_mapping

log = get_logger()


class CheckRedfish:

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
        NBVM,
        NBVMInterface,
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
        "collect_hardware_asset_tag": True
    }

    init_successful = False
    inventory = None
    name = None
    source_tag = None
    source_type = "check_redfish"
    enabled = False
    inventory_file_path = None

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

        self.add_firmware_custom_field()

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

                device_data = {
                    "name": get_string_or_none(grab(system, "host_name")),
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

                device_object.update(data=device_data, source=self)

                self.update_power_supply(device_object, file_content)
                self.update_fan(device_object, file_content)
                self.update_memory(device_object, file_content)
                self.update_proc(device_object, file_content)
                self.update_physical_drive(device_object, file_content)
                self.update_storage_controller(device_object, file_content)
                self.update_network_adapter(device_object, file_content)
                self.update_network_interface(device_object, file_content)

    def update_power_supply(self, device_object, inventory_data):

        # get power supplies
        current_ps = list()
        for ps in self.inventory.get_all_items(NBPowerPort):
            if grab(ps, "data.device") == device_object:
                current_ps.append(ps)

        current_ps.sort(key=lambda x: grab(x, "data.name"))

        ps_index = 0
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
            if health_status is not None:
                description.append(f"Health: {health_status}")
            if model is not None:
                description.append(f"Model: {model}")

            # compile inventory item data
            self._update_item({
                "device": device_object,
                "description": description,
                "full_name": name,
                "name_start": ps_name,
                "serial": get_string_or_none(grab(ps, "serial")),
                "manufacturer": get_string_or_none(grab(ps, "vendor")),
                "part_number": get_string_or_none(grab(ps, "part_number")),
                "firmware": firmware
            })

            connected = False
            if f"{health_status}" == "OK" or grab(ps, "last_power_output", fallback=0) > 0:
                connected = True

            # compile power supply data
            ps_data = {
                "device": device_object,
                "description": ", ".join(description),
                "mark_connected": connected,
                "name": name
            }

            if capacity_in_watt is not None:
                ps_data["maximum_draw"] = capacity_in_watt
            if firmware is not None:
                ps_data["custom_fields"] = {"firmware": firmware}

            # add/update power supply data
            ps_object = grab(current_ps, f"{ps_index}")
            if ps_object is None:
                self.inventory.add_object(NBPowerPort, data=ps_data, source=self)
            else:
                ps_object.update(data=ps_data, source=self)

            ps_index += 1

    def update_fan(self, device_object, inventory_data):

        for fan in grab(inventory_data, "inventory.fan", fallback=list()):

            if grab(fan, "operation_status") in ["NotPresent", "Absent"]:
                continue

            fan_name = get_string_or_none(grab(fan, "name"))
            health_status = get_string_or_none(grab(fan, "health_status"))
            physical_context = get_string_or_none(grab(fan, "physical_context"))

            description = list()
            description.append(f"Health: {health_status}")
            if physical_context is not None:
                description.append(f"Context: {physical_context}")

            self._update_item({
                "device": device_object,
                "description": description,
                "name_start": fan_name,
                "full_name": fan_name
            })

    def update_memory(self, device_object, inventory_data):

        for memory in grab(inventory_data, "inventory.memory", fallback=list()):

            if grab(memory, "operation_status") in ["NotPresent", "Absent"]:
                continue

            memory_name = get_string_or_none(grab(memory, "name"))
            health_status = get_string_or_none(grab(memory, "health_status"))
            size_in_mb = grab(memory, "size_in_mb", fallback=0)
            channel = get_string_or_none(grab(memory, "channel"))
            slot = get_string_or_none(grab(memory, "slot"))
            socket = get_string_or_none(grab(memory, "socket"))
            speed = get_string_or_none(grab(memory, "speed"))
            dimm_type = get_string_or_none(grab(memory, "type"))

            if size_in_mb == 0 or (health_status is None and grab(memory, "operation_status") != "GoodInUse"):
                continue

            size_in_gb = size_in_mb / 1024

            name = f"{memory_name}"
            name_details = list()
            name_details.append(f"{size_in_gb}GB")
            if dimm_type is not None:
                name_details.append(f"{dimm_type}")
            if speed is not None:
                name_details.append(f"{speed}MHz")

            name += f" ({' '.join(name_details)})"

            description = list()
            if socket is not None:
                description.append(f"Socket: {socket}")
            if channel is not None:
                description.append(f"Channel: {channel}")
            if slot is not None:
                description.append(f"Slot: {slot}")

            self._update_item({
                "device": device_object,
                "description": description,
                "name_start": memory_name,
                "full_name": name,
                "serial": get_string_or_none(grab(memory, "serial")),
                "manufacturer": get_string_or_none(grab(memory, "manufacturer")),
                "part_number": get_string_or_none(grab(memory, "part_number"))
            })

    def update_proc(self, device_object, inventory_data):

        for processor in grab(inventory_data, "inventory.processor", fallback=list()):

            if grab(processor, "operation_status") in ["NotPresent", "Absent"]:
                continue

            instruction_set = get_string_or_none(grab(processor, "instruction_set"))
            current_speed = get_string_or_none(grab(processor, "current_speed"))
            model = get_string_or_none(grab(processor, "model"))
            cores = get_string_or_none(grab(processor, "cores"))
            threads = get_string_or_none(grab(processor, "threads"))
            socket = get_string_or_none(grab(processor, "socket"))

            name = f"{socket} ({model})"

            description = list()
            if instruction_set is not None:
                description.append(f"{instruction_set}")
            if current_speed is not None:
                description.append(f"{current_speed}MHz")
            if cores is not None:
                description.append(f"Cores: {cores}")
            if threads is not None:
                description.append(f"Threads: {threads}")

            self._update_item({
                "device": device_object,
                "description": description,
                "manufacturer": get_string_or_none(grab(processor, "manufacturer")),
                "full_name": name,
                "name_start": socket,
                "serial": get_string_or_none(grab(processor, "serial"))
            })

    def update_physical_drive(self, device_object, inventory_data):

        pd_serials = list()
        for pd in grab(inventory_data, "inventory.physical_drive", fallback=list()):

            if grab(pd, "operation_status") in ["NotPresent", "Absent"]:
                continue

            pd_name = get_string_or_none(grab(pd, "name"))
            firmware = get_string_or_none(grab(pd, "firmware"))
            interface_type = get_string_or_none(grab(pd, "interface_type"))
            health_status = get_string_or_none(grab(pd, "health_status"))
            size_in_byte = grab(pd, "size_in_byte", fallback=0)
            model = get_string_or_none(grab(pd, "model"))
            speed_in_rpm = get_string_or_none(grab(pd, "speed_in_rpm"))
            location = get_string_or_none(grab(pd, "location"))
            bay = get_string_or_none(grab(pd, "bay"))
            pd_type = get_string_or_none(grab(pd, "type"))
            serial = get_string_or_none(grab(pd, "serial"))

            if serial is not None and serial in pd_serials:
                continue

            pd_serials.append(serial)

            if pd_name.lower().startswith("hp"):
                pd_name = "Physical Drive"

            if location is not None and location not in pd_name:
                pd_name += f" {location}"
            elif bay is not None and bay not in pd_name:
                pd_name += f" {bay}"

            name = pd_name

            name_details = list()
            if pd_type is not None:
                name_details.append(pd_type)
            if model is not None and model not in name:
                name_details.append(model)
            if size_in_byte is not None and size_in_byte != "0":
                name_details.append("%dGB" % (size_in_byte / 1000 ** 3))

            name += f" ({' '.join(name_details)})"

            description = list()
            if health_status is not None:
                description.append(f"Health: {health_status}")
            if interface_type is not None:
                description.append(f"Interface: {interface_type}")
            if speed_in_rpm is not None:
                description.append(f"Speed: {speed_in_rpm}RPM")
            if firmware is not None:
                description.append(f"Firmware: {firmware}")

            self._update_item({
                "device": device_object,
                "description": description,
                "manufacturer": get_string_or_none(grab(pd, "manufacturer")),
                "full_name": name,
                "name_start": pd_name,
                "serial": serial,
                "part_number": get_string_or_none(grab(pd, "part_number")),
                "firmware": firmware
            })

    def update_storage_controller(self, device_object, inventory_data):

        for sc in grab(inventory_data, "inventory.storage_controller", fallback=list()):

            if grab(sc, "operation_status") in ["NotPresent", "Absent"]:
                continue

            sc_name = get_string_or_none(grab(sc, "name"))
            model = get_string_or_none(grab(sc, "model"))
            firmware = get_string_or_none(grab(sc, "firmware"))
            health_status = get_string_or_none(grab(sc, "health_status"))
            serial = get_string_or_none(grab(sc, "serial"))
            location = get_string_or_none(grab(sc, "location"))

            if sc_name.lower().startswith("hp"):
                sc_name = model

            name = sc_name

            if location is not None and location not in name:
                name += f" {location}"

            description = list()
            if health_status is not None:
                description.append(f"Health: {health_status}")
            if firmware is not None:
                description.append(f"Firmware: {firmware}")

            self._update_item({
                "device": device_object,
                "description": description,
                "manufacturer": get_string_or_none(grab(sc, "manufacturer")),
                "full_name": name,
                "name_start": sc_name,
                "serial": serial,
                "firmware": firmware
            })

    def update_network_adapter(self, device_object, inventory_data):

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

            if adapter_id != adapter_name:
                adapter_name = f"{adapter_name} ({adapter_id})"

            name = adapter_name
            if model is not None:
                name += f" - {model}"
            if num_ports is not None:
                name += f" ({num_ports} ports)"

            description = list()
            if health_status is not None:
                description.append(f"Health: {health_status}")
            if firmware is not None:
                description.append(f"Firmware: {firmware}")

            self._update_item({
                "device": device_object,
                "description": description,
                "manufacturer": get_string_or_none(grab(adapter, "manufacturer")),
                "full_name": name,
                "name_start": adapter_name,
                "serial": serial,
                "part_number": get_string_or_none(grab(adapter, "part_number")),
                "firmware": firmware
            })

    def update_network_interface(self, device_object, inventory_data):

        port_data_dict = dict()

        for nic_port in grab(inventory_data, "inventory.network_port", fallback=list()):

            if grab(nic_port, "operation_status") in ["Disabled"]:
                continue

            port_name = get_string_or_none(grab(nic_port, "name"))
            port_id = get_string_or_none(grab(nic_port, "id"))
            mac_address = get_string_or_none(grab(nic_port, "addresses.0"))  # get 1st mac address
#            ipv4_addresses = grab(nic_port, "ipv4_addresses")
#            ipv6_addresses = grab(nic_port, "ipv6_addresses")
            link_status = get_string_or_none(grab(nic_port, "link_status"))
            manager_ids = grab(nic_port, "manager_ids", fallback=list())
            hostname = get_string_or_none(grab(nic_port, "hostname"))

            if port_name is None:
                port_name = port_id

            # get port speed
            link_speed = grab(nic_port, "capable_speed") or grab(nic_port, "current_speed")

            description = list()
            if hostname is not None:
                description.append(f"Hostname: {hostname}")
            if link_speed is not None and link_speed > 0 and interface_speed_type_mapping.get(link_speed) is None:
                if link_speed >= 1000:
                    description.append("Speed: %iGb/s" % int(link_speed / 1000))
                else:
                    description.append(f"Speed: {link_speed}Mb/s")

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
                "name": port_name,
                "device": device_object,
                "mac_address": normalize_mac_address(mac_address),
                "enabled": enabled,
                "description": ", ".join(description),
                "type": interface_speed_type_mapping.get(link_speed, "other"),
                "mgmt_only": mgmt_only
            }

        data = map_object_interfaces_to_current_interfaces(self.inventory, device_object, port_data_dict)

        for port_name, port_data in port_data_dict.items():

            # get current object for this interface if it exists
            nic_object = data.get(port_name)

            # create or update interface with data
            if nic_object is None:
                self.inventory.add_object(NBInterface, data=port_data, source=self)
            else:
                tags = nic_object.get_tags()
                if self.source_tag in tags:
                    tags.remove(self.source_tag)

                # no other source for this interface
                if len([x for x in tags if x.startswith("Source")]) == 0:
                    nic_object.update(data=port_data, source=self)
                else:
                    # only append data
                    data_to_update = dict()
                    for key, value in port_data.items():
                        if str(grab(nic_object, f"data.{key}")) == "":
                            data_to_update[key] = value

                    nic_object.update(data=data_to_update, source=self)

    def _update_item(self, item_data: dict):

        device = item_data.get("device")
        name_start = item_data.get("name_start")
        full_name = item_data.get("full_name")
        label = item_data.get("label")
        manufacturer = item_data.get("manufacturer")
        part_number = item_data.get("part_number")
        serial = item_data.get("serial")
        description = item_data.get("description")
        firmware = item_data.get("firmware")

        if device is None or name_start is None:
            return False

        if isinstance(description, list):
            description = ", ".join(description)

        # compile inventory item data
        inventory_data = {
            "device": device,
            "discovered": True,
            "name": full_name,
        }

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
        if firmware is not None:
            inventory_data["custom_fields"] = {"firmware": firmware}

        # find inventory item
        inventory_object = None
        if serial is not None:
            inventory_object = self.inventory.get_by_data(NBInventoryItem,
                                                          data={
                                                              "device": device,
                                                              "serial": serial
                                                          })

        # by name
        if inventory_object is None:
            inventory_object = self.inventory.get_by_data(NBInventoryItem,
                                                          data={
                                                              "device": device,
                                                              "name": full_name
                                                          })

        # starts with the same name
        if inventory_object is None:
            for inventory_item in self.inventory.get_all_items(NBInventoryItem):
                if grab(inventory_item, "data.device") == device and \
                        grab(inventory_item, "data.name").startswith(f"{name_start} "):

                    inventory_object = inventory_item
                    break

        if inventory_object is None:
            self.inventory.add_object(NBInventoryItem, data=inventory_data, source=self)
        else:
            inventory_object.update(data=inventory_data, source=self)

        return True

    def add_firmware_custom_field(self):

        content_types = [
            "dcim.inventoryitem",
            "dcim.powerport"
        ]
        data = {
            "name": "firmware",
            "label": "Firmware",
            "content_types": content_types,
            "type": "text",
            "description": "Item Firmware"
        }

        custom_field = self.inventory.get_by_data(NBCustomField, data={"name": data.get("name")})

        if custom_field is None:
            self.inventory.add_object(NBCustomField, data=data, source=self)
        else:
            custom_field.update(data={"content_types": content_types}, source=self)

# EOF
