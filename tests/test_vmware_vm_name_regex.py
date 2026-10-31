# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Regression tests for issue #465 (vm_name_regex).

The regex is applied to the vCenter VM name before any other name handling. Its named
group 'name' becomes the NetBox VM name and an optional named group 'description' sets
the VM description. A regex without a 'name' group is rejected at config load time.
"""
import logging
import re
from types import SimpleNamespace

import pytest

from module.common.logging import DEBUG2
from module.netbox.object_classes import NBVM
from module.sources import instantiate_sources
from module.sources.vmware.config import VMWareConfig
from module.sources.vmware.connection import VMWareHandler

EXAMPLE_REGEX = r"^(?P<name>[^ (]+)(?: \((?P<description>.*)\))?$"

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


def _make_source(vm_name_regex=None, strip_vm_domain_name=False):
    # build the handler without its network-bound __init__
    src = object.__new__(VMWareHandler)
    src.settings = SimpleNamespace(
        vm_name_regex=vm_name_regex,
        strip_vm_domain_name=strip_vm_domain_name,
    )
    return src


def _parse_vmware(load_config, option_lines):
    load_config(NETBOX + SOURCE + option_lines, filename="settings.yaml")
    handler = VMWareConfig()
    handler.source_name = "vc"
    return handler.parse(do_log=False)


def test_regex_rewrites_name_and_sets_description():
    src = _make_source(vm_name_regex=re.compile(EXAMPLE_REGEX))

    name, description = src.get_vm_name_and_description("web01 (production)")

    assert name == "web01"
    assert description == "production"


def test_regex_without_description_group_keeps_description_unset():
    src = _make_source(vm_name_regex=re.compile(r"^(?P<name>[^ (]+)(?: \(.*\))?$"))

    name, description = src.get_vm_name_and_description("web01 (production)")

    assert name == "web01"
    assert description is None


def test_non_matching_name_is_used_unchanged(caplog):
    caplog.set_level(DEBUG2, logger="NetBox-Sync")
    src = _make_source(vm_name_regex=re.compile(r"^prefix-(?P<name>.+)$"))

    name, description = src.get_vm_name_and_description("web01")

    assert name == "web01"
    assert description is None
    assert any("does not match vm_name_regex" in record.getMessage() for record in caplog.records)


def test_strip_vm_domain_name_runs_after_the_regex():
    # the regex captures the full name (with domain), strip_vm_domain_name then trims it
    src = _make_source(vm_name_regex=re.compile(EXAMPLE_REGEX), strip_vm_domain_name=True)

    name, description = src.get_vm_name_and_description("web01.example.com (production)")

    assert name == "web01"
    assert description == "production"


def test_name_group_that_takes_no_part_in_the_match_keeps_the_name(caplog):
    # an optional name group matches the whole name but returns None for it
    src = _make_source(vm_name_regex=re.compile(r"^(?P<name>vm[0-9]+)?_?.*$"))

    with caplog.at_level(logging.WARNING, logger="NetBox-Sync"):
        name, description = src.get_vm_name_and_description("web01")

    assert name == "web01"
    assert description is None
    assert any("matched nothing" in record.getMessage() for record in caplog.records)


def test_blank_name_group_keeps_the_name():
    # the documented example with '*' instead of '+' matches an empty name for "(prod)"
    src = _make_source(vm_name_regex=re.compile(r"^(?P<name>[^ (]*)(?: \((?P<description>.*)\))?$"))

    assert src.get_vm_name_and_description(" (prod)") == (" (prod)", "prod")
    assert src.get_vm_name_and_description("   ") == ("   ", None)


def test_empty_description_group_sets_no_description():
    src = _make_source(vm_name_regex=re.compile(EXAMPLE_REGEX))

    assert src.get_vm_name_and_description("web01 ()") == ("web01", None)
    assert src.get_vm_name_and_description("web01 ( )") == ("web01", None)


def test_regex_is_anchored_at_the_start_of_the_name():
    # match(), not search(): a pattern without '^' still has to match from the first character
    src = _make_source(vm_name_regex=re.compile(r"(?P<name>vm\d+)"))

    assert src.get_vm_name_and_description("x-vm002") == ("x-vm002", None)
    assert src.get_vm_name_and_description("vm002-x") == ("vm002", None)


def test_overlong_results_are_reported(caplog):
    src = _make_source(vm_name_regex=re.compile(r"^(?P<name>\S+) (?P<description>.*)$"))

    with caplog.at_level(logging.WARNING, logger="NetBox-Sync"):
        name, description = src.get_vm_name_and_description("n" * 70 + " " + "d" * 210)

    assert name == "n" * 70 and description == "d" * 210
    messages = [record.getMessage() for record in caplog.records]
    assert any("longer than 64" in message for message in messages), messages
    assert any("longer than 200" in message for message in messages), messages


def test_no_regex_leaves_the_name_untouched():
    src = _make_source()

    name, description = src.get_vm_name_and_description("web01.example.com")

    assert name == "web01.example.com"
    assert description is None


def test_vm_name_regex_is_unset_by_default(load_config):
    settings = _parse_vmware(load_config, "")

    # ConfigOptions answers None for any unknown attribute, so make sure the option exists
    assert "vm_name_regex" in vars(settings)
    assert settings.vm_name_regex is None


def test_regex_with_name_group_is_compiled(load_config):
    settings = _parse_vmware(load_config, f"    vm_name_regex: '{EXAMPLE_REGEX}'\n")

    assert settings.vm_name_regex is not None
    assert "name" in settings.vm_name_regex.groupindex


def test_invalid_regex_is_rejected(load_config, caplog):
    caplog.set_level(logging.ERROR, logger="NetBox-Sync")

    with pytest.raises(SystemExit):
        _parse_vmware(load_config, '    vm_name_regex: "(?P<name>"\n')

    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any("regular expression" in message for message in errors), errors


def test_regex_without_name_group_is_rejected(load_config, caplog):
    caplog.set_level(logging.ERROR, logger="NetBox-Sync")

    with pytest.raises(SystemExit):
        _parse_vmware(load_config, '    vm_name_regex: "^foo.*$"\n')

    errors = [record.getMessage() for record in caplog.records if record.levelno == logging.ERROR]
    assert any("named group 'name'" in message for message in errors), errors


#
# end to end through vcsim
#

def test_description_reaches_the_netbox_vm(vcsim, inventory, load_config, vmware_settings):
    """The description group lands on the NBVM object, so it is sent to NetBox."""
    if vcsim.name != "vc001":
        pytest.skip("asserts VM names of the vc001 inventory")

    load_config(vmware_settings + "\nvm_name_regex = ^(?P<name>vm\\d+)_(?P<description>.*)$\n")
    sources = instantiate_sources()
    assert sources and sources[0].init_successful

    inventory.resolve_relations()
    sources[0].apply()

    vms = {vm.data.get("name"): vm for vm in inventory.get_all_items(NBVM)}
    assert "vm002" in vms and "vm002_xyz-veeam" not in vms
    assert vms["vm002"].data.get("description") == "xyz-veeam"
