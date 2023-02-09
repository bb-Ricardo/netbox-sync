# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption
from module.sources.common.conifg import *


class VMWareConfig(ConfigBase):

    section_name = source_config_section_name
    source_name = None

    def __init__(self):
        self.options = [
            ConfigOption(**config_option_enabled_definition),

            ConfigOption("type",
                         str,
                         description="type of source. This defines which source handler to use",
                         config_example="vmware",
                         mandatory=True),

            ConfigOption("host_fqdn",
                         str,
                         description="host name / IP address of the vCenter",
                         config_example="my-netbox.local",
                         mandatory=True),

            ConfigOption("port",
                         int,
                         description="TCP port to connect to",
                         default_value=443,
                         mandatory=True),

            ConfigOption("username",
                         str,
                         description="username to use to log into vCenter",
                         config_example="vcenter-admin",
                         mandatory=True),

            ConfigOption("password",
                         str,
                         description="password to use to log into vCenter",
                         config_example="super-secret",
                         sensitive=True,
                         mandatory=True),

            ConfigOption("validate_tls_certs",
                         bool,
                         description="""Enforces TLS certificate validation.
                         If vCenter uses a valid TLS certificate then this option should be set
                         to 'true' to ensure a secure connection."""),

            ConfigOption("proxy_host",
                         str,
                         description="""EXPERIMENTAL: Connect to a vCenter using a proxy server
                         (socks proxies are not supported). define a host name or an IP address""",
                         config_example="10.10.1.10"),

            ConfigOption("proxy_port",
                         int,
                         description="""EXPERIMENTAL: Connect to a vCenter using a proxy server
                         (socks proxies are not supported).
                         define proxy server port number""",
                         config_example=3128),

            ConfigOption(**config_option_permitted_subnets_definition),

            ConfigOption("cluster_exclude_filter",
                         str),
        ]

        super().__init__()

    def validate_options(self):

        for option in self.options:

            pass
