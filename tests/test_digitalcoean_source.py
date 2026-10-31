from configparser import ConfigParser as INIParser
from ipaddress import ip_interface
from pathlib import Path
from unittest.mock import Mock

import pytest

from module.config.file_output import ConfigFileOutput
from module.config.files import ConfigFileINI, ConfigFileYAML
from module.config.parser import ConfigParser
from module.netbox import (
    NBCluster, NBClusterGroup, NBCustomField, NBIPAddress, NBPlatform, NBRegion, NBTag, NBVM, NBVMInterface,
    NBVirtualDisk,
)
from module.netbox.connection import NetBoxHandler
from module.sources import instantiate_sources, valid_sources
from module.sources.aws.config import AWSConfig
from module.sources.aws.connection import AWSHandler
from module.sources.digitalocean.config import DigitalOceanConfig
from module.sources.digitalocean.connection import DigitalOceanHandler
from module.sources.digitalocean.client import DigitalOceanClient


@pytest.fixture
def digitalocean(inventory, monkeypatch):
    parser = ConfigParser()
    monkeypatch.setattr(parser, "content", {
        "source": {"cloud": {"type": "digitalocean", "api_token": "test-token"}}
    })
    monkeypatch.setattr(parser, "parsing_finished", True)
    client = Mock()
    client.get_collection.side_effect = lambda path, key: {
        "droplets": [dict(
            id=123,
            name="web-1",
            status="active",
            region={"slug": "nyc1", "name": "New York 1"},
            memory=2048,
            vcpus=2,
            disk=25,
            image={"distribution": "Ubuntu", "name": "Ubuntu 24.04"},
            tags=["production", "web"],
            volume_ids=["volume-1"],
            networks={
                "v4": [
                    {"ip_address": "10.10.0.5", "netmask": "255.255.255.0", "type": "private"},
                    {"ip_address": "203.0.113.10", "netmask": "255.255.255.0", "type": "public"},
                ],
                "v6": [
                    {"ip_address": "2001:db8::5", "cidr": 64, "type": "public"},
                ],
            },
        )],
        "volumes": [dict(
            id="volume-1",
            name="database-data",
            size_gigabytes=20,
            droplet_ids=[123],
        )],
        "databases": [dict(
            id="database-uuid",
            name="orders",
            region={"slug": "nyc1", "name": "New York 1"},
            status="online",
            engine="pg",
            version="16",
            size="db-s-2vcpu-4gb",
            num_nodes=2,
            storage_size_mib=102400,
            private_connection={
                "host": "db-private.example.org",
                "port": 25060,
                "uri": "postgres://user:secret@db-private.example.org:25060/db",
            },
            connection={
                "host": "db-public.example.org",
                "port": 25060,
                "password": "do-not-copy",
            },
            tags=["production"],
        )],
    }[path]
    monkeypatch.setattr(
        "module.sources.digitalocean.connection.DigitalOceanClient",
        Mock(return_value=client),
    )
    return inventory, client


def test_source_registration_and_settings_generation(monkeypatch):
    assert DigitalOceanHandler in valid_sources
    assert AWSHandler in valid_sources
    assert DigitalOceanConfig in ConfigFileOutput.source_config_list
    assert AWSConfig in ConfigFileOutput.source_config_list
    assert DigitalOceanHandler.implements("digitalocean")

    for file_type in (ConfigFileINI, ConfigFileYAML):
        output = ConfigFileOutput.__new__(ConfigFileOutput)
        output.config_file_type = file_type
        output.comment_prefix = file_type.comment_prefix
        output.lines = []
        output.format()
        text = "\n".join(output.lines)
        assert "my-digitalocean-example" in text
        assert "sync_databases" in text
        assert "api_token" in text
        assert "region_mapping" in text


def test_configured_source_is_instantiated(digitalocean):
    inventory, _ = digitalocean

    source, = instantiate_sources()

    assert isinstance(source, DigitalOceanHandler)
    assert source.init_successful
    assert source.name == "cloud"
    assert inventory.source_list == [source]


def test_settings_example_includes_all_digitalocean_options():
    parser = INIParser()
    text = (Path(__file__).parents[1] / "settings-example.ini").read_text()
    parser.read_string(text)
    section_name = f"source/{DigitalOceanConfig.source_name_example}"

    assert section_name in parser
    assert "source/my-aws-example" in parser
    section = text.split(f"[{section_name}]", 1)[1]
    for option in DigitalOceanConfig().options:
        assert any(
            line.lstrip(";").startswith(f"{option.key} =")
            for line in section.splitlines()
        ), option.key


@pytest.mark.parametrize("mapping, expected", [
    (None, {}),
    ("fra1=Germany, nyc1=United States", {"fra1": "Germany", "nyc1": "United States"}),
    (' fra1 = "Germany", nyc1 = "New York, USA" ', {"fra1": "Germany", "nyc1": "New York, USA"}),
])
def test_region_mapping_configuration(mapping, expected):
    config = DigitalOceanConfig()
    config.source_name = "cloud"
    config.config_content = {"source": {"cloud": {"api_token": "test-token", "region_mapping": mapping}}}

    assert config.parse(do_log=False).region_mapping == expected


@pytest.mark.parametrize("mapping", [
    "fra1", "=Germany", "fra1=", "fra1=Germany,",
    "fra1=Germany,fra1=Europe", ["fra1=Germany"], "fra1=" + "x" * 101,
])
def test_invalid_region_mapping_is_rejected(mapping, caplog):
    config = DigitalOceanConfig()
    config.source_name = "cloud"
    config.config_content = {"source": {"cloud": {"api_token": "test-token", "region_mapping": mapping}}}

    with pytest.raises(SystemExit):
        config.parse(do_log=False)

    assert "region_mapping" in caplog.text


@pytest.mark.parametrize("region, name, slug", [
    ({"slug": "fra1", "name": "Frankfurt 1"}, "Frankfurt 1", "fra1"),
    ("fra1", "fra1", "fra1"),
])
def test_missing_region_is_created_and_reused(digitalocean, region, name, slug, caplog):
    inventory, _ = digitalocean
    source = DigitalOceanHandler("cloud")

    cluster = source._get_cluster(region, None, None, {})
    netbox_region = cluster.data["scope_id"]
    assert netbox_region.data["name"] == name
    assert netbox_region.data["slug"] == slug
    assert netbox_region.read_only is False
    assert {"name", "slug"} <= set(netbox_region.updated_items)
    assert cluster.data["scope_type"] == NBRegion.object_type
    assert source._get_cluster(region, None, None, {}) is cluster
    assert len(inventory.get_all_items(NBRegion)) == 1
    assert "Unable to determine second key" not in caplog.text


@pytest.mark.parametrize("existing_name, existing_slug", [
    ("Germany", "de"), ("Federal Republic", "germany"), (None, None),
])
def test_region_mapping_overrides_provider_match(digitalocean, existing_name, existing_slug):
    inventory, _ = digitalocean
    provider_region = inventory.add_object(
        NBRegion, data={"name": "Frankfurt 1", "slug": "fra1"}, read_from_netbox=True,
    )
    existing = None
    if existing_name is not None:
        existing = inventory.add_object(
            NBRegion, data={"name": existing_name, "slug": existing_slug}, read_from_netbox=True,
        )
    source = DigitalOceanHandler("cloud")
    source.settings.region_mapping = {"fra1": "Germany", "fra2": "Germany"}
    cluster = source._get_cluster({"slug": "fra1", "name": "Frankfurt 1"}, None, None, {})

    mapped_region = cluster.data["scope_id"]
    assert mapped_region is not provider_region
    if existing is not None:
        assert mapped_region is existing
        assert mapped_region.data["name"] == existing_name
        assert mapped_region.data["slug"] == existing_slug
        assert not mapped_region.updated_items
    else:
        assert mapped_region.data["name"] == "Germany"
        assert mapped_region.data["slug"] == "germany"
    other_cluster = source._get_cluster({"slug": "fra2", "name": "Frankfurt 2"}, None, None, {})
    assert other_cluster.data["scope_id"] is mapped_region
    assert len(inventory.get_all_items(NBRegion)) == 2


@pytest.mark.parametrize("previously_unscoped", [True, False])
def test_existing_source_cluster_is_rescoped_without_duplicates(digitalocean, previously_unscoped):
    inventory, _ = digitalocean
    source = DigitalOceanHandler("cloud")
    source.apply()
    cluster = inventory.get_all_items(NBCluster)[0]
    original_vm = inventory.get_all_items(NBVM)[0]
    if previously_unscoped:
        cluster.data.pop("scope_id")
        cluster.data.pop("scope_type")

    source.settings.region_mapping = {"nyc1": "United States"}
    source.apply()

    assert inventory.get_all_items(NBCluster) == [cluster]
    assert original_vm.data["cluster"] is cluster
    assert cluster.data["scope_id"].data["name"] == "United States"


def test_old_netbox_does_not_create_region_scopes(digitalocean):
    inventory, _ = digitalocean
    inventory.netbox_api_version = "4.1.11"
    source = DigitalOceanHandler("cloud")
    source.settings.region_mapping = {"fra1": "Germany"}

    cluster = source._get_cluster({"slug": "fra1", "name": "Frankfurt 1"}, None, None, {})

    assert "scope_id" not in cluster.data
    assert "scope_type" not in cluster.data
    assert not inventory.get_all_items(NBRegion)


@pytest.mark.parametrize("name, slug", [
    ("Frankfurt 1", "frankfurt"), ("Existing Frankfurt", "fra1"), ("fra1", "existing"),
])
def test_unmapped_region_reuses_existing_name_or_slug(digitalocean, name, slug):
    inventory, _ = digitalocean
    existing = inventory.add_object(
        NBRegion, data={"name": name, "slug": slug}, read_from_netbox=True,
    )
    source = DigitalOceanHandler("cloud")

    cluster = source._get_cluster({"slug": "fra1", "name": "Frankfurt 1"}, None, None, {})

    assert cluster.data["scope_id"] is existing
    assert existing.data["name"] == name
    assert existing.data["slug"] == slug
    assert not existing.updated_items
    assert len(inventory.get_all_items(NBRegion)) == 1


def test_region_is_written_before_cluster_with_integer_scope_id(digitalocean):
    inventory, _ = digitalocean
    source = DigitalOceanHandler("cloud")
    cluster = source._get_cluster({"slug": "fra1", "name": "Frankfurt 1"}, None, None, {})
    region = cluster.data["scope_id"]
    handler = NetBoxHandler.__new__(NetBoxHandler)
    handler.inventory = inventory
    handler.request = Mock(side_effect=lambda object_type, req_type, data, nb_id: {
        **data, "id": 101 if object_type is NBRegion else 201,
    })

    handler.update_instance()

    calls = handler.request.call_args_list
    assert [call.args[0] for call in calls] == [NBRegion, NBCluster]
    assert all(call.kwargs["req_type"] == "POST" for call in calls)
    assert calls[0].kwargs["data"]["name"] == "Frankfurt 1"
    assert calls[1].kwargs["data"]["scope_id"] == 101
    assert calls[1].kwargs["data"]["scope_type"] == NBRegion.object_type
    assert cluster.data["scope_id"] is region
    assert not region.is_new
    assert not cluster.is_new
    assert not region.updated_items
    assert not cluster.updated_items
    handler.request.reset_mock()
    handler.update_instance()
    handler.request.assert_not_called()


def test_ambiguous_source_clusters_are_rejected(digitalocean, caplog):
    inventory, _ = digitalocean
    source = DigitalOceanHandler("cloud")
    source.apply()
    cluster = inventory.get_all_items(NBCluster)[0]
    inventory.add_object(NBCluster, data=dict(cluster.data), read_from_netbox=True)

    with pytest.raises(ValueError, match="Multiple DigitalOcean clusters"):
        source.apply()

    assert "Multiple DigitalOcean clusters" in caplog.text
    assert len(inventory.get_all_items(NBCluster)) == 2


def test_digitalocean_discovery_maps_droplets_volumes_and_database_clusters(digitalocean):
    inventory, client = digitalocean
    inventory.add_object(
        NBRegion,
        data={"name": "New York 1", "slug": "nyc1"},
        read_from_netbox=True,
    )
    source = DigitalOceanHandler("cloud")
    assert source.init_successful
    inventory.resolve_relations()
    source.apply()

    client.close.assert_called_once()
    assert [call.args for call in client.get_collection.call_args_list] == [
        ("droplets", "droplets"),
        ("volumes", "volumes"),
        ("databases", "databases"),
    ]
    assert len(inventory.get_all_items(NBCluster)) == 1
    assert len(inventory.get_all_items(NBClusterGroup)) == 1
    cluster = inventory.get_all_items(NBCluster)[0]
    assert cluster.data["scope_type"] == NBRegion.object_type
    assert cluster.data["scope_id"].data["slug"] == "nyc1"
    assert len(inventory.get_all_items(NBVM)) == 2
    assert len(inventory.get_all_items(NBVMInterface)) == 2
    assert len(inventory.get_all_items(NBIPAddress)) == 3
    assert len(inventory.get_all_items(NBVirtualDisk)) == 3

    droplet = next(vm for vm in inventory.get_all_items(NBVM) if vm.data["name"] == "web-1")
    assert droplet.data["status"] == "active"
    assert droplet.data["serial"] == "123"
    assert droplet.data["vcpus"] == 2
    assert droplet.data["memory"] == 2048
    assert droplet.data["platform"].data["name"] == "Ubuntu 24.04"
    assert droplet.data["primary_ip4"].data["address"] == "203.0.113.10/24"
    assert droplet.data["primary_ip6"].data["address"] == "2001:db8::5/64"
    assert {tag.data["name"] for tag in droplet.data["tags"]} == {"production", "web"}
    assert {
        disk.data["name"]: disk.data["size"]
        for disk in inventory.get_all_items(NBVirtualDisk)
        if disk.data["virtual_machine"] is droplet
    } == {"root": 25600, "do-volume-1": 20480}

    database = next(vm for vm in inventory.get_all_items(NBVM) if vm.data["name"] == "db-orders")
    assert database.data["status"] == "active"
    assert database.data["serial"] == "database-uuid"
    assert database.data["platform"].data["name"] == "pg 16"
    assert database.data["custom_fields"]["digitalocean_resource_type"] == "database"
    assert database.data["custom_fields"]["digitalocean_engine"] == "pg"
    assert database.data["custom_fields"]["digitalocean_version"] == "16"
    assert database.data["custom_fields"]["digitalocean_node_count"] == "2"
    assert database.data["custom_fields"]["digitalocean_endpoint"] == "db-private.example.org"
    assert database.data["custom_fields"]["digitalocean_port"] == "25060"
    assert "secret" not in repr(database.data)
    assert len(inventory.get_all_items(NBCustomField)) == 8
    assert {tag.data["name"] for tag in inventory.get_all_items(NBTag)} == {
        "production", "web",
    }

    source.apply()
    assert len(inventory.get_all_items(NBCluster)) == 1
    assert len(inventory.get_all_items(NBVM)) == 2
    assert len(inventory.get_all_items(NBVirtualDisk)) == 3
    assert len(inventory.get_all_items(NBPlatform)) == 2
    assert len(inventory.get_all_items(NBClusterGroup)) == 1
    assert len(inventory.get_all_items(NBTag)) == 2


def test_same_named_droplets_in_different_clusters_keep_their_children(digitalocean):
    inventory, client = digitalocean
    droplets = [
        {
            "id": index,
            "name": "prod-netops-FE-2",
            "status": status,
            "region": {"slug": slug, "name": region_name},
            "disk": 25,
            "networks": {"v4": [
                {"ip_address": address, "cidr": 24, "type": "public"},
            ]},
        }
        for index, slug, region_name, address, status in [
            (1, "nyc3", "New York 3", "203.0.113.1", "off"),
            (2, "nyc1", "New York 1", "203.0.113.2", "active"),
        ]
    ]
    client.get_collection.side_effect = lambda path, key: droplets if path == "droplets" else []
    source = DigitalOceanHandler("cloud")

    for _ in range(2):
        source.apply()
        vms = inventory.get_all_items(NBVM)
        assert len(vms) == 2
        assert len(inventory.get_all_items(NBVMInterface)) == 2
        assert len(inventory.get_all_items(NBVirtualDisk)) == 2
        for vm in vms:
            ip = vm.data["primary_ip4"]
            interface = ip.data["assigned_object_id"]
            assert interface.data["virtual_machine"] is vm
            assert len(vm.get_virtual_disks()) == 1
            assert vm.get_virtual_disks()[0].data["virtual_machine"] is vm


@pytest.mark.parametrize("object_type", [NBVMInterface, NBVirtualDisk])
def test_vm_children_match_resolved_parent_not_duplicate_name(inventory, object_type):
    clusters = [
        inventory.add_object(NBCluster, data={"name": name}, read_from_netbox=True)
        for name in ("New York 3", "New York 1")
    ]
    vms = [
        inventory.add_object(NBVM, data={"name": "same-name", "cluster": cluster}, read_from_netbox=True)
        for cluster in clusters
    ]
    children = [
        inventory.add_object(
            object_type, data={"name": "public", "virtual_machine": vm}, read_from_netbox=True,
        )
        for vm in vms
    ]

    for vm, child in zip(vms, children):
        assert inventory.add_update_object(
            object_type, data={"name": "PUBLIC", "virtual_machine": vm},
        ) is child
        assert child.data["virtual_machine"] is vm
        assert "virtual_machine" not in child.updated_items


@pytest.mark.parametrize("ownership", ["same-tag", "same-run", "other-tag", "unknown", "mixed-tags", "other-source"])
def test_ip_conflict_repairs_only_confirmed_same_source(digitalocean, ownership, caplog):
    inventory, _ = digitalocean
    source = DigitalOceanHandler("cloud")
    vms = [
        inventory.add_object(NBVM, data={"name": name}, read_from_netbox=True)
        for name in ("old-vm", "new-vm")
    ]
    interfaces = [
        inventory.add_object(
            NBVMInterface, data={"name": "public", "virtual_machine": vm}, read_from_netbox=True,
        )
        for vm in vms
    ]
    ip = inventory.add_object(NBIPAddress, data={
        "id": 19223, "address": "203.0.113.1/24",
        "assigned_object_type": NBVMInterface.object_type, "assigned_object_id": interfaces[0],
    }, read_from_netbox=True)
    if ownership in ("same-tag", "mixed-tags", "other-source"):
        ip.add_tags(source.source_tag)
    if ownership in ("other-tag", "mixed-tags"):
        ip.add_tags("Source: another-account")
    if ownership == "same-run":
        ip.source = source
    if ownership == "other-source":
        ip.source = Mock()
    vms[0].data["primary_ip4"] = ip

    if ownership in ("same-tag", "same-run"):
        assert source._sync_ip(ip_interface("203.0.113.1/24"), interfaces[1]) is ip
        assert ip.data["assigned_object_id"] is interfaces[1]
        assert ip.source is source
        assert "primary_ip4" in vms[0].unset_items
    else:
        assert source._sync_ip(ip_interface("203.0.113.1/24"), interfaces[1]) is None
        assert "ownership" in caplog.text
        assert ip.data["assigned_object_id"] is interfaces[0]
        assert vms[0].data["primary_ip4"] is ip
        assert "primary_ip4" not in vms[0].unset_items
        if ownership == "mixed-tags":
            assert ip.source is source
            assert "Source: another-account" in ip.get_tags()
            inventory.source_list = [source]
            inventory.tag_all_the_things(Mock(
                primary_tag="NetBox-synced", orphaned_tag="NetBox-synced: Orphaned",
            ))
            assert "NetBox-synced: Orphaned" not in ip.get_tags()
        elif ownership == "other-source":
            assert ip.source is not source
        else:
            assert ip.source is None


def test_repaired_ip_is_not_orphaned(digitalocean):
    inventory, _ = digitalocean
    source = DigitalOceanHandler("cloud")
    source.apply()
    ip = inventory.get_by_data(NBIPAddress, data={"address": "203.0.113.10/24"})
    target_interface = ip.data["assigned_object_id"]
    wrong_vm = inventory.add_object(NBVM, data={"name": "wrong-vm"}, read_from_netbox=True)
    wrong_interface = inventory.add_object(
        NBVMInterface, data={"name": "public", "virtual_machine": wrong_vm}, read_from_netbox=True,
    )
    ip.data["assigned_object_id"] = wrong_interface
    ip.source = None
    ip.add_tags(["NetBox-synced", source.source_tag, "NetBox-synced: Orphaned"])
    inventory.source_list = [source]

    source.apply()
    handler = Mock(
        primary_tag="NetBox-synced", orphaned_tag="NetBox-synced: Orphaned",
    )
    inventory.tag_all_the_things(handler)

    assert ip.data["assigned_object_id"] is target_interface
    assert ip.source is source
    assert source.source_tag in ip.get_tags()
    assert "NetBox-synced: Orphaned" not in ip.get_tags()
    assert target_interface.data["virtual_machine"].data["primary_ip4"] is ip


@pytest.mark.parametrize("second_prefix", [24, 32])
def test_conflicting_droplet_ip_claims_skip_and_continue(digitalocean, second_prefix, caplog):
    inventory, client = digitalocean
    droplets = [
        {"id": index, "name": name, "region": "nyc1", "networks": {
            "v4": [{"ip_address": "203.0.113.1", "cidr": prefix, "type": "public"}],
        }}
        for index, name, prefix in [(1, "first", 24), (2, "second", second_prefix), (3, "third", 24)]
    ]
    droplets[2]["networks"]["v4"][0]["ip_address"] = "203.0.113.2"
    client.get_collection.side_effect = lambda path, key: droplets if path == "droplets" else []
    source = DigitalOceanHandler("cloud")

    source.apply()
    assert "multiple interfaces" in caplog.text

    ip = inventory.get_all_items(NBIPAddress)[0]
    assert ip.data["assigned_object_id"].data["virtual_machine"].data["name"] == "first"
    vms = {vm.data["name"]: vm for vm in inventory.get_all_items(NBVM)}
    assert "primary_ip4" not in vms["second"].data
    assert vms["third"].data["primary_ip4"].data["address"] == "203.0.113.2/24"


def test_ownership_conflict_continues_other_addresses_and_disks(digitalocean, caplog):
    inventory, _ = digitalocean
    source = DigitalOceanHandler("cloud")
    old_vm = inventory.add_object(NBVM, data={"name": "old"}, read_from_netbox=True)
    interface = inventory.add_object(NBVMInterface, data={
        "name": "private", "virtual_machine": old_vm,
    }, read_from_netbox=True)
    existing = inventory.add_object(NBIPAddress, data={
        "id": 1234, "address": "10.10.0.5/24",
        "assigned_object_type": NBVMInterface.object_type, "assigned_object_id": interface,
    }, read_from_netbox=True)
    source.apply()
    assert "Skipping" in caplog.text
    assert existing.data["assigned_object_id"] is interface
    droplet = next(vm for vm in inventory.get_all_items(NBVM) if vm.data["name"] == "web-1")
    assert droplet.data["primary_ip4"].data["address"] == "203.0.113.10/24"
    assert droplet.data["primary_ip6"].data["address"] == "2001:db8::5/64"
    assert len(droplet.get_virtual_disks()) == 2
    assert len(inventory.get_all_items(NBVM)) == 3


def test_api_failure_closes_session_before_inventory_changes(digitalocean):
    inventory, client = digitalocean
    client.get_collection.side_effect = RuntimeError("API request failed")
    source = DigitalOceanHandler("cloud")

    with pytest.raises(RuntimeError, match="API request failed"):
        source.apply()

    client.close.assert_called_once()
    assert not inventory.get_all_items(NBCluster)
    assert not inventory.get_all_items(NBVM)


def test_database_sync_can_be_disabled(digitalocean):
    inventory, client = digitalocean
    source = DigitalOceanHandler("cloud")
    source.settings.sync_databases = False
    source.settings.sync_volumes = False
    source.apply()

    assert [call.args for call in client.get_collection.call_args_list] == [("droplets", "droplets")]
    assert all(vm.data["name"] != "db-orders" for vm in inventory.get_all_items(NBVM))


def test_digitalocean_client_paginates_and_uses_bearer_auth():
    response_one = Mock()
    response_one.json.return_value = {
        "droplets": [{"id": 1}],
        "links": {"pages": {"next": "https://api.digitalocean.com/v2/droplets?page=2"}},
    }
    response_two = Mock()
    response_two.json.return_value = {"droplets": [{"id": 2}], "links": {"pages": {}}}
    session = Mock()
    session.get.side_effect = [response_one, response_two]

    client = DigitalOceanClient("test-token", session=session)

    assert client.get_collection("droplets", "droplets") == [{"id": 1}, {"id": 2}]
    first_call, second_call = session.get.call_args_list
    assert first_call.kwargs["headers"]["Authorization"] == "Bearer test-token"
    assert first_call.kwargs["params"] == {"per_page": 200}
    assert second_call.kwargs["params"] is None


@pytest.mark.parametrize("databases", [None, []])
def test_digitalocean_client_accepts_empty_databases(databases):
    response = Mock()
    response.json.return_value = {"databases": databases}
    session = Mock()
    session.get.return_value = response
    client = DigitalOceanClient("test-token", session=session)

    assert client.get_collection("databases", "databases") == []
    response.raise_for_status.assert_called_once()
    session.get.assert_called_once()


@pytest.mark.parametrize("path, payload", [
    ("databases", {}),
    ("databases", {"databases": {}}),
    ("databases", {"databases": ""}),
    ("databases", {"databases": False}),
    ("droplets", {"droplets": None}),
    ("volumes", {"volumes": None}),
])
def test_digitalocean_client_rejects_invalid_collections(path, payload):
    response = Mock()
    response.json.return_value = payload
    session = Mock()
    session.get.return_value = response
    client = DigitalOceanClient("test-token", session=session)

    with pytest.raises(ValueError, match=f"has no '{path}' list"):
        client.get_collection(path, path)


def test_digitalocean_client_rejects_external_pagination_link():
    response = Mock()
    response.json.return_value = {
        "droplets": [],
        "links": {"pages": {"next": "https://attacker.example/steal"}},
    }
    session = Mock()
    session.get.return_value = response
    client = DigitalOceanClient("test-token", session=session)

    with pytest.raises(ValueError, match="pagination link"):
        client.get_collection("droplets", "droplets")

    session.get.assert_called_once()
