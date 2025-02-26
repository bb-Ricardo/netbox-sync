# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.config.option import ConfigOption
from module.config.base import ConfigBase
from module.config import netbox_config_section_name
from module.common.logging import get_logger

log = get_logger()


class NetBoxConfig(ConfigBase):
    """Controls the connection parameters to your netBox instance
    """

    section_name = netbox_config_section_name

    def __init__(self):
        self.options = [
            ConfigOption("api_token",
                         str,
                         description="""Requires an NetBox API token with full permissions on all objects except
                         'auth', 'secrets' and 'users'
                         """,
                         config_example="XYZ",
                         mandatory=True,
                         sensitive=True),

            ConfigOption("host_fqdn",
                         str,
                         description="Requires a hostname or IP which points to your NetBox instance",
                         config_example="netbox.example.com",
                         mandatory=True),

            ConfigOption("port",
                         int,
                         description="""Define the port your NetBox instance is listening on.
                         If 'disable_tls' is set to "true" this option might be set to 80
                         """,
                         default_value=443),

            ConfigOption("disable_tls",
                         bool,
                         description="Whether TLS encryption is enabled or disabled",
                         default_value=False),

            ConfigOption("validate_tls_certs",
                         bool,
                         description="""Enforces TLS certificate validation. If this system doesn't trust the NetBox
                         web server certificate then this option needs to be changed
                         """,
                         default_value=True),

            ConfigOption("proxy",
                         str,
                         description="""Defines a proxy which will be used to connect to NetBox.
                         Proxy setting needs to include the schema.
                         Proxy basic auth example: http://user:pass@10.10.1.10:312
                         """,
                         config_example="http://example.com:3128"),

            ConfigOption("client_cert",
                         str,
                         description="Specify a client certificate which can be used to authenticate to NetBox",
                         config_example="client.pem"),

            ConfigOption("client_cert_key",
                         str,
                         description="Specify the client certificate private key belonging to the client cert",
                         config_example="client.key"),

            ConfigOption("prune_enabled",
                         bool,
                         description="""Whether items which were created by this program but
                         can't be found in any source anymore will be deleted or not
                         """,
                         default_value=False),

            ConfigOption("prune_delay_in_days",
                         int,
                         description="""Orphaned objects will first be tagged before they get deleted.
                         Once the amount of days passed the object will actually be deleted
                         """,
                         default_value=30),

            ConfigOption("ignore_unknown_source_object_pruning",
                         bool,
                         description="""This will tell netbox-sync to ignore objects in NetBox
                         with tag 'NetBox-synced' from pruning if the source is not defined in
                         this config file (https://github.com/bb-Ricardo/netbox-sync/issues/176)
                         """,
                         default_value=False),

            ConfigOption("default_netbox_result_limit",
                         int,
                         description="""The maximum number of objects returned in a single request.
                         If a NetBox instance is very quick responding the value should be raised
                         """,
                         default_value=200),

            ConfigOption("timeout",
                         int,
                         description="""The maximum time a query is allowed to execute before being
                         killed and considered failed
                         """,
                         default_value=30),

            ConfigOption("max_retry_attempts",
                         int,
                         description="""The amount of times a failed request will be reissued.
                         Once the maximum is reached the syncing process will be stopped completely.
                         """,
                         default_value=4),

            ConfigOption("use_caching",
                         bool,
                         description="""Defines if caching of NetBox objects is used or not.
                         If problems with unresolved dependencies occur, switching off caching might help.
                         """,
                         default_value=True),

            ConfigOption("cache_directory_location",
                         str,
                         description="The location of the directory where the cache files should be stored",
                         default_value="cache")
        ]

        super().__init__()

    def validate_options(self):

        for option in self.options:

            if option.key == "proxy" and option.value is not None:
                if "://" not in option.value or \
                        (not option.value.startswith("http") and not option.value.startswith("socks5")):
                    log.error(f"Config option 'proxy' in '{NetBoxConfig.section_name}' must contain the schema "
                              f"http, https, socks5 or socks5h")
                    self.set_validation_failed()
