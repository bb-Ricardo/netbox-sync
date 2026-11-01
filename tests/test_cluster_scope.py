# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
A cluster scoped to a site has to carry the site object, not a name (NetBox wants the
id of the scoped object), and a scope read back from NetBox as an id has to resolve to
that object again, otherwise every run reports a change.
"""
import logging
from datetime import datetime

import pytest

from module.netbox.object_classes import NBCluster, NBClusterType, NBVM, NBSite, NBSiteGroup


def test_cluster_scope_is_resolved_from_an_id(inventory):
    site = inventory.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    site.nb_id = 7
    ctype = inventory.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)

    # this is the shape NetBox hands back for a scoped cluster
    cluster = inventory.add_object(NBCluster, data={
        "name": "c1", "type": ctype, "scope_type": "dcim.site", "scope_id": 7,
    }, read_from_netbox=True)
    cluster.resolve_relations()

    assert cluster.data.get("scope_id") is site, "scope was left as a plain id"


def test_a_site_object_survives_resolving(inventory):
    site = inventory.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    ctype = inventory.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)

    cluster = inventory.add_object(NBCluster, data={
        "name": "c1", "type": ctype, "scope_type": "dcim.site", "scope_id": site,
    }, read_from_netbox=True)
    cluster.resolve_relations()

    assert cluster.data.get("scope_id") is site
    assert cluster.data.get("scope_type") == "dcim.site", "scope type was dropped"


def test_unscoped_cluster_has_no_missing_scope_warning(inventory, caplog):
    cluster = inventory.add_object(NBCluster, data={"name": "global"})

    assert cluster.get_display_name(including_second_key=True) == "global"
    assert "Unable to determine second key 'scope_id'" not in caplog.text


def test_vm_lookup_does_not_serialize_cluster_metadata(inventory):
    cluster = inventory.add_object(NBCluster, data={
        "name": "eks-cluster",
        "custom_fields": {"aws_metadata": {"createdAt": datetime(2026, 10, 23)}},
    })
    vm = inventory.add_object(NBVM, data={"name": "worker", "cluster": cluster})

    assert inventory.get_by_data(NBVM, data={"name": "worker", "cluster": cluster}) is vm


def test_source_scopes_a_cluster_with_the_site_object(vcsim, inventory, load_config, vmware_settings):
    """A dict here is sent to NetBox as is and rejected with 'scope_id: A valid integer is required.'"""
    from module.sources import instantiate_sources

    load_config(vmware_settings + "cluster_site_relation = .* = site1\n")
    source = instantiate_sources()[0]
    assert source.init_successful
    inventory.resolve_relations()
    source.apply()

    clusters = list(inventory.get_all_items(NBCluster))
    assert clusters, "no cluster was synced"
    for cluster in clusters:
        scope = cluster.data.get("scope_id")
        assert isinstance(scope, NBSite), f"scope_id is {type(scope).__name__}, NetBox needs an object it can turn into an id"
        assert scope.get_display_name() == "site1"



def sync_with_scope_relation(inventory, load_config, vmware_settings, scope_id, scope_type="dcim.sitegroup"):
    from module.sources import instantiate_sources

    load_config(vmware_settings + f"cluster_scope_type_relation = .* = {scope_type}\n"
                f"cluster_scope_id_relation = .* = {scope_id}\n")
    source = instantiate_sources()[0]
    assert source.init_successful
    inventory.resolve_relations()
    source.apply()

    clusters = list(inventory.get_all_items(NBCluster))
    assert clusters, "no cluster was synced"
    return clusters


def assert_only_errors(caplog, expected):
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, f"no error logged, expected '{expected}'"
    assert all(expected in message for message in errors), errors


def test_source_scopes_a_cluster_with_the_configured_scope_id(vcsim, inventory, load_config, vmware_settings):
    """The configured scope id is a NetBox id, the cluster has to carry the object it points to"""
    site_group = inventory.add_object(NBSiteGroup, data={"name": "group1"}, read_from_netbox=True)
    site_group.nb_id = 3

    for cluster in sync_with_scope_relation(inventory, load_config, vmware_settings, 3):
        assert cluster.data.get("scope_type") == "dcim.sitegroup"
        assert cluster.data.get("scope_id") is site_group, \
            f"scope_id is {cluster.data.get('scope_id')!r}, the configured id was not resolved"


@pytest.mark.parametrize("scope_id, error", [
    ("99", "Unable to find site group with id '99'"),
    ("group1", "Invalid scope id 'group1'"),
    # str.isdigit() accepts it, int() does not
    ("\u00b3", "Invalid scope id '\u00b3'"),
], ids=["missing", "name", "superscript"])
def test_source_skips_an_invalid_scope_id(vcsim, inventory, load_config, vmware_settings, caplog, scope_id, error):
    site_group = inventory.add_object(NBSiteGroup, data={"name": "group1"}, read_from_netbox=True)
    site_group.nb_id = 3

    clusters = sync_with_scope_relation(inventory, load_config, vmware_settings, scope_id)

    for cluster in clusters:
        assert cluster.data.get("scope_id") is None
        assert cluster.data.get("scope_type") is None, "scope type was set without a scope"
    assert_only_errors(caplog, error)


def test_source_skips_an_unsupported_scope_type(vcsim, inventory, load_config, vmware_settings, caplog):
    """netbox-sync knows 'dcim.device', but a cluster can't be scoped to it"""
    clusters = sync_with_scope_relation(inventory, load_config, vmware_settings, 3, scope_type="dcim.device")

    for cluster in clusters:
        assert cluster.data.get("scope_id") is None
        assert cluster.data.get("scope_type") is None
    # without the check it ends in two 'Invalid data type' errors per cluster
    assert_only_errors(caplog, "Unsupported scope type 'dcim.device'")
