# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Most vCenter objects share a few hundred tags, so get_vmware_object_tags() fetches
each tag and category from the vSphere tagging API once per source, not once per
object it is attached to. The result must be the same as without the cache.

vcsim serves no tag API, so the tag session is faked.
"""
from types import SimpleNamespace

import pytest

from module.sources.vmware import connection as vmware_connection
from module.sources.vmware.connection import VMWareHandler


@pytest.fixture(autouse=True)
def dynamic_id(monkeypatch):
    """The vSphere automation SDK is optional and absent here, the tag call needs the type."""
    monkeypatch.setattr(vmware_connection, "DynamicID",
                        lambda **kwargs: SimpleNamespace(**kwargs), raising=False)


class _CountingTagging:
    """The parts of the vSphere tagging API get_vmware_object_tags() uses, counting lookups."""

    def __init__(self):
        self.tag_lookups = 0
        self.category_lookups = 0
        tags = {
            "tag-1": SimpleNamespace(name="prod", description="production", category_id="cat-1"),
            "tag-2": SimpleNamespace(name="web", description="", category_id="cat-2"),
        }
        categories = {"cat-1": "env", "cat-2": "role"}
        outer = self

        class _Tag:
            @staticmethod
            def get(tag_id):
                outer.tag_lookups += 1
                return tags[tag_id]

        class _Category:
            @staticmethod
            def get(category_id):
                outer.category_lookups += 1
                return SimpleNamespace(name=categories[category_id])

        class _TagAssociation:
            @staticmethod
            def list_attached_tags(_dynamic_id):
                return ["tag-1", "tag-2"]

        self.Tag, self.Category, self.TagAssociation = _Tag, _Category, _TagAssociation


def _handler(inventory, tagging, include_category):
    handler = object.__new__(VMWareHandler)
    handler.inventory = inventory
    handler.name = "test"
    handler.source_tag = "Source: test"
    handler.tag_session = SimpleNamespace(tagging=tagging)
    handler.settings = SimpleNamespace(tag_name_include_category=include_category)
    return handler


def _collect(handler, moid):
    obj = SimpleNamespace(name=moid, _wsdlName="VirtualMachine", _moId=moid)
    return [tag.get_display_name() for tag in handler.get_vmware_object_tags(obj)]


@pytest.mark.parametrize("include_category, expected", [
    (False, ["prod", "web"]),
    (True, ["env:prod", "role:web"]),
])
def test_each_tag_and_category_is_fetched_once(inventory, include_category, expected):
    tagging = _CountingTagging()
    handler = _handler(inventory, tagging, include_category)

    results = [_collect(handler, f"vm-{n}") for n in range(5)]

    assert results == [expected] * 5, "the cached tags differ from the fetched ones"
    assert tagging.tag_lookups == 2, "a tag was fetched more than once"
    assert tagging.category_lookups == (2 if include_category else 0)


def test_cache_is_per_source(inventory):
    # two sources are two vCenters: tag ids are only unique within one
    tagging_a, tagging_b = _CountingTagging(), _CountingTagging()

    _collect(_handler(inventory, tagging_a, False), "vm-1")
    _collect(_handler(inventory, tagging_b, False), "vm-1")

    assert (tagging_a.tag_lookups, tagging_b.tag_lookups) == (2, 2)
