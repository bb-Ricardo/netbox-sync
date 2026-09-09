"""
permitted_subnets through the real config path (issue #482):
ConfigParser -> VMWareConfig.parse() -> PermittedSubnets -> .permitted()

A YAML config may express the option as a list, which used to raise a bare
ValueError out of config parsing. Any other unsupported type, a malformed entry
and the option placed at the wrong nesting level have to fail config validation
with a message naming the offending value, never silently drop every IP.
"""
import logging

import pytest

from module.sources.vmware.config import VMWareConfig

IP = "172.26.34.15/24"

NETBOX = """
netbox:
  host_fqdn: netbox.example.com
  api_token: xyz
"""
SOURCE = """
source:
  vc:
    type: vmware
    host_fqdn: vcenter.example.com
    username: u
    password: p
"""


def _parse_vmware(load_config, option_lines: str):
    load_config(NETBOX + SOURCE + option_lines, filename="settings.yaml")
    handler = VMWareConfig()
    handler.source_name = "vc"
    return handler.parse(do_log=False)


@pytest.mark.parametrize("value", [
    "172.16.0.0/12, 10.0.0.0/8, 192.168.0.0/16",
    "[172.16.0.0/12, 10.0.0.0/8, 192.168.0.0/16]",
    "\n      - 172.16.0.0/12\n      - 10.0.0.0/8\n      - 192.168.0.0/16",
], ids=["comma-string", "flow-list", "block-list"])
def test_yaml_string_and_list_forms_permit_the_same_subnets(load_config, value):
    settings = _parse_vmware(load_config, f"    permitted_subnets: {value}\n")
    assert settings.permitted_subnets.permitted(IP) is True
    assert settings.permitted_subnets.permitted("8.8.8.8/32") is False


def test_unsupported_value_type_fails_validation(load_config, caplog):
    caplog.set_level(logging.ERROR, logger="NetBox-Sync")
    with pytest.raises(SystemExit):
        _parse_vmware(load_config, "    permitted_subnets:\n      first: 10.0.0.0/8\n")
    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any("dict" in message for message in errors), errors


def test_malformed_entry_fails_validation_naming_the_entry(load_config, caplog):
    caplog.set_level(logging.ERROR, logger="NetBox-Sync")
    with pytest.raises(SystemExit):
        _parse_vmware(load_config, "    permitted_subnets: 172.16.0.0/12, 10.0.0.0/8x\n")
    assert any("10.0.0.0/8x" in record.getMessage() for record in caplog.records), caplog.text


def test_option_next_to_the_source_name_is_a_config_error(load_config):
    parser = load_config(NETBOX + SOURCE + "  permitted_subnets: 10.0.0.0/8\n", filename="settings.yaml")
    assert any("permitted_subnets" in error for error in parser.config_errors), parser.config_errors
