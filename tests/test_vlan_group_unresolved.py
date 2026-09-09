"""
A VLAN whose group relation is an unresolved API dict must not crash the VLAN
lookup (issue #529). That is the state of every VLAN group that netbox-sync did
not fetch itself, e.g. groups created by another tool.
"""
from module.netbox.object_classes import NBVLAN
from module.sources.common.source_base import SourceBase


def test_unresolved_vlan_group_dict_does_not_crash_lookup(inventory):
    inventory.add_object(NBVLAN, data={
        "vid": 100,
        "name": "vlan100",
        "group": {"id": 53392, "url": "https://netbox/api/ipam/vlan-groups/53392/",
                  "display": "external", "name": "external"},
    }, read_from_netbox=True)
    source = SourceBase()
    source.inventory = inventory

    # must fall through to the "no match" path instead of raising AttributeError
    result = source.get_vlan_object_if_exists({"vid": 100, "name": "vlan100"})

    assert result is not None
