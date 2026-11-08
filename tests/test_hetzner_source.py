# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import json
from urllib.parse import urlsplit

import pytest
import requests

from module.netbox import NBVirtualDisk
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
