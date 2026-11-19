# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
A failed vCenter tag lookup must cost the object those tags, not the whole sync:
get_vmware_object_tags() and collect_object_tags() always return a list, which
their callers extend their tag lists with.

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


class _Folder(vim.Folder):
    """vim.Folder with a plain name and no vCenter connection behind it."""

    def __init__(self, mo_id, name):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)

    name = property(lambda self: self._name)
    parent = property(lambda self: None)


class _VirtualMachine(vim.VirtualMachine):
    """vim.VirtualMachine with a plain name and folder."""

    def __init__(self, mo_id, name, parent):
        super().__init__(mo_id)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_parent", parent)

    name = property(lambda self: self._name)
    parent = property(lambda self: self._parent)


class _Tagging:
    """The parts of the vSphere tagging API get_vmware_object_tags() uses. Listing the tags
    attached to one of the 'failing' objects raises, as a vCenter error or timeout would."""

    def __init__(self, failing):
        tags = {"tag-1": SimpleNamespace(name="prod", description="", category_id="cat-1")}

        class _Tag:
            @staticmethod
            def get(tag_id):
                return tags[tag_id]

        class _TagAssociation:
            @staticmethod
            def list_attached_tags(dynamic_id):
                if dynamic_id.id in failing:
                    raise RuntimeError("simulated vCenter error")
                return ["tag-1"]

        self.Tag, self.TagAssociation = _Tag, _TagAssociation


def _handler(inventory, failing):
    handler = object.__new__(VMWareHandler)
    handler.inventory = inventory
    handler.name = "test"
    handler.source_tag = "Source: test"
    handler.tag_session = SimpleNamespace(tagging=_Tagging(failing))
    handler.settings = SimpleNamespace(tag_name_include_category=False,
                                       vm_tag_source=["object", "parent_folder_1"])
    return handler


FOLDER = _Folder("group-v1", "Prod")
VM = _VirtualMachine("vm-1", "web01", FOLDER)


def _names(tags):
    return [tag.get_display_name() for tag in tags]


def test_a_failed_tag_lookup_keeps_the_other_tag_sources(inventory):
    # the VM's own tags can't be listed, its folder's can
    handler = _handler(inventory, failing={"vm-1"})

    assert _names(handler.collect_object_tags(VM)) == ["prod"]


def test_tag_lookups_return_a_list_when_nothing_can_be_read(inventory):
    handler = _handler(inventory, failing={"vm-1", "group-v1"})

    assert handler.get_vmware_object_tags(VM) == []
    assert handler.get_vmware_object_tags(None) == []
    assert handler.collect_object_tags(VM) == []
    assert handler.collect_object_tags(None) == []
