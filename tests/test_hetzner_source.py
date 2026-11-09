# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import copy
import json
import types
from urllib.parse import urlsplit

import pytest
import requests

from module.netbox import (
    NetBoxObject, NBCluster, NBClusterType, NBIPAddress, NBSite, NBTag, NBVirtualDisk, NBVM, NBVMInterface,
)
from module.netbox.connection import NetBoxHandler
from module.sources import instantiate_sources

TOKEN = "test-only"


def hetzner_project():
    """One server with a 10 GB volume attached, answered the way the Hetzner Cloud API does."""
    return {
        "/servers": (200, {
            "servers": [{
                "id": 42, "name": "srv1", "status": "running",
                "public_net": {
                    "ipv4": {"id": 4201, "ip": "203.0.113.10", "blocked": False, "dns_ptr": "srv1.example.com"},
                    "ipv6": None, "floating_ips": [], "firewalls": [],
                },
                "private_net": [],
                "volumes": [7],
            }],
            "meta": {"pagination": {"page": 1, "per_page": 50, "previous_page": None, "next_page": None,
                                    "last_page": 1, "total_entries": 1}},
        }),
        "/volumes/7": (200, {"volume": {"id": 7, "name": "data", "size": 10, "server": 42}}),
    }


@pytest.fixture
def hetzner_api(monkeypatch):
    """
    Stands in for the Hetzner Cloud API behind hcloud. Maps an API token to the
    responses of its project (API path -> (HTTP status, JSON body)), any other
    token gets the 401 the real API answers with.
    """
    projects = dict()

    def request(session, method, url, headers=None, **kwargs):
        project = projects.get(headers["Authorization"].removeprefix("Bearer "))
        if isinstance(project, Exception):
            raise project
        if project is None:
            status, body = 401, {"error": {"code": "unauthorized", "message": "unable to authenticate"}}
        else:
            status, body = project[urlsplit(url).path.removeprefix("/v1")]
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(body).encode()
        return response

    monkeypatch.setattr(requests.Session, "request", request)
    return projects


@pytest.fixture
def hetzner_sources(inventory, load_config, hetzner_api):
    def make(*sources):
        load_config("".join(f"[source/{name}]\ntype = hetzner\napi_token = {token}\n" for name, token in sources))
        return instantiate_sources()
    return make


def test_volume_size_follows_netbox_disk_unit(hetzner_api, hetzner_sources, inventory):
    # NetBox stored virtual disk sizes in GB and switched to MB with 4.1.0
    hetzner_api[TOKEN] = hetzner_project()
    source, = hetzner_sources(("test", TOKEN))

    sizes = dict()
    for api_version in ["3.7.0", "4.0.11", "4.1.0", "4.3.0"]:
        inventory.netbox_api_version = api_version
        source.apply()
        sizes[api_version] = [disk.data["size"] for disk in inventory.get_all_items(NBVirtualDisk)]

    assert sizes == {"3.7.0": [10], "4.0.11": [10], "4.1.0": [10240], "4.3.0": [10240]}


@pytest.mark.parametrize("failure", ["unauthorized", "unreachable"])
def test_unusable_api_skips_source_and_keeps_the_others(hetzner_api, hetzner_sources, inventory, caplog, failure):
    hetzner_api[TOKEN] = hetzner_project()
    if failure == "unreachable":
        hetzner_api["other-token"] = requests.ConnectionError("Failed to resolve 'api.hetzner.cloud'")

    sources = hetzner_sources(("good", TOKEN), ("broken", "other-token"))

    assert [source.name for source in sources] == ["good"]
    # an enabled source which failed to initialize makes netbox-sync disable pruning
    broken, = [source for source in inventory.source_list if source.name == "broken"]
    assert broken.settings.enabled is True and broken.init_successful is False
    reason = {"unauthorized": "unable to authenticate (unauthorized)",
              "unreachable": "Failed to resolve 'api.hetzner.cloud'"}[failure]
    assert any(record.levelname == "ERROR" and "'broken'" in record.getMessage() and reason in record.getMessage()
               for record in caplog.records)


def test_api_error_during_apply_exits_cleanly(hetzner_api, hetzner_sources, caplog):
    project = hetzner_project()
    project["/volumes/7"] = (503, {"error": {"code": "unavailable", "message": "service unavailable"}})
    hetzner_api[TOKEN] = project
    source, = hetzner_sources(("test", TOKEN))

    with pytest.raises(SystemExit) as exit_info:
        source.apply()

    assert exit_info.value.code == 1
    assert ("Hetzner source 'test': service unavailable (unavailable). "
            "No NetBox writes or pruning will be performed.") in caplog.text


@pytest.mark.parametrize("api_version, placement", [
    ("4.1.0", ["site"]),
    ("4.2.0", ["scope_type", "scope_id"]),
    ("4.4.0", ["scope_type", "scope_id"]),
])
def test_cluster_is_placed_at_the_site(hetzner_api, hetzner_sources, inventory, caplog, api_version, placement):
    # NetBox 4.2 replaced the site of a cluster with a scope, and it rejects a scope_id without a scope_type
    inventory.netbox_api_version = api_version
    hetzner_api[TOKEN] = hetzner_project()
    source, = hetzner_sources(("test", TOKEN))

    source.apply()

    cluster, = inventory.get_all_items(NBCluster)
    site, = inventory.get_all_items(NBSite)
    assert [key for key in ("site", "scope_type", "scope_id") if key in cluster.data] == placement
    assert cluster.get_site_name() == site.data["name"]
    assert "Invalid data type" not in caplog.text


def netbox_after_first_run():
    """What a first run of the source 'test' left in NetBox, in the shape the NetBox API returns it."""
    tag_names = [NetBoxHandler.primary_tag, NetBoxHandler.orphaned_tag, "Source: test"]
    tags = {name: {"id": nb_id, "name": name, "slug": NetBoxObject.format_slug(name)}
            for nb_id, name in enumerate(tag_names, start=1)}
    synced = [tags[NetBoxHandler.primary_tag], tags["Source: test"]]
    return {
        NBTag: list(tags.values()),
        NBSite: [{"id": 1, "name": "cloud", "slug": "cloud", "tags": synced}],
        NBClusterType: [{"id": 1, "name": "cloud", "slug": "cloud", "tags": synced}],
        NBCluster: [{"id": 1, "name": "Hetzner: test", "type": {"id": 1}, "scope_type": "dcim.site", "scope_id": 1,
                     "tags": synced}],
        NBVM: [{"id": 1, "name": "srv1", "status": {"value": "active"}, "cluster": {"id": 1}, "site": {"id": 1},
                "tags": synced}],
        NBVMInterface: [{"id": 1, "name": "eth0", "virtual_machine": {"id": 1}, "enabled": True, "tags": synced}],
        NBIPAddress: [{"id": 1, "address": "203.0.113.10/32", "assigned_object_type": "virtualization.vminterface",
                       "assigned_object_id": 1, "tags": synced}],
        NBVirtualDisk: [{"id": 1, "name": "srv1-data", "virtual_machine": {"id": 1}, "size": 10240, "tags": synced}],
    }


@pytest.fixture
def second_run(hetzner_api, hetzner_sources, inventory, tmp_path):
    """
    Runs the source against what the first run left in NetBox, the way netbox-sync does it:
    only the object types the source depends on are read from NetBox.
    """
    def run(project):
        hetzner_api[TOKEN] = project
        source, = hetzner_sources(("test", TOKEN))
        netbox = object.__new__(NetBoxHandler)
        netbox.inventory = inventory
        netbox.settings = types.SimpleNamespace(use_caching=False, prune_enabled=True, orphaned_vm_status=None,
                                                ignore_unknown_source_object_pruning=False)
        netbox.cache_directory = str(tmp_path)
        netbox.resolved_dependencies = set()
        netbox.request = lambda object_class, **kwargs: {
            "results": copy.deepcopy(netbox_after_first_run().get(object_class, []))}

        netbox.query_current_data(source.dependent_netbox_objects)
        inventory.resolve_relations()
        source.apply()
        inventory.tag_all_the_things(netbox)

    return run


def test_second_run_changes_nothing(second_run, inventory):
    second_run(hetzner_project())

    changes = [f"{this_object.name} '{this_object.get_display_name()}': {this_object.updated_items or 'new'}"
               for object_type in NetBoxObject.__subclasses__() for this_object in inventory.get_all_items(object_type)
               if this_object.is_new or this_object.updated_items]
    assert changes == []


def test_server_gone_from_hetzner_is_tagged_orphaned(second_run, inventory):
    project = hetzner_project()
    project["/servers"][1]["servers"] = []

    second_run(project)

    orphaned = [this_object.name for object_type in (NBVM, NBVMInterface, NBIPAddress, NBVirtualDisk)
                for this_object in inventory.get_all_items(object_type)
                if NetBoxHandler.orphaned_tag in this_object.get_tags()]
    assert orphaned == [NBVM.name, NBVMInterface.name, NBIPAddress.name, NBVirtualDisk.name]
