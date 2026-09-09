"""
Source discovery from NBS_SOURCE_* environment variables (issue #517).

A source is identified by NBS_SOURCE_<index>_NAME. An option whose own name ends
in _name (strip_host_domain_name, vlan_sync_exclude_by_name, ...) must not be
mistaken for a source, and each source must only receive its own variables.
"""
import os

import pytest

from module.config.parser import ConfigParser

BASE = {
    "NBS_SOURCE_1_NAME": "my-vcenter",
    "NBS_SOURCE_1_TYPE": "vmware",
    "NBS_SOURCE_1_HOST_FQDN": "vcenter.example.com",
}


def _parse(monkeypatch, tmp_path, env):
    for key in list(os.environ):
        if key.startswith("NBS_"):
            monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config_file = tmp_path / "settings.ini"
    config_file.write_text("")
    parser = ConfigParser()
    parser.file_list.clear()
    parser.content.clear()
    parser.config_errors.clear()
    parser.config_warnings.clear()
    parser.parsing_finished = False
    parser.add_config_file(str(config_file))
    parser.read_config()
    return parser


def test_option_ending_in_name_does_not_become_a_source(monkeypatch, tmp_path):
    # the reporter's case: strip_host_domain_name=true turned into a source called "true"
    parser = _parse(monkeypatch, tmp_path, {**BASE, "NBS_SOURCE_1_STRIP_HOST_DOMAIN_NAME": "true"})
    sources = parser.content["source"]
    assert set(sources) == {"my-vcenter"}, sources
    assert sources["my-vcenter"]["strip_host_domain_name"] == "true"


@pytest.mark.parametrize("option", [
    "strip_vm_domain_name",
    "vlan_sync_exclude_by_name",
    "vlan_group_relation_by_name",
    "overwrite_vm_interface_name",
    "overwrite_device_interface_name",
])
def test_every_name_suffixed_option_is_safe(monkeypatch, tmp_path, option):
    parser = _parse(monkeypatch, tmp_path, {**BASE, f"NBS_SOURCE_1_{option.upper()}": "x"})
    assert set(parser.content["source"]) == {"my-vcenter"}
    assert parser.content["source"]["my-vcenter"][option] == "x"


def test_sources_only_receive_their_own_variables(monkeypatch, tmp_path):
    parser = _parse(monkeypatch, tmp_path, {
        **BASE,
        "NBS_SOURCE_2_NAME": "other",
        "NBS_SOURCE_2_TYPE": "vmware",
        "NBS_SOURCE_2_HOST_FQDN": "other.example.com",
    })
    sources = parser.content["source"]
    assert set(sources) == {"my-vcenter", "other"}
    assert sources["my-vcenter"]["host_fqdn"] == "vcenter.example.com"
    assert sources["other"]["host_fqdn"] == "other.example.com"
    for name, config in sources.items():
        leaked = [key for key in config if key.startswith("nbs_source_")]
        assert not leaked, f"{name} received foreign variables: {leaked}"


def test_variable_without_a_source_name_is_reported(monkeypatch, tmp_path):
    parser = _parse(monkeypatch, tmp_path, {**BASE, "NBS_SOURCE_9_HOST_FQDN": "orphan.example.com"})
    assert any("NBS_SOURCE_9_HOST_FQDN" in warning for warning in parser.config_warnings), parser.config_warnings
