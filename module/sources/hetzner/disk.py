# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from packaging import version

from module.netbox.inventory import NBVirtualDisk


def sync_vm_disks(handler, vm, server):
    """
    Sync Hetzner volumes → NetBox virtual disks
    """

    inventory = handler.inventory

    if not server.volumes:
        return

    for volume in server.volumes:

        disk_name = f"{server.name}-{volume.name}"[:60]

        # Hetzner reports volume sizes in GB, NetBox stores virtual disk sizes in GB before 4.1.0 and in MB since
        disk_size = int(volume.size)
        if version.parse(inventory.netbox_api_version) >= version.parse("4.1.0"):
            disk_size *= 1024

        disk_data = {
            "name": disk_name,
            "virtual_machine": vm,   # object, не id
            "size": disk_size,
        }

        existing_disk = None

        for disk in inventory.get_all_items(NBVirtualDisk):
            if (
                disk.data.get("name") == disk_name
                and disk.data.get("virtual_machine") == vm
            ):
                existing_disk = disk
                break

        if existing_disk is None:
            inventory.add_object(
                NBVirtualDisk,
                data=disk_data,
                source=handler,
            )
        else:
            existing_disk.update(disk_data, source=handler)
