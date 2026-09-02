# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
Unit tests for the "sync VMware Tools guest hostname to a NetBox custom field" feature.

Covers module.sources.vmware.connection.VMWareHandler.get_object_custom_fields() as well as the
generic custom field handling in module.netbox.object_classes.NetBoxObject that this feature relies on.

Run with: python -m unittest discover -s tests
"""

import os
import sys
import types
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from module.netbox.inventory import NetBoxInventory                           # noqa: E402
from module.netbox.object_classes import NBCustomField, NBVM                  # noqa: E402
from module.sources.vmware.connection import VMWareHandler                    # noqa: E402
from module.sources.vmware.config import VMWareConfig                        # noqa: E402


def make_vm_obj(hostname=None, name="APPPRD01", guest=True):
    """
    Build a minimal fake pyVmomi VirtualMachine object, providing just the
    attributes accessed by get_object_custom_fields()/add_virtual_machine().
    """

    guest_info = None
    if guest:
        guest_info = types.SimpleNamespace(hostName=hostname)

    return types.SimpleNamespace(
        _wsdlName="VirtualMachine",
        name=name,
        guest=guest_info,
        customValue=[],
        availableField=[],
    )


class GuestHostnameCustomFieldTestCase(unittest.TestCase):
    """
    Tests the guest hostname handling in VMWareHandler.get_object_custom_fields() directly,
    without requiring a live vCenter connection.
    """

    def setUp(self):
        self.inventory = NetBoxInventory()
        # isolate tests from each other, inventory is a process wide singleton
        self.inventory.base_structure[NBCustomField.name] = []
        self.inventory.base_structure[NBVM.name] = []
        self.inventory.netbox_api_version = "4.1.0"

        # build a VMWareHandler instance without running its network-bound __init__
        self.handler = VMWareHandler.__new__(VMWareHandler)
        self.handler.inventory = self.inventory
        self.handler.name = "test-vcenter"
        self.handler.settings = types.SimpleNamespace(
            sync_custom_attributes=False,
            vm_custom_object_attributes=[],
            host_custom_object_attributes=[],
            custom_attribute_exclude=None,
            vm_guest_hostname_custom_field=None,
        )

    # 8. feature not configured -> unchanged/backward compatible behavior
    def test_feature_disabled_by_default(self):
        obj = make_vm_obj(hostname="appprd01.corp.example.com")

        result = self.handler.get_object_custom_fields(obj)

        self.assertEqual(result, {})
        self.assertEqual(len(self.inventory.get_all_items(NBCustomField)), 0)

    # 1. VMware Tools reports a valid hostname
    def test_valid_hostname_is_synced_and_field_is_created(self):
        self.handler.settings.vm_guest_hostname_custom_field = "vmware_guest_hostname"
        obj = make_vm_obj(hostname="appprd01")

        result = self.handler.get_object_custom_fields(obj)

        self.assertEqual(result, {"vmware_guest_hostname": "appprd01"})

        field = self.inventory.get_by_data(NBCustomField, data={"name": "vmware_guest_hostname"})
        self.assertIsNotNone(field)
        self.assertEqual(field.data.get("type"), "text")
        self.assertIn("virtualization.virtualmachine", field.data.get("object_types"))

    # 2. VMware Tools hostname is an FQDN -> preserved exactly, no domain stripping/case changes
    def test_fqdn_hostname_preserved_exactly(self):
        self.handler.settings.vm_guest_hostname_custom_field = "vmware_guest_hostname"
        obj = make_vm_obj(hostname="srv01.Example.LOCAL")

        result = self.handler.get_object_custom_fields(obj)

        self.assertEqual(result["vmware_guest_hostname"], "srv01.Example.LOCAL")

    # 3. VMware Tools returns no hostname (empty string)
    def test_empty_hostname_is_not_synced(self):
        self.handler.settings.vm_guest_hostname_custom_field = "vmware_guest_hostname"
        obj = make_vm_obj(hostname="")

        result = self.handler.get_object_custom_fields(obj)

        self.assertNotIn("vmware_guest_hostname", result)

    # 4. VMware Tools unavailable entirely (no guest info at all)
    def test_vmware_tools_unavailable_does_not_raise(self):
        self.handler.settings.vm_guest_hostname_custom_field = "vmware_guest_hostname"
        obj = make_vm_obj(guest=False)

        result = self.handler.get_object_custom_fields(obj)

        self.assertNotIn("vmware_guest_hostname", result)

    # 5. configured custom field already exists (e.g. pre-created by an administrator)
    def test_pre_existing_field_label_is_preserved(self):
        self.inventory.add_object(NBCustomField, data={
            "name": "vmware_guest_hostname",
            "label": "Guest OS Hostname (Admin Managed)",
            "object_types": ["virtualization.virtualmachine"],
            "type": "text",
        }, read_from_netbox=True)

        self.handler.settings.vm_guest_hostname_custom_field = "vmware_guest_hostname"
        obj = make_vm_obj(hostname="appprd01")

        result = self.handler.get_object_custom_fields(obj)

        self.assertEqual(result["vmware_guest_hostname"], "appprd01")
        field = self.inventory.get_by_data(NBCustomField, data={"name": "vmware_guest_hostname"})
        self.assertEqual(field.data.get("label"), "Guest OS Hostname (Admin Managed)")

    # 6. configured custom field does not exist yet -> created automatically, sync does not fail
    def test_missing_field_is_created_automatically(self):
        self.handler.settings.vm_guest_hostname_custom_field = "vmware_guest_hostname"
        self.assertIsNone(self.inventory.get_by_data(NBCustomField, data={"name": "vmware_guest_hostname"}))

        obj = make_vm_obj(hostname="appprd01")
        result = self.handler.get_object_custom_fields(obj)

        self.assertEqual(result["vmware_guest_hostname"], "appprd01")
        self.assertIsNotNone(self.inventory.get_by_data(NBCustomField, data={"name": "vmware_guest_hostname"}))

    # 7. guest hostname changes between synchronization runs
    def test_hostname_change_between_runs_updates_vm(self):
        self.handler.settings.vm_guest_hostname_custom_field = "vmware_guest_hostname"

        obj = make_vm_obj(hostname="appprd01")
        vm = self.inventory.add_update_object(NBVM, data={
            "name": "APPPRD01",
            "custom_fields": self.handler.get_object_custom_fields(obj),
        })
        self.assertEqual(vm.data["custom_fields"]["vmware_guest_hostname"], "appprd01")

        obj_renamed = make_vm_obj(hostname="appprd02")
        vm.update(data={"custom_fields": self.handler.get_object_custom_fields(obj_renamed)})

        self.assertEqual(vm.data["custom_fields"]["vmware_guest_hostname"], "appprd02")

    # unavailable data must preserve previously synced value instead of clearing it
    def test_unavailable_hostname_preserves_previous_value(self):
        self.handler.settings.vm_guest_hostname_custom_field = "vmware_guest_hostname"

        obj = make_vm_obj(hostname="appprd01")
        vm = self.inventory.add_update_object(NBVM, data={
            "name": "APPPRD01",
            "custom_fields": self.handler.get_object_custom_fields(obj),
        })
        self.assertEqual(vm.data["custom_fields"]["vmware_guest_hostname"], "appprd01")

        # VMware Tools stops reporting a hostname on a later run
        obj_no_tools = make_vm_obj(hostname="")
        custom_fields_no_tools = self.handler.get_object_custom_fields(obj_no_tools)
        self.assertNotIn("vmware_guest_hostname", custom_fields_no_tools)

        vm.update(data={"custom_fields": custom_fields_no_tools})

        self.assertEqual(vm.data["custom_fields"]["vmware_guest_hostname"], "appprd01")

    # 9. existing (unrelated) custom field VM sync behavior remains unchanged
    def test_unrelated_custom_fields_unaffected_when_feature_disabled(self):
        obj = make_vm_obj(hostname="appprd01")
        obj.customValue = [types.SimpleNamespace(key=1, value="42")]
        obj.availableField = [types.SimpleNamespace(key=1, name="Ticket")]
        self.handler.settings.sync_custom_attributes = True

        result = self.handler.get_object_custom_fields(obj)

        self.assertIn("vcsa_ticket", result)
        self.assertNotIn("vmware_guest_hostname", result)


class CustomFieldNotFoundBehaviorTestCase(unittest.TestCase):
    """
    Verifies the generic safety net (module.netbox.object_classes.NetBoxObject) this feature
    relies on when a configured custom field name is unknown to the inventory: the whole VM
    sync must not crash, only the custom_fields update for that pass is skipped.
    """

    def setUp(self):
        self.inventory = NetBoxInventory()
        self.inventory.base_structure[NBCustomField.name] = []
        self.inventory.base_structure[NBVM.name] = []
        self.inventory.netbox_api_version = "4.1.0"

    def test_unknown_custom_field_is_skipped_not_fatal(self):
        vm = self.inventory.add_object(NBVM, data={"name": "APPPRD01"})

        # should not raise, even though "does_not_exist_field" was never registered
        vm.update(data={"custom_fields": {"does_not_exist_field": "value"}})

        self.assertNotIn("does_not_exist_field", (vm.data.get("custom_fields") or {}))
        self.assertEqual(vm.data.get("name"), "APPPRD01")


class VMWareConfigGuestHostnameOptionTestCase(unittest.TestCase):
    """
    Verifies the 'vm_guest_hostname_custom_field' config option definition itself.
    """

    def _parse(self, source_settings):
        config = VMWareConfig()
        config.source_name = "my-vcenter-example"
        config.config_content = {
            "source": {
                "my-vcenter-example": {
                    "type": "vmware",
                    "host_fqdn": "vcenter.example.com",
                    "username": "readonly",
                    "password": "secret",
                    **source_settings,
                }
            }
        }
        return config.parse(do_log=False)

    def test_option_defaults_to_disabled(self):
        settings = self._parse({})
        self.assertIsNone(settings.vm_guest_hostname_custom_field)

    def test_option_value_is_passed_through_unmodified(self):
        settings = self._parse({"vm_guest_hostname_custom_field": "vmware_guest_hostname"})
        self.assertEqual(settings.vm_guest_hostname_custom_field, "vmware_guest_hostname")


if __name__ == "__main__":
    unittest.main()
