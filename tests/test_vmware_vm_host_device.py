"""
Integration tests for 'track_vm_host', which fills the NetBox VM "device" field with the
ESXi host a VM currently runs on.

Run against vcsim and cross-checked against what pyVmomi reports for vm.runtime.host, so
the assertions describe the placement instead of restating the sync.
"""
from pyVmomi import vim

from module.netbox.object_classes import NBDevice, NBVM
from module.sources import instantiate_sources
from module.sources.vmware.config import VMWareConfig


def _sdk_vm_placement(sdk):
    """{vm name: host name} as the hypervisor reports it."""
    view = sdk.viewManager.CreateContainerView(sdk.rootFolder, [vim.VirtualMachine], True)
    try:
        return {vm.name: vm.runtime.host.name for vm in view.view if vm.runtime.host is not None}
    finally:
        view.Destroy()


def _sync(inventory, load_config, settings):
    load_config(settings)
    source = instantiate_sources()[0]
    assert source.init_successful, "VMware source failed to initialise"
    inventory.resolve_relations()
    source.apply()
    return inventory


def test_vm_host_device_is_the_host_the_vm_runs_on(inventory, load_config, vmware_settings, sdk):
    _sync(inventory, load_config, vmware_settings + "track_vm_host = True\n")

    placement = _sdk_vm_placement(sdk)
    assert placement, "the captured inventory has no VM with a running host"

    checked = 0
    for vm in inventory.get_all_items(NBVM):
        expected_host = placement.get(vm.get_display_name())
        if expected_host is None:
            continue

        device = vm.data.get("device")
        assert device is not None, f"{vm.get_display_name()} has no host device"
        assert isinstance(device, NBDevice)
        assert device.get_display_name() == expected_host
        checked += 1

    assert checked == len(placement)


def test_vm_host_device_is_unset_by_default(vmware_sync):
    # the shared settings do not enable track_vm_host, so nothing may reference a device
    for vm in vmware_sync.inventory.get_all_items(NBVM):
        assert vm.data.get("device") is None, vm.get_display_name()


def test_vm_host_device_unset_when_host_is_excluded_from_sync(inventory, load_config, vmware_settings, sdk):
    """
    A host kept out of NetBox by a filter has no device to point at. The VMs must still
    sync, with the field left unset rather than the run failing.
    """
    settings = vmware_settings + "track_vm_host = True\nhost_exclude_filter = .*\n"
    _sync(inventory, load_config, settings)

    assert len(inventory.get_all_items(NBDevice)) == 0
    vms = inventory.get_all_items(NBVM)
    assert len(vms) == len(_sdk_vm_placement(sdk))

    for vm in vms:
        assert vm.data.get("device") is None, vm.get_display_name()


def test_vm_host_device_follows_the_vm_to_another_host(inventory, load_config, vmware_settings):
    """A VM moved to another host by vMotion must end up pointing at the new host device."""
    _sync(inventory, load_config, vmware_settings + "track_vm_host = True\n")

    devices = inventory.get_all_items(NBDevice)
    if len(devices) < 2:
        return

    vm = next(v for v in inventory.get_all_items(NBVM) if v.data.get("device") is not None)
    previous_host = vm.data["device"]
    new_host = next(d for d in devices if d is not previous_host)

    vm.update(data={"device": new_host})

    assert vm.data["device"] is new_host
    assert "device" in vm.updated_items


def _parse_config(**source_settings):
    config = VMWareConfig()
    config.source_name = "my-vcenter-example"
    config.config_content = {
        "source": {
            "my-vcenter-example": {
                "type": "vmware",
                "host_fqdn": "vcenter.example.com",
                "username": "readonly",
                "password": "secret",
                **source_settings,
            }
        }
    }
    return config.parse(do_log=False)


def test_track_vm_host_defaults_to_disabled():
    assert _parse_config().track_vm_host is False


def test_track_vm_host_can_be_enabled():
    assert _parse_config(track_vm_host="true").track_vm_host is True
