# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.config.config_option import ConfigOption
from module.sources.common.source_base import SourceBaseConfig


class VMWareConfig(SourceBaseConfig):

    source_type = "vmware"

    host_fqdn = ConfigOption("host_fqdn",
                             str,
                             description="host name / IP address of the vCenter",
                             config_example="my-netbox.local",
                             mandatory=True)

    port = ConfigOption("port",
                        int,
                        description="TCP port to connect to",
                        default_value=443,
                        mandatory=True)

    username = ConfigOption("username",
                            str,
                            description="username to use to log into vCenter",
                            config_example="vcenter-admin",
                            mandatory=True)

    password = ConfigOption("password",
                            str,
                            description="password to use to log into vCenter",
                            config_example="super-secret",
                            mandatory=True)

    validate_tls_certs = ConfigOption("validate_tls_certs",
                                      bool,
                                      description="""Enforces TLS certificate validation.
                                      If vCenter uses a valid TLS certificate then this option should be set
                                      to 'true' to ensure a secure connection.""")

    proxy_host = ConfigOption("proxy_host",
                              str,
                              description="""EXPERIMENTAL: Connect to a vCenter using a proxy server
                              (socks proxies are not supported).
                              define a host name or an IP address""",
                              config_example="10.10.1.10")

    proxy_port = ConfigOption("proxy_port",
                              int,
                              description="""EXPERIMENTAL: Connect to a vCenter using a proxy server
                              (socks proxies are not supported).
                              define proxy server port number""",
                              config_example=3128)
