"""
A cluster scoped to a site has to carry the site object, not a name (NetBox wants the
id of the scoped object), and a scope read back from NetBox as an id has to resolve to
that object again, otherwise every run reports a change.
"""
from module.netbox.object_classes import NBCluster, NBClusterType, NBSite


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
