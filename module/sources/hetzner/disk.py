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
        size_mb = int(volume.size) * 1024  # Hetzner size = GB

        disk_data = {
            "name": disk_name,
            "virtual_machine": vm,   # object, не id
            "size": size_mb,
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
