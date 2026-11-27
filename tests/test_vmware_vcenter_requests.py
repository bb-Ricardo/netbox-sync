# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Reading a property of a pyVmomi managed object is a round trip to vCenter which returns the whole
property ('config' of a host is about 1 MB). The VMware source must not ask vCenter for the same data
over and over again.
"""
import collections
from types import SimpleNamespace

import pytest

from module.sources.vmware import connection as vmware_connection
from module.sources.vmware.connection import VMWareHandler


@pytest.fixture
def fetch_counter(monkeypatch):
    """
    Counts how often each property of each managed object was transferred from vCenter, read one by
    one (SOAP 'Fetch') or in bulk (PropertyCollector). The keys of the bulk reads carry the property
    path as requested. The SOAP methods called are counted under the key 'methods'.
    """
    from pyVmomi import SoapAdapter

    counts = collections.Counter()
    counts.methods = collections.Counter()
    counts.single_reads = collections.Counter()
    original = SoapAdapter.SoapStubAdapter.InvokeMethod

    def invoke(self, mo, info, args, outerStub=None):
        counts.methods[info.wsdlName] += 1
        if info.wsdlName == "Fetch":
            counts[(mo._wsdlName, mo._moId, args[0])] += 1
            counts.single_reads[(mo._wsdlName, mo._moId, args[0])] += 1

        result = original(self, mo, info, args, outerStub)

        if info.wsdlName in ["RetrievePropertiesEx", "ContinueRetrievePropertiesEx"]:
            retrieved = result[1] if isinstance(result, tuple) else result
            for object_content in getattr(retrieved, "objects", None) or list():
                for prop in object_content.propSet or list():
                    counts[(object_content.obj._wsdlName, object_content.obj._moId, prop.name)] += 1

        return result

    monkeypatch.setattr(SoapAdapter.SoapStubAdapter, "InvokeMethod", invoke)
    return counts


def test_host_and_vm_properties_are_not_requested_repeatedly(vmware_source, fetch_counter):
    vmware_source.apply()

    assert len(fetch_counter) > 0, "no property reads were counted"

    # an offline VM is looked at in both runs over all VMs, so twice is fine
    repeated = {key: count for key, count in fetch_counter.items()
                if key[0] in ("VirtualMachine", "HostSystem") and count > 2}

    assert repeated == {}, "properties requested again and again from vCenter"


def test_a_vm_parsed_in_the_first_run_is_not_requested_again_in_the_second(vmware_source, fetch_counter, sdk):
    from pyVmomi import vim

    view = sdk.viewManager.CreateContainerView(sdk.rootFolder, [vim.VirtualMachine], True)
    powered_on = {vm._moId for vm in view.view if vm.runtime.powerState == "poweredOn"}
    view.Destroy()

    # the reads above went through the counted stub as well
    fetch_counter.clear()
    fetch_counter.single_reads.clear()
    vmware_source.apply()

    reevaluated = {obj._moId for obj in vmware_source.objects_to_reevaluate}
    config_reads = [fetch_counter[("VirtualMachine", mo_id, "config")] for mo_id in powered_on - reevaluated]

    assert len(config_reads) > 0
    assert set(config_reads) == {1}


def test_vm_and_host_properties_are_retrieved_in_bulk(vmware_source, fetch_counter):
    vmware_source.apply()

    single_reads = [key for key in fetch_counter.single_reads if key[0] in ("VirtualMachine", "HostSystem")]

    assert fetch_counter.methods["RetrievePropertiesEx"] > 0
    assert single_reads == [], "properties requested object by object"


def test_property_cache_serves_prefetched_properties(sdk, fetch_counter):
    from pyVmomi import vim
    from module.sources.vmware.property_cache import PropertyCache

    view = sdk.viewManager.CreateContainerView(sdk.rootFolder, [vim.VirtualMachine], True)
    vms = list(view.view)
    view.Destroy()
    expected = [(vm.name, vm.runtime.powerState) for vm in vms]

    cache = PropertyCache.for_stub(vms[0]._stub)
    prefetched = cache.prefetch(sdk, vms, vim.VirtualMachine, ["name", "runtime.powerState"])

    # same objects in the same order, now reading through the cache
    assert prefetched == vms
    assert all(vm._stub is cache for vm in prefetched)

    fetch_counter.single_reads.clear()
    assert [(vm.name, cache.get(vm, "runtime.powerState")[1]) for vm in prefetched] == expected
    assert fetch_counter.single_reads == {}, "prefetched properties were requested again"

    # a property which was not prefetched is requested once, objects reached through it use the cache too
    host = prefetched[0].runtime.host
    prefetched[0].runtime
    assert fetch_counter.single_reads == {("VirtualMachine", vms[0]._moId, "runtime"): 1}
    assert host._stub is cache

    # only small properties survive eviction
    cache.evict(prefetched)
    assert cache.get(prefetched[0], "name") == (True, expected[0][0])
    assert cache.get(prefetched[0], "runtime.powerState") == (False, None)


def test_an_unknown_custom_object_attribute_does_not_break_the_sync(vcsim, inventory, load_config, vmware_settings,
                                                                     sdk):
    from pyVmomi import vim
    from module.netbox.object_classes import NBVM
    from module.sources import instantiate_sources

    load_config(vmware_settings + "vm_custom_object_attributes = config.doesNotExist, config.uuid\n")
    source = instantiate_sources()[0]
    inventory.resolve_relations()
    source.apply()

    view = sdk.viewManager.CreateContainerView(sdk.rootFolder, [vim.VirtualMachine], True)
    uuids = {vm.name: vm.config.uuid for vm in view.view}
    view.Destroy()

    synced = {vm.get_display_name(): vm for vm in inventory.get_all_items(NBVM)}
    assert set(synced) == set(uuids)
    for name, vm in synced.items():
        assert uuids[name] in vm.data.get("custom_fields", dict()).values()


def test_custom_object_attributes_walk_list_indexes_and_managed_objects(vcsim, inventory, load_config,
                                                                       vmware_settings, sdk):
    from pyVmomi import vim
    from module.netbox.object_classes import NBVM
    from module.sources import instantiate_sources

    load_config(vmware_settings + "vm_custom_object_attributes = runtime.host.name, guest.net.0.macAddress\n")
    source = instantiate_sources()[0]
    inventory.resolve_relations()
    source.apply()

    view = sdk.viewManager.CreateContainerView(sdk.rootFolder, [vim.VirtualMachine], True)
    expected = {vm.name: (vm.runtime.host.name, vm.guest.net[0].macAddress if vm.guest.net else None)
                for vm in view.view}
    view.Destroy()

    synced = {vm.get_display_name(): vm.data.get("custom_fields", dict()) for vm in inventory.get_all_items(NBVM)}
    assert set(synced) == set(expected)
    with_guest_net = [name for name, (_, mac) in expected.items() if mac is not None]
    assert len(with_guest_net) > 0
    for name, (host_name, mac) in expected.items():
        values = synced[name].values()
        assert host_name in values, name
        if mac is not None:
            assert mac in values, name


def test_a_vm_queued_for_reevaluation_is_parsed_again(vmware_source, monkeypatch):
    from pyVmomi import vim

    parsed_again = set()
    get_vm_name_and_description = vmware_source.get_vm_name_and_description

    def record(name):
        if vmware_source.parsing_objects_to_reevaluate:
            parsed_again.add(name)
        return get_vm_name_and_description(name)

    monkeypatch.setattr(vmware_source, "get_vm_name_and_description", record)
    vmware_source.apply()

    queued = {vmware_source.get_object_name(obj) for obj in vmware_source.objects_to_reevaluate
              if isinstance(obj, vim.VirtualMachine)}
    if len(queued) == 0:
        pytest.skip("no VM of this capture is queued for reevaluation")

    # the managed object id check must not skip a VM in the reevaluation run
    assert queued <= parsed_again


#
# unit tests without vcsim
#

@pytest.fixture(autouse=True)
def dynamic_id(monkeypatch):
    """The vSphere automation SDK is optional and absent here, the tag call needs the type."""
    monkeypatch.setattr(vmware_connection, "DynamicID",
                        lambda **kwargs: SimpleNamespace(**kwargs), raising=False)


class _CountingTagging:
    """The parts of the vSphere tagging API get_vmware_object_tags() uses, counting each call."""

    def __init__(self, attached):
        self.calls = collections.Counter()
        outer = self

        class _Tag:
            @staticmethod
            def get(tag_id):
                outer.calls["Tag.get"] += 1
                return SimpleNamespace(name=f"name-{tag_id}", description="", category_id="cat-1")

        class _Category:
            @staticmethod
            def get(_category_id):
                outer.calls["Category.get"] += 1
                return SimpleNamespace(name="env")

        class _TagAssociation:
            @staticmethod
            def list_attached_tags(dynamic_id):
                outer.calls["list_attached_tags"] += 1
                return attached[dynamic_id.id]

        self.Tag, self.Category, self.TagAssociation = _Tag, _Category, _TagAssociation


def _bare_handler(inventory, **settings):
    handler = object.__new__(VMWareHandler)
    handler.inventory = inventory
    handler.name = "test"
    handler.source_tag = "Source: test"
    handler.settings = SimpleNamespace(**settings)
    return handler


def test_tag_definitions_and_attached_tags_are_requested_once(inventory):
    tagging = _CountingTagging({"vm-1": ["t1", "t2"], "vm-2": ["t1"], "group-v1": ["t2"]})
    handler = _bare_handler(inventory, tag_name_include_category=True)
    handler.tag_session = SimpleNamespace(tagging=tagging)

    def vm(mo_id):
        return SimpleNamespace(name=mo_id, _wsdlName="VirtualMachine", _moId=mo_id)

    folder = SimpleNamespace(name="folder", _wsdlName="Folder", _moId="group-v1")

    results = list()
    for obj in [vm("vm-1"), folder, vm("vm-2"), folder, vm("vm-1")]:
        results.append(sorted(t.get_display_name() for t in handler.get_vmware_object_tags(obj)))

    assert results == [["env:name-t1", "env:name-t2"], ["env:name-t2"], ["env:name-t1"],
                       ["env:name-t2"], ["env:name-t1", "env:name-t2"]]
    assert tagging.calls == {"list_attached_tags": 3, "Tag.get": 2, "Category.get": 1}


class _Node(SimpleNamespace):
    """Fake managed object counting how often its parent is requested."""

    def __init__(self, mo_id, parent, counter):
        super().__init__(_moId=mo_id, _parent=parent, _counter=counter)

    @property
    def parent(self):
        self._counter[self._moId] += 1
        return self._parent


class _Datacenter(_Node):
    pass


def test_parents_are_requested_once_per_object():
    counter = collections.Counter()
    handler = object.__new__(VMWareHandler)

    datacenter = _Datacenter("datacenter-1", None, counter)
    folder = _Node("group-h1", datacenter, counter)
    cluster = _Node("domain-c1", folder, counter)

    for _ in range(5):
        assert handler.get_parent_object_by_class(cluster, _Datacenter) is datacenter

    assert counter == {"domain-c1": 1, "group-h1": 1}


def test_unsuccessful_parent_lookups_do_not_break_later_ones():
    # a walk which ended at the root used to leave its depth (11 here) behind, the next walk then
    # gave up after 20 - 11 levels and didn't find the datacenter 10 levels up
    counter = collections.Counter()
    handler = object.__new__(VMWareHandler)

    datacenter = _Datacenter("datacenter-1", None, counter)
    chain = datacenter
    for level in range(10):
        chain = _Node(f"node-{level}", chain, counter)

    class _NotInTree:
        pass

    assert handler.get_parent_object_by_class(chain, _NotInTree) is None

    assert handler.get_parent_object_by_class(chain, _Datacenter) is datacenter


def test_custom_field_definitions_are_only_requested_for_custom_values(inventory):
    handler = _bare_handler(inventory, sync_custom_attributes=True, vm_custom_object_attributes=[],
                            host_custom_object_attributes=[], custom_attribute_exclude=None,
                            vm_guest_hostname_custom_field=None)
    inventory.netbox_api_version = "4.3.0"

    class _VM(SimpleNamespace):
        reads = 0

        @property
        def availableField(self):
            _VM.reads += 1
            return [SimpleNamespace(key=1, name="Owner")]

    without_values = _VM(_wsdlName="VirtualMachine", customValue=[])
    with_values = [_VM(_wsdlName="VirtualMachine", customValue=[SimpleNamespace(key=1, value=f"user{i}")])
                   for i in range(3)]

    assert handler.get_object_custom_fields(without_values) == dict()
    assert _VM.reads == 0

    results = [handler.get_object_custom_fields(vm) for vm in with_values]

    assert results == [{"vcsa_owner": f"user{i}"} for i in range(3)]
    assert _VM.reads == 1


class _BulkTagging(_CountingTagging):
    """Adds the bulk request for attached tags, optionally failing like an old vCenter would."""

    def __init__(self, attached, bulk_fails=False):
        super().__init__(attached)
        outer = self

        class _TagAssociation(self.TagAssociation):
            @staticmethod
            def list_attached_tags_on_objects(dynamic_ids):
                outer.calls["list_attached_tags_on_objects"] += 1
                if bulk_fails:
                    raise RuntimeError("not supported")
                # objects without tags are left out, as vCenter does
                return [SimpleNamespace(object_id=x, tag_ids=attached[x.id]) for x in dynamic_ids if attached[x.id]]

        self.TagAssociation = _TagAssociation


@pytest.mark.parametrize("bulk_fails", [False, True], ids=["bulk", "bulk-fails"])
def test_attached_tags_are_retrieved_in_bulk(inventory, bulk_fails):
    attached = {"vm-1": ["t1", "t2"], "vm-2": [], "vm-3": ["t1"]}
    tagging = _BulkTagging(attached, bulk_fails=bulk_fails)
    handler = _bare_handler(inventory, tag_name_include_category=False)
    handler.tag_session = SimpleNamespace(tagging=tagging)

    vms = [SimpleNamespace(name=x, _wsdlName="VirtualMachine", _moId=x) for x in attached]

    handler.prefetch_vmware_object_tags(vms)
    results = [sorted(t.get_display_name() for t in handler.get_vmware_object_tags(vm)) for vm in vms]

    assert results == [["name-t1", "name-t2"], [], ["name-t1"]]
    assert tagging.calls["list_attached_tags_on_objects"] == 1
    assert tagging.calls["list_attached_tags"] == (3 if bulk_fails else 0)
