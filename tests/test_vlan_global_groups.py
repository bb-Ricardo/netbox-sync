# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
A VLAN in a VLAN group without a scope matches neither by site, nor by group (no
scope to match), nor as global (it has a group). Where another tool keeps the VLANs
in such a group, e.g. filled from an IPAM, every discovered VLAN therefore got a
duplicate without site or group. vlan_global_groups names those groups: their VLANs
are reused like global ones, ahead of VLANs with neither site nor group. Unset, the
lookup is unchanged.
"""
from types import SimpleNamespace

from module.netbox.object_classes import NBSite, NBVLAN, NBVLANGroup
from module.sources.common.source_base import SourceBase
from module.sources.vmware.config import VMWareConfig

MINIMAL_CONFIG = """
[netbox]
host_fqdn = netbox.example.com
api_token = xyz

[source/vc]
type = vmware
host_fqdn = vcenter.example.com
username = u
password = p
"""


def _source(inventory, global_groups=None):
    src = SourceBase()
    src.inventory = inventory
    src.settings = SimpleNamespace(vlan_global_groups=global_groups)
    return src


def _site(inventory, name="site1", group=None):
    return inventory.add_object(NBSite, data={"name": name, "slug": name, "group": group}, read_from_netbox=True)


def _group(inventory, name, scope=None):
    data = {"name": name, "slug": name.lower().replace(" ", "-")}
    if scope is not None:
        data.update({"scope_type": "dcim.site", "scope_id": scope})
    return inventory.add_object(NBVLANGroup, data=data, read_from_netbox=True)


def _vlan(inventory, name, vid=891, group=None, site=None):
    return inventory.add_object(NBVLAN, data={"vid": vid, "name": name, "group": group, "site": site},
                                read_from_netbox=True)


def _lookup(src, site, vid=891):
    return src.get_vlan_object_if_exists({"vid": vid, "name": "port-group-891"}, vlan_site=site)


def test_unset_keeps_the_current_lookup(inventory):
    site = _site(inventory)
    ipam = _group(inventory, "ANU")
    _vlan(inventory, "ITS-Automation", group=ipam)
    plain = _vlan(inventory, "its-automation-891")

    assert _lookup(_source(inventory), site) is plain


def test_vlan_in_an_unscoped_group_alone_is_not_reused_by_default(inventory):
    site = _site(inventory)
    _vlan(inventory, "ITS-Automation", group=_group(inventory, "ANU"))

    assert isinstance(_lookup(_source(inventory), site), dict)


def test_named_global_group_is_reused(inventory):
    site = _site(inventory)
    ipam_vlan = _vlan(inventory, "ITS-Automation", group=_group(inventory, "ANU"))

    assert _lookup(_source(inventory, ["ANU"]), site) is ipam_vlan


def test_named_global_group_wins_over_a_vlan_without_site_and_group(inventory):
    site = _site(inventory)
    plain = _vlan(inventory, "its-automation-891")
    ipam_vlan = _vlan(inventory, "ITS-Automation", group=_group(inventory, "ANU"))

    assert _lookup(_source(inventory, ["anu"]), site) is ipam_vlan, "group names match case-insensitively"
    assert plain is not ipam_vlan


def test_vlan_in_a_group_scoped_to_the_site_still_wins(inventory):
    site = _site(inventory)
    _vlan(inventory, "ITS-Automation", group=_group(inventory, "ANU"))
    scoped = _vlan(inventory, "site-891", group=_group(inventory, "Site VLANs", scope=site))

    assert _lookup(_source(inventory, ["ANU"]), site) is scoped


def test_vlan_of_the_site_still_wins(inventory):
    site = _site(inventory)
    _vlan(inventory, "ITS-Automation", group=_group(inventory, "ANU"))
    on_site = _vlan(inventory, "on-site-891", site=site)

    assert _lookup(_source(inventory, ["ANU"]), site) is on_site


def test_a_scoped_group_is_not_made_global_by_naming_it(inventory):
    site, other = _site(inventory, "site1"), _site(inventory, "site2")
    _vlan(inventory, "other-891", group=_group(inventory, "ANU", scope=other))

    assert isinstance(_lookup(_source(inventory, ["ANU"]), site), dict), "a scoped group matches only its scope"


def test_other_vids_are_not_affected(inventory):
    site = _site(inventory)
    _vlan(inventory, "ITS-Automation", group=_group(inventory, "ANU"))

    assert isinstance(_lookup(_source(inventory, ["ANU"]), site, vid=892), dict)


def test_option_is_unset_by_default_and_parses_a_list(load_config):
    load_config(MINIMAL_CONFIG)
    handler = VMWareConfig()
    handler.source_name = "vc"
    assert handler.parse(do_log=False).vlan_global_groups is None

    load_config(MINIMAL_CONFIG + 'vlan_global_groups = ANU, "Campus, Labs"\n')
    handler = VMWareConfig()
    handler.source_name = "vc"
    assert handler.parse(do_log=False).vlan_global_groups == ["ANU", "Campus, Labs"]


def test_first_listed_global_group_wins(inventory):
    site = _site(inventory)
    in_a = _vlan(inventory, "in-a", group=_group(inventory, "A"))
    in_b = _vlan(inventory, "in-b", group=_group(inventory, "B"))

    assert _lookup(_source(inventory, ["A", "B"]), site) is in_a
    assert _lookup(_source(inventory, ["B", "A"]), site) is in_b


def test_first_listed_global_group_wins_whatever_netbox_returns_first(inventory):
    site = _site(inventory)
    in_b = _vlan(inventory, "in-b", group=_group(inventory, "B"))
    in_a = _vlan(inventory, "in-a", group=_group(inventory, "A"))

    assert _lookup(_source(inventory, ["A", "B"]), site) is in_a
    assert _lookup(_source(inventory, ["B", "A"]), site) is in_b


def test_a_group_whose_scope_is_not_in_the_inventory_is_not_global(inventory):
    """The scope resolves to None if its object is not in the inventory, the scope type stays."""

    site = _site(inventory)
    group = inventory.add_object(NBVLANGroup, data={"name": "ANU", "slug": "anu", "scope_type": "dcim.site",
                                                    "scope_id": 4711}, read_from_netbox=True)
    group.resolve_relations()
    assert group.data["scope_id"] is None and group.data["scope_type"] == "dcim.site"
    _vlan(inventory, "ITS-Automation", group=group)

    assert isinstance(_lookup(_source(inventory, ["ANU"]), site), dict)
