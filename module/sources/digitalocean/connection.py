# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from collections import Counter
from ipaddress import ip_interface
from packaging import version
import requests

from module.common.logging import get_logger
from module.common.misc import do_error_exit
from module.netbox import (
    NBCustomField, NBCluster, NBClusterGroup, NBClusterType, NBIPAddress, NBPlatform,
    NBRegion, NBTag, NBVM, NBVMInterface, NBVirtualDisk, NBVRF, NetBoxObject,
)
from module.netbox.inventory import NetBoxInventory
from module.netbox.connection import NetBoxHandler
from module.sources.common.source_base import SourceBase
from module.sources.digitalocean.client import DigitalOceanClient
from module.sources.digitalocean.config import DigitalOceanConfig

log = get_logger()


class DigitalOceanHandler(SourceBase):

    source_type = "digitalocean"
    settings = DigitalOceanConfig()

    dependent_netbox_objects = [
        NBCustomField, NBCluster, NBClusterGroup, NBClusterType, NBIPAddress, NBPlatform,
        NBRegion, NBTag, NBVM, NBVMInterface, NBVirtualDisk, NBVRF,
    ]

    def __init__(self, name=None):
        if not name:
            raise ValueError(f"Invalid value for attribute 'name': '{name}'.")

        self.inventory = NetBoxInventory()
        self.name = name
        self.log = get_logger()
        self._ip_claims = {}
        self._regions_by_slug = {}
        self._vm_name_counts = Counter()
        self.set_source_tag()

        settings_handler = DigitalOceanConfig()
        settings_handler.source_name = name
        self.settings = settings_handler.parse()

        if self.settings.enabled is False:
            log.info(f"Source '{name}' is currently disabled. Skipping")
            return

        self.init_successful = True

    def apply(self):
        self._ip_claims = {}
        client = DigitalOceanClient(self.settings.api_token)

        # Fetch everything before touching inventory so API failures cannot produce
        # a partial run that later looks like missing resources.
        try:
            regions = client.get_collection("regions", "regions")
            droplets = client.get_collection("droplets", "droplets")
            volumes = client.get_collection("volumes", "volumes") if self.settings.sync_volumes else []
            databases = client.get_collection("databases", "databases") if self.settings.sync_databases else []
        except (requests.RequestException, ValueError) as exc:
            do_error_exit(f"DigitalOcean discovery failed for source '{self.name}': {exc}")
        finally:
            client.close()

        self._regions_by_slug = {region["slug"]: region for region in regions}
        self.log.info(
            f"Connected to DigitalOcean, found {len(droplets)} droplets, "
            f"{len(volumes)} volumes and {len(databases)} database clusters"
        )
        if self.settings.private_ip_vrf is None:
            self.log.info(
                f"DigitalOcean source '{self.name}': private_ip_vrf is unset; "
                "private addresses use the global table."
            )
        else:
            self.log.info(
                f"DigitalOcean source '{self.name}': private addresses use VRF "
                f"'{self.settings.private_ip_vrf}'; public addresses use the global table."
            )

        cluster_group = self.inventory.add_update_object(
            NBClusterGroup,
            data={
                "name": f"DigitalOcean: {self.name}",
                "slug": NetBoxObject.format_slug(f"digitalocean-{self.name}"),
            },
            source=self,
        )
        cluster_type = self.inventory.add_update_object(
            NBClusterType,
            data={"name": "DigitalOcean", "slug": "digitalocean"},
            source=self,
        )
        volumes_by_id = {str(volume["id"]): volume for volume in volumes}
        clusters = {}
        resources = []

        for resource_type, collection in (("droplet", droplets), ("database", databases)):
            for resource in collection:
                cluster = self._get_cluster(resource.get("region"), cluster_group, cluster_type, clusters)
                if cluster is None:
                    log.warning(f"DigitalOcean {resource_type} '{resource.get('name')}' has no region; skipping.")
                    continue
                resources.append((resource_type, resource, cluster))
        self._vm_name_counts = Counter(
            (cluster, self._resource_name(resource, resource_type)[:64].casefold())
            for resource_type, resource, cluster in resources
        )
        for resource_type, resource, cluster in resources:
            if resource_type == "droplet":
                vm = self._sync_droplet(resource, cluster)
                self._sync_droplet_network(vm, resource)
                self._sync_droplet_disks(vm, resource, volumes_by_id)
            else:
                self._sync_database(resource, cluster)

    def _get_cluster(self, region, cluster_group, cluster_type, clusters):
        region_slug = region.get("slug") if isinstance(region, dict) else region
        region = self._regions_by_slug.get(region_slug, region)
        if isinstance(region, dict):
            region_slug = region.get("slug") or region.get("name")
            region_name = region.get("name") or region_slug
        else:
            region_slug = region
            region_name = region
        if not region_slug:
            return None

        key = str(region_slug)
        if key in clusters:
            return clusters[key]

        cluster_data = {
            "name": f"{self.name}: {region_name}"[:100],
            "type": cluster_type,
            "group": cluster_group,
        }
        if version.parse(self.inventory.netbox_api_version) >= version.parse("4.2.0"):
            mapped_name = self.settings.region_mapping.get(key)
            target_name = mapped_name or region_name
            target_slug = NetBoxObject.format_slug(mapped_name) if mapped_name else key
            regions = self.inventory.get_all_items(NBRegion)
            netbox_region = next((
                item for item in regions if item.data.get("name") == target_name
            ), None)
            if netbox_region is None:
                netbox_region = next((
                    item for item in regions
                    if item.data.get("slug") == target_slug or
                    (mapped_name is None and item.data.get("name") == key)
                ), None)
            if netbox_region is None:
                netbox_region = self.inventory.add_object(
                    NBRegion, data={"name": target_name, "slug": target_slug}, source=self,
                )
            elif self.source_tag in netbox_region.get_tags():
                netbox_region.set_source(self)
            cluster_data["scope_type"] = NBRegion.object_type
            cluster_data["scope_id"] = netbox_region

        # Scope changes must update this source's cluster rather than create a duplicate.
        candidates = [
            item for item in self.inventory.get_all_items(NBCluster)
            if item.data.get("name") == cluster_data["name"] and item.data.get("group") is cluster_group
        ]
        if len(candidates) > 1:
            message = f"Multiple DigitalOcean clusters match '{cluster_data['name']}' in the source cluster group."
            log.error(message)
            raise ValueError(message)
        if candidates:
            cluster = candidates[0]
            cluster.update(data=cluster_data, source=self)
        else:
            cluster = self.inventory.add_update_object(NBCluster, data=cluster_data, source=self)
        clusters[key] = cluster
        return cluster

    def _sync_droplet(self, droplet, cluster):
        name = str(droplet.get("name", "")).strip()
        if not name:
            log.warning(f"Skipping DigitalOcean droplet without a name (ID: {droplet.get('id')}).")
            return None

        size = droplet.get("size") or {}
        memory = droplet.get("memory")
        if memory is None:
            memory = size.get("memory")
        vcpus = droplet.get("vcpus")
        if vcpus is None:
            vcpus = size.get("vcpus")
        disk_gb = droplet.get("disk")
        if disk_gb is None:
            disk_gb = size.get("disk")
        vm_data = {
            "name": self._vm_name(droplet, cluster, "droplet"),
            "cluster": cluster,
            "status": "active" if droplet.get("status") == "active" else "offline",
            "vcpus": vcpus,
            "memory": memory,
        }
        if version.parse(self.inventory.netbox_api_version) >= version.parse("4.1.0"):
            vm_data["serial"] = str(droplet.get("id")) if droplet.get("id") is not None else None
        if disk_gb is not None and version.parse(self.inventory.netbox_api_version) < version.parse("3.7.0"):
            vm_data["disk"] = int(disk_gb)

        platform = self._droplet_platform(droplet.get("image"))
        if platform:
            platform_name = platform[:100]
            vm_data["platform"] = self.inventory.add_update_object(
                NBPlatform,
                data={"name": platform_name, "slug": NetBoxObject.format_slug(platform_name)},
                source=self,
            )
        tags = [tag[:100] for tag in (droplet.get("tags") or []) if isinstance(tag, str) and tag]
        if tags:
            self._ensure_tags(tags)
            vm_data["tags"] = tags

        custom_fields = self._custom_fields({
            "digitalocean_resource_type": "droplet",
            "digitalocean_resource_id": droplet.get("id"),
        })
        if custom_fields:
            vm_data["custom_fields"] = custom_fields

        return self._sync_vm(vm_data)

    @staticmethod
    def _resource_name(resource, resource_type):
        name = str(resource.get("name", "")).strip()
        return f"db-{name}" if resource_type == "database" else name

    def _vm_name(self, resource, cluster, resource_type):
        name = self._resource_name(resource, resource_type)
        if self._vm_name_counts[(cluster, name[:64].casefold())] > 1:
            suffix = f" ({resource['id']})"
            return name[:64 - len(suffix)] + suffix
        return name[:64]

    def _sync_vm(self, data):
        identity = (data.get("custom_fields") or {}).get("digitalocean_resource_id")
        cluster = data["cluster"]
        group = cluster.data.get("group")
        candidates = [
            vm for vm in self.inventory.get_all_items(NBVM)
            if identity is not None and
            (vm.data.get("custom_fields") or {}).get("digitalocean_resource_id") == identity and
            (vm.source is self or self.source_tag in vm.get_tags() or vm.data.get("cluster") is cluster or
             (group is not None and vm.data.get("cluster") is not None and
              vm.data["cluster"].data.get("group") is group))
        ]
        if len(candidates) > 1:
            current = [vm for vm in candidates if NetBoxHandler.orphaned_tag not in vm.get_tags()]
            if len(current) == 1:
                for orphan in candidates:
                    if orphan is current[0]:
                        continue
                    log.warning(
                        f"Ignoring orphaned DigitalOcean VM '{orphan.get_display_name()}' "
                        f"(NetBox ID {orphan.nb_id}) with resource ID '{identity}' for source '{self.name}'; "
                        f"using '{current[0].get_display_name()}' (NetBox ID {current[0].nb_id}). "
                        "The orphaned VM is left for normal orphan pruning."
                    )
                candidates = current
            else:
                details = "; ".join(
                    f"'{vm.get_display_name()}' (NetBox ID {vm.nb_id}, "
                    f"cluster '{vm.data['cluster'].get_display_name() if vm.data.get('cluster') else None}', "
                    f"orphaned={NetBoxHandler.orphaned_tag in vm.get_tags()})"
                    for vm in candidates
                )
                do_error_exit(
                    f"Multiple DigitalOcean VMs match resource ID '{identity}' for source '{self.name}': "
                    f"{details}. Resolve the duplicate records in NetBox before retrying."
                )
        vm = candidates[0] if candidates else self.inventory.get_by_data(NBVM, data=data)
        if not candidates and vm is not None:
            existing_id = (vm.data.get("custom_fields") or {}).get("digitalocean_resource_id")
            if existing_id is not None and existing_id != identity:
                vm = None
        if vm is None:
            return self.inventory.add_object(NBVM, data=data, source=self)
        vm.update(data=data, source=self)
        return vm

    @staticmethod
    def _droplet_platform(image):
        if not isinstance(image, dict):
            return None
        distribution = image.get("distribution")
        image_name = image.get("name")
        if image_name and distribution and distribution.lower() not in image_name.lower():
            return f"{distribution} {image_name}"
        return image_name or distribution

    def _sync_droplet_network(self, vm, droplet):
        if vm is None:
            return

        networks = droplet.get("networks") or {}
        primary = {4: None, 6: None}
        primary_is_public = {4: False, 6: False}
        for family in ("v4", "v6"):
            for address in networks.get(family, []) or []:
                ip_value = address.get("ip_address")
                if ip_value is None:
                    continue
                ip_value = str(ip_value)
                interface_name = str(address.get("type") or "network").lower()
                try:
                    ip_obj = self._parse_network_address(ip_value, address)
                except ValueError:
                    log.warning(f"Invalid IP address '{ip_value}' on DigitalOcean droplet '{vm.data['name']}'.")
                    continue

                interface = self.inventory.add_update_object(
                    NBVMInterface,
                    data={
                        "name": interface_name[:64],
                        "virtual_machine": vm,
                        "enabled": True,
                    },
                    source=self,
                )
                vrf = self._get_private_ip_vrf() if interface_name == "private" else None
                ip_object = self._sync_ip(ip_obj, interface, vrf=vrf)
                if ip_object is None:
                    continue
                ip_version = ip_obj.version
                is_public = interface_name == "public"
                if primary[ip_version] is None or (is_public and not primary_is_public[ip_version]):
                    primary[ip_version] = ip_object
                    primary_is_public[ip_version] = is_public

        primary_data = {}
        if primary[4] is not None:
            primary_data["primary_ip4"] = primary[4]
        if primary[6] is not None:
            primary_data["primary_ip6"] = primary[6]
        if primary_data:
            vm.update(data=primary_data, source=self)

    @staticmethod
    def _parse_network_address(ip_value, address):
        prefix = address.get("netmask")
        if prefix is None:
            prefix = address.get("cidr")
        if prefix is None:
            prefix = 32 if ":" not in ip_value else 128
        return ip_interface(f"{ip_value}/{prefix}")

    def _get_private_ip_vrf(self):
        name = self.settings.private_ip_vrf
        if name is None:
            return None
        candidates = [vrf for vrf in self.inventory.get_all_items(NBVRF) if vrf.data.get("name") == name]
        if len(candidates) > 1:
            do_error_exit(f"Multiple NetBox VRFs match private_ip_vrf '{name}' for source '{self.name}'.")
        if candidates:
            vrf = candidates[0]
            if self.source_tag in vrf.get_tags():
                vrf.set_source(self)
            return vrf
        return self.inventory.add_object(NBVRF, data={"name": name}, source=self)

    def _sync_ip(self, ip_obj, interface, vrf=None):
        claim_key = (str(ip_obj.ip), vrf)
        previous_claim = self._ip_claims.get(claim_key)
        if previous_claim is not None and previous_claim is not interface:
            message = (f"DigitalOcean source '{self.name}' reports IP '{ip_obj}' on multiple interfaces. "
                       f"Skipping assignment to '{interface.get_display_name()}'; keeping the first assignment.")
            log.error(message)
            return None

        ip_data = {
            "address": str(ip_obj),
            "assigned_object_type": "virtualization.vminterface",
            "assigned_object_id": interface,
        }
        if vrf is not None:
            ip_data["vrf"] = vrf
        candidates = [
            item for item in self.inventory.get_all_items(NBIPAddress)
            if ip_interface(item.data["address"]).ip == ip_obj.ip and item.data.get("vrf") is vrf
        ]
        if len(candidates) > 1:
            vrf_name = vrf.get_display_name() if vrf is not None else "global"
            do_error_exit(
                f"Multiple NetBox IPs match '{ip_obj.ip}' in VRF '{vrf_name}' for source '{self.name}': "
                f"{[item.nb_id for item in candidates]}. Resolve the duplicate records before retrying."
            )
        existing = candidates[0] if candidates else None
        if existing is not None:
            assigned_object = existing.data.get("assigned_object_id")
            if assigned_object is not None and assigned_object is not interface:
                source_tags = {tag for tag in existing.get_tags() if tag.startswith("Source: ")}
                same_source = existing.source is self or self.source_tag in source_tags
                other_source = (
                    (existing.source is not None and existing.source is not self) or
                    bool(source_tags - {self.source_tag})
                )
                if not same_source or other_source:
                    assigned_vm = (
                        assigned_object.data.get("virtual_machine")
                        if isinstance(assigned_object, NBVMInterface) else None
                    )
                    vrf = existing.data.get("vrf")
                    vrf_name = vrf.get_display_name() if isinstance(vrf, NetBoxObject) else vrf
                    owner_name = getattr(existing.source, "name", None)
                    owner_vm_tags = sorted(assigned_vm.get_tags()) if assigned_vm is not None else []
                    assigned_name = (
                        assigned_object.get_display_name()
                        if isinstance(assigned_object, NetBoxObject) else str(assigned_object)
                    )
                    assigned_cluster = assigned_vm.data.get("cluster") if assigned_vm is not None else None
                    assigned_cluster_name = (
                        assigned_cluster.get_display_name() if assigned_cluster is not None else None
                    )
                    message = (
                        f"DigitalOcean IP '{ip_obj}' has conflicting or uncertain source ownership; "
                        f"cannot reassign it for source '{self.name}'. "
                        f"Existing NetBox IP ID {existing.nb_id}, VRF={vrf_name!r}, "
                        f"assigned to '{assigned_name}', "
                        f"assigned interface ID {getattr(assigned_object, 'nb_id', None)}, "
                        f"assigned VM ID {assigned_vm.nb_id if assigned_vm is not None else None}, "
                        f"assigned cluster={assigned_cluster_name!r}, target interface ID {interface.nb_id}, "
                        f"IP source tags={sorted(source_tags)}, current-run source={owner_name!r}, "
                        f"assigned VM tags={owner_vm_tags}. "
                        f"Skipping assignment to '{interface.get_display_name()}'; resolve the existing assignment."
                    )
                    log.error(message)
                    if existing.source is None and self.source_tag in source_tags:
                        # Retain previously managed IPs without changing their assignment or ownership tags.
                        existing.set_source(self)
                    return None
                log.info(f"Repairing DigitalOcean IP '{ip_obj}' assignment for source '{self.name}' "
                         f"to interface '{interface.get_display_name()}'.")
        if existing is None:
            result = self.inventory.add_object(NBIPAddress, data=ip_data, source=self)
        else:
            existing.update(data=ip_data, source=self)
            result = existing
        self._ip_claims[claim_key] = interface
        return result

    def _sync_droplet_disks(self, vm, droplet, volumes_by_id):
        if vm is None:
            return

        disk_gb = droplet.get("disk")
        if disk_gb is None:
            disk_gb = (droplet.get("size") or {}).get("disk")
        disk_factor = 1024 if version.parse(self.inventory.netbox_api_version) >= version.parse("4.1.0") else 1
        total_disk = int(disk_gb or 0) * disk_factor
        attached_volumes = [
            volumes_by_id[str(volume_id)]
            for volume_id in (droplet.get("volume_ids") or [])
            if str(volume_id) in volumes_by_id
        ]
        total_disk += sum(int(volume.get("size_gigabytes", 0)) * disk_factor for volume in attached_volumes)

        if version.parse(self.inventory.netbox_api_version) < version.parse("3.7.0"):
            vm.update(data={"disk": total_disk}, source=self)
            return

        if disk_gb is not None:
            self._sync_virtual_disk(
                vm, "root", int(disk_gb) * disk_factor, "DigitalOcean Droplet root disk"
            )
        for volume in attached_volumes:
            self._sync_virtual_disk(
                vm,
                f"do-{volume['id']}"[:64],
                int(volume.get("size_gigabytes", 0)) * disk_factor,
                volume.get("name"),
            )

    def _sync_database(self, database, cluster):
        name = str(database.get("name", "")).strip()
        if not name:
            log.warning(f"Skipping DigitalOcean database cluster without a name (ID: {database.get('id')}).")
            return

        vm_data = {
            "name": self._vm_name(database, cluster, "database"),
            "cluster": cluster,
            "status": "active" if database.get("status") == "online" else "offline",
            "comments": "DigitalOcean managed database cluster",
        }
        if version.parse(self.inventory.netbox_api_version) >= version.parse("4.1.0"):
            vm_data["serial"] = str(database.get("id")) if database.get("id") is not None else None
        engine = database.get("engine")
        db_version = database.get("version")
        platform = " ".join(str(value) for value in (engine, db_version) if value)
        if platform:
            platform_name = platform[:100]
            vm_data["platform"] = self.inventory.add_update_object(
                NBPlatform,
                data={"name": platform_name, "slug": NetBoxObject.format_slug(platform_name)},
                source=self,
            )
        tags = [tag[:100] for tag in (database.get("tags") or []) if isinstance(tag, str) and tag]
        if tags:
            self._ensure_tags(tags)
            vm_data["tags"] = tags

        connection = database.get("private_connection") or database.get("connection") or {}
        custom_fields = self._custom_fields({
            "digitalocean_resource_type": "database",
            "digitalocean_resource_id": database.get("id"),
            "digitalocean_engine": engine,
            "digitalocean_version": db_version,
            "digitalocean_size": database.get("size"),
            "digitalocean_node_count": database.get("num_nodes"),
            "digitalocean_endpoint": connection.get("host"),
            "digitalocean_port": connection.get("port"),
        })
        if custom_fields:
            vm_data["custom_fields"] = custom_fields
        vm = self._sync_vm(vm_data)

        storage_size = database.get("storage_size_mib")
        if storage_size is not None:
            storage_size = int(storage_size)
            if version.parse(self.inventory.netbox_api_version) < version.parse("4.1.0"):
                storage_size //= 1024
            if version.parse(self.inventory.netbox_api_version) < version.parse("3.7.0"):
                vm.update(data={"disk": storage_size}, source=self)
            else:
                self._sync_virtual_disk(vm, "database-storage", storage_size, "Managed database storage")

    def _custom_fields(self, values):
        custom_fields = {}
        for field_name, value in values.items():
            if value is None:
                continue
            self.add_update_custom_field({
                "name": field_name,
                "label": field_name.replace("digitalocean_", "DO ").replace("_", " ")[:50],
                "object_types": [NBVM.object_type],
                "type": "text",
                "description": f"DigitalOcean metadata for source '{self.name}'",
            })
            custom_fields[field_name] = str(value)
        return custom_fields

    def _ensure_tags(self, tags):
        for tag in tags:
            if not isinstance(tag, str) or not tag:
                continue
            self.inventory.add_update_object(
                NBTag,
                data={"name": tag, "slug": NetBoxObject.format_slug(tag)},
                source=self,
            )

    def _sync_virtual_disk(self, vm, name, size, description=None):
        self.inventory.add_update_object(
            NBVirtualDisk,
            data={
                "name": name,
                "virtual_machine": vm,
                "size": size,
                "description": description,
            },
            source=self,
        )
