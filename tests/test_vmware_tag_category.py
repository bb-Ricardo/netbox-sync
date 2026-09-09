"""
vCenter tag handling: the exclude filter has to compare tag names, and putting the
category into the tag name is opt-in and must not change anything else (PR #518).

vcsim serves no tag API, so the tag session is faked where the tags themselves matter.
"""
from types import SimpleNamespace

import pytest

from module.netbox.object_classes import NBTag, NBVM
from module.sources import instantiate_sources
from module.sources.vmware import connection as vmware_connection
from module.sources.vmware.connection import VMWareHandler


@pytest.fixture(autouse=True)
def dynamic_id(monkeypatch):
    """The vSphere automation SDK is optional and absent here, the tag call needs the type."""
    monkeypatch.setattr(vmware_connection, "DynamicID",
                        lambda **kwargs: SimpleNamespace(**kwargs), raising=False)


class _FakeTagging:
    """The parts of the vSphere tagging API get_vmware_object_tags() uses."""

    def __init__(self, name="prod", description="", category="env"):
        self.category_lookups = 0
        tag = SimpleNamespace(name=name, description=description, category_id="cat-1")
        outer = self

        class _Tag:
            @staticmethod
            def get(_tag_id):
                return tag

        class _Category:
            @staticmethod
            def get(_category_id):
                outer.category_lookups += 1
                return SimpleNamespace(name=category)

        class _TagAssociation:
            @staticmethod
            def list_attached_tags(_dynamic_id):
                return ["tag-1"]

        self.Tag, self.Category, self.TagAssociation = _Tag, _Category, _TagAssociation


def _handler_with_tags(inventory, tagging, include_category=False):
    handler = object.__new__(VMWareHandler)
    handler.inventory = inventory
    handler.name = "test"
    handler.source_tag = "Source: test"
    handler.tag_session = SimpleNamespace(tagging=tagging)
    handler.settings = SimpleNamespace(tag_name_include_category=include_category)
    return handler


def _collect(handler):
    obj = SimpleNamespace(name="vm1", _wsdlName="VirtualMachine", _moId="vm-1")
    return handler.get_vmware_object_tags(obj)


def test_tag_name_and_description_are_untouched_by_default(inventory):
    tagging = _FakeTagging(description="production")
    tags = _collect(_handler_with_tags(inventory, tagging))

    assert [tag.get_display_name() for tag in tags] == ["prod"]
    assert tags[0].data.get("description") == "NetBox-synced: production"
    assert tagging.category_lookups == 0, "the category was looked up although the option is off"


def test_tag_name_includes_the_category_when_enabled(inventory):
    tagging = _FakeTagging(description="production")
    tags = _collect(_handler_with_tags(inventory, tagging, include_category=True))

    assert [tag.get_display_name() for tag in tags] == ["env:prod"]
    assert tags[0].data.get("description") == "NetBox-synced: production"


def test_vm_exclude_by_tag_filter_skips_matching_vms(vcsim, inventory, load_config, vmware_settings, monkeypatch):
    load_config(vmware_settings + "vm_exclude_by_tag_filter = no-sync\n")
    source = instantiate_sources()[0]
    assert source.init_successful

    excluded = inventory.add_update_object(NBTag, data={"name": "no-sync"})
    monkeypatch.setattr(source, "collect_object_tags", lambda _obj: [excluded])

    inventory.resolve_relations()
    source.apply()

    assert list(inventory.get_all_items(NBVM)) == [], "vm_exclude_by_tag_filter did not exclude anything"
