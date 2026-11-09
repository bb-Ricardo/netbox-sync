# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from hcloud import HCloudException
from packaging import version
from requests.exceptions import RequestException

from module.common.logging import get_logger
from module.common.misc import do_error_exit
from module.sources.common.source_base import SourceBase
from module.sources.hetzner.client import HetznerClient
from module.sources.hetzner.config import HetznerConfig
from module.sources.hetzner.network import sync_vm_network
from module.sources.hetzner.disk import sync_vm_disks

log = get_logger()


from module.netbox.inventory import (
    NetBoxInventory,
    NBTag,
    NBVM,
    NBSite,
    NBCluster,
    NBClusterType,
    NBVMInterface,
    NBIPAddress,
    NBVirtualDisk,
)


class HetznerHandler(SourceBase):

    source_type = "hetzner"
    source_tag = "hetzner"

    settings = HetznerConfig()

    dependent_netbox_objects = [
        NBTag,
        NBVM,
        NBCluster,
        NBSite,
        NBClusterType,
        NBIPAddress,
        NBVirtualDisk,
        NBVMInterface,
    ]

    def __init__(self, name=None):

        if name is None:
            raise ValueError(f"Invalid value for attribute 'name': '{name}'.")

        self.inventory = NetBoxInventory()
        self.name = name
        self.log = get_logger()
        self.client = None

        settings_handler = HetznerConfig()
        settings_handler.source_name = self.name
        self.settings = settings_handler.parse()

        self.set_source_tag()

        if self.settings.enabled is False:
            log.info(f"Source '{name}' is currently disabled. Skipping")
            return

        self.client = HetznerClient(token=self.settings.api_token)

        try:
            self.client.check_connection()
        except (HCloudException, RequestException) as e:
            log.error(f"Unable to connect to the Hetzner Cloud API for source '{name}': {e}")
            log.info(f"Source '{name}' is currently unavailable. Skipping")
            return

        self.init_successful = True

    @classmethod
    def implements(cls, source_type):
        return source_type == "hetzner"

    def apply(self):

        try:
            self.sync_servers()
        except (HCloudException, RequestException) as e:
            # pruning is already decided and part of this source may be in the inventory,
            # so stop the whole run instead of writing a partial sync to NetBox
            message = f"Hetzner source '{self.name}': {e}. No NetBox writes or pruning will be performed."
            log.error(message)
            do_error_exit(message)

    def sync_servers(self):

        servers = self.client.get_servers()

        self.log.info(f"Connected to Hetzner, found {len(servers)} servers")

        # ---------------------------
        # main object
        # ---------------------------

        site = self.inventory.add_update_object(
            NBSite,
            data={"name": "cloud"},
            source=self,
        )

        cluster_type = self.inventory.add_update_object(
            NBClusterType,
            data={"name": "cloud"},
            source=self,
        )

        cluster_name = f"Hetzner: {self.name}"

        cluster_data = {
            "name": cluster_name,
            "type": cluster_type,
        }

        # clusters are scoped since NetBox 4.2, before they had a site
        if version.parse(self.inventory.netbox_api_version) >= version.parse("4.2.0"):
            cluster_data["scope_type"] = "dcim.site"
            cluster_data["scope_id"] = site
        else:
            cluster_data["site"] = site

        cluster = self.inventory.add_update_object(
            NBCluster,
            data=cluster_data,
            source=self,
        )

        # ---------------------------
        # servers loop
        # ---------------------------

        for server in servers:

            # -------- VM --------
            vm = self.inventory.add_update_object(
                NBVM,
                data={
                    "name": server.name,
                    "status": "active",
                    "cluster": cluster,
                    "site": site,
                },
                source=self,
            )

            # -------- interfaces --------
            sync_vm_network(self, vm, server)

            # -------- disks --------
            sync_vm_disks(self, vm, server)
