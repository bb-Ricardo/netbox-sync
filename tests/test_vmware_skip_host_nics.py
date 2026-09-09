"""
`skip_host_nics` must skip only the physical interfaces of an ESXi host. The host
itself, its VMkernel interfaces and the VMs on it are still synced (PR #442 review).
"""
from module.netbox.object_classes import NBDevice, NBInterface, NBVM
from module.sources import instantiate_sources


def test_skip_host_nics_keeps_hosts_and_vmkernel_interfaces(vcsim, inventory, load_config, vmware_settings):
    load_config(vmware_settings + "\nskip_host_nics = True\n")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful
    inventory.resolve_relations()
    sources[0].apply()

    hosts = list(inventory.get_all_items(NBDevice))
    assert hosts, "hosts must still be synced with skip_host_nics enabled"
    assert list(inventory.get_all_items(NBVM)), "VMs must still be synced"

    interfaces = list(inventory.get_all_items(NBInterface))
    physical = [i for i in interfaces if "virtual" not in str(i.data.get("type", "virtual"))]
    vmkernel = [i for i in interfaces if "virtual" in str(i.data.get("type", "virtual"))]
    assert physical == [], f"physical interfaces were synced: {[i.get_display_name() for i in physical]}"
    assert vmkernel, "VMkernel interfaces must still be synced"
