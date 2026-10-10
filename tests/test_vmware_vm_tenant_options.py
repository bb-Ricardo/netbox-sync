# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
VM tenant assignment (issues #531, #463 and #454): the tenant of a VM can be resolved
from the resource pool or the VM folder it is organized in, can be inherited from the
cluster and an existing tenant in NetBox can be protected from being overwritten. VMs
can also be filtered by their resource pool (issue #494).

These file contains unit tests driving the real VMWareHandler against the in-memory
NetBoxInventory with pyVmomi-typed stand-ins, so neither a vCenter nor a NetBox is
needed. The vcsim-backed tests in test_vmware_vm_tenant_vcsim.py cover the same
behaviour against a captured inventory.
"""
import re
from types import SimpleNamespace

from pyVmomi import vim

from module.netbox.object_classes import NBCluster, NBClusterType, NBSite, NBTenant, NBVM
from module.sources.vmware.config import VMWareConfig
from module.sources.vmware.connection import VMWareHandler

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


class _Folder(vim.Folder):
    """vim.Folder with a plain name and parent, no vCenter connection behind it."""

    def __init__(self, mo_id, name, parent=None):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_parent", parent)

    name = property(lambda self: self._name)
    parent = property(lambda self: self._parent)


class _Datacenter(vim.Datacenter):
    """vim.Datacenter with a plain name, no vCenter connection behind it."""

    def __init__(self, mo_id, name):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)

    name = property(lambda self: self._name)


class _ResourcePool(vim.ResourcePool):
    """vim.ResourcePool with a plain name and parent, no vCenter connection behind it."""

    def __init__(self, mo_id, name, parent=None):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_parent", parent)

    name = property(lambda self: self._name)
    parent = property(lambda self: self._parent)


class _VirtualMachine(vim.VirtualMachine):
    """vim.VirtualMachine with a plain name, folder and resource pool."""

    def __init__(self, mo_id, name, parent=None, resource_pool=None):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_parent", parent)
        object.__setattr__(self, "_resource_pool", resource_pool)

    name = property(lambda self: self._name)
    parent = property(lambda self: self._parent)
    resourcePool = property(lambda self: self._resource_pool)


# a small inventory tree:
#   datacenter/vm folder/Dept-A/Prod
#   cluster root pool "Resources"/Parent/Child
ROOT_POOL = _ResourcePool("resgroup-1", "Resources")
PARENT_POOL = _ResourcePool("resgroup-2", "Parent", parent=ROOT_POOL)
CHILD_POOL = _ResourcePool("resgroup-3", "Child", parent=PARENT_POOL)

DC = _Datacenter("datacenter-1", "DC1")
VM_FOLDER = _Folder("folder-1", "vm", parent=DC)
DEPT_A_FOLDER = _Folder("folder-2", "Dept-A", parent=VM_FOLDER)
PROD_FOLDER = _Folder("folder-3", "Prod", parent=DEPT_A_FOLDER)


def _relation(*pairs):
    """Relations in the shape the config parser hands them to the handler."""
    return [{"object_regex": re.compile(regex), "assigned_name": assigned} for regex, assigned in pairs]


def _make_source(inventory, **overrides):
    # build the handler without its network-bound __init__
    src = object.__new__(VMWareHandler)
    src.inventory = inventory
    src.name = "test"
    src.source_tag = "Source: test"
    src.object_cache = dict()
    src.object_path_cache = dict()
    settings = {
        "match_host_by_serial": True,
        "match_vm_by_serial": True,
        "match_vm_by_mac_address": True,
        "match_vm_by_ip_address": True,
        "overwrite_device_platform": True,
        "overwrite_vm_platform": True,
        "overwrite_vm_tenant": True,
        "host_role_relation": [],
        "vm_role_relation": [],
        "host_interface_exclude_filter": None,
        "vm_interface_exclude_filter": None,
        "set_primary_ip": "when-undefined",
        "vm_exclude_disk_sync": None,
        "vm_exclude_disk_sync_by_tag": None,
        "vm_status_on_create": None,
        "vm_status_preserve": None,
        "vm_tenant_relation": [],
        "vm_tenant_resource_pool_relation": [],
        "vm_tenant_folder_relation": [],
        "cluster_tenant_relation": [],
        "vm_tenant_inherit_from_cluster": False,
        "vm_include_by_resource_pool_filter": None,
        "vm_exclude_by_resource_pool_filter": None,
    }
    settings.update(overrides)
    src.settings = SimpleNamespace(**settings)
    return src


def _cluster(inventory, tenant_name=None):
    site = inventory.add_object(NBSite, data={"name": "site1"}, read_from_netbox=True)
    ctype = inventory.add_object(NBClusterType, data={"name": "vmware"}, read_from_netbox=True)
    data = {"name": "c1", "type": ctype, "scope": site}
    if tenant_name is not None:
        data["tenant"] = {"name": tenant_name}
    return inventory.add_object(NBCluster, data=data, read_from_netbox=True)


def _vm(mo_id, name, folder=None, pool=None):
    return _VirtualMachine(mo_id, name, parent=folder, resource_pool=pool)


def _tenant_of(vm):
    tenant = vm.data.get("tenant")
    return None if tenant is None else tenant.get_display_name()


def _sync_vm(src, cluster, name="vm1", tenant=None):
    # the handler stores the object in the inventory, it does not hand it back
    object_data = {"name": name, "cluster": cluster, "status": "active"}
    if tenant is not None:
        object_data["tenant"] = {"name": tenant}
    src.add_device_vm_to_inventory(NBVM, object_data=object_data,
                                   pnic_data=dict(), vnic_data=dict())
    return src.inventory.get_by_data(NBVM, data={"name": name, "cluster": cluster})


#
# config parsing
#

def test_new_tenant_and_filter_options_are_unset_by_default(load_config):
    """A config that does not mention them must leave today's behaviour in place."""
    load_config(MINIMAL_CONFIG)
    handler = VMWareConfig()
    handler.source_name = "vc"
    settings = handler.parse(do_log=False)

    assert not settings.vm_tenant_resource_pool_relation
    assert not settings.vm_tenant_folder_relation
    assert settings.vm_tenant_inherit_from_cluster is False
    assert settings.overwrite_vm_tenant is True
    assert settings.vm_include_by_resource_pool_filter is None
    assert settings.vm_exclude_by_resource_pool_filter is None


def test_new_tenant_and_filter_options_are_parsed(load_config):
    load_config(MINIMAL_CONFIG + """
vm_tenant_resource_pool_relation = Resources/Customers.* = Customer A
vm_tenant_folder_relation = Dept-A/Prod = Customer B
vm_tenant_inherit_from_cluster = True
overwrite_vm_tenant = False
vm_exclude_by_resource_pool_filter = POWERED OFF
vm_include_by_resource_pool_filter = ^Resources
""")
    handler = VMWareConfig()
    handler.source_name = "vc"
    settings = handler.parse(do_log=False)

    assert settings.vm_tenant_resource_pool_relation == \
        [{"object_regex": re.compile("Resources/Customers.*"), "assigned_name": "Customer A"}]
    assert settings.vm_tenant_folder_relation == \
        [{"object_regex": re.compile("Dept-A/Prod"), "assigned_name": "Customer B"}]
    assert settings.vm_tenant_inherit_from_cluster is True
    assert settings.overwrite_vm_tenant is False
    assert settings.vm_exclude_by_resource_pool_filter.pattern == "POWERED OFF"
    assert settings.vm_include_by_resource_pool_filter.pattern == "^Resources"


#
# resource pool and folder paths
#

def test_resource_pool_path_excludes_the_cluster_root_pool(inventory):
    src = _make_source(inventory)

    assert src.get_resource_pool_path(_vm("vm-1", "vm1", pool=CHILD_POOL)) == "Parent/Child"
    assert src.get_resource_pool_path(_vm("vm-2", "vm2", pool=ROOT_POOL)) == ""
    assert src.get_resource_pool_path(_vm("vm-3", "vm3", pool=None)) is None


def test_resource_pool_path_ends_at_the_pool_below_the_cluster_whatever_its_name(inventory):
    # the root pool is the one whose parent is the cluster, not a pool; a child pool may well
    # carry the name vCenter gives the root pool, and a root pool may carry another name
    src = _make_source(inventory)
    nested_resources = _ResourcePool("resgroup-7", "Resources", parent=PARENT_POOL)
    renamed_root = _ResourcePool("resgroup-8", "Pool0")

    assert src.get_resource_pool_path(_vm("vm-7", "vm7", pool=nested_resources)) == "Parent/Resources"
    assert src.get_resource_pool_path(_vm("vm-8", "vm8", pool=renamed_root)) == ""


def test_resource_pool_path_is_cached_per_managed_object(inventory):
    src = _make_source(inventory)
    vm = _vm("vm-1", "vm1", pool=CHILD_POOL)

    assert src.get_resource_pool_path(vm) == "Parent/Child"

    # a different pool must not change the answer, the path was cached for this VM
    object.__setattr__(vm, "_resource_pool", _ResourcePool("resgroup-9", "Other", parent=ROOT_POOL))
    assert src.get_resource_pool_path(vm) == "Parent/Child"
    assert len(src.object_path_cache) == 1


def test_vm_folder_path_is_relative_to_the_datacenter_vm_folder(inventory):
    src = _make_source(inventory)

    assert src.get_vm_folder_path(_vm("vm-1", "vm1", folder=PROD_FOLDER)) == "Dept-A/Prod"
    assert src.get_vm_folder_path(_vm("vm-2", "vm2", folder=VM_FOLDER)) == ""
    # a VM organized in a vApp (a resource pool) has no folder path
    assert src.get_vm_folder_path(_vm("vm-3", "vm3", folder=CHILD_POOL)) is None


def test_vm_folder_path_is_cached_per_managed_object(inventory):
    src = _make_source(inventory)
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER)

    assert src.get_vm_folder_path(vm) == "Dept-A/Prod"

    # a different folder must not change the answer, the path was cached for this VM
    object.__setattr__(vm, "_parent", DEPT_A_FOLDER)
    assert src.get_vm_folder_path(vm) == "Dept-A/Prod"
    assert len(src.object_path_cache) == 1


#
# tenant resolution
#

def test_vm_tenant_relation_takes_precedence_over_everything(inventory):
    src = _make_source(
        inventory,
        vm_tenant_relation=_relation(("vm1", "Name Tenant")),
        vm_tenant_resource_pool_relation=_relation((".*", "Pool Tenant")),
        vm_tenant_folder_relation=_relation((".*", "Folder Tenant")),
        cluster_tenant_relation=_relation((".*", "Cluster Tenant")),
        vm_tenant_inherit_from_cluster=True,
    )
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER, pool=CHILD_POOL)

    assert src.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") == "Name Tenant"


def test_resource_pool_relation_takes_precedence_over_the_folder_relation(inventory):
    src = _make_source(
        inventory,
        vm_tenant_resource_pool_relation=_relation((".*", "Pool Tenant")),
        vm_tenant_folder_relation=_relation((".*", "Folder Tenant")),
    )
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER, pool=CHILD_POOL)

    assert src.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") == "Pool Tenant"


def test_tenant_is_resolved_by_resource_pool_path_and_plain_pool_name(inventory):
    vm = _vm("vm-1", "vm1", pool=CHILD_POOL)

    by_path = _make_source(inventory,
                           vm_tenant_resource_pool_relation=_relation(("^Parent/Child$", "Path Tenant")))
    assert by_path.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") == "Path Tenant"

    by_name = _make_source(inventory,
                           vm_tenant_resource_pool_relation=_relation(("^Child$", "Name Tenant")))
    assert by_name.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") == "Name Tenant"


def test_tenant_is_resolved_by_folder_path_and_plain_folder_name(inventory):
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER, pool=ROOT_POOL)

    by_path = _make_source(inventory,
                           vm_tenant_folder_relation=_relation(("^Dept-A/Prod$", "Path Tenant")))
    assert by_path.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") == "Path Tenant"

    by_name = _make_source(inventory,
                           vm_tenant_folder_relation=_relation(("^Prod$", "Name Tenant")))
    assert by_name.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") == "Name Tenant"


def test_no_tenant_is_resolved_without_relations(inventory):
    src = _make_source(inventory)
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER, pool=CHILD_POOL)

    assert src.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") is None


def test_tenant_is_inherited_from_the_cluster_relation(inventory):
    src = _make_source(
        inventory,
        cluster_tenant_relation=_relation(("DC1/c1", "Cluster Tenant")),
        vm_tenant_inherit_from_cluster=True,
    )
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER, pool=CHILD_POOL)

    assert src.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") == "Cluster Tenant"


def test_cluster_tenant_object_and_unusable_shapes(inventory):
    # the cluster tenant may be a resolved tenant object; an id alone or a dict without a
    # name gives no tenant
    src = _make_source(inventory, vm_tenant_inherit_from_cluster=True)
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER, pool=CHILD_POOL)
    cluster = _cluster(inventory)

    cluster.data["tenant"] = inventory.add_object(NBTenant, data={"name": "Object Tenant"},
                                                  read_from_netbox=True)
    assert src.get_vm_tenant_name(vm, "vm1", cluster, "DC1/c1") == "Object Tenant"

    cluster.data["tenant"] = 7
    assert src.get_vm_tenant_name(vm, "vm1", cluster, "DC1/c1") is None

    cluster.data["tenant"] = {"id": 7}
    assert src.get_vm_tenant_name(vm, "vm1", cluster, "DC1/c1") is None


def test_empty_paths_are_not_matched_against_the_relations(inventory):
    # a VM in the cluster root pool or directly in the datacenter's VM folder has empty
    # paths; like the filters, the relations only see its plain pool and folder names
    src = _make_source(inventory,
                       vm_tenant_resource_pool_relation=_relation(("^$", "Empty Pool Path")),
                       vm_tenant_folder_relation=_relation(("^$", "Empty Folder Path")))
    cluster = _cluster(inventory)
    vm = _vm("vm-1", "vm1", folder=VM_FOLDER, pool=ROOT_POOL)

    assert src.get_vm_tenant_name(vm, "vm1", cluster, "DC1/c1") is None

    by_name = _make_source(inventory,
                           vm_tenant_resource_pool_relation=_relation(("^Resources$", "Root Pool")),
                           vm_tenant_folder_relation=_relation(("^vm$", "Root Folder")))
    assert by_name.get_vm_tenant_name(vm, "vm1", cluster, "DC1/c1") == "Root Pool"
    assert by_name.get_vm_tenant_name(_vm("vm-2", "vm2", folder=VM_FOLDER), "vm2", cluster,
                                      "DC1/c1") == "Root Folder"


def test_a_path_deeper_than_the_walk_limit_is_reported(inventory, caplog):
    src = _make_source(inventory)
    pool = ROOT_POOL
    for level in range(25):
        pool = _ResourcePool(f"resgroup-{100 + level}", f"L{level + 1}", parent=pool)
    folder = VM_FOLDER
    for level in range(25):
        folder = _Folder(f"folder-{100 + level}", f"F{level + 1}", parent=folder)
    vm = _vm("vm-1", "vm1", folder=folder, pool=pool)

    with caplog.at_level("WARNING"):
        pool_path = src.get_resource_pool_path(vm)
        folder_path = src.get_vm_folder_path(vm)

    assert pool_path.endswith("/L25") and folder_path.endswith("/F25")
    assert sum("deeper than 20 levels" in record.message for record in caplog.records) == 2


def test_tenant_is_inherited_from_the_netbox_cluster_object(inventory):
    # the tenant set on the NetBox cluster is used when the cluster relation matched nothing
    src = _make_source(inventory, vm_tenant_inherit_from_cluster=True)
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER, pool=CHILD_POOL)

    cluster = _cluster(inventory, tenant_name="Cluster Object Tenant")
    assert src.get_vm_tenant_name(vm, "vm1", cluster, "DC1/c1") == "Cluster Object Tenant"

    # covers a cluster tenant which was not resolved to a tenant object (yet)
    cluster.data["tenant"] = {"id": 5, "name": "Unresolved Tenant"}
    assert src.get_vm_tenant_name(vm, "vm1", cluster, "DC1/c1") == "Unresolved Tenant"


def test_cluster_tenant_is_not_inherited_when_disabled(inventory):
    src = _make_source(
        inventory,
        cluster_tenant_relation=_relation((".*", "Cluster Tenant")),
        vm_tenant_inherit_from_cluster=False,
    )
    vm = _vm("vm-1", "vm1", folder=PROD_FOLDER, pool=CHILD_POOL)

    assert src.get_vm_tenant_name(vm, "vm1", _cluster(inventory), "DC1/c1") is None


#
# overwrite_vm_tenant
#

def test_existing_vm_tenant_is_kept_when_overwrite_is_disabled(inventory):
    cluster = _cluster(inventory)
    tenant = inventory.add_object(NBTenant, data={"name": "Existing Tenant"}, read_from_netbox=True)
    inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster, "status": "active",
                                     "tenant": tenant}, read_from_netbox=True)

    src = _make_source(inventory, overwrite_vm_tenant=False)
    vm = _sync_vm(src, cluster, tenant="Resolved Tenant")

    assert _tenant_of(vm) == "Existing Tenant"


def test_equal_tenant_stays_referenced_when_overwrite_is_disabled(inventory):
    # the resolved tenant is the one already set: the VM keeps it and the tenant object stays
    # referenced by the source, so it keeps the source tag instead of losing it every run
    cluster = _cluster(inventory)
    tenant = inventory.add_object(NBTenant, data={"name": "Existing Tenant"}, read_from_netbox=True)
    inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster, "status": "active",
                                     "tenant": tenant}, read_from_netbox=True)

    src = _make_source(inventory, overwrite_vm_tenant=False)
    vm = _sync_vm(src, cluster, tenant="Existing Tenant")

    assert _tenant_of(vm) == "Existing Tenant"
    assert tenant.source is src


def test_equal_tenant_in_api_shape_stays_referenced_when_overwrite_is_disabled(inventory):
    # same as above, with the existing tenant still in the shape the NetBox API returns it
    cluster = _cluster(inventory)
    tenant = inventory.add_object(NBTenant, data={"name": "Existing Tenant"}, read_from_netbox=True)
    inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster, "status": "active",
                                     "tenant": {"id": tenant.get_nb_reference(), "name": "Existing Tenant"}},
                         read_from_netbox=True)

    src = _make_source(inventory, overwrite_vm_tenant=False)
    vm = _sync_vm(src, cluster, tenant="Existing Tenant")

    assert _tenant_of(vm) == "Existing Tenant"
    assert tenant.source is src


def test_existing_vm_tenant_is_overwritten_by_default(inventory):
    cluster = _cluster(inventory)
    tenant = inventory.add_object(NBTenant, data={"name": "Existing Tenant"}, read_from_netbox=True)
    inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster, "status": "active",
                                     "tenant": tenant}, read_from_netbox=True)

    src = _make_source(inventory)
    vm = _sync_vm(src, cluster, tenant="Resolved Tenant")

    assert _tenant_of(vm) == "Resolved Tenant"


def test_vm_without_tenant_gets_the_resolved_tenant_even_when_overwrite_is_disabled(inventory):
    cluster = _cluster(inventory)
    inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster, "status": "active"},
                         read_from_netbox=True)

    src = _make_source(inventory, overwrite_vm_tenant=False)
    vm = _sync_vm(src, cluster, tenant="Resolved Tenant")

    assert _tenant_of(vm) == "Resolved Tenant"


#
# resource pool filters
#

def test_vm_passes_when_no_resource_pool_filter_is_set(inventory):
    src = _make_source(inventory)

    assert src.vm_passes_resource_pool_filter(_vm("vm-1", "vm1", pool=CHILD_POOL)) is True
    assert src.vm_passes_resource_pool_filter(_vm("vm-2", "vm2", pool=None)) is True


def test_exclude_filter_matches_the_resource_pool_path(inventory):
    src = _make_source(inventory, vm_exclude_by_resource_pool_filter=re.compile("^Parent"))

    assert src.vm_passes_resource_pool_filter(_vm("vm-1", "vm1", pool=CHILD_POOL)) is False
    assert src.vm_passes_resource_pool_filter(_vm("vm-2", "vm2", pool=ROOT_POOL)) is True


def test_exclude_filter_matches_the_plain_pool_name(inventory):
    src = _make_source(inventory, vm_exclude_by_resource_pool_filter=re.compile("^Child$"))

    assert src.vm_passes_resource_pool_filter(_vm("vm-1", "vm1", pool=CHILD_POOL)) is False


def test_root_pool_vm_is_judged_by_its_plain_pool_name(inventory):
    vm = _vm("vm-1", "vm1", pool=ROOT_POOL)

    excluded = _make_source(inventory, vm_exclude_by_resource_pool_filter=re.compile("^Resources$"))
    assert excluded.vm_passes_resource_pool_filter(vm) is False

    included = _make_source(inventory, vm_include_by_resource_pool_filter=re.compile("^Resources"))
    assert included.vm_passes_resource_pool_filter(vm) is True


def test_include_filter_accepts_the_pool_path_or_the_plain_pool_name(inventory):
    # one of the two identifiers has to match the include filter, not both: the plain name
    # of a VM in a nested pool is the leaf pool only and can never match a path pattern
    vm = _vm("vm-1", "vm1", pool=CHILD_POOL)

    by_path = _make_source(inventory, vm_include_by_resource_pool_filter=re.compile("^Parent/"))
    assert by_path.vm_passes_resource_pool_filter(vm) is True

    by_name = _make_source(inventory, vm_include_by_resource_pool_filter=re.compile("^Child$"))
    assert by_name.vm_passes_resource_pool_filter(vm) is True


def test_exclude_filter_wins_over_a_matching_include_filter(inventory):
    src = _make_source(inventory, vm_include_by_resource_pool_filter=re.compile("^Parent/"),
                       vm_exclude_by_resource_pool_filter=re.compile("^Child$"))

    assert src.vm_passes_resource_pool_filter(_vm("vm-1", "vm1", pool=CHILD_POOL)) is False


def test_include_filter_excludes_vms_in_other_pools(inventory):
    src = _make_source(inventory, vm_include_by_resource_pool_filter=re.compile("^Sandbox$"))

    assert src.vm_passes_resource_pool_filter(_vm("vm-1", "vm1", pool=CHILD_POOL)) is False
