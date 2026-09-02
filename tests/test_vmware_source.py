"""
Integration tests for the VMware source, run against vcsim.

Each test gets a vcsim serving one of the captured inventories in
tests/fixtures/vcsim and compares what netbox-sync put into its in-memory
inventory with what pyVmomi reports for the same simulator, so the assertions
describe the sync rather than restating it.
"""
from module.netbox.object_classes import (
    NBCluster,
    NBDevice,
    NBIPAddress,
    NBMACAddress,
    NBVM,
    NBVMInterface,
)


def _sdk_vms(sdk):
    from pyVmomi import vim

    view = sdk.viewManager.CreateContainerView(sdk.rootFolder, [vim.VirtualMachine], True)
    try:
        return {vm.name: vm for vm in view.view}
    finally:
        view.Destroy()


def _sdk_hosts(sdk):
    from pyVmomi import vim

    view = sdk.viewManager.CreateContainerView(sdk.rootFolder, [vim.HostSystem], True)
    try:
        return {host.name: host for host in view.view}
    finally:
        view.Destroy()


def _interface_macs(inventory, interface):
    """MAC addresses NetBox would hold for an interface, lower case."""
    return sorted(
        str(mac.data.get("mac_address")).lower()
        for mac in inventory.get_all_items(NBMACAddress)
        if mac.data.get("assigned_object_id") is interface
    )


def _vm_interfaces(inventory, vm):
    return [i for i in inventory.get_all_items(NBVMInterface) if i.data.get("virtual_machine") is vm]


def test_source_initialises_and_syncs(vmware_sync):
    # the handler connected to vcsim and produced an inventory
    inventory = vmware_sync.inventory
    assert len(inventory.get_all_items(NBVM)) > 0
    assert len(inventory.get_all_items(NBDevice)) > 0
    assert len(inventory.get_all_items(NBCluster)) > 0


def test_every_vm_is_synced(vmware_sync, sdk):
    synced = {vm.get_display_name() for vm in vmware_sync.inventory.get_all_items(NBVM)}
    expected = set(_sdk_vms(sdk))
    assert synced == expected


def test_every_host_is_synced_with_hardware_details(vmware_sync, sdk):
    inventory = vmware_sync.inventory
    hosts = {host.get_display_name(): host for host in inventory.get_all_items(NBDevice)}
    assert set(hosts) == set(_sdk_hosts(sdk))

    for name, sdk_host in _sdk_hosts(sdk).items():
        host = hosts[name]
        hardware = sdk_host.hardware.systemInfo
        assert host.data.get("serial") == hardware.serialNumber
        assert host.data["device_type"].get_display_name() == hardware.model
        # the vendor string is normalised to a canonical manufacturer, so
        # "FUJITSU" becomes "Fujitsu" and "Dell Inc." becomes "Dell"
        manufacturer = host.data["device_type"].data["manufacturer"].get_display_name()
        assert manufacturer, name
        assert hardware.vendor.lower().startswith(manufacturer.lower()), (
            f"{name}: manufacturer {manufacturer!r} does not match vendor {hardware.vendor!r}"
        )
        assert host.data.get("cluster") is not None


def test_vm_resources_match_the_hypervisor(vmware_sync, sdk):
    # vm_disk_and_ram_in_decimal is off in the test config, so memory is the
    # hypervisor value unchanged
    inventory = vmware_sync.inventory
    vms = {vm.get_display_name(): vm for vm in inventory.get_all_items(NBVM)}

    for name, sdk_vm in _sdk_vms(sdk).items():
        hardware = sdk_vm.config.hardware
        assert vms[name].data.get("vcpus") == hardware.numCPU, name
        assert vms[name].data.get("memory") == hardware.memoryMB, name


def test_vm_interface_macs_match_the_hypervisor(vmware_sync, sdk):
    inventory = vmware_sync.inventory
    vms = {vm.get_display_name(): vm for vm in inventory.get_all_items(NBVM)}

    checked = 0
    for name, sdk_vm in _sdk_vms(sdk).items():
        expected = sorted(
            device.macAddress.lower()
            for device in sdk_vm.config.hardware.device
            if getattr(device, "macAddress", None)
        )
        synced = sorted(
            mac
            for interface in _vm_interfaces(inventory, vms[name])
            for mac in _interface_macs(inventory, interface)
        )
        # the same MAC may be recorded more than once while objects are still
        # unsaved, so compare the sets of addresses
        assert set(synced) == set(expected), name
        checked += len(expected)

    assert checked > 0, "no VM NICs in this inventory, the assertion above proves nothing"


def test_guest_ips_are_synced_and_bound_to_an_interface(vmware_sync, sdk):
    inventory = vmware_sync.inventory

    expected = set()
    for sdk_vm in _sdk_vms(sdk).values():
        guest = sdk_vm.guest
        # netbox-sync only reads IPs from VMs whose guest tools are running
        if guest is None or guest.toolsRunningStatus != "guestToolsRunning":
            continue
        for nic in guest.net or []:
            for ip in (nic.ipConfig.ipAddress if nic.ipConfig else []):
                # link-local addresses are filtered out by the source
                if ip.ipAddress.startswith(("fe80:", "169.254.")):
                    continue
                expected.add(f"{ip.ipAddress}/{ip.prefixLength}")

    if not expected:
        # a capture without running guest tools cannot say anything about IPs
        return

    synced = {ip.data.get("address") for ip in inventory.get_all_items(NBIPAddress)}
    assert expected <= synced

    for ip in inventory.get_all_items(NBIPAddress):
        assert ip.data.get("assigned_object_id") is not None, ip.data.get("address")


def test_sync_is_deterministic(vmware_source, inventory, vmware_settings, load_config):
    from module.sources import instantiate_sources

    def counts(inv):
        return {
            cls.__name__: len(inv.get_all_items(cls))
            for cls in (NBCluster, NBDevice, NBVM, NBVMInterface, NBIPAddress, NBMACAddress)
        }

    vmware_source.apply()
    first = counts(inventory)

    # a second run from a clean inventory against the same simulator
    inventory.base_structure = {}
    inventory.source_list = []
    inventory.init()
    inventory.netbox_api_version = "4.3.0"
    load_config(vmware_settings)
    second_source = instantiate_sources()[0]
    inventory.resolve_relations()
    second_source.apply()

    assert counts(inventory) == first


def test_rerun_against_saved_objects_creates_nothing(vmware_sync):
    """
    A second run over an inventory whose objects already exist in NetBox must
    match them instead of creating new ones. Objects are marked as saved the way
    they would be after being read back from NetBox.
    """
    inventory = vmware_sync.inventory
    tracked = (NBCluster, NBDevice, NBVM, NBVMInterface, NBIPAddress, NBMACAddress)

    def counts():
        return {cls.__name__: len(inventory.get_all_items(cls)) for cls in tracked}

    before = counts()

    next_id = 1
    for cls in tracked:
        for item in inventory.get_all_items(cls):
            item.is_new = False
            item.nb_id = next_id
            next_id += 1

    vmware_sync.source.apply()

    assert counts() == before
