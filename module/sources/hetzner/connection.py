from module.common.logging import get_logger
from module.sources.common.source_base import SourceBase
from module.sources.hetzner.client import HetznerClient
from module.sources.hetzner.config import HetznerConfig
from module.sources.hetzner.network import sync_vm_network
from module.sources.hetzner.disk import sync_vm_disks



from module.netbox.inventory import (
    NetBoxInventory,
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

        settings_handler = HetznerConfig()
        settings_handler.source_name = self.name
        self.settings = settings_handler.parse()

        self.set_source_tag()

        if self.settings.enabled is False:
            log.info(f"Source '{name}' is currently disabled. Skipping")
            return

        self.init_successful = True




    @classmethod
    def implements(cls, source_type):
        return source_type == "hetzner"

    def apply(self):

        token = self.settings.api_token

        self.log.error(f"TOKEN DEBUG >>> {repr(token)}")

        if not token:
            self.log.error("Hetzner api_token not defined in settings.ini")
            return

        self.client = HetznerClient(token=token)

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

        cluster = self.inventory.add_update_object(
            NBCluster,
            data={
                "name": cluster_name,
                "type": cluster_type,
                "scope_type": 17,
                "scope_id": site,
            },
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