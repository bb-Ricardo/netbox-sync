"""
Parsing a VM more than once in a run (it is queued for reevaluation) must not
create a second NBMACAddress for the same interface and address (issue #542).
On the 6.7 capture one VM is queued and, on development, its interfaces end up
with 18 duplicate MAC objects.
"""
from module.netbox.object_classes import NBMACAddress


def test_no_duplicate_mac_objects_after_a_sync(vmware_sync):
    inventory = vmware_sync.inventory
    macs = list(inventory.get_all_items(NBMACAddress))
    pairs = {(id(mac.data.get("assigned_object_id")), str(mac.data.get("mac_address")).lower()) for mac in macs}
    duplicates = len(macs) - len(pairs)
    assert duplicates == 0, f"{duplicates} MAC address objects duplicate an (interface, address) pair"
