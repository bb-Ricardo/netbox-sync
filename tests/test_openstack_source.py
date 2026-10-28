"""OpenStack discovery against fake SDK resources and the real NetBox models."""

import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from module.config.file_output import ConfigFileOutput
from module.config.files import ConfigFileINI, ConfigFileYAML
from module.config.parser import ConfigParser
from module.netbox import (
    NBCluster, NBClusterGroup, NBCustomField, NBDevice, NBInterface, NBIPAddress, NBMACAddress,
    NBPrefix, NBSite, NBTenant, NBVirtualDisk, NBVM, NBVMInterface, NBVRF,
)
from module.sources import instantiate_sources, valid_sources
from module.sources.openstack.config import OpenStackConfig
from module.sources.openstack.connection import OpenStackHandler


def resource(**values):
    obj = SimpleNamespace(**values)
    obj.to_dict = lambda: values.copy()
    return obj


def server(**overrides):
    values = dict(
        name="app.example.org", id="vm-uuid", status="ACTIVE", availability_zone="zone.example",
        flavor={"original_name": "m1.small", "vcpus": 2, "ram": 2048, "disk": 10},
        image={"id": "image"}, attached_volumes=[{"id": "volume-1"}], compute_host="compute1",
        addresses={"private": [
            {"addr": "10.0.0.8", "version": 4, "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:01",
             "OS-EXT-IPS:type": "fixed"},
            {"addr": "fd00::8", "version": 6, "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:01",
             "OS-EXT-IPS:type": "fixed"},
            {"addr": "192.0.2.8", "version": 4, "OS-EXT-IPS:type": "floating"},
        ]},
    )
    values.update(overrides)
    return resource(**values)


@pytest.fixture
def cloud(monkeypatch, inventory):
    settings = dict(
        type="openstack", auth_url="https://identity.example/v3", project="admin",
        username="sync", password="test-only", region="RegionOne", user_domain="Default",
        project_domain="Default", permitted_subnets="10.0.0.0/8, fd00::/8",
        dns_name_lookup=False, cluster_site_relation=".* = Cloud", vm_tenant_relation=".* = Operations",
        cluster_tag_relation=".* = OpenStack", vm_tag_relation=".* = Production",
    )
    parser = ConfigParser()
    monkeypatch.setattr(parser, "content", {"source": {"cloud": settings}})
    monkeypatch.setattr(parser, "parsing_finished", True)
    sdk = ModuleType("openstack")
    session = Mock()
    session.compute.availability_zones.return_value = [
        resource(name="zone.example", hosts={"compute1": {}}),
        resource(name="empty", hosts=None),
    ]
    session.compute.hypervisors.return_value = [
        resource(name="compute1.example", service_details={"host": "compute1"}, status="enabled",
                 state="up", host_ip="10.0.0.2", hypervisor_type="QEMU", hypervisor_version=8000000),
    ]
    session.block_storage.volumes.return_value = [resource(id="volume-1", size=20)]
    session.compute.servers.return_value = [server()]
    sdk.connect = Mock(return_value=session)
    monkeypatch.setitem(sys.modules, "openstack", sdk)
    return SimpleNamespace(settings=settings, sdk=sdk, session=session, inventory=inventory)


@pytest.mark.parametrize("netbox_version", ["3.6.0", "3.7.0", "4.0.0", "4.1.0", "4.2.0", "4.3.0"])
def test_discovery_uses_current_inventory_apis(cloud, netbox_version):
    inventory = cloud.inventory
    inventory.netbox_api_version = netbox_version
    site = inventory.add_object(NBSite, data={"id": 1, "name": "Cloud"}, read_from_netbox=True)
    tenant = inventory.add_object(NBTenant, data={"id": 1, "name": "Network"}, read_from_netbox=True)
    vrf = inventory.add_object(NBVRF, data={"id": 1, "name": "Cloud VRF"}, read_from_netbox=True)
    prefix_data = {"prefix": "10.0.0.0/24", "tenant": tenant, "vrf": vrf}
    if netbox_version >= "4.2.0":
        prefix_data.update(scope_type=NBSite.object_type, scope_id=site)
    else:
        prefix_data["site"] = site
    prefix = inventory.add_object(NBPrefix, data=prefix_data, read_from_netbox=True)
    # Model the resolved relations returned by the real NetBox inventory loader.
    prefix.data.update(tenant=tenant, vrf=vrf)
    prefix.data["scope_id" if netbox_version >= "4.2.0" else "site"] = site
    inventory.add_object(NBPrefix, data={"prefix": "fd00::/64"}, read_from_netbox=True)
    handler = OpenStackHandler("cloud")
    assert handler.init_successful
    assert cloud.sdk.connect.call_args.kwargs["verify"] is True
    handler.apply()
    cluster = handler.get_object_from_cache(NBCluster, "zone.example")
    assert cluster.get_site_name() == "Cloud"
    if netbox_version >= "4.2.0":
        assert cluster.data["scope_id"] is site
        assert cluster.data["scope_type"] == "dcim.site"
        assert "site" not in cluster.data
    host = inventory.get_all_items(NBDevice)[0]
    vm = inventory.get_all_items(NBVM)[0]
    assert host.data["primary_ip4"].data["address"] == "10.0.0.2/24"
    assert host.data["platform"].data["name"] == "QEMU"
    assert vm.data["device"] is host
    assert vm.data["status"] == "active"
    assert vm.data["vcpus"] == 2
    assert vm.data["memory"] == 2048
    if netbox_version < "3.7.0":
        assert vm.data["disk"] == 30
        assert not vm.get_virtual_disks()
    else:
        assert "disk" not in vm.data
        disks = {disk.data["name"]: disk.data["size"] for disk in vm.get_virtual_disks()}
        multiplier = 1000 if netbox_version >= "4.1.0" else 1
        assert disks == {"root": 10 * multiplier, "volume-1": 20 * multiplier}
    assert vm.data["platform"].data["name"] == "m1.small"
    assert "Production" in [tag.data["name"] for tag in vm.data["tags"]]
    assert "OpenStack" in [tag.data["name"] for tag in cluster.data["tags"]]
    nic = inventory.get_all_items(NBVMInterface)[0]
    assert len(inventory.get_all_items(NBVMInterface)) == 1
    assert nic.data["name"] == "vNIC1 (private)"
    assert {ip.data["address"] for ip in nic.get_ip_addresses()} == {"10.0.0.8/24", "fd00::8/64"}
    assert vm.data["primary_ip4"].data["vrf"].data["name"] == "Cloud VRF"
    assert vm.data["primary_ip4"].data["tenant"].data["name"] == "Operations"
    assert vm.data["primary_ip6"].data["address"] == "fd00::8/64"
    if netbox_version >= "4.2.0":
        assert len(inventory.get_all_items(NBMACAddress)) == 1
        assert nic.data["primary_mac_address"].data["mac_address"] == "FA:16:3E:00:00:01"
    else:
        assert nic.data["mac_address"] == "FA:16:3E:00:00:01"
    handler.apply()
    assert len(inventory.get_all_items(NBVM)) == 1
    assert len(inventory.get_all_items(NBVMInterface)) == 1
    assert len(inventory.get_all_items(NBCluster)) == 2
    if netbox_version >= "3.7.0":
        assert len(inventory.get_all_items(NBVirtualDisk)) == 2
    handler.finish()
    cloud.session.close.assert_called_once()


def test_registration_authentication_and_project_scope(cloud):
    assert OpenStackHandler in valid_sources
    assert OpenStackConfig in ConfigFileOutput.source_config_list
    cloud.settings.update(validate_tls_certs=False, all_projects=False)
    handlers = instantiate_sources()
    assert len(handlers) == 1
    handler = handlers[0]
    cloud.sdk.connect.assert_called_once_with(
        auth_url=cloud.settings["auth_url"], project_name="admin", username="sync",
        password="test-only", region_name="RegionOne", user_domain_name="Default",
        project_domain_name="Default", verify=False, app_name="netbox-sync", app_version="0.1",
    )
    cloud.session.authorize.assert_called_once()
    handler.apply()
    cloud.session.compute.servers.assert_called_once_with(details=True, all_projects=False)
    cloud.session.block_storage.volumes.assert_called_once_with(details=True, all_projects=False)


def test_missing_optional_sdk_is_explicit_and_disabled_sources_do_not_import(cloud, monkeypatch, caplog):
    def missing_sdk(name):
        raise ModuleNotFoundError(name)
    monkeypatch.setattr("module.sources.openstack.connection.import_module", missing_sdk)
    handler = OpenStackHandler("cloud")
    assert not handler.init_successful
    assert "requirements-openstack.txt" in caplog.text
    assert handler.session is None
    cloud.settings["enabled"] = False
    import_mock = Mock(side_effect=AssertionError("disabled source imported SDK"))
    monkeypatch.setattr("module.sources.openstack.connection.import_module", import_mock)
    assert not OpenStackHandler("cloud").init_successful
    import_mock.assert_not_called()


def test_authentication_failure_without_legacy_msg_attribute(cloud, caplog):
    cloud.session.authorize.side_effect = RuntimeError("identity unavailable")
    handler = OpenStackHandler("cloud")
    assert not handler.init_successful
    assert handler.session is None
    assert "identity unavailable" in caplog.text
    cloud.session.close.assert_called_once()


def test_filters_active_duplicates_and_uuid_custom_field(cloud):
    cloud.settings.update(
        strip_host_domain_name=True, strip_vm_domain_name=True,
        vm_exclude_filter="ignore.*", cluster_exclude_filter="empty",
        vm_role_relation=".* = Application",
    )
    cloud.session.compute.servers.return_value = [
        server(status="SHUTOFF", id="inactive"), server(),
        server(name="ignore.example", id="ignored"),
    ]
    handler = OpenStackHandler("cloud")
    handler.apply()
    vms = cloud.inventory.get_all_items(NBVM)
    assert len(vms) == 1
    assert vms[0].data["name"] == "app"
    assert vms[0].data["status"] == "active"
    assert vms[0].data["role"].data["name"] == "Application"
    assert cloud.inventory.get_all_items(NBDevice)[0].data["name"] == "compute1"
    assert len(cloud.inventory.get_all_items(NBCluster)) == 1
    cloud.settings["set_vm_name_to_uuid"] = True
    cloud.session.compute.servers.return_value = [server()]
    handler = OpenStackHandler("cloud")
    handler.apply()
    vm = cloud.inventory.get_all_items(NBVM)[0]
    assert vm.data["name"] == "vm-uuid"
    assert vm.data["custom_fields"]["openstack_vm_name"] == "app.example.org"
    field = cloud.inventory.get_all_items(NBCustomField)[0]
    assert field.data["object_types"] == ["virtualization.virtualmachine"]
    handler.apply()
    assert field.data["object_types"] == ["virtualization.virtualmachine"]


def test_duplicate_vm_names_keep_the_existing_server_identity(cloud):
    cloud.session.compute.servers.return_value = [
        server(id="server-a", status="SHUTOFF"),
        server(id="server-b", status="ACTIVE"),
    ]
    handler = OpenStackHandler("cloud")
    handler.apply()
    vm = cloud.inventory.get_all_items(NBVM)[0]
    assert vm.data["comments"] == "server-b"

    cloud.session.compute.servers.return_value = [
        server(id="server-a", status="ACTIVE"),
        server(id="server-b", status="SHUTOFF"),
    ]
    handler.apply()
    assert vm.data["comments"] == "server-b"
    assert vm.data["status"] == "offline"
    assert len(cloud.inventory.get_all_items(NBVM)) == 1


def test_duplicate_zone_hosts_are_assigned_to_the_nova_compute_zone(cloud):
    cloud.session.compute.availability_zones.return_value = [
        resource(name="internal", hosts={
            "compute1": {"nova-scheduler": {}, "nova-conductor": {}},
        }),
        resource(name="nova", hosts={"compute1": {"nova-compute": {}}}),
    ]
    cloud.session.compute.servers.return_value = [server(availability_zone="nova")]

    handler = OpenStackHandler("cloud")
    handler.apply()

    host = cloud.inventory.get_all_items(NBDevice)[0]
    vm = cloud.inventory.get_all_items(NBVM)[0]
    assert host.data["cluster"].data["name"] == "nova"
    assert vm.data["cluster"].data["name"] == "nova"
    assert vm.data["device"] is host


def test_interface_names_and_host_interface_type_are_preserved(cloud):
    addresses = {
        "private": [{
            "addr": "10.0.0.8", "version": 4,
            "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:01",
        }],
        "secondary": [{
            "addr": "10.0.0.9", "version": 4,
            "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:02",
        }],
    }
    cloud.session.compute.servers.return_value = [server(addresses=addresses)]
    handler = OpenStackHandler("cloud")
    handler.apply()
    vm_nic = next(nic for nic in cloud.inventory.get_all_items(NBVMInterface)
                  if nic.data["name"] == "vNIC2 (secondary)")
    host_interface = cloud.inventory.get_all_items(NBInterface)[0]
    host_interface.data["type"] = "1000base-t"

    cloud.session.compute.servers.return_value = [
        server(addresses={"secondary": addresses["secondary"]}),
    ]
    handler.apply()

    assert vm_nic.data["name"] == "vNIC2 (secondary)"
    assert host_interface.data["type"] == "1000base-t"


def test_network_ports_invalid_addresses_and_floating_primary_preference(cloud):
    cloud.settings["permitted_subnets"] += ", 192.0.2.0/24"
    vm_resource = server()
    vm_resource.addresses["private"].extend([
        {"addr": "invalid", "version": 4},
        {"addr": "10.0.0.9", "version": 4, "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:02"},
    ])
    vm_resource.addresses["without-mac"] = [{"addr": "10.0.1.9", "version": 4}]
    cloud.session.compute.servers.return_value = [vm_resource]
    OpenStackHandler("cloud").apply()
    vm = cloud.inventory.get_all_items(NBVM)[0]
    nics = cloud.inventory.get_all_items(NBVMInterface)
    assert len(nics) == 3
    assert vm.data["primary_ip4"].data["address"] == "10.0.0.8/32"
    assert {ip.data["address"] for ip in nics[0].get_ip_addresses()} == {
        "10.0.0.8/32", "fd00::8/128", "192.0.2.8/32",
    }


def test_primary_ip_uses_first_fixed_address(cloud):
    cloud.settings["permitted_subnets"] += ", 192.0.2.0/24"
    cloud.session.compute.servers.return_value = [server(addresses={
        "private": [
            {"addr": "192.0.2.8", "version": 4, "OS-EXT-IPS:type": "floating"},
            {"addr": "10.0.0.8", "version": 4, "OS-EXT-IPS:type": "fixed"},
            {"addr": "10.0.0.9", "version": 4, "OS-EXT-IPS:type": "fixed"},
        ],
    })]

    OpenStackHandler("cloud").apply()

    vm = cloud.inventory.get_all_items(NBVM)[0]
    assert vm.data["primary_ip4"].data["address"] == "10.0.0.8/32"


def test_flavor_lookup_unknown_volume_and_ipv6_hypervisor(cloud):
    cloud.session.compute.servers.return_value = [server(flavor={"id": "flavor-1"},
                                                        attached_volumes=[{"id": "missing"}])]
    cloud.session.compute.get_flavor.return_value = resource(name="large", vcpus=4, ram=4096, disk=20)
    cloud.session.compute.hypervisors.return_value[0].host_ip = "fd00::2"
    handler = OpenStackHandler("cloud")
    handler.apply()
    vm = cloud.inventory.get_all_items(NBVM)[0]
    assert vm.data["memory"] == 4096
    assert vm.data["vcpus"] == 4
    assert "disk" not in vm.data
    host = cloud.inventory.get_all_items(NBDevice)[0]
    assert "primary_ip4" not in host.data
    assert host.data["primary_ip6"].data["address"] == "fd00::2/128"
    cloud.session.compute.get_flavor.assert_called_once_with("flavor-1")


def test_cluster_matching_does_not_reassign_another_cloud(cloud):
    other_group = cloud.inventory.add_object(NBClusterGroup, data={"name": "Other"})
    other_site = cloud.inventory.add_object(NBSite, data={"name": "Other Site"})
    foreign = cloud.inventory.add_object(NBCluster, data={
        "name": "zone.example", "group": other_group, "type": {"name": "Openstack"},
        "scope_type": NBSite.object_type, "scope_id": other_site,
    })
    handler = OpenStackHandler("cloud")
    handler.apply()
    assert foreign.data["group"] is other_group
    assert foreign.get_site_name() == "Other Site"
    assert handler.get_object_from_cache(NBCluster, "zone.example") is not foreign


def test_clusters_with_same_default_group_are_isolated_by_source_tags(cloud):
    other_source = SimpleNamespace(source_tag="Source: other")
    cloud.inventory.source_list = [other_source]
    other_site = cloud.inventory.add_object(NBSite, data={"name": "Other Site"})
    foreign = cloud.inventory.add_object(NBCluster, data={
        "name": "zone.example", "group": {"name": "Openstack"}, "type": {"name": "Openstack"},
        "scope_type": NBSite.object_type, "scope_id": other_site, "tags": ["Source: other"],
    })
    handler = OpenStackHandler("cloud")
    handler.apply()
    assert foreign.get_site_name() == "Other Site"
    assert handler.get_object_from_cache(NBCluster, "zone.example") is not foreign


def test_missing_volume_retains_existing_virtual_disk_size(cloud):
    handler = OpenStackHandler("cloud")
    handler.apply()
    vm = cloud.inventory.get_all_items(NBVM)[0]
    disk = next(disk for disk in vm.get_virtual_disks() if disk.data["name"] == "volume-1")
    cloud.session.block_storage.volumes.return_value = []
    handler.apply()
    assert disk.data["size"] == 20000
    assert disk.source is handler
    assert len(vm.get_virtual_disks()) == 2


@pytest.mark.parametrize("setting,value", [
    ("vm_include_filter", "["), ("vm_tenant_relation", "missing separator"),
    ("set_primary_ip", "sometimes"), ("ip_tenant_inheritance_order", "unknown"),
])
def test_invalid_settings_fail_cleanly(cloud, setting, value):
    cloud.settings[setting] = value
    with pytest.raises(SystemExit):
        OpenStackHandler("cloud")


def test_mac_and_primary_ip_matching_use_current_netbox_objects(cloud):
    handler = OpenStackHandler("cloud")
    handler.apply()
    vm = cloud.inventory.get_all_items(NBVM)[0]
    assert handler.get_object_based_on_macs(NBVM, ["FA:16:3E:00:00:01"]) is vm
    assert handler.get_object_based_on_primary_ip(NBVM, primary_ip4="10.0.0.8") is vm
    assert handler.get_object_from_cache(NBVM, "zone.example") is None


def test_interface_exclusions_and_resource_custom_attributes(cloud):
    cloud.settings.update(
        vm_interface_exclude_filter="vNIC1.*", host_interface_exclude_filter="eth0",
        vm_custom_object_attributes="id, flavor.vcpus", host_custom_object_attributes="name",
    )
    OpenStackHandler("cloud").apply()
    assert not cloud.inventory.get_all_items(NBVMInterface)
    assert not cloud.inventory.get_all_items(NBIPAddress)
    vm = cloud.inventory.get_all_items(NBVM)[0]
    assert vm.data["custom_fields"]["openstack_id"] == "vm-uuid"
    assert vm.data["custom_fields"]["openstack_flavor_vcpus"] == "2"
    host = cloud.inventory.get_all_items(NBDevice)[0]
    assert host.data["custom_fields"]["openstack_name"] == "compute1.example"


def test_source_neutral_vm_ip_updates_remove_undiscovered_addresses(cloud):
    handler = OpenStackHandler("cloud")
    handler.apply()
    vm = cloud.inventory.get_all_items(NBVM)[0]
    old_ip = vm.data["primary_ip4"]
    cloud.session.compute.servers.return_value = [server(addresses={
        "private": [{"addr": "10.0.0.9", "version": 4,
                     "OS-EXT-IPS-MAC:mac_addr": "fa:16:3e:00:00:01"}],
    })]
    handler.settings.set_primary_ip = "always"
    handler.apply()
    assert vm.data["primary_ip4"].data["address"] == "10.0.0.9/32"
    assert "assigned_object_id" in old_ip.unset_items
    assert len(cloud.inventory.get_all_items(NBVMInterface)) == 1


@pytest.mark.parametrize("file_type", [ConfigFileINI, ConfigFileYAML])
def test_config_generation_does_not_require_sdk(monkeypatch, file_type):
    monkeypatch.setattr("module.sources.openstack.connection.import_module",
                        Mock(side_effect=AssertionError("config generation imported SDK")))
    output = ConfigFileOutput.__new__(ConfigFileOutput)
    output.config_file_type = file_type
    output.comment_prefix = file_type.comment_prefix
    output.lines = []
    output.format()
    text = "\n".join(output.lines)
    assert "my-openstack-example" in text
    assert "openstack" in text
    assert "all_projects" in text
    assert "ip_tenant_inheritance_order" in text
