# -*- coding: utf-8 -*-
# Licensed under the MIT license.
from ipaddress import ip_address, ip_interface
import re
from module.common.logging import get_logger
from module.netbox.inventory import (NetBoxInventory, NBVM, NBSite, NBCluster, NBClusterType,
    NBVMInterface, NBIPAddress, NBVirtualDisk, NBPlatform, NBTenant, NBTag,
    NBDevice, NBInterface, NBDeviceType, NBDeviceRole, NBManufacturer, NBCustomField)
from module.sources.common.source_base import SourceBase
from module.sources.vmmanager.client import VMmanagerClient
from module.sources.vmmanager.config import VMmanagerConfig

log = get_logger()

def _pick(data, *keys):
    if not isinstance(data, dict): return None
    for key in keys:
        value = data.get(key)
        if value is not None and value != "": return value
    return None

def _scalar(value):
    return _pick(value, "value", "name", "title", "label", "id") if isinstance(value, dict) else value

def _slug(value):
    """Create a stable NetBox-compatible slug from a platform label."""
    value = str(value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return (value or "vmmanager-platform")[:100]


def _as_int(value):
    value = _scalar(value)
    if value is None or isinstance(value, bool): return None
    try: return int(float(value))
    except (TypeError, ValueError, OverflowError): return None

def _status(value):
    value = _scalar(value)
    if isinstance(value, str):
        value = value.lower()
        if value in ("active", "running", "on", "started", "working"): return "active"
        if value in ("planned", "creating", "new", "pending"): return "planned"
        if value in ("failed", "error", "crashed"): return "failed"
        if value in ("offline", "stopped", "off", "shutoff", "deleted"): return "offline"
    return "active"

def _items(value):
    if value is None: return []
    if isinstance(value, list): return value
    if isinstance(value, dict):
        for key in ("doc", "items", "list", "data", "item"):
            if isinstance(value.get(key), list): return value[key]
        return [value]
    return [value]

def _ip_entries(host):
    entries = []
    # VMmanager 6 exposes IPv4 and IPv6 as separate arrays; process both.
    raw_lists = []
    for field in ("ip4", "ip6", "ip", "ips", "ip_addresses", "ip_address", "addresses"):
        value = host.get(field)
        if value is not None:
            raw_lists.extend(_items(value))
    for item in raw_lists:
        if isinstance(item, dict):
            address = _pick(item, "ip", "address", "ip_addr", "value")
            if address: entries.append((str(address), item))
        elif item: entries.append((str(item), {}))
    for iface in _items(_pick(host, "interfaces", "network_interfaces", "networks", "nics")):
        if not isinstance(iface, dict): continue
        for item in _items(_pick(iface, "ip", "ips", "ip_addresses", "addresses")):
            if isinstance(item, dict):
                address = _pick(item, "ip", "address", "ip_addr", "value")
                if address: entries.append((str(address), {**item, "_interface": iface}))
            elif item: entries.append((str(item), {"_interface": iface}))
    return entries

class VMmanagerHandler(SourceBase):
    """Read-only VMmanager source; never changes VMmanager state."""
    source_type = "vmmanager"
    dependent_netbox_objects = [NBVM, NBCluster, NBSite, NBClusterType, NBIPAddress,
        NBVirtualDisk, NBVMInterface, NBPlatform, NBTenant, NBTag, NBDevice,
        NBInterface, NBDeviceType, NBDeviceRole, NBManufacturer, NBCustomField]

    def __init__(self, name=None):
        if not name: raise ValueError("Invalid value for attribute 'name': source name is required")
        self.inventory, self.name, self.log = NetBoxInventory(), name, get_logger()
        self.client, self.snapshot_complete = None, False
        settings_handler = VMmanagerConfig()
        settings_handler.source_name = self.name
        self.settings = settings_handler.parse()
        self.set_source_tag()
        if self.settings.enabled is False:
            self.log.info("Source '%s' is disabled. Skipping", name)
            return
        self.init_successful = True

    @classmethod
    def implements(cls, source_type): return source_type == "vmmanager"

    def _resolve_relation(self, object_name, relation_name, multiple=False):
        """Resolve VM name against the source's regex-based relation setting."""
        matches = []
        for relation in getattr(self.settings, relation_name, None) or []:
            regex = relation.get("object_regex")
            if regex and regex.match(object_name):
                value = relation.get("assigned_name")
                if value:
                    matches.append(value)
        return matches if multiple else (matches[0] if matches else None)

    def _find_existing_vm(self, vmmanager_id, cluster):
        for vm in self.inventory.get_all_items(NBVM):
            if str(vm.data.get("serial") or "") == str(vmmanager_id) and vm.data.get("cluster") == cluster:
                return vm
        return None

    def _sync_vm(self, host, cluster, site, source_disks=None):
        vm_id = str(host["id"])
        name = str(_pick(host, "name", "hostname", "title") or f"vm-{vm_id}").strip() or f"vm-{vm_id}"
        cpu = _as_int(_pick(host, "cpu_number", "vcpus", "vcpu", "cpu", "cores", "cpu_count"))
        # VMmanager 6 exposes memory in MiB.
        memory = _as_int(_pick(host, "ram_mib", "memory", "mem", "ram", "vram", "memory_mb"))
        data = {"name": name[:64], "serial": vm_id[:50], "status": _status(_pick(host, "status", "state", "power_state")), "cluster": cluster, "site": site}
        if cpu is not None and cpu >= 0:
            data["vcpus"] = float(cpu)
        if memory is not None and memory >= 0:
            data["memory"] = memory

        # Map VMmanager's OS name to a NetBox Platform object.
        os_name = _pick(host, "os_name", "operating_system", "os")
        if os_name:
            platform_name = str(_scalar(os_name)).strip()[:100]
            if platform_name:
                platform = self.inventory.add_update_object(
                    NBPlatform,
                    data={"name": platform_name, "slug": _slug(platform_name)},
                    source=self,
                )
                data["platform"] = platform

        # Do not write VM.disk directly from total_disks_size_mib. NetBox validates
        # this field against the sum of assigned VirtualDisk objects, while VMmanager
        # may report the aggregate even when per-disk sizes are not present in the
        # host payload. _sync_disks() imports individual disk sizes when available.

        storage = host.get("storage") if isinstance(host.get("storage"), dict) else {}
        storage_name = _pick(storage, "name")
        comments = []
        # Preserve the administrator's VMmanager comment (often key=value
        # inventory metadata) in NetBox, alongside the generated OS/storage
        # details. This is refreshed on every sync.
        source_comment = _pick(host, "comment")
        if source_comment:
            comments.append(str(_scalar(source_comment)).strip())
        generated_comments = []
        if os_name:
            generated_comments.append(f"OS: {str(_scalar(os_name)).strip()}")
        if storage_name:
            generated_comments.append(f"VMmanager storage: {str(storage_name).strip()}")
        # Keep VMmanager's semicolon-separated metadata on its own paragraph.
        source_comments = comments[:1]
        data["comments"] = "\n\n".join(
            part for part in ("\n".join(source_comments), "\n".join(generated_comments)) if part
        )[:2000]

        # Optional regex-based tenant and tag mappings, compatible with the
        # vm_tenant_relation / vm_tag_relation syntax used by other sources.
        tenant_name = self._resolve_relation(name, "vm_tenant_relation")
        if tenant_name:
            data["tenant"] = {"name": tenant_name}

        tag_names = self._resolve_relation(name, "vm_tag_relation", multiple=True)
        if tag_names:
            data["tags"] = [
                self.inventory.add_update_object(NBTag, data={"name": tag_name}, source=self)
                for tag_name in dict.fromkeys(tag_names)
            ]
        vm = self._find_existing_vm(vm_id, cluster)
        if vm is None: vm = self.inventory.add_update_object(NBVM, data=data, source=self)
        else: vm.update(data, source=self)
        self._sync_disks(vm, host, source_disks=source_disks)
        primary_ips = self._sync_network(vm, host)
        vm_update = {}
        if primary_ips.get(4) is not None:
            vm_update["primary_ip4"] = primary_ips[4]
        if primary_ips.get(6) is not None:
            vm_update["primary_ip6"] = primary_ips[6]
        if vm_update:
            vm.update(vm_update, source=self)
        return vm

    def _sync_disks(self, vm, host, source_disks=None):
        """Sync per-disk sizes from VMmanager's dedicated `/vm/v3/disk` endpoint.

        The host endpoint's `disks[]` contains IDs/names but usually omits sizes.
        The disk endpoint exposes `host.id`, `target_dev`, and `size_mib`.
        """
        # Prefer the dedicated disk endpoint. Only fall back to host-embedded
        # details when callers do not provide a disk snapshot (e.g. legacy tests).
        if source_disks is not None:
            disks = [disk for disk in source_disks if isinstance(disk, dict)]
            use_dedicated_endpoint = True
        else:
            disks = _items(_pick(host, "disks", "disk_list", "virtual_disks", "volumes"))
            use_dedicated_endpoint = False

        sizes_imported = []
        invalid_size_records = []
        for index, disk in enumerate(disks):
            if not isinstance(disk, dict):
                continue
            # In /vm/v3/disk, host is a relation object and size_mib is the
            # authoritative per-disk size. The host endpoint's singular `disk`
            # object instead uses disk_mib.
            if use_dedicated_endpoint:
                size = _as_int(_pick(disk, "size_mib"))
                disk_host = disk.get("host") if isinstance(disk.get("host"), dict) else {}
                if str(_pick(disk_host, "id")) != str(host.get("id")):
                    continue
            else:
                detail = host.get("disk") if isinstance(host.get("disk"), dict) else {}
                disk_id = _pick(disk, "id", "uuid", "name", "title")
                detail_matches = bool(detail) and (
                    str(_pick(detail, "id")) == str(disk_id)
                    or _pick(detail, "target_dev") == _pick(disk, "target_dev")
                )
                combined = dict(detail if detail_matches else {})
                combined.update(disk)
                size = _as_int(_pick(combined, "disk_mib", "size_mib", "size", "size_mb", "capacity", "disk_size", "space"))

            if size is None or size < 0:
                invalid_size_records.append(disk)
                self.log.warning("VM '%s': disk '%s' has no usable size; skipping disk size update",
                                 vm.data.get("name"), _pick(disk, "name", "target_dev", "id") or index)
                continue

            target_dev = _pick(disk, "target_dev")
            disk_label = str(target_dev or _pick(disk, "name", "title") or f"disk-{_pick(disk, 'id') or index}")
            disk_name = f"{vm.data.get('name')}-{disk_label}"[:64]
            self.inventory.add_update_object(
                NBVirtualDisk,
                data={"name": disk_name, "virtual_machine": vm, "size": size},
                source=self,
            )
            sizes_imported.append(size)

        # With the dedicated endpoint, an empty list is meaningful only if the
        # VM reports no disks. Otherwise avoid overwriting VM.disk based on an
        # incomplete or incorrectly associated disk snapshot.
        aggregate = _as_int(_pick(host, "total_disks_size_mib"))
        if source_disks is not None and not disks and aggregate not in (None, 0):
            self.log.warning("VM '%s': VMmanager reports %s MiB total disk size but /vm/v3/disk returned no disks for host id %s; leaving VM.disk unchanged",
                             vm.data.get("name"), aggregate, host.get("id"))
            return

        # Check that the dedicated endpoint covers every disk ID referenced by
        # the host response. The host's `disks[]` contains IDs but not sizes.
        host_disks = _items(_pick(host, "disks", "disk_list", "virtual_disks", "volumes"))
        expected_ids = {str(_pick(item, "id")) for item in host_disks
                        if isinstance(item, dict) and _pick(item, "id") is not None}
        actual_ids = {str(_pick(item, "id")) for item in disks
                      if isinstance(item, dict) and _pick(item, "id") is not None}
        missing_ids = expected_ids - actual_ids if source_disks is not None else set()
        source_total = sum(sizes_imported)
        if missing_ids or invalid_size_records:
            self.log.warning(
                "VM '%s': incomplete per-disk data from VMmanager (missing disk IDs: %s, "
                "disks without sizes: %d); leaving VM.disk unchanged",
                vm.data.get("name"), sorted(missing_ids), len(invalid_size_records),
            )
            return
        if aggregate is not None and aggregate >= 0 and source_disks is not None and source_total != aggregate:
            self.log.warning(
                "VM '%s': VMmanager aggregate is %s MiB but /vm/v3/disk records sum to %s MiB; "
                "leaving VM.disk unchanged because the source data is inconsistent",
                vm.data.get("name"), aggregate, source_total,
            )
            return

        # NetBox validates VM.disk against the sum of all assigned virtual disk
        # objects. Only update the aggregate when the imported source disk set is
        # complete and the NetBox disk objects now sum to the same value. This
        # avoids both rejected PATCH requests and writing a misleading total when
        # NetBox contains stale or externally managed disk objects.
        virtual_disks = vm.get_virtual_disks()
        netbox_sizes = [_as_int(disk.data.get("size")) for disk in virtual_disks]
        if any(size is None or size < 0 for size in netbox_sizes):
            self.log.warning("VM '%s': one or more assigned NetBox virtual disks have no valid size; leaving VM.disk unchanged",
                             vm.data.get("name"))
            return
        netbox_total = sum(netbox_sizes)
        authoritative_total = aggregate if aggregate is not None and aggregate >= 0 else source_total
        if netbox_total != authoritative_total:
            self.log.warning(
                "VM '%s': VMmanager disk total is %s MiB, but assigned NetBox virtual disks sum to %s MiB; "
                "leaving VM.disk unchanged. Check for stale or externally managed virtual disks",
                vm.data.get("name"), authoritative_total, netbox_total,
            )
            return

        if vm.data.get("disk") != netbox_total:
            vm.update({"disk": netbox_total}, source=self)

    def _sync_network(self, vm, host):
        interfaces = _items(_pick(host, "interfaces", "network_interfaces", "networks", "nics"))
        by_key = {}
        primary_ips = {}
        for index, iface in enumerate(interfaces):
            if not isinstance(iface, dict): continue
            key = str(_pick(iface, "host_interface", "interface", "id", "uuid", "name", "device", "ifname") or index)
            name = str(_pick(iface, "host_interface", "name", "device", "ifname", "label") or f"eth{index}")
            mac = _pick(iface, "mac", "mac_address", "macaddr", "hwaddr")
            data = {"name": name[:64], "virtual_machine": vm, "enabled": True}
            if mac: data["mac_address"] = str(mac).upper()
            by_key[key] = self.inventory.add_update_object(NBVMInterface, data=data, source=self)

        seen_addresses = set()
        # VMmanager returns addresses in separate ip4/ip6 arrays, with the
        # interface name in each record's `interface` field.
        for index, (raw_address, details) in enumerate(_ip_entries(host)):
            address = raw_address.strip()
            try:
                parsed_ip = ip_address(address.split("/")[0])
                parsed = ip_interface(address if "/" in address else f"{address}/{32 if parsed_ip.version == 4 else 128}")
            except ValueError:
                self.log.warning("Ignoring invalid IP '%s' for VM '%s'", address, vm.data.get("name")); continue
            iface_data = details.get("_interface", {}) if isinstance(details, dict) else {}
            key = str(_pick(iface_data, "host_interface", "interface", "id", "uuid", "name", "device", "ifname") or _pick(details, "interface") or f"ip-{index}")
            interface = by_key.get(key)
            if interface is None:
                name = str(_pick(iface_data, "name", "device", "ifname", "label") or f"eth{index}")
                mac = _pick(iface_data, "mac", "mac_address", "macaddr", "hwaddr")
                iface_data_nb = {"name": name[:64], "virtual_machine": vm, "enabled": True}
                if mac: iface_data_nb["mac_address"] = str(mac).upper()
                interface = self.inventory.add_update_object(NBVMInterface, data=iface_data_nb, source=self)
                by_key[key] = interface
            address_text = str(parsed)
            if address_text in seen_addresses:
                continue
            seen_addresses.add(address_text)
            existing = next((item for item in self.inventory.get_all_items(NBIPAddress) if item.data.get("address") == address_text), None)
            ip_data = {"address": address_text, "assigned_object_type": "virtualization.vminterface", "assigned_object_id": interface}
            ip_object = None
            if existing is None:
                ip_object = self.inventory.add_object(NBIPAddress, data=ip_data, source=self)
            else:
                assigned_object = existing.data.get("assigned_object_id")
                assigned_type = existing.data.get("assigned_object_type")
                is_unassigned = assigned_object is None and assigned_type is None
                is_source_owned = existing.source is self or self.source_tag in existing.get_tags()
                if assigned_object == interface or is_unassigned or is_source_owned:
                    existing.update(ip_data, source=self)
                    ip_object = existing
                else:
                    self.log.warning(
                        "IP %s for VM '%s' is already assigned elsewhere; leaving it unchanged",
                        address_text, vm.data.get("name")
                    )
            if ip_object is not None:
                primary_ips.setdefault(parsed_ip.version, ip_object)

        return primary_ips

    @staticmethod
    def _custom_field_applies_to_device(field):
        data = getattr(field, "data", {}) or {}
        raw_types = data.get("object_types") or data.get("content_types") or []
        if not isinstance(raw_types, list):
            raw_types = [raw_types]
        normalized = set()
        for value in raw_types:
            if isinstance(value, dict):
                value = _pick(value, "value", "name", "slug")
            if value:
                normalized.add(str(value).lower())
        return "dcim.device" in normalized or "device" in normalized

    def _device_custom_field_names(self):
        """Return supported CPU/RAM custom-field names already defined in NetBox."""
        fields = {}
        aliases = {
            "cpu": ("cpu", "cpu_count", "cpu_cores", "cores", "vcpus"),
            "ram": ("ram", "ram_mib", "memory", "memory_mib", "ram_mb"),
        }
        for field in self.inventory.get_all_items(NBCustomField):
            data = getattr(field, "data", {}) or {}
            name = str(data.get("name") or "").strip()
            if not name or not self._custom_field_applies_to_device(field):
                continue
            field_type = _scalar(data.get("type"))
            # CPU counts and RAM MiB are numeric; only integer custom fields are safe targets.
            if field_type != "integer":
                continue
            normalized = name.lower()
            for purpose, candidates in aliases.items():
                if normalized in candidates and purpose not in fields:
                    fields[purpose] = name
        return fields

    def _sync_node(self, node, site, clusters, device_type, device_role, custom_field_names):
        node_id = str(node["id"])
        name = str(_pick(node, "name", "hostname", "title") or f"vmmanager-node-{node_id}").strip()
        source_cluster = node.get("cluster") if isinstance(node.get("cluster"), dict) else {}
        source_cluster_name = _pick(source_cluster, "name")
        cluster = clusters.get(str(source_cluster_name)) if source_cluster_name is not None else None
        if cluster is None and source_cluster_name is not None:
            self.log.warning("Node '%s' refers to cluster '%s' that was not imported; cluster relation left empty",
                             name, source_cluster_name)

        # Match an already-managed physical server by its stable NetBox identity
        # (name + site) before building the update payload. Existing device type,
        # serial, asset tag, platform and description are administrator-owned data:
        # VMmanager's node ID is not a hardware serial and its generic hypervisor
        # type must never replace the real manufacturer/model in NetBox.
        lookup = {"name": name[:64], "site": site}
        existing_device = self.inventory.get_by_data(NBDevice, data=lookup)
        data = {
            "name": name[:64], "site": site,
            "status": _status(_pick(node, "state", "status")),
        }
        if existing_device is None:
            # New/unmatched nodes still need a valid Device Type and Role. Do not
            # put the VMmanager node ID in NetBox's hardware serial field.
            data["device_type"] = device_type
            data["device_role"] = device_role
        if cluster is not None:
            data["cluster"] = cluster

        # Only populate platform/description on newly created devices. On existing
        # physical servers those fields may contain curated inventory information.
        if existing_device is None:
            os_version = _pick(node, "os_version", "operating_system", "os")
            if os_version:
                platform_name = str(_scalar(os_version)).strip()[:100]
                if platform_name:
                    data["platform"] = self.inventory.add_update_object(
                        NBPlatform, data={"name": platform_name, "slug": _slug(platform_name)}, source=self)

        cpu = _as_int(_pick(node.get("cpu") if isinstance(node.get("cpu"), dict) else {},
                             "number", "total", "count") or _pick(node, "cpu_number", "cpu_count", "cores"))
        ram_data = node.get("ram_mib") if isinstance(node.get("ram_mib"), dict) else {}
        ram = _as_int(_pick(ram_data, "total", "allocated", "size") or _pick(node, "ram_mib", "memory_mib", "ram_mb"))
        custom_values = {}
        if cpu is not None and cpu >= 0 and custom_field_names.get("cpu"):
            custom_values[custom_field_names["cpu"]] = cpu
        if ram is not None and ram >= 0 and custom_field_names.get("ram"):
            # VMmanager reports ram_mib in MiB; keep the source unit explicit in the field name/documentation.
            custom_values[custom_field_names["ram"]] = ram
        if custom_values:
            data["custom_fields"] = custom_values

        if existing_device is None:
            # Include useful source facts for newly created devices only. Do not
            # replace a manually maintained description on an existing server.
            description_parts = [f"VMmanager node ID: {node_id}"]
            if cpu is not None:
                description_parts.append(f"CPU: {cpu}")
            if ram is not None:
                description_parts.append(f"RAM: {ram} MiB")
            if node.get("kernel_version"):
                description_parts.append(f"Kernel: {str(node['kernel_version']).strip()}")
            data["description"] = "; ".join(description_parts)[:200]

        device = self.inventory.add_update_object(NBDevice, data=data, source=self)
        if existing_device is not None:
            self.log.info(
                "Matched existing NetBox device '%s' to VMmanager node id %s; preserving device type, serial, asset tag, platform and description",
                name, node_id,
            )
        raw_ip = _pick(node, "ip", "management_ip", "mgmt_ip", "address")
        if not raw_ip:
            self.log.warning("Node '%s' has no management IP in VMmanager response", name)
            return device
        raw_ip = str(raw_ip).strip().split("/")[0]
        try:
            parsed = ip_address(raw_ip)
            address = f"{parsed}/{32 if parsed.version == 4 else 128}"
        except ValueError:
            self.log.warning("Ignoring invalid management IP '%s' for node '%s'", raw_ip, name)
            return device

        interface = self.inventory.add_update_object(NBInterface, data={
            "name": "mgmt0", "device": device, "type": "other", "enabled": True,
            "mgmt_only": True, "description": "VMmanager management interface",
        }, source=self)
        existing = next((item for item in self.inventory.get_all_items(NBIPAddress)
                         if item.data.get("address") == address), None)
        ip_object = None
        ip_data = {"address": address, "assigned_object_type": "dcim.interface", "assigned_object_id": interface}
        if existing is None:
            ip_object = self.inventory.add_object(NBIPAddress, data=ip_data, source=self)
        else:
            assigned_object = existing.data.get("assigned_object_id")
            assigned_type = existing.data.get("assigned_object_type")
            is_unassigned = assigned_object is None and assigned_type is None
            is_source_owned = existing.source is self or self.source_tag in existing.get_tags()
            if assigned_object == interface or is_unassigned or is_source_owned:
                existing.update(ip_data, source=self)
                ip_object = existing
            else:
                self.log.warning("Management IP %s for node '%s' is already assigned elsewhere; leaving it unchanged",
                                 address, name)
        if ip_object is not None:
            device.update({"primary_ip4" if parsed.version == 4 else "primary_ip6": ip_object}, source=self)
        return device

    def finish(self):
        """Close the VMmanager HTTP session if it was initialized."""
        if self.client is not None:
            try:
                self.client.session.close()
            except Exception as exc:
                self.log.warning("Failed to close VMmanager session for source '%s': %s", self.name, exc)

    def apply(self):
        if self.settings.enabled is False:
            return
        if not self.settings.api_url or not self.settings.email or not self.settings.password:
            raise RuntimeError(f"VMmanager source '{self.name}' requires api_url, email and password; aborting before NetBox pruning")
        self.client = VMmanagerClient(self.settings.api_url, self.settings.email, self.settings.password,
                                      verify_ssl=self.settings.verify_ssl is not False, timeout=self.settings.timeout or 30)

        # Fetch and validate all required snapshots before marking this source safe for pruning.
        hosts = self.client.get_hosts()
        disks_from_api = self.client.get_disks()
        clusters_from_api = self.client.get_clusters() if self.settings.sync_clusters is not False or self.settings.sync_nodes is not False else []
        nodes = self.client.get_nodes() if self.settings.sync_nodes is not False else []
        for label, items in (("VM", hosts), ("disk", disks_from_api), ("cluster", clusters_from_api), ("node", nodes)):
            if any(not isinstance(item, dict) or not item.get("id") for item in items):
                raise RuntimeError(f"VMmanager source '{self.name}' returned an incomplete {label} inventory; aborting before pruning")

        disks_by_host = {}
        for disk in disks_from_api:
            disk_host = disk.get("host") if isinstance(disk.get("host"), dict) else {}
            host_id = _pick(disk_host, "id")
            if host_id is None:
                self.log.warning("Ignoring VMmanager disk id %s without host.id", disk.get("id"))
                continue
            disks_by_host.setdefault(str(host_id), []).append(disk)

        exclude_dummy = self.settings.exclude_dummy_clusters is True
        usable_clusters = [cluster for cluster in clusters_from_api
                           if not (exclude_dummy and str(_pick(cluster, "virtualization_type") or "").lower() == "dummy")]
        usable_cluster_ids = {str(cluster["id"]) for cluster in usable_clusters}
        if exclude_dummy:
            # Do not import VMs or nodes that belong to a deliberately excluded dummy cluster.
            def belongs_to_excluded_dummy(item):
                relation = item.get("cluster") if isinstance(item.get("cluster"), dict) else {}
                cluster_id = _pick(relation, "id")
                return cluster_id is not None and str(cluster_id) not in usable_cluster_ids
            hosts = [host for host in hosts if not belongs_to_excluded_dummy(host)]
            nodes = [node for node in nodes if not belongs_to_excluded_dummy(node)]

        self.snapshot_complete = True
        self.log.info("Connected to VMmanager source '%s': %d VMs, %d clusters, %d hypervisor nodes",
                      self.name, len(hosts), len(usable_clusters), len(nodes))
        site_name = str(self.settings.site_name or "VMmanager")
        site = self.inventory.add_update_object(NBSite, data={"name": site_name}, source=self)
        cluster_type = self.inventory.add_update_object(
            NBClusterType, data={"name": str(self.settings.cluster_type_name or "VMmanager")}, source=self)

        clusters = {}
        # Create/update all API clusters (including clusters with zero VMs), then ensure
        # any cluster referenced only by a VM is still represented.
        if self.settings.sync_clusters is not False:
            for source_cluster in usable_clusters:
                source_name = str(_pick(source_cluster, "name") or f"cluster-{source_cluster['id']}")
                netbox_name = str(self.settings.cluster_name or source_name)
                if netbox_name not in clusters:
                    comments = str(_pick(source_cluster, "comment", "description") or "")
                    details = []
                    if _pick(source_cluster, "virtualization_type"):
                        details.append(f"Virtualization: {_pick(source_cluster, 'virtualization_type')}")
                    if _pick(source_cluster, "node_count") is not None:
                        details.append(f"Nodes: {_pick(source_cluster, 'node_count')}")
                    if _pick(source_cluster, "host_count") is not None:
                        details.append(f"VMs: {_pick(source_cluster, 'host_count')}")
                    combined_comments = "\n".join(part for part in (comments, "; ".join(details)) if part)
                    clusters[netbox_name] = self.inventory.add_update_object(
                        NBCluster, data={"name": netbox_name, "comments": combined_comments[:2000],
                                         "type": cluster_type, "scope_type": "dcim.site", "scope_id": site}, source=self)

        # Preserve existing behavior for VM records even if cluster sync is disabled.
        for host in hosts:
            source_cluster = host.get("cluster") if isinstance(host.get("cluster"), dict) else {}
            source_cluster_name = _pick(source_cluster, "name")
            cluster_name = str(self.settings.cluster_name or source_cluster_name or f"VMmanager: {self.name}")
            if cluster_name not in clusters:
                clusters[cluster_name] = self.inventory.add_update_object(
                    NBCluster, data={"name": cluster_name, "type": cluster_type,
                                     "scope_type": "dcim.site", "scope_id": site}, source=self)
            self._sync_vm(host, clusters[cluster_name], site, source_disks=disks_by_host.get(str(host["id"]), []))

        missing_custom_fields = []
        if self.settings.sync_nodes is not False:
            manufacturer = self.inventory.add_update_object(
                NBManufacturer, data={"name": "VMmanager", "slug": "vmmanager",
                                      "description": "Generic manufacturer for VMmanager hypervisor nodes"}, source=self)
            device_type = self.inventory.add_update_object(
                NBDeviceType, data={"model": str(self.settings.hypervisor_device_type or "VMmanager Hypervisor"),
                                    "slug": _slug(self.settings.hypervisor_device_type or "VMmanager Hypervisor"),
                                    "manufacturer": manufacturer}, source=self)
            device_role = self.inventory.add_update_object(
                NBDeviceRole, data={"name": str(self.settings.hypervisor_device_role or "Hypervisor"),
                                    "slug": _slug(self.settings.hypervisor_device_role or "Hypervisor"),
                                    "color": "607d8b", "vm_role": False}, source=self)
            field_names = self._device_custom_field_names()
            if not field_names.get("cpu"):
                missing_custom_fields.append(("cpu", "CPU count / cores", "integer"))
            if not field_names.get("ram"):
                missing_custom_fields.append(("ram_mib", "RAM (MiB)", "integer"))
            for node in nodes:
                source_cluster = node.get("cluster") if isinstance(node.get("cluster"), dict) else {}
                source_cluster_name = _pick(source_cluster, "name")
                cluster_name = str(self.settings.cluster_name or source_cluster_name or "")
                if cluster_name and cluster_name not in clusters:
                    clusters[cluster_name] = self.inventory.add_update_object(
                        NBCluster, data={"name": cluster_name, "type": cluster_type,
                                         "scope_type": "dcim.site", "scope_id": site}, source=self)
                self._sync_node(node, site, clusters, device_type, device_role, field_names)

        if missing_custom_fields:
            details = ", ".join(f"{name} (integer; {label})" for name, label, _ in missing_custom_fields)
            self.log.warning("NetBox custom fields not found for dcim.device: %s. Create these fields in NetBox -> Custom Fields, assign them to Devices, and run sync again. RAM is stored in MiB.", details)
        self.log.info("VMmanager source '%s' inventory loaded successfully", self.name)
