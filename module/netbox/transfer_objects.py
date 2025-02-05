# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2023 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.netbox import *

class DTOBase:

    def __repr__(self):
        return str(self.__dict__)

    def _set_string_attribute(self, attribute: str, value):
        if value is None:
            return

        if not isinstance(attribute, str):
            raise ValueError("argument 'attribute' needs to be a string")

        if not hasattr(self, attribute):
            raise ValueError(f"class '{type(self)}' has no attribute '{attribute}'")

        if isinstance(value, str) and len(value) > 0:
            setattr(self, attribute, value.strip())


class DTOServer(DTOBase):

    def __init__(self):
        self.model_type = None ##
        self.name = None ##
        self.serial = None ##
        self.interfaces = list() #
        self.tags = list() #
        self.primary_ipv4 = None #
        self.primary_ipv6 = None ##
        self.platform = None ##
        self.comments = None ##
        self.tenant = None ##
        self.custom_fields = list() ##
        self.site = None ##
        self.cluster = None ##
        self.status = None ##
        self.memory = None ##
        self.cpus = None ##
        self.parent_device = None ##
        self.asset_tag = None ##
        self.model = None  ##
        self.manufacturer = None ##
        self.disks = list() ##

    def set_name(self, value: str):
        self._set_string_attribute("name", value)

    def set_serial(self, value: str):
        self._set_string_attribute("serial", value)

    def set_platform(self, value: str):
        if isinstance(value, NBPlatform):
            self.manufacturer = value
        else:
            self._set_string_attribute("platform", value)

    def set_comments(self, value: str):
        self._set_string_attribute("comments", value)

    def set_status(self, value: str):
        self._set_string_attribute("status", value)

    def set_asset_tag(self, value: str):
        self._set_string_attribute("asset_tag", value)

    def set_model(self, value: str):
        self._set_string_attribute("model", value)

    def set_manufacturer(self, value):
        if isinstance(value, NBManufacturer):
            self.manufacturer = value
        else:
            self._set_string_attribute("manufacturer", value)

    def set_type(self, value):
        if value not in [NBDevice, NBVM]:
            raise ValueError("type can only be NBDevice or NBVM")
        self.model_type = value

    def set_tenant(self, value):
        if isinstance(value, NBTenant):
            self.tenant = value
        else:
            self._set_string_attribute("tenant", value)

    def set_site(self, value):
        if isinstance(value, NBSite):
            self.site = value
        else:
            self._set_string_attribute("site", value)

    def set_cluster(self, value):
        if isinstance(value, NBCluster):
            self.cluster = value
        else:
            self._set_string_attribute("cluster", value)

    def set_parent_device(self, value: NBDevice):
        if value is None:
            return

        if not isinstance(value, NBDevice):
            raise ValueError("value needs to be a NBDevice object")
        self.parent_device = value

    def add_tag(self, value):
        if value is not None:
            if isinstance(value, list):
                self.tags.extend(value)
            else:
                self.tags.append(value)

    def add_network_interface(self, value):
        if not isinstance(value, DTOInterface):
            raise ValueError("value needs to be an instance of DTOInterface")

        self.interfaces.append(value)

    def add_disk(self, value):
        if not isinstance(value, DTODisk):
            raise ValueError("value needs to be an instance of DTODisk")

        self.disks.append(value)

    def add_custom_field(self, value: NBCustomField):
        if isinstance(value, list):
            for item in value:
                self.add_custom_field(item)
            return

        if not isinstance(value, dict):
            raise ValueError("value needs to be an instance of NBCustomField")

        self.custom_fields.append(value)

    def set_primary_ipv4(self, value):
        self._set_string_attribute("primary_ipv4", value)

    def set_primary_ipv6(self, value):
        self._set_string_attribute("primary_ipv6", value)

    def set_memory(self, value: int):
        """
        define memory in MB
        """
        if not isinstance(value, int):
            raise ValueError("memory needs to be an int")

        self.memory = value

    def set_cpus(self, value: int):
        """
        define number of CPUs
        """
        if not isinstance(value, int):
            raise ValueError("cpus needs to be an int")

        self.cpus = value

    def get_ip_addresses(self) -> list[str]:
        result = list()
        for int_data in self.interfaces:
            result.extend(int_data.ip_addresses)

        return result

class DTOInterface(DTOBase):

    def __init__(self):

        self.int_type = None #
        self.netbox_type = None #
        self.name = None #
        self.mac_addresses = list() #
        self.ip_addresses = list() #
        self.tags = list() #
        self.tenant = None #
        self.description = None #
        self.untagged_vlan = None #
        self.tagged_vlans = list() #
        self.mtu = None #
        self.mode = None #
        self.mark_connected = None #
        self.speed = 0
        self.duplex = None

    def set_type(self, value):
        if value not in [NBInterface, NBVMInterface]:
            raise ValueError("type can only be NBInterface or NBVMInterface")
        self.int_type = value

    def set_netbox_type(self, value: str):
        self._set_string_attribute("netbox_type", value)

    def set_name(self, value: str):
        self._set_string_attribute("name", value)

    def add_tag(self, value):
        if value is not None:
            if isinstance(value, list):
                self.tags.extend(value)
            else:
                self.tags.append(value)

    def set_tenant(self, value):
        if isinstance(value, NBTenant):
            self.tenant = value
        else:
            self._set_string_attribute("tenant", value)

    def set_description(self, value: str):
        self._set_string_attribute("description", value)

    def set_untagged_vlan(self, value):
        if not isinstance(value, DTOVlan):
            raise ValueError("untagged vlan needs to be an instance of DTOVlan")

        self.untagged_vlan = value

    def add_tagged_vlan(self, value):
        if isinstance(value, list):
            for item in value:
                self.add_tagged_vlan(item)
            return

        if not isinstance(value, DTOVlan):
            raise ValueError("tagged vlan needs to be an instance of DTOVlan")

        self.tagged_vlans.append(value)

    def set_mtu(self, value: int):
        if value is None:
            return

        if not isinstance(value, int):
            raise ValueError("mtu needs to be an int")

        self.mtu = value

    def set_mode(self, value: str):
        if value is None:
            return

        if not isinstance(value, str):
            raise ValueError("interface mode needs to be a str")

        if value.strip() not in ["access", "tagged", "tagged-all"]:
            raise ValueError("interface mode needs to be 'access', 'tagged' or 'tagged-all'")

        self.mode = value.strip()

    def set_connected(self, value: bool):
        if not isinstance(value, bool):
            raise ValueError("value for connected needs to be a bool")

        self.mark_connected = value

    def add_mac_address(self, value):
        if value is None:
            return

        if not isinstance(value, str):
            raise ValueError("mac address needs to be a string")

        self.mac_addresses.append(value)

    def add_ip_address(self, value):
        if value is None:
            return

        if not isinstance(value, str):
            raise ValueError("ip address needs to be a string")

        self.ip_addresses.append(value)

    def set_speed(self, value):
        if value is None:
            return

        if not isinstance(value, int):
            raise ValueError("interface speed must be of type int")

        self.speed = value

    def set_duplex(self, value):
        if value is None:
            return

        if not isinstance(value, str):
            raise ValueError("interface duplex needs to be a str")

        if value.strip() not in ["half", "full"]:
            raise ValueError("interface mode needs to be 'half' or 'full'")

        self.duplex = value.strip()

class DTOVlan(DTOBase):

    def __init__(self):
        self.name = None
        self.id = 0

    def set_name(self, value):
        self._set_string_attribute("name", value)

    def set_id(self, value):
        if not isinstance(value, int):
            raise ValueError("VLAN id must be of type int")
        self.id = value


class DTODisk(DTOBase):
    def __init__(self):
        self.name = None
        self.size = 0
        self.description = None

    def set_name(self, value):
        self._set_string_attribute("name", value)

    def set_description(self, value):
        self._set_string_attribute("description", value)

    def set_size(self, value):
        """
        set size of disk in bytes
        """
        if not isinstance(value, int):
            raise ValueError("disk size must be of type int")
        self.size = value
