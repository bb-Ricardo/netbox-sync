# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
The NetBox Branching branch schema ID from the config is sent as 'X-NetBox-Branch'
header on the shared NetBox session (and therefore on every API request).
"""
from module.netbox.connection import NetBoxHandler


class _Settings:
    def __init__(self, branch_schema_id):
        self.api_token = "0123456789abcdef0123456789abcdef01234567"
        self.branch_schema_id = branch_schema_id

    def __getattr__(self, name):  # any other setting create_session might look at
        return None


def _headers(branch_schema_id):
    # skip __init__: it parses the config and talks to NetBox
    handler = object.__new__(NetBoxHandler)
    handler.settings = _Settings(branch_schema_id)
    return handler.create_session().headers


def test_no_branch_header_by_default():
    assert "X-NetBox-Branch" not in _headers(None)


def test_branch_schema_id_is_sent_as_header():
    assert _headers("td5smq0f")["X-NetBox-Branch"] == "td5smq0f"


def test_branch_header_does_not_touch_other_headers():
    headers = _headers("td5smq0f")
    assert headers["Authorization"] == "Token 0123456789abcdef0123456789abcdef01234567"
    assert headers["Content-Type"] == "application/json"
