"""
Regression tests for matching a vCenter cluster to an existing NetBox cluster when several
VMware sources are configured (issue #501, related to #472).

Different vCenters commonly carry the same datacenter and cluster names. add_cluster() used
to adopt a same-named cluster which another source had already scoped to a different site.
NetBox rejects the resulting re-scope ("N devices are assigned as hosts for this cluster but
are not in site ...") on every run and the second vCenter's hosts end up attached to the
first vCenter's cluster.

These tests drive the real VMWareHandler.add_datacenter()/add_cluster() against the in-memory
NetBoxInventory with minimal pyVmomi-typed stand-ins, so neither a vCenter nor a NetBox is
needed.
"""
import re
from types import SimpleNamespace

from pyVmomi import vim

from module.netbox.object_classes import NBCluster, NBClusterGroup, NBClusterType, NBSite
from module.sources.vmware.connection import VMWareHandler


class _Datacenter(vim.Datacenter):
    """vim.Datacenter with a plain name and no vCenter connection behind it."""

    def __init__(self, moid, name):
        super().__init__(moid)
        object.__setattr__(self, "_name", name)

    name = property(lambda self: self._name)


class _Cluster(vim.ClusterComputeResource):
    """vim.ClusterComputeResource with a plain name and parent."""

    def __init__(self, moid, name, parent):
        super().__init__(moid)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_parent", parent)

    name = property(lambda self: self._name)
    parent = property(lambda self: self._parent)


def _relation(assigned_name):
    """A parsed '.* = <assigned_name>' relation, or no relation at all."""
    if assigned_name is None:
        return []
    return [{"object_regex": re.compile(".*"), "assigned_name": assigned_name}]


def _make_source(inventory, name, site=None, source_name_as_group=False):
    """
    A VMWareHandler carrying just the state add_datacenter()/add_cluster() touch,
    registered with the inventory like instantiate_sources() does.
    """
    src = VMWareHandler.__new__(VMWareHandler)
    src.inventory = inventory
    src.name = name
    src.source_tag = f"Source: {name}"
    src.site_name = f"vCenter: {name}"
    src.tag_session = None
    src.object_cache = {}
    src.recursion_level = 0
    src.settings = SimpleNamespace(
        set_source_name_as_cluster_group=source_name_as_group,
        strip_host_domain_name=False,
        cluster_include_filter=None,
        cluster_exclude_filter=None,
        cluster_site_relation=_relation(site),
        cluster_scope_type_relation=[],
        cluster_scope_id_relation=[],
        cluster_tenant_relation=[],
        cluster_tag_relation=[],
        cluster_tag_source=None,
    )
    inventory.add_source(src)
    return src


def _sync_cluster(source, dc_name="Datacenter01", cluster_name="Cluster01"):
    """Run one vCenter's datacenter and cluster through the source, return the NBCluster it settled on."""
    datacenter = _Datacenter(f"datacenter-{source.name}", dc_name)
    cluster = _Cluster(f"domain-{source.name}", cluster_name, datacenter)
    source.add_datacenter(datacenter)
    source.add_cluster(cluster)
    return source.get_object_from_cache(cluster)


def _netbox_cluster(inventory, nb_id, name, group_name, site_name, source_tags):
    """A cluster as it looks after being read from NetBox: id, group, site scope and source tags."""
    group = inventory.add_update_object(NBClusterGroup, data={"name": group_name}, read_from_netbox=True)
    site = inventory.add_update_object(NBSite, data={"name": site_name}, read_from_netbox=True)
    cluster_type = inventory.add_update_object(NBClusterType, data={"name": "VMware ESXi"}, read_from_netbox=True)
    cluster = inventory.add_object(NBCluster, data={
        "id": nb_id,
        "name": name,
        "type": cluster_type,
        "group": group,
        "scope_type": "dcim.site",
        "scope_id": site,
    }, read_from_netbox=True)
    cluster.add_tags(["NetBox-synced"] + list(source_tags))
    cluster.updated_items = []
    return cluster


def test_first_run_creates_one_cluster_per_vcenter(inventory):
    # issue #501: both vCenters have Datacenter01/Cluster01, each source scopes it to its own site
    src_a = _make_source(inventory, "vc-sr1", site="SR1")
    src_b = _make_source(inventory, "vc-sr2", site="SR2")

    cluster_a = _sync_cluster(src_a)
    cluster_b = _sync_cluster(src_b)

    assert cluster_b is not cluster_a
    assert len(inventory.get_all_items(NBCluster)) == 2
    assert cluster_a.get_site_name() == "SR1"
    assert cluster_b.get_site_name() == "SR2"


def test_existing_clusters_are_matched_by_site_not_by_shared_datacenter_name(inventory):
    # steady state of #501: both clusters exist in NetBox, the other vCenter's cluster is listed
    # first and shares the cluster group (datacenter name)
    src_a = _make_source(inventory, "vc-sr1", site="SR1")
    src_b = _make_source(inventory, "vc-sr2", site="SR2")
    nb_a = _netbox_cluster(inventory, 3, "Cluster01", "Datacenter01", "SR1", [src_a.source_tag])
    nb_b = _netbox_cluster(inventory, 4, "Cluster01", "Datacenter01", "SR2", [src_b.source_tag])

    assert _sync_cluster(src_b) is nb_b
    assert _sync_cluster(src_a) is nb_a
    assert len(inventory.get_all_items(NBCluster)) == 2
    assert nb_a.get_site_name() == "SR1"
    assert nb_b.get_site_name() == "SR2"


def test_cluster_already_shared_by_both_sources_stays_at_its_site(inventory):
    # the state #501 leaves behind: one cluster tagged by both sources, scoped to the site of the
    # hosts it holds. The source configured for the other site must not try to re-scope it.
    src_a = _make_source(inventory, "vc-sr1", site="SR1")
    src_b = _make_source(inventory, "vc-sr2", site="SR2")
    shared = _netbox_cluster(inventory, 3, "Cluster01", "Datacenter01", "SR2",
                             [src_a.source_tag, src_b.source_tag])

    cluster_a = _sync_cluster(src_a)

    assert cluster_a is not shared
    assert shared.get_site_name() == "SR2"
    assert shared.updated_items == []
    assert _sync_cluster(src_b) is shared


def test_single_source_still_moves_its_cluster_to_a_new_site(inventory):
    # one source whose cluster_site_relation changed: the cluster is re-scoped, not duplicated
    src = _make_source(inventory, "vc", site="SR2")
    existing = _netbox_cluster(inventory, 3, "Cluster01", "Datacenter01", "SR1", [src.source_tag])

    assert _sync_cluster(src) is existing
    assert existing.get_site_name() == "SR2"
    assert len(inventory.get_all_items(NBCluster)) == 1


def test_cluster_of_a_source_no_longer_configured_is_adopted(inventory):
    # a renamed source section: the tag on the cluster belongs to no configured source, so the
    # cluster is adopted and re-scoped as before
    src = _make_source(inventory, "vc-new", site="SR2")
    existing = _netbox_cluster(inventory, 3, "Cluster01", "Datacenter01", "SR1", ["Source: vc-old"])

    assert _sync_cluster(src) is existing
    assert existing.get_site_name() == "SR2"
    assert len(inventory.get_all_items(NBCluster)) == 1


def test_other_sources_cluster_without_site_is_still_adopted(inventory):
    # nothing tells the clusters apart when the first source assigns no site: unchanged behaviour
    src_a = _make_source(inventory, "vc-a")
    src_b = _make_source(inventory, "vc-b", site="SR2")

    cluster_a = _sync_cluster(src_a)

    assert _sync_cluster(src_b) is cluster_a
    assert len(inventory.get_all_items(NBCluster)) == 1
