# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
NetBox Branching support: a configured branch (schema ID, name or ID) is sent as
'X-NetBox-Branch' header on the shared NetBox session (and therefore on every API
request). Name/ID are resolved to the schema ID via the plugin API first, because the
plugin only accepts the schema ID in the header.
"""
import pytest

from module.netbox.config import NetBoxConfig
from module.netbox.connection import NetBoxHandler


class _Settings:
    def __init__(self, branch_schema_id=None, branch_name=None, branch_id=None, use_caching=True):
        self.api_token = "0123456789abcdef0123456789abcdef01234567"
        self.branch_schema_id = branch_schema_id
        self.branch_name = branch_name
        self.branch_id = branch_id
        self.use_caching = use_caching

    def __getattr__(self, name):  # any other setting create_session/setup_branch_header might look at
        return None


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, reason="OK"):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.reason = reason
        self.text = str(self._payload)

    def json(self):
        return self._payload


def _handler(settings):
    # skip __init__: it parses the config and talks to NetBox
    handler = object.__new__(NetBoxHandler)
    handler.settings = settings
    handler.url = "https://netbox.example.com/api/"
    handler.session = handler.create_session()
    return handler


def test_no_branch_header_by_default():
    handler = _handler(_Settings())
    handler.setup_branch_header()
    assert "X-NetBox-Branch" not in handler.session.headers


def test_branch_schema_id_is_sent_as_header_without_api_call():
    handler = _handler(_Settings(branch_schema_id="td5smq0f"))

    def _fail_on_request(*args, **kwargs):
        raise AssertionError("no API call expected when branch_schema_id is set directly")

    handler.session.get = _fail_on_request
    handler.setup_branch_header()
    assert handler.session.headers["X-NetBox-Branch"] == "td5smq0f"


def test_branch_name_is_resolved_to_schema_id():
    handler = _handler(_Settings(branch_name="my-feature-branch"))
    requested = {}

    def _fake_get(url, params=None, **kwargs):
        requested["url"] = url
        requested["params"] = params
        return _FakeResponse(payload={"results": [{"id": 7, "name": "my-feature-branch", "schema_id": "ab12cd34"}]})

    handler.session.get = _fake_get
    handler.setup_branch_header()

    assert requested["url"] == "https://netbox.example.com/api/plugins/branching/branches/"
    assert requested["params"] == {"name": "my-feature-branch"}
    assert handler.session.headers["X-NetBox-Branch"] == "ab12cd34"


def test_branch_id_is_resolved_to_schema_id():
    handler = _handler(_Settings(branch_id=7))
    requested = {}

    def _fake_get(url, params=None, **kwargs):
        requested["url"] = url
        assert params is None
        return _FakeResponse(payload={"id": 7, "name": "my-feature-branch", "schema_id": "ab12cd34"})

    handler.session.get = _fake_get
    handler.setup_branch_header()

    assert requested["url"] == "https://netbox.example.com/api/plugins/branching/branches/7/"
    assert handler.session.headers["X-NetBox-Branch"] == "ab12cd34"


def test_unknown_branch_name_exits():
    handler = _handler(_Settings(branch_name="does-not-exist"))
    handler.session.get = lambda *a, **k: _FakeResponse(payload={"results": []})
    with pytest.raises(SystemExit):
        handler.setup_branch_header()


def test_missing_plugin_api_exits():
    handler = _handler(_Settings(branch_name="my-feature-branch"))
    handler.session.get = lambda *a, **k: _FakeResponse(status_code=404, reason="Not Found")
    with pytest.raises(SystemExit):
        handler.setup_branch_header()


def test_caching_is_disabled_when_a_branch_is_used():
    handler = _handler(_Settings(branch_schema_id="td5smq0f", use_caching=True))
    handler.setup_branch_header()
    assert handler.settings.use_caching is False


def test_branch_header_does_not_touch_other_headers():
    handler = _handler(_Settings(branch_schema_id="td5smq0f"))
    handler.setup_branch_header()
    assert handler.session.headers["Authorization"] == "Token 0123456789abcdef0123456789abcdef01234567"
    assert handler.session.headers["Content-Type"] == "application/json"


def _config_with(**values):
    config = NetBoxConfig()
    for key, value in values.items():
        config.get_option_by_name(key).set_value(value)
    config.validate_options()
    return config


def test_single_branch_option_is_valid():
    assert _config_with(branch_name="my-feature-branch")._parsing_failed is False


@pytest.mark.parametrize("values", [
    {"branch_schema_id": "td5smq0f", "branch_name": "my-feature-branch"},
    {"branch_schema_id": "td5smq0f", "branch_id": 7},
    {"branch_name": "my-feature-branch", "branch_id": 7},
    {"branch_schema_id": "td5smq0f", "branch_name": "my-feature-branch", "branch_id": 7},
])
def test_multiple_branch_options_are_rejected(values):
    assert _config_with(**values)._parsing_failed is True
