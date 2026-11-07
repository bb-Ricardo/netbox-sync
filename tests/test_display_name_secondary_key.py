# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
A display name with the secondary key only needs the name of the related object. It used to
call str() on that object as well, which dumps all of its data to JSON: on every lookup, and
fatal once the data holds a value JSON can't take, like a datetime in a JSON custom field.
"""
from datetime import datetime

from module.netbox.object_classes import NBCluster, NBCustomField, NBVM


def test_vm_lookup_does_not_serialize_the_cluster(inventory):
    inventory.add_object(NBCustomField, data={
        "name": "metadata", "type": "json", "object_types": [NBCluster.object_type],
    })
    cluster = inventory.add_object(NBCluster, data={
        "name": "cluster1", "custom_fields": {"metadata": {"created": datetime(2026, 10, 23)}},
    })
    assert isinstance(cluster.data["custom_fields"]["metadata"]["created"], datetime)
    vm = inventory.add_object(NBVM, data={"name": "vm1", "cluster": cluster})

    assert inventory.get_by_data(NBVM, data={"name": "vm1", "cluster": cluster}) is vm
