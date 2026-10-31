# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
vcsim-backed tests for VM tenant assignment (issues #531, #463 and #454) and resource
pool based VM filtering (issue #494).

The vc001 inventory organizes its 34 VMs in VM folders below the datacenter's "vm"
folder ("CBK", "CBK/Templates", "CBK/Backup", "CBK/vms-kg-160er", "CBK/vms-kg-160er/VX",
"CBK/vms-cha-170er", "CBK/vms-iaa-180er", "Customers/Xyz", "Customers/Abc", "vCLS")
while all of them sit directly in the single cluster root resource pool "Resources",
which gives every VM an empty pool path and the plain pool name "Resources".
"""
import pytest

from module.netbox.object_classes import (
    NBCluster, NBDevice, NBIPAddress, NBTenant, NBVM, NBVMInterface,
)
from module.sources import instantiate_sources


def _sync(inventory, load_config, vmware_settings, extra_options=""):
    """Run the real VMware source against the running vcsim with the given options."""
    load_config(vmware_settings + extra_options)
    sources = instantiate_sources()
    assert len(sources) == 1 and sources[0].init_successful, "VMware source failed to initialise"
    inventory.resolve_relations()
    sources[0].apply()
    return sources[0]


def _empty(inventory):
    """Start over with an empty inventory, the same way the fixture does."""
    inventory.base_structure = dict()
    inventory.source_list = list()
    inventory.init()


def _vms_by_name(inventory):
    return {vm.get_display_name(): vm for vm in inventory.get_all_items(NBVM)}


def _tenant_of(nb_object):
    tenant = nb_object.data.get("tenant")
    return None if tenant is None else tenant.get_display_name()


def _mark_objects_as_saved(inventory):
    """Mark the synced objects the way they look after a NetBox round trip."""
    next_id = 1
    for cls in (NBCluster, NBDevice, NBVM, NBVMInterface, NBIPAddress, NBTenant):
        for item in inventory.get_all_items(cls):
            item.is_new = False
            item.nb_id = next_id
            next_id += 1


def test_folder_relation_assigns_tenants(vcsim, inventory, load_config, vmware_settings):
    """A folder relation using real folder paths of the vc001 inventory (issues #463, #454)."""
    if vcsim.name != "vc001":
        pytest.skip("uses the VM folder layout of the vc001 inventory")

    _sync(inventory, load_config, vmware_settings,
          "vm_tenant_folder_relation = Customers/.* = Customer Tenants\n")

    vms = _vms_by_name(inventory)
    # matched via the folder paths "Customers/Xyz" and "Customers/Abc"
    assert _tenant_of(vms["vm002_xyz-veeam"]) == "Customer Tenants"
    assert _tenant_of(vms["vm169_CRFE-baraClient01"]) == "Customer Tenants"
    # VMs in unrelated folders stay without a tenant
    assert _tenant_of(vms["z_vc001"]) is None
    assert _tenant_of(vms["tmp01_W2k22"]) is None


def test_resource_pool_relation_assigns_tenants(vcsim, inventory, load_config, vmware_settings):
    """A resource pool relation using the real pool name of the vc001 inventory (issue #463)."""
    if vcsim.name != "vc001":
        pytest.skip("uses the resource pool layout of the vc001 inventory")

    _sync(inventory, load_config, vmware_settings,
          "vm_tenant_resource_pool_relation = ^Resources$ = Pool Tenant\n")

    vms = _vms_by_name(inventory)
    assert vms, "no VM was synced"
    # every vc001 VM sits in the cluster root pool, matched by its plain pool name
    for vm in vms.values():
        assert _tenant_of(vm) == "Pool Tenant", vm.get_display_name()


def test_tenant_relation_precedence(vcsim, inventory, load_config, vmware_settings):
    """The name relation beats the pool relation, the pool relation beats the folder one."""
    if vcsim.name != "vc001":
        pytest.skip("uses the VM folder layout of the vc001 inventory")

    _sync(inventory, load_config, vmware_settings,
          "vm_tenant_relation = ^vm150-.* = Name Tenant\n"
          "vm_tenant_resource_pool_relation = ^Resources$ = Pool Tenant\n"
          "vm_tenant_folder_relation = Customers/.* = Folder Tenant\n")

    vms = _vms_by_name(inventory)
    assert _tenant_of(vms["vm150-w2k22"]) == "Name Tenant"
    # both sit in the "Customers" folders, but the resource pool relation wins
    assert _tenant_of(vms["vm002_xyz-veeam"]) == "Pool Tenant"
    assert _tenant_of(vms["vm169_CRFE-baraClient01"]) == "Pool Tenant"


def test_folder_relation_wins_when_no_pool_relation_matches(vcsim, inventory, load_config, vmware_settings):
    if vcsim.name != "vc001":
        pytest.skip("uses the VM folder layout of the vc001 inventory")

    _sync(inventory, load_config, vmware_settings,
          "vm_tenant_resource_pool_relation = ^Sandbox$ = Pool Tenant\n"
          "vm_tenant_folder_relation = Customers/.* = Folder Tenant\n")

    vms = _vms_by_name(inventory)
    assert _tenant_of(vms["vm002_xyz-veeam"]) == "Folder Tenant"
    assert _tenant_of(vms["vm169_CRFE-baraClient01"]) == "Folder Tenant"
    # no pool matched and no folder matched: no tenant
    assert _tenant_of(vms["z_vc001"]) is None


def test_vm_tenants_are_inherited_from_the_cluster(vcsim, inventory, load_config, vmware_settings):
    """The tenant inheritance asked for in issue #531."""
    _sync(inventory, load_config, vmware_settings,
          "cluster_tenant_relation = .* = Cluster Tenant\n"
          "vm_tenant_inherit_from_cluster = True\n")

    vms = _vms_by_name(inventory)
    assert vms, "no VM was synced"
    for vm in vms.values():
        assert _tenant_of(vm) == "Cluster Tenant", vm.get_display_name()


def test_vm_ip_addresses_inherit_the_vm_tenant(vcsim, inventory, load_config, vmware_settings):
    """The 'device' step of 'ip_tenant_inheritance_order' hands the VM tenant to its IPs (issue #531)."""
    _sync(inventory, load_config, vmware_settings,
          "vm_tenant_resource_pool_relation = ^Resources$ = Pool Tenant\n"
          "ip_tenant_inheritance_order = device\n")

    vm_ip_addresses = 0
    for ip in inventory.get_all_items(NBIPAddress):
        if not isinstance(ip.get_interface(), NBVMInterface):
            continue
        vm_ip_addresses += 1
        tenant = ip.data.get("tenant")
        assert tenant is not None, \
            f"IP '{ip.data.get('address')}' of a tenanted VM has no tenant"
        assert tenant.get_display_name() == "Pool Tenant"

    if vm_ip_addresses == 0:
        pytest.skip("no guest IP addresses in this inventory")


def test_exclude_filter_skips_vms_in_the_matching_pool(vcsim, inventory, load_config, vmware_settings):
    """The resource pool exclude filter asked for in issue #494."""
    if vcsim.name != "vc001":
        pytest.skip("asserts the resource pool layout of the vc001 inventory")

    _sync(inventory, load_config, vmware_settings,
          "vm_exclude_by_resource_pool_filter = ^Resources$\n")

    # every vc001 VM sits in the cluster root pool "Resources" and is skipped by it
    assert len(list(inventory.get_all_items(NBVM))) == 0
    # hosts and clusters are not affected by a VM filter
    assert len(list(inventory.get_all_items(NBDevice))) > 0
    assert len(list(inventory.get_all_items(NBCluster))) > 0


def test_include_filter_keeps_vms_of_the_matching_pool(vcsim, inventory, load_config, vmware_settings):
    """An include filter matching the plain pool name keeps root pool VMs (issue #494)."""
    if vcsim.name != "vc001":
        pytest.skip("asserts the resource pool layout of the vc001 inventory")

    _sync(inventory, load_config, vmware_settings)
    unfiltered = len(_vms_by_name(inventory))
    assert unfiltered > 0

    _empty(inventory)
    _sync(inventory, load_config, vmware_settings,
          "vm_include_by_resource_pool_filter = ^Resources\n")
    assert len(_vms_by_name(inventory)) == unfiltered, \
        "the empty pool path must not filter out root pool VMs"

    _empty(inventory)
    _sync(inventory, load_config, vmware_settings,
          "vm_include_by_resource_pool_filter = ^Nowhere$\n")
    assert len(_vms_by_name(inventory)) == 0


def test_existing_vm_tenant_is_kept_when_overwrite_is_disabled(vcsim, inventory, load_config,
                                                              vmware_settings):
    _sync(inventory, load_config, vmware_settings,
          "vm_tenant_resource_pool_relation = ^Resources$ = Tenant A\n")

    vms = _vms_by_name(inventory)
    assert vms
    for vm in vms.values():
        assert _tenant_of(vm) == "Tenant A"

    _mark_objects_as_saved(inventory)

    # a second run resolves a different tenant but must keep the one already set
    _sync(inventory, load_config, vmware_settings,
          "vm_tenant_resource_pool_relation = ^Resources$ = Tenant B\n"
          "overwrite_vm_tenant = False\n")

    vms = _vms_by_name(inventory)
    for vm in vms.values():
        assert _tenant_of(vm) == "Tenant A", vm.get_display_name()
