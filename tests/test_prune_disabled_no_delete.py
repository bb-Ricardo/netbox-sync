# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""With prune_enabled = False netbox-sync must not send a single DELETE request to NetBox.

A token without the permission to delete is the sane setup for that case, and NetBox answers every
DELETE with a 403 which ends the whole run.
"""

import types
from unittest.mock import Mock

import pytest

from module.netbox.connection import NetBoxHandler
from module.netbox.object_classes import NBCluster, NBClusterType, NBTag, NBVM, NBVirtualDisk


def make_handler(inventory, prune_enabled):
    handler = object.__new__(NetBoxHandler)
    handler.inventory = inventory
    handler.settings = types.SimpleNamespace(prune_enabled=prune_enabled)
    handler.resolved_dependencies = set()
    handler.query_current_data = Mock()
    handler.request = Mock(return_value=True)
    return handler


def deletes(handler):
    return [call for call in handler.request.call_args_list if call.kwargs.get("req_type") == "DELETE"]


def gone_disk(inventory):
    """A virtual disk which exists in NetBox and which the source does not report anymore."""

    cluster_type = inventory.add_object(NBClusterType, data={"id": 1, "name": "vmware"}, read_from_netbox=True)
    cluster = inventory.add_object(NBCluster, data={"id": 1, "name": "cluster1", "type": cluster_type},
                                   read_from_netbox=True)
    vm = inventory.add_object(NBVM, data={"id": 1, "name": "vm01", "cluster": cluster}, read_from_netbox=True)
    disk = inventory.add_object(NBVirtualDisk, data={"id": 5, "name": "Hard disk 2", "virtual_machine": vm,
                                                     "size": 1024}, read_from_netbox=True)
    disk.deleted = True
    return disk


def unused_tag(inventory):
    """A tag of this program which no object carries anymore."""

    return inventory.add_object(NBTag, data={"id": 7, "name": "Source: gone",
                                             "description": f"{NetBoxHandler.primary_tag}: unused",
                                             "tagged_items": 0}, read_from_netbox=True)


def test_disk_gone_from_the_source_is_deleted_if_pruning_is_enabled(inventory):
    gone_disk(inventory)
    handler = make_handler(inventory, prune_enabled=True)

    handler.update_object(NBVirtualDisk, last_run=True)

    assert [call.kwargs["nb_id"] for call in deletes(handler)] == [5]


def test_disk_gone_from_the_source_is_kept_if_pruning_is_disabled(inventory):
    gone_disk(inventory)
    handler = make_handler(inventory, prune_enabled=False)

    handler.update_object(NBVirtualDisk, last_run=True)

    assert deletes(handler) == []


@pytest.mark.parametrize("prune_enabled, expected", [(True, [7]), (False, [])])
def test_unused_tags_are_only_deleted_if_pruning_is_enabled(inventory, prune_enabled, expected):
    unused_tag(inventory)
    handler = make_handler(inventory, prune_enabled=prune_enabled)

    handler.delete_unused_tags()

    assert [call.kwargs["nb_id"] for call in deletes(handler)] == expected
