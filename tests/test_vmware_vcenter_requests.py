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
    """Counts every property read (SOAP 'Fetch') per managed object and property."""
    from pyVmomi import SoapAdapter

    counts = collections.Counter()
    original = SoapAdapter.SoapStubAdapter.InvokeMethod

    def invoke(self, mo, info, args, outerStub=None):
        if info.wsdlName == "Fetch":
            counts[(mo._wsdlName, mo._moId, args[0])] += 1
        return original(self, mo, info, args, outerStub)

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
    vmware_source.apply()

    reevaluated = {obj._moId for obj in vmware_source.objects_to_reevaluate}
    config_reads = [fetch_counter[("VirtualMachine", mo_id, "config")] for mo_id in powered_on - reevaluated]

    assert len(config_reads) > 0
    assert set(config_reads) == {1}


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
