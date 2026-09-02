from module.netbox.inventory import NBVMInterface, NBIPAddress


def sync_vm_network(handler, vm, server):
    """
    Create interfaces + assign IPs for Hetzner VM
    """

    inventory = handler.inventory
    interfaces = []

    # -----------------------
    # interfaces
    # -----------------------

    # public → eth0
    if server.public_net and server.public_net.ipv4:
        iface = inventory.add_update_object(
            NBVMInterface,
            data={
                "name": "eth0",
                "virtual_machine": vm,
                "enabled": True,
            },
            source=handler,
        )
        interfaces.append(iface)

    # private → ethX
    if server.private_net:
        start_index = 1 if len(interfaces) > 0 else 0

        for idx, net in enumerate(server.private_net, start=start_index):
            iface = inventory.add_update_object(
                NBVMInterface,
                data={
                    "name": f"eth{idx}",
                    "virtual_machine": vm,
                    "enabled": True,
                },
                source=handler,
            )
            interfaces.append(iface)

    # -----------------------
    # IP assignment
    # -----------------------

    # public ip
    if server.public_net and server.public_net.ipv4 and len(interfaces) >= 1:
        ip_addr = server.public_net.ipv4.ip
        if "/" not in ip_addr:
            ip_addr += "/32"

        assign_ip(inventory, handler, ip_addr, interfaces[0])

    # private ips
    if server.private_net:
        private_start_index = 1 if (server.public_net and server.public_net.ipv4) else 0

        for idx, net in enumerate(server.private_net, start=private_start_index):

            if len(interfaces) <= idx:
                continue

            ip_addr = net.ip
            if "/" not in ip_addr:
                ip_addr += "/32"

            assign_ip(inventory, handler, ip_addr, interfaces[idx])


def assign_ip(inventory, handler, ip_addr, interface):
    """
    Safe IP assign without duplicates
    """

    ip_data = {
        "address": ip_addr,
        "assigned_object_type": "virtualization.vminterface",
        "assigned_object_id": interface,
    }

    existing_ip = next(
        (ip for ip in inventory.get_all_items(NBIPAddress)
         if ip.data.get("address") == ip_addr),
        None
    )

    if existing_ip is None:
        inventory.add_object(NBIPAddress, data=ip_data, source=handler)
    else:
        existing_ip.update(ip_data, source=handler)
