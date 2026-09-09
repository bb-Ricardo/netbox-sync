"""
The NetBox API token from the config is sent with the right scheme (issue #513).

NetBox 4.5+ tokens (nbt_<key>.<token>) use "Bearer", legacy tokens use "Token".
A scheme typed into the config value itself must not be sent twice.
"""
import pytest

from module.netbox.connection import NetBoxHandler


class _Settings:
    def __init__(self, api_token):
        self.api_token = api_token

    def __getattr__(self, name):  # any other setting create_session might look at
        return None


def _authorization(token):
    # skip __init__: it parses the config and talks to NetBox
    handler = object.__new__(NetBoxHandler)
    handler.settings = _Settings(token)
    return handler.create_session().headers["Authorization"]


def test_legacy_token_uses_the_token_scheme():
    assert _authorization("0123456789abcdef0123456789abcdef01234567") == \
        "Token 0123456789abcdef0123456789abcdef01234567"


def test_v2_token_uses_the_bearer_scheme():
    assert _authorization("nbt_abc.def") == "Bearer nbt_abc.def"


@pytest.mark.parametrize("configured, expected", [
    ("Bearer nbt_abc.def", "Bearer nbt_abc.def"),
    ("bearer nbt_abc.def", "Bearer nbt_abc.def"),
    ("Token 0123abcd", "Token 0123abcd"),
    ("token  0123abcd", "Token 0123abcd"),
])
def test_scheme_in_the_config_value_is_not_sent_twice(configured, expected):
    assert _authorization(configured) == expected
