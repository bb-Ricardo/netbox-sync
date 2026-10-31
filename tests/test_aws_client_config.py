# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""Every boto3 client of the AWS source retries with backoff and has a short connect timeout."""

import ast
from pathlib import Path

import module.sources.aws.client as aws_client


def test_boto_config_values():
    config = aws_client.BOTO_CONFIG

    assert config.retries == {"mode": "standard", "max_attempts": 5}
    assert config.connect_timeout == 10
    assert config.read_timeout == 60


def test_every_client_is_created_with_the_config():
    tree = ast.parse(Path(aws_client.__file__).read_text(encoding="utf-8"))

    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "client"
        and isinstance(node.func.value, ast.Attribute) and node.func.value.attr == "session"
    ]

    assert calls, "no session.client() calls found, the check needs updating"
    without_config = [call.lineno for call in calls if "config" not in {kw.arg for kw in call.keywords}]
    assert not without_config, f"session.client() without config= at lines {without_config}"
