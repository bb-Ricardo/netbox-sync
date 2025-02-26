# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.common.logging import get_logger
import re

log = get_logger()


class VLANFilter:

    def __init__(self, vlan, filter_type):
        self._validation_failed = False

        self.site = None
        self.filter_type = filter_type

        if vlan is None or len(f"{vlan}") == 0:
            self._validation_failed = True
            log.error(f"submitted VLAN {self.filter_type} string for VLAN was " + "'None'" if vlan is None else "empty")
            return

        vlan_split = [x.replace('\\', "") for x in re.split(r'(?<!\\)/', vlan)]

        if len(vlan_split) == 1:
            self._value = vlan_split[0]
        elif len(vlan_split) == 2:
            self.site = vlan_split[0]
            self._value = vlan_split[1]
        else:
            self._validation_failed = True
            log.error(f"submitted VLAN {self.filter_type} string for VLAN filter contains name or site including '/'. " +
                      "A '/' which belongs to the name needs to be escaped like '\\/'.")

    def site_matches(self, site_name):

        if self.site is None:
            return True

        # string or regex matches
        # noinspection PyBroadException
        try:
            if ([self.site, site_name]).count(None) == 0 and re.search(f"^{self.site}$", site_name):
                log.debug2(f"VLAN {self.filter_type} site name '{site_name}' matches '{self.site}'")
                return True
        except Exception:
            return False

        return False

    def is_valid(self):

        return not self._validation_failed


class FilterVLANByName(VLANFilter):
    """
    initializes and verifies if a VLAN matches by name
    """

    def __init__(self, vlan, filter_type="exclude"):

        super().__init__(vlan, filter_type)

        self.name = None

        if self._validation_failed is True:
            return

        self.name = self._value

    def matches(self, name, site=None):

        if self.site_matches(site) is False:
            return False

        # string or regex matches
        try:
            if ([self.name, name]).count(None) == 0 and re.search(f"^{self.name}$", name):
                log.debug2(f"VLAN {self.filter_type} name '{name}' matches '{self.name}'")
                return True
        except Exception as e:
            log.warning(f"Unable to match {self.filter_type} VLAN name '{name}' to '{self.name}': {e}")
            return False

        return False


class FilterVLANByID(VLANFilter):
    """
    initializes and verifies if a VLAN matches by ID
    """

    def __init__(self, vlan, filter_type="exclude"):

        super().__init__(vlan, filter_type)

        self.range = None

        if self._validation_failed is True:
            return

        try:
            if "-" in self._value and int(self._value.split("-")[0]) >= int(self._value.split("-")[1]):
                log.error(f"VLAN {self.filter_type} range has to start with the lower ID: {self._value}")
                self._validation_failed = True
                return

            self.range = sum(
                ((list(range(*[int(j) + k for k, j in enumerate(i.split('-'))])) if '-' in i else [int(i)])
                 for i in self._value.split(',')), []
            )
        except Exception as e:
            log.error(f"unable to extract VLAN IDs from value '{self._value}': {e}")
            self._validation_failed = True

    def matches(self, vlan_id, site=None):

        if self.site_matches(site) is False:
            log.debug2(f"VLAN {self.filter_type} site name '{site_name}' matches '{self.site}'")
            return False

        try:
            if int(vlan_id) in self.range:
                log.debug2(f"VLAN {self.filter_type} ID '{vlan_id}' matches '{self._value}'")
                return True
        except Exception as e:
            log.warning(f"Unable to match {self.filter_type} VLAN ID '{vlan_id}' to '{self._value}': {e}")
            return False

        return False
