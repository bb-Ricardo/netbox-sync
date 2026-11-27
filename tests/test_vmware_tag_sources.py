# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
The 'parent_folder_1' and 'parent_folder_2' tag sources: the tags of the folder an object is
organized in (1 level up) and of the folder that folder is organized in (2 levels up).

vcsim serves no tag API usable by the vcf-sdk, so the tag session is faked.
"""
from types import SimpleNamespace

import pytest
from pyVmomi import vim

from module.sources.vmware import connection as vmware_connection
from module.sources.vmware.connection import VMWareHandler


@pytest.fixture(autouse=True)
def dynamic_id(monkeypatch):
    """The vSphere automation SDK is optional, the tag call only needs type and id."""
    monkeypatch.setattr(vmware_connection, "DynamicID",
                        lambda **kwargs: SimpleNamespace(**kwargs), raising=False)


class _Datacenter(vim.Datacenter):
    """vim.Datacenter with a plain name and no vCenter connection behind it."""

    def __init__(self, mo_id, name):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)

    name = property(lambda self: self._name)
    parent = property(lambda self: None)


class _Folder(vim.Folder):
    """vim.Folder with a plain name and parent."""

    def __init__(self, mo_id, name, parent):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_parent", parent)

    name = property(lambda self: self._name)
    parent = property(lambda self: self._parent)


class _VirtualMachine(vim.VirtualMachine):
    """vim.VirtualMachine with a plain name and folder."""

    def __init__(self, mo_id, name, parent):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_parent", parent)

    name = property(lambda self: self._name)
    parent = property(lambda self: self._parent)


# datacenter "DC" / its VM folder "vm" / "Dept" / "Prod"; every folder and the datacenter carry one tag
DATACENTER = _Datacenter("datacenter-1", "DC")
VM_ROOT = _Folder("group-v1", "vm", DATACENTER)
DEPT = _Folder("group-v2", "Dept", VM_ROOT)
PROD = _Folder("group-v3", "Prod", DEPT)
# the datacenter is tagged too, its tag must never show up as a folder tag
TAGS_BY_OBJECT = {"datacenter-1": ["dc"], "group-v1": ["root"], "group-v2": ["dept"], "group-v3": ["prod"]}


class _Tagging:
    """The parts of the vSphere tagging API get_vmware_object_tags() uses."""

    def __init__(self):

        class _Tag:
            @staticmethod
            def get(tag_id):
                return SimpleNamespace(name=tag_id, description="", category_id="cat-1")

        class _TagAssociation:
            @staticmethod
            def list_attached_tags(dynamic_id):
                return TAGS_BY_OBJECT.get(dynamic_id.id, [])

        self.Tag, self.TagAssociation = _Tag, _TagAssociation


def _vm_tags(inventory, vm, tag_source):
    handler = object.__new__(VMWareHandler)
    handler.inventory = inventory
    handler.name = "test"
    handler.source_tag = "Source: test"
    handler.tag_session = SimpleNamespace(tagging=_Tagging())
    handler.settings = SimpleNamespace(tag_name_include_category=False, vm_tag_source=tag_source)
    return [tag.get_display_name() for tag in handler.collect_object_tags(vm)]


@pytest.mark.parametrize("tag_source, expected", [
    (["parent_folder_1"], ["prod"]),
    (["parent_folder_2"], ["dept"]),
    (["parent_folder_1", "parent_folder_2"], ["prod", "dept"]),
])
def test_parent_folder_tags_come_from_one_and_two_levels_up(inventory, tag_source, expected):
    vm = _VirtualMachine("vm-1", "web01", PROD)

    assert _vm_tags(inventory, vm, tag_source) == expected


def test_no_second_folder_above_the_top_folder_of_a_datacenter(inventory):
    # the VM sits in the datacenter's VM folder itself, the next level up is the datacenter
    vm = _VirtualMachine("vm-2", "db01", VM_ROOT)

    assert _vm_tags(inventory, vm, ["parent_folder_1", "parent_folder_2"]) == ["root"]
