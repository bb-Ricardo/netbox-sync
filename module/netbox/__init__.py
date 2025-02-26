# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.netbox.object_classes import (
    NetBoxInterfaceType,
    NetBoxObject,
    NBObjectList,
    NBCustomField,
    NBTag,
    NBTagList,
    NBTenant,
    NBSite,
    NBSiteGroup,
    NBVRF,
    NBVLAN,
    NBVLANList,
    NBVLANGroup,
    NBPrefix,
    NBManufacturer,
    NBDeviceType,
    NBPlatform,
    NBClusterType,
    NBClusterGroup,
    NBDeviceRole,
    NBCluster,
    NBDevice,
    NBVM,
    NBVMInterface,
    NBVirtualDisk,
    NBInterface,
    NBIPAddress,
    NBMACAddress,
    NBFHRPGroupItem,
    NBInventoryItem,
    NBPowerPort
)

primary_tag_name = "NetBox-synced"
