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
from module.sources import valid_sources
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
    assert DigitalOceanConfig in ConfigFileOutput.source_config_list
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
