# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Prefix lookup for an IP (issue #579).

An IP is matched against the prefixes of its site first and, if none fits,
against the prefixes without a scope. NBPrefix.matches_site(None) returned
False unconditionally, so a prefix without a scope was never found.

The prefixes are fed in the shape the NetBox API returns them and resolved
like a real run does, so the scope handling of NBPrefix is part of the test.
"""
from ipaddress import ip_address
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import (
    NBCluster, NBClusterType, NBIPAddress, NBLocation, NBPrefix, NBRegion, NBSite, NBSiteGroup, NBVM, NBVMInterface,
    NBVRF,
)
from module.sources.common.source_base import SourceBase


@pytest.fixture
def source(inventory):
    src = SourceBase()
    src.inventory = inventory
    src.name = "test"
    return src


@pytest.fixture
def site(inventory):
    return inventory.add_object(NBSite, data={"id": 1, "name": "site1"}, read_from_netbox=True)


def add_prefix(inventory, prefix, scope=None, **data):
    """a prefix as read from /api/ipam/prefixes/ on NetBox 4.2+, scope given as (type, id)"""
    scope_type, scope_id = scope or (None, None)
    return inventory.add_object(NBPrefix, data={"prefix": prefix, "scope_type": scope_type, "scope_id": scope_id,
                                                **data}, read_from_netbox=True)


def find(source, ip, site_name=None):
    source.inventory.resolve_relations()
    return source.return_longest_matching_prefix_for_ip(ip_address(ip), site_name)


def test_a_prefix_without_a_scope_is_found_without_a_site(inventory, source, site):
    prefix = add_prefix(inventory, "10.40.232.0/22")

    assert find(source, "10.40.232.163") is prefix


def test_a_prefix_without_a_scope_is_not_a_site_prefix(inventory, source, site):
    add_prefix(inventory, "10.40.232.0/22")

    assert find(source, "10.40.232.163", "site1") is None


def test_a_site_prefix_is_not_a_global_one(inventory, source, site):
    prefix = add_prefix(inventory, "10.40.232.0/22", scope=("dcim.site", site.nb_id))

    assert find(source, "10.40.232.163", "site1") is prefix
    assert find(source, "10.40.232.163") is None


def test_the_site_prefix_and_the_global_one_are_told_apart(inventory, source, site):
    global_prefix = add_prefix(inventory, "10.40.0.0/16")
    site_prefix = add_prefix(inventory, "10.40.232.0/22", scope=("dcim.site", site.nb_id))

    assert find(source, "10.40.232.163", "site1") is site_prefix
    assert find(source, "10.40.1.1", "site1") is None
    assert find(source, "10.40.1.1") is global_prefix


def test_the_longest_global_prefix_wins(inventory, source):
    add_prefix(inventory, "192.168.0.0/16")
    longer = add_prefix(inventory, "192.168.10.0/23")

    assert find(source, "192.168.11.214") is longer


def test_a_site_group_prefix_matches_the_sites_of_the_group(inventory, source):
    group = inventory.add_object(NBSiteGroup, data={"id": 5, "name": "dc"}, read_from_netbox=True)
    inventory.add_object(NBSite, data={"id": 1, "name": "site1", "group": {"id": 5, "name": "dc"}},
                         read_from_netbox=True)
    prefix = add_prefix(inventory, "10.40.232.0/22", scope=("dcim.sitegroup", group.nb_id))

    assert find(source, "10.40.232.163", "site1") is prefix
    assert find(source, "10.40.232.163") is None


def test_a_prefix_of_a_region_or_location_is_not_global(inventory, source, site):
    # not matched by site either; that has not changed
    region = inventory.add_object(NBRegion, data={"id": 7, "name": "eu"}, read_from_netbox=True)
    location = inventory.add_object(NBLocation, data={"id": 3, "name": "row 1", "site": {"id": 1, "name": "site1"}},
                                    read_from_netbox=True)
    add_prefix(inventory, "10.40.232.0/22", scope=("dcim.region", region.nb_id))
    add_prefix(inventory, "10.40.0.0/16", scope=("dcim.location", location.nb_id))

    assert find(source, "10.40.232.163", "site1") is None
    assert find(source, "10.40.232.163") is None


def test_a_prefix_whose_scope_is_not_in_the_inventory_is_not_global(inventory, source, site):
    # the scope resolves to None then, the scope type is still set
    add_prefix(inventory, "10.40.232.0/22", scope=("dcim.site", 99))
    add_prefix(inventory, "10.40.0.0/16", scope=("dcim.region", 7))

    assert find(source, "10.40.232.163", "site1") is None
    assert find(source, "10.40.232.163") is None


def test_a_prefix_with_a_site_of_an_old_netbox_is_not_global(inventory, source, site):
    # NetBox before 4.2 reports the site of a prefix directly
    prefix = inventory.add_object(NBPrefix, data={"prefix": "10.40.232.0/22", "site": {"id": 1, "name": "site1"}},
                                  read_from_netbox=True)

    assert find(source, "10.40.232.163", "site1") is prefix
    assert find(source, "10.40.232.163") is None


def vm_interface_at_site(inventory, source, site):
    """a VM interface on a cluster of the given site, and a source ready to sync its IPs"""
    cluster_type = inventory.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)
    cluster = inventory.add_object(NBCluster, data={"name": "c1", "type": cluster_type, "scope_type": "dcim.site",
                                                    "scope_id": site.nb_id}, read_from_netbox=True)
    vm = inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster}, read_from_netbox=True)
    nic = inventory.add_object(NBVMInterface, data={"name": "eth0", "virtual_machine": vm, "enabled": True},
                               read_from_netbox=True)
    inventory.resolve_relations()
    assert cluster.get_site_name() == "site1"
    source.source_tag = "Source: test"
    source.settings = SimpleNamespace(
        skip_fhrp_group_ips=False,
        ip_tenant_inheritance_order=["disabled"],
        disable_vlan_sync=True,
        vlan_group_relation_by_id=None,
        vlan_group_relation_by_name=None,
        vlan_sync_exclude_by_id=None,
        vlan_sync_exclude_by_name=None,
    )
    return vm, nic


def sync_ip(source, vm, nic, address):
    _, ip_objects = source.add_update_interface(
        interface_object=nic, device_object=vm, interface_data={"name": "eth0"}, interface_ips=[address],
        vmware_object=SimpleNamespace(guest=SimpleNamespace(toolsRunningStatus="guestToolsRunning")),
    )
    return ip_objects


def test_an_ip_without_a_length_is_added_with_the_length_of_the_global_prefix(inventory, source, site):
    # the symptom from the report: the IP was skipped with "Unable to add IP address to NetBox"
    vrf = inventory.add_object(NBVRF, data={"id": 2, "name": "lab"}, read_from_netbox=True)
    add_prefix(inventory, "10.40.232.0/22", vrf={"id": 2, "name": "lab"})
    vm, nic = vm_interface_at_site(inventory, source, site)

    ip_objects = sync_ip(source, vm, nic, "10.40.232.163")

    assert [str(ip.data.get("address")) for ip in ip_objects] == ["10.40.232.163/22"]
    assert ip_objects[0].data.get("vrf") is vrf
    assert inventory.get_by_data(NBIPAddress, data={"address": "10.40.232.163/22"}) is ip_objects[0]


def test_the_prefix_of_the_own_site_wins_over_the_global_one(inventory, source, site):
    site_vrf = inventory.add_object(NBVRF, data={"id": 2, "name": "site-vrf"}, read_from_netbox=True)
    inventory.add_object(NBVRF, data={"id": 3, "name": "global-vrf"}, read_from_netbox=True)
    add_prefix(inventory, "10.40.232.0/22", scope=("dcim.site", site.nb_id), vrf={"id": 2, "name": "site-vrf"})
    add_prefix(inventory, "10.40.232.0/22", vrf={"id": 3, "name": "global-vrf"})
    vm, nic = vm_interface_at_site(inventory, source, site)

    ip_objects = sync_ip(source, vm, nic, "10.40.232.163/22")

    assert ip_objects[0].data.get("vrf") is site_vrf
