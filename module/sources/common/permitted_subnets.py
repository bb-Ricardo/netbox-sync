# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from ipaddress import ip_address, ip_network, ip_interface

from module.common.logging import get_logger

log = get_logger()


class PermittedSubnets:
    """
    initializes and verifies if an IP address is part of a permitted subnet
    """

    def __init__(self, config_string: str):

        self._validation_failed = False

        self.included_subnets = list()
        self.excluded_subnets = list()

        if config_string is None:
            log.info(f"Config option 'permitted_subnets' is undefined. No IP addresses will be populated to NetBox!")
            return

        if not isinstance(config_string, str):
            raise ValueError("permitted subnets need to be of type string")

        subnet_list = [x.strip() for x in config_string.split(",") if x.strip() != ""]

        for subnet in subnet_list:
            excluded = False
            if subnet[0] == "!":
                excluded = True
                subnet = subnet[1:].strip()

            if "/" not in subnet:
                log.error(f"permitted subnet '{subnet}' is missing the prefix length (i.e.: {subnet}/24)")
                self._validation_failed = True

            try:
                if excluded is True:
                    self.excluded_subnets.append(ip_network(subnet))
                else:
                    self.included_subnets.append(ip_network(subnet))
            except Exception as e:
                log.error(f"Problem parsing permitted subnet: {e}")
                self._validation_failed = True

    @property
    def validation_failed(self) -> bool:
        return self._validation_failed

    def permitted(self, ip, interface_name=None) -> bool:
        """
        performs a couple of checks to see if an IP address is valid and allowed
        to be added to NetBox

        IP address must always be passed as interface notation
            * 192.168.0.1/24
            * fd00::0/64
            * 192.168.23.24/255.255.255.24

        Parameters
        ----------
        ip: str
            IP address to validate
        interface_name: str
            name of the interface this IP shall be added. Important for meaningful log messages

        Returns
        -------
        bool: if IP address is valid
        """

        if ip is None:
            log.warning("No IP address passed to validate if this IP belongs to a permitted subnet")
            return False

        ip_text = f"'{ip}'"
        if interface_name is not None:
            ip_text = f"{ip_text} for {interface_name}"

        try:
            if "/" in ip:
                ip_a = ip_interface(ip).ip
            else:
                ip_a = ip_address(ip)
        except ValueError:
            log.error(f"IP address {ip_text} invalid!")
            return False

        if ip_a.is_link_local is True:
            log.debug(f"IP address {ip_text} is a link local address. Skipping.")
            return False

        if ip_a.is_loopback is True:
            log.debug(f"IP address {ip_text} is a loopback address. Skipping.")
            return False

        for excluded_subnet in self.excluded_subnets:
            if ip_a in excluded_subnet:
                return False

        for permitted_subnet in self.included_subnets:
            if ip_a in permitted_subnet:
                return True

        log.debug(f"IP address {ip_text} not part of any permitted subnet. Skipping.")
        return False
