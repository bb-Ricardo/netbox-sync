# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""Every boto3 client of the AWS source retries with backoff and has a short connect timeout, and
failures of the SDK itself end up as AWSDiscoveryError like every other discovery failure."""

import ast
from pathlib import Path
from unittest.mock import Mock

import pytest
from botocore.exceptions import EndpointConnectionError, ProfileNotFound

import module.sources.aws.client as aws_client
from module.sources.aws.client import AWSClient, AWSDiscoveryError


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


@pytest.mark.parametrize("method", ["discover", "discover_organization"])
def test_sdk_error_during_discovery_is_a_discovery_error(monkeypatch, method):
    session = Mock(region_name="us-west-2")
    session.client.side_effect = EndpointConnectionError(endpoint_url="https://sts.us-west-2.amazonaws.com")
    monkeypatch.setattr(aws_client.boto3, "Session", Mock(return_value=session))
    client = AWSClient()

    with pytest.raises(AWSDiscoveryError, match="Incomplete AWS discovery"):
        if method == "discover":
            client.discover(["us-west-2"])
        else:
            client.discover_organization(["us-west-2"], "OrganizationAccountAccessRole")


def test_unknown_profile_is_a_discovery_error(monkeypatch):
    monkeypatch.setattr(aws_client.boto3, "Session", Mock(side_effect=ProfileNotFound(profile="nope")))

    with pytest.raises(AWSDiscoveryError, match="Unable to initialize AWS credentials"):
        AWSClient(profile="nope")
