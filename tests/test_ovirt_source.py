import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from module.config.file_output import ConfigFileOutput
from module.config.parser import ConfigParser
from module.netbox import (
    NBCluster, NBDevice, NBInterface, NBIPAddress, NBMACAddress, NBPrefix, NBSite,
    NBVirtualDisk, NBVM, NBVMInterface,
)
from module.sources import instantiate_sources, valid_sources
from module.sources.ovirt import connection
from module.sources.ovirt.config import OVirtConfig


def obj(**kwargs):
    return SimpleNamespace(**kwargs)


@pytest.fixture(autouse=True)
def windows_config_paths(monkeypatch):
    if os.name == "nt":
        monkeypatch.setattr(ConfigParser, "get_config_file_path", staticmethod(os.path.abspath))


@pytest.fixture
def ovirt_source(inventory, load_config, monkeypatch):
    def make(extra="", api_version="4.3.0"):
        inventory.netbox_api_version = api_version
        load_config(f"""
[source/test]
type = ovirt
url = https://engine.example.com/ovirt-engine/api
username = admin@internal
password = test-only
permitted_subnets = 10.0.0.0/8, 2001:db8::/32
dns_name_lookup = False
{extra}
""")
        monkeypatch.setattr(connection, "types", obj(
            HostStatus=obj(UP="up"), NicStatus=obj(UP="up"),
            VmStatus=obj(UP="up"), ReportedDeviceType=obj(NETWORK="network"),
            IpVersion=obj(V4="v4", V6="v6"),
        ))
        session = Mock()
        monkeypatch.setattr(connection, "sdk", obj(Connection=Mock(return_value=session)))
        sources = instantiate_sources()
        assert len(sources) == 1
        source = sources[0]
        assert source.init_successful
        return source
    return make


def populate(source, status="up", reported=True, disks=True):
    datacenter = obj(id="dc", name="DC")
    cluster = obj(id="cluster", name="Production", data_center=datacenter)
    source.add_datacenter(datacenter)
    source.add_cluster(cluster)
    nic = obj(
        name="eth0", mac=obj(address="52:54:00:00:00:01"), speed=1000000000,
        base_interface=None, bonding=None, status="up", mtu=1500, vlan=None,
        ip=obj(address="10.0.0.1", netmask="255.255.255.0"), ipv6=None,
    )
    host = obj(
        id="host", name="host.example.com", cluster=obj(id="cluster"),
        hardware_information=obj(
            manufacturer="Example", product_name="Server", uuid="host-uuid",
            serial_number="asset",
        ),
        os=obj(type="Linux", version=obj(full_version="9")), status="up",
        nics=[nic], address="10.0.0.1",
    )
    source.add_host(host)
    vm = obj(
        id="vm-uuid", name="vm.example.com", status=status,
        host=obj(id="host") if status == "up" else None, cluster=obj(id="cluster"),
        memory=2 * 1024**3, cpu=obj(topology=obj(cores=2, sockets=2, threads=1)),
        os=obj(type="linux"),
        guest_operating_system=obj(distribution="Debian", version=obj(full_version="12")),
        disk_attachments=[obj(disk=obj(id="disk"))] if disks else [],
        nics=[obj(name="nic1", mac=obj(address="52:54:00:00:00:02"), plugged=True)],
        reported_devices=[
            obj(name="ens3", type="network", mac=obj(address="52:54:00:00:00:02"),
                ips=[obj(address="10.0.0.2", version="v4"),
                     obj(address="2001:db8::2", version="v6")]),
        ] if reported else [],
    )
    source.disk_cache["disk"] = obj(
        id="disk", name="root", alias="root", provisioned_size=10 * 1024**3,
        actual_size=5 * 1024**3,
    )
    return vm, host


def test_source_and_config_are_registered():
    assert connection.OVirtHandler in valid_sources
    assert OVirtConfig in ConfigFileOutput.source_config_list
    assert NBMACAddress in connection.OVirtHandler.dependent_netbox_objects
    assert NBVirtualDisk in connection.OVirtHandler.dependent_netbox_objects


@pytest.mark.parametrize("api_version", ["3.6.0", "3.7.0", "4.1.0", "4.3.0"])
def test_host_vm_network_and_disk_sync(ovirt_source, inventory, api_version):
    source = ovirt_source(api_version=api_version)
    inventory.add_object(NBPrefix, data={"id": 1, "prefix": "10.0.0.0/24"}, read_from_netbox=True)
    inventory.add_object(NBPrefix, data={"id": 2, "prefix": "2001:db8::/64"}, read_from_netbox=True)
    vm, _ = populate(source)
    source.add_virtual_machine(vm)
    nbvm, = inventory.get_all_items(NBVM)
    host, = inventory.get_all_items(NBDevice)
    cluster, = inventory.get_all_items(NBCluster)
    assert nbvm.data["vcpus"] == 4
    assert nbvm.data["memory"] == 2048
    assert nbvm.data["platform"].data["name"] == "Debian 12"
    assert cluster.get_site_name() == source.site_name
    assert nbvm.data["primary_ip4"].data["address"] == "10.0.0.2/24"
    assert nbvm.data["primary_ip6"].data["address"] == "2001:db8::2/64"
    assert host.data["primary_ip4"].data["address"] == "10.0.0.1/24"
    assert inventory.get_all_items(NBInterface)[0].data["type"] == "1000base-t"
    interface, = inventory.get_all_items(NBVMInterface)
    assert interface.data["name"] == "ens3"
    assert len(inventory.get_all_items(NBIPAddress)) == 3
    if api_version >= "4.2":
        assert cluster.data["scope_type"] == "dcim.site"
        assert isinstance(cluster.data["scope_id"], NBSite)
        assert len(inventory.get_all_items(NBMACAddress)) == 2
        assert interface.data["primary_mac_address"].data["mac_address"] == "52:54:00:00:00:02"
    if api_version < "3.7":
        assert nbvm.data["disk"] == 10
    else:
        disk, = inventory.get_all_items(NBVirtualDisk)
        assert disk.data["virtual_machine"] is nbvm
        assert disk.data["size"] == (10 if api_version < "4.1" else 10240)
    source.add_virtual_machine(vm)
    assert len(inventory.get_all_items(NBVM)) == 1
    assert len(inventory.get_all_items(NBVMInterface)) == 1


def test_offline_vm_without_agent_or_cpu_is_synced(ovirt_source, inventory):
    source = ovirt_source()
    vm, _ = populate(source, status="down", reported=False)
    vm.cpu = None
    vm.guest_operating_system = None
    source.add_virtual_machine(vm)
    nbvm, = inventory.get_all_items(NBVM)
    assert nbvm.data["status"] == "offline"
    assert "vcpus" not in nbvm.data
    interface, = inventory.get_all_items(NBVMInterface)
    assert interface.data["name"] == "nic1"


def test_unknown_disk_size_is_not_invented(ovirt_source, inventory, caplog):
    source = ovirt_source()
    vm, _ = populate(source)
    source.disk_cache["disk"].provisioned_size = None
    source.add_virtual_machine(vm)
    assert len(inventory.get_all_items(NBVM)) == 1
    assert inventory.get_all_items(NBVirtualDisk) == []
    assert "provisioned size" in caplog.text


def test_vm_rename_matches_existing_primary_mac(ovirt_source, inventory):
    source = ovirt_source()
    vm, _ = populate(source)
    source.add_virtual_machine(vm)
    original, = inventory.get_all_items(NBVM)
    vm.name = "renamed-vm"
    source.add_virtual_machine(vm)
    assert inventory.get_all_items(NBVM) == [original]
    assert original.data["name"] == "renamed-vm"


def test_lost_guest_agent_preserves_existing_ip(ovirt_source, inventory):
    source = ovirt_source()
    inventory.add_object(NBPrefix, data={"id": 1, "prefix": "10.0.0.0/24"}, read_from_netbox=True)
    vm, _ = populate(source)
    source.add_virtual_machine(vm)
    nbvm, = inventory.get_all_items(NBVM)
    primary = nbvm.data["primary_ip4"]
    vm.reported_devices = []
    vm.status = "down"
    source.add_virtual_machine(vm)
    assert nbvm.data["primary_ip4"] is primary
    assert primary.data["assigned_object_id"].data["virtual_machine"] is nbvm


def test_cluster_filter_accepts_qualified_name(ovirt_source, inventory):
    source = ovirt_source("cluster_include_filter = DC/Production")
    vm, _ = populate(source)
    source.add_virtual_machine(vm)
    assert len(inventory.get_all_items(NBVM)) == 1


def test_different_sources_do_not_move_each_others_clusters(ovirt_source, inventory):
    source = ovirt_source()
    vm, _ = populate(source)
    original, = inventory.get_all_items(NBCluster)
    other = connection.OVirtHandler.__new__(connection.OVirtHandler)
    other.name = "other"
    other.set_source_tag()
    other.settings = source.settings
    other.site_name = "Other site"
    other.inventory = inventory
    other.object_cache = {}
    inventory.add_source(other)
    datacenter = obj(id="dc-other", name="DC")
    other.add_datacenter(datacenter)
    other.add_cluster(obj(id="other-cluster", name="Production", data_center=datacenter))
    assert len(inventory.get_all_items(NBCluster)) == 2
    assert original.get_site_name() == source.site_name


def test_uuid_name_custom_attributes_and_skip_platform(ovirt_source, inventory):
    source = ovirt_source("""
set_vm_name_to_uuid = True
vm_custom_object_attributes = id
host_custom_object_attributes = id
skip_vm_platform = True
""")
    vm, _ = populate(source)
    source.add_virtual_machine(vm)
    nbvm, = inventory.get_all_items(NBVM)
    host, = inventory.get_all_items(NBDevice)
    assert nbvm.data["name"] == vm.id
    assert "platform" not in nbvm.data
    assert nbvm.data["custom_fields"]["ovirt_vm_name"] == vm.name
    assert nbvm.data["custom_fields"]["ovirt_id"] == vm.id
    assert host.data["custom_fields"]["ovirt_id"] == "host"


def test_missing_sdk_is_reported_without_breaking_other_sources(
        inventory, load_config, monkeypatch, caplog):
    load_config("""
[source/test]
type = ovirt
url = https://engine.example.com/ovirt-engine/api
username = admin@internal
password = test-only
""")
    monkeypatch.setattr(connection, "sdk", None)
    assert instantiate_sources() == []
    assert "requirements-ovirt.txt" in caplog.text


def test_connection_error_is_reported(ovirt_source, monkeypatch, caplog):
    source = ovirt_source()
    source.session = None
    monkeypatch.setattr(connection.sdk, "Connection", Mock(side_effect=RuntimeError("unavailable")))
    assert source.create_ovirt_session() is False
    assert "unavailable" in caplog.text


def test_apply_queries_related_data_and_closes_session(ovirt_source, inventory):
    source = ovirt_source()
    vm, host = populate(source)
    system = source.session.system_service.return_value
    system.disks_service.return_value.list.return_value = list(source.disk_cache.values())
    system.data_centers_service.return_value.list.return_value = []
    system.clusters_service.return_value.list.return_value = []
    system.hosts_service.return_value.list.return_value = []
    system.vms_service.return_value.list.return_value = [vm]
    source.apply()
    assert len(inventory.get_all_items(NBVM)) == 1
    system.vms_service.return_value.list.assert_called_once_with(
        follow="nics,disk_attachments,reported_devices")
    source.finish()
    source.session.close.assert_called_once()
