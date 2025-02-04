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

    def _set_string_attribute(self, attribute: str, value):
        if value is None:
            return

        if not isinstance(attribute, str):
            raise ValueError("argument 'attribute' needs to be a string")

        if not hasattr(self, attribute):
            raise ValueError(f"class '{type(self)}' has no attribute '{attribute}'")

        if isinstance(name, str) and len(name) > 0:
            setattr(self, attribute, value.strip())


class DTOServer(DTOBase):

    def __init__(self):
        self.model_type = None ##
        self.name = None ##
        self.serial = None ##
        self.interfaces = list()
        self.tags = list() #
        self.primary_ipv4 = None
        self.primary_ipv6 = None
        self.platform = None ##
        self.comments = None ##
        self.tenant = None ##
        self.custom_fields = list()
        self.site = None ##
        self.cluster = None ##
        self.status = None ##
        self.memory = None
        self.cpus = None
        self.parent_device = None ##
        self.asset_tag = None ##
        self.model = None  ##
        self.manufacturer = None ##

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

    def set_parent_device(self, value):
        if not isinstance(value, NBDevice):
            raise ValueError("value needs to be a NBDevice object")
        self.parent_device = value

    def add_tag(self, value):
        if value is not None:
            self.tags.append(value)

    def add_interface(self, value):
        if not isinstance(value, DTOInterface):
            raise ValueError("value needs to be a DTOInterface")

        self.interfaces.append(value)

class DTOInterface(DTOBase):

    def __init__(self):

        self.type = None
        self.mac_addresses = list()
        self.ip_addresses = list()
        self.tags = list()
        self.tenant = None
        self.description = None
        self.untagged_vlan = None
        self.tagged_vlans = list()
        self.mtu = None
        self.mode = None
        self.mark_connected = None


class DTOVlan(DTOBase):

    def __init__(self):
        self.name = None
        self.id = 0


class DTODisk(DTOBase):
    def __init__(self):
        self.name = None
        self.size = 0
        self.description = None
