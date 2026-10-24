from ipaddress import ip_address, ip_interface, ip_network

from packaging.version import Version

from module.common.logging import get_logger
from module.common.misc import do_error_exit
from module.common.support import normalize_mac_address
from module.netbox import (
    NBCustomField, NBCluster, NBClusterGroup, NBClusterType, NBIPAddress,
    NBMACAddress, NBSite, NBTag, NBTenant, NBVirtualDisk, NBVM, NBVMInterface, NBVRF,
)
from module.netbox.inventory import NetBoxInventory
from module.sources.aws.client import AWSClient, AWSDiscoveryError
from module.sources.aws.config import AWSConfig
from module.sources.common.source_base import SourceBase

log = get_logger()


class AWSHandler(SourceBase):
    source_type = "aws"
    dependent_netbox_objects = [
        NBTag, NBCustomField, NBTenant, NBSite, NBClusterGroup, NBClusterType,
        NBCluster, NBVM, NBVMInterface, NBMACAddress, NBIPAddress, NBVRF, NBVirtualDisk,
    ]

    def __init__(self, name=None):
        if not name:
            raise ValueError(f"Invalid source name '{name}'.")
        self.name = name
        self.inventory = NetBoxInventory()
        config = AWSConfig()
        config.source_name = name
        self.settings = config.parse()
        self.set_source_tag()
        self.init_successful = False
        if not self.settings.enabled:
            log.info(f"Source '{name}' is disabled. Skipping.")
            return
        try:
            self.client = AWSClient(profile=self.settings.profile)
        except AWSDiscoveryError as exc:
            self.abort(exc)
        self.init_successful = True

    def abort(self, error):
        message = f"AWS source '{self.name}': {error}. No NetBox writes or pruning will be performed."
        log.error(message)
        do_error_exit(message)

    def apply(self):
        try:
            if self.settings.organization:
                snapshots = self.client.discover_organization(
                    self.settings.regions, self.settings.organization_role_name,
                    expected_account=self.settings.account_id,
                    discovery_region=self.settings.discovery_region,
                    include_management=self.settings.organization_include_management,
                    external_id=self.settings.organization_external_id,
                    account_ids=self.settings.organization_account_ids)
            else:
                account, snapshot = self.client.discover(
                    self.settings.regions, self.settings.account_id, self.settings.discovery_region)
                snapshots = {account: snapshot}
            vrfs = {}
            for account, snapshot in snapshots.items():
                self.validate_snapshot(account, snapshot, vrfs)
            for account, snapshot in snapshots.items():
                self.sync_snapshot(account, snapshot)
        except (AWSDiscoveryError, ValueError, KeyError) as exc:
            self.abort(exc)

    def vrf_name(self, account, region, vpc):
        return self.settings.vpc_vrf_relation.get(
            f"{account}/{region}/{vpc}", self.settings.vpc_vrf_relation.get(
                f"{region}/{vpc}", f"AWS {account}/{region}/{vpc}"))

    def get_vrf(self, account, region, vpc):
        name = self.vrf_name(account, region, vpc)
        vrf = self.inventory.get_by_data(NBVRF, data={"name": name})
        if vrf is None:
            raise ValueError(f"Create NetBox VRF '{name}' for VPC '{vpc}' before importing its IPs")
        return vrf

    def addresses(self, eni, subnet):
        networks = [ip_network(subnet["CidrBlock"])]
        networks.extend(
            ip_network(entry["Ipv6CidrBlock"])
            for entry in subnet.get("Ipv6CidrBlockAssociationSet", [])
            if entry.get("Ipv6CidrBlockState", {}).get("State") == "associated"
        )
        addresses = [
            (entry["PrivateIpAddress"], entry.get("Primary", False))
            for entry in eni.get("PrivateIpAddresses", [])
        ]
        addresses.extend(
            (entry["Ipv6Address"], entry.get("IsPrimaryIpv6", False))
            for entry in eni.get("Ipv6Addresses", [])
        )
        result = []
        for value, primary in addresses:
            address = ip_address(value)
            network = next((net for net in networks
                            if net.version == address.version and address in net), None)
            if network is None:
                raise ValueError(f"ENI '{eni['NetworkInterfaceId']}' IP '{value}' has no matching AWS subnet")
            value = f"{address}/{network.prefixlen}"
            if self.settings.permitted_subnets.permitted(value, interface_name=eni["NetworkInterfaceId"]):
                result.append((value, primary))
        return result

    def validate_snapshot(self, account, snapshot, vrfs=None):
        if vrfs is None:
            vrfs = {}
        for region, data in snapshot.items():
            for instance in data["instances"]:
                spec = data["InstanceTypes"].get(instance["InstanceType"])
                if spec is None:
                    raise ValueError(f"Missing AWS instance type '{instance['InstanceType']}'")
                if "DefaultVCpus" not in spec["VCpuInfo"] or "SizeInMiB" not in spec["MemoryInfo"]:
                    raise ValueError(f"Incomplete AWS specifications for '{instance['InstanceType']}'")
                for tag in instance.get("Tags", []):
                    if tag["Key"] in self.settings.tag_keys and len(f"AWS:{tag['Key']}={tag['Value']}") > 100:
                        raise ValueError(f"AWS tag '{tag['Key']}' exceeds the NetBox tag name limit (100 characters)")
                for attachment in instance.get("BlockDeviceMappings", []):
                    volume_id = attachment.get("Ebs", {}).get("VolumeId")
                    if volume_id and volume_id not in data["Volumes"]:
                        raise ValueError(f"Missing attached EBS volume '{volume_id}'")
                for reference in instance.get("NetworkInterfaces", []):
                    eni = data["NetworkInterfaces"].get(reference["NetworkInterfaceId"])
                    if eni is None:
                        raise ValueError(f"Missing attached ENI '{reference['NetworkInterfaceId']}'")
                    if eni.get("Attachment", {}).get("InstanceId") != instance["InstanceId"]:
                        raise ValueError(f"ENI '{eni['NetworkInterfaceId']}' changed attachment during discovery")
                    if not eni.get("MacAddress"):
                        raise ValueError(f"ENI '{eni['NetworkInterfaceId']}' has no MAC address")
                    if not self.settings.sync_private_ips:
                        continue
                    vpc, subnet = eni["VpcId"], data["Subnets"].get(eni["SubnetId"])
                    if vpc not in data["Vpcs"] or subnet is None or subnet["VpcId"] != vpc:
                        raise ValueError(f"Incomplete VPC/subnet discovery for ENI '{eni['NetworkInterfaceId']}'")
                    if self.addresses(eni, subnet):
                        vrf = self.get_vrf(account, region, vpc)
                        identity = (account, region, vpc)
                        if vrf in vrfs and vrfs[vrf] != identity:
                            raise ValueError(f"VRF '{vrf.data['name']}' is mapped to more than one AWS VPC")
                        vrfs[vrf] = identity

    def custom_fields(self):
        fields = (
            ("aws_account_id", "AWS account ID", [NBVM.object_type]),
            ("aws_region", "AWS region", [NBVM.object_type]),
            ("aws_instance_id", "AWS instance ID", [NBVM.object_type]),
            ("aws_name", "AWS Name tag", [NBVM.object_type]),
            ("aws_instance_type", "AWS instance type", [NBVM.object_type]),
            ("aws_availability_zone", "AWS Availability Zone", [NBVM.object_type]),
            ("aws_vpc_id", "AWS VPC ID", [NBVM.object_type, NBVMInterface.object_type]),
            ("aws_eni_id", "AWS ENI ID", [NBVMInterface.object_type]),
            ("aws_subnet_id", "AWS subnet ID", [NBVMInterface.object_type]),
            ("aws_public_ipv4", "AWS public IPv4", [NBVMInterface.object_type]),
        )
        for name, label, types in fields:
            self.add_update_custom_field({
                "name": name, "label": label, "object_types": types, "type": "text",
                "description": f"Reported by AWS ({self.name})",
            })

    def sync_snapshot(self, account, snapshot):
        self.custom_fields()
        group = self.inventory.add_update_object(
            NBClusterGroup, data={"name": f"AWS {account}"}, source=self)
        api_version = Version(self.inventory.netbox_api_version)
        for region, data in snapshot.items():
            site = self.inventory.add_update_object(
                NBSite, data={"name": f"AWS {region}"}, source=self)
            cluster_data = {
                "name": f"AWS {account}/{region}", "group": group,
                "type": {"name": "AWS EC2"},
            }
            if api_version >= Version("4.2"):
                cluster_data.update(scope_type="dcim.site", scope_id=site)
            else:
                cluster_data["site"] = site
            cluster = self.inventory.add_update_object(NBCluster, data=cluster_data, source=self)
            for instance in data["instances"]:
                self.sync_instance(account, region, instance, data, site, cluster, api_version)

    def sync_instance(self, account, region, instance, data, site, cluster, api_version):
        instance_id = instance["InstanceId"]
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        spec = data["InstanceTypes"][instance["InstanceType"]]
        vm_data = {
            "name": instance_id, "cluster": cluster,
            "status": "active" if instance["State"]["Name"] == "running" else "offline",
            "vcpus": spec["VCpuInfo"]["DefaultVCpus"],
            "memory": spec["MemoryInfo"]["SizeInMiB"],
            "custom_fields": {
                "aws_account_id": account, "aws_region": region,
                "aws_instance_id": instance_id, "aws_name": tags.get("Name", ""),
                "aws_instance_type": instance["InstanceType"],
                "aws_availability_zone": instance["Placement"]["AvailabilityZone"],
                "aws_vpc_id": instance.get("VpcId", ""),
            },
            "tags": [f"AWS:{key}={tags[key]}" for key in self.settings.tag_keys if key in tags],
        }
        cpu = instance.get("CpuOptions", {})
        if cpu.get("CoreCount") is not None and cpu.get("ThreadsPerCore") is not None:
            vm_data["vcpus"] = cpu["CoreCount"] * cpu["ThreadsPerCore"]
        if api_version >= Version("3.3"):
            vm_data["site"] = site
        if api_version >= Version("4.1"):
            vm_data["serial"] = instance_id
        if self.settings.tenant:
            vm_data["tenant"] = {"name": self.settings.tenant}
        disks = [
            data["Volumes"][entry["Ebs"]["VolumeId"]]
            for entry in instance.get("BlockDeviceMappings", []) if "Ebs" in entry
        ]
        if api_version < Version("3.7"):
            vm_data["disk"] = sum(volume["Size"] for volume in disks)
        vm = self.inventory.add_update_object(NBVM, data=vm_data, source=self)
        for tag in vm.get_tags():
            if any(tag.startswith(f"AWS:{key}=") for key in self.settings.tag_keys) and tag not in vm_data["tags"]:
                vm.remove_tags(tag)
        if api_version >= Version("3.7"):
            for volume in disks:
                self.add_update_vm_child(NBVirtualDisk, vm, {
                    "name": volume["VolumeId"], "virtual_machine": vm,
                    "size": volume["Size"] * (1024 if api_version >= Version("4.1") else 1),
                    "description": f"AWS EBS {volume['VolumeType']}",
                })
        for reference in instance.get("NetworkInterfaces", []):
            eni = data["NetworkInterfaces"][reference["NetworkInterfaceId"]]
            self.sync_interface(account, region, vm, eni, data, api_version)

    def add_update_vm_child(self, object_type, vm, data):
        # Display-name matching loses the VM's account/cluster when names repeat.
        existing = next((item for item in self.inventory.get_all_items(object_type)
                         if item.data.get("virtual_machine") is vm and
                         item.data.get("name") == data["name"]), None)
        if existing is None:
            return self.inventory.add_object(object_type, data=data, source=self)
        existing.update(data=data, source=self)
        return existing

    def sync_interface(self, account, region, vm, eni, data, api_version):
        interface_data = {
            "name": eni["NetworkInterfaceId"], "virtual_machine": vm, "enabled": True,
            "description": eni.get("Description", ""),
            "custom_fields": {
                "aws_eni_id": eni["NetworkInterfaceId"], "aws_vpc_id": eni.get("VpcId", ""),
                "aws_subnet_id": eni.get("SubnetId", ""),
                "aws_public_ipv4": ", ".join(sorted({
                    address["Association"]["PublicIp"]
                    for address in eni.get("PrivateIpAddresses", [])
                    if address.get("Association", {}).get("PublicIp")
                })),
            },
        }
        mac = normalize_mac_address(eni.get("MacAddress"))
        if api_version < Version("4.2"):
            interface_data["mac_address"] = mac
        interface = self.add_update_vm_child(NBVMInterface, vm, interface_data)
        if api_version >= Version("4.2") and mac is not None:
            mac_object = next((item for item in self.inventory.get_all_items(NBMACAddress)
                               if item.data.get("mac_address") == mac and
                               item.data.get("assigned_object_id") in (None, interface)), None)
            mac_data = {"mac_address": mac, "assigned_object_id": interface,
                        "assigned_object_type": NBVMInterface}
            if mac_object is None:
                mac_object = self.inventory.add_object(NBMACAddress, data=mac_data, source=self)
            else:
                mac_object.update(data=mac_data, source=self)
            for old in interface.get_mac_addresses():
                if old is not mac_object:
                    old.remove_interface_association()
            interface.update(data={"primary_mac_address": mac_object}, source=self)
        if not self.settings.sync_private_ips:
            for address in interface.get_ip_addresses():
                if self.source_tag in address.get_tags():
                    address.set_source(self)
            return
        subnet = data["Subnets"][eni["SubnetId"]]
        addresses = self.addresses(eni, subnet)
        discovered = []
        for address, primary in addresses:
            vrf = self.get_vrf(account, region, eni["VpcId"])
            parsed = ip_interface(address)
            candidates = [
                item for item in self.inventory.get_all_items(NBIPAddress)
                if ip_interface(item.data["address"]).ip == parsed.ip and item.data.get("vrf") is vrf
            ]
            if len(candidates) > 1:
                raise ValueError(f"Multiple NetBox IPs match '{address}' in VRF '{vrf.data['name']}'")
            existing = candidates[0] if candidates else None
            if existing is not None and existing.data.get("assigned_object_id") is not None and \
                    existing.get_interface() is not interface:
                owner = existing.get_device_vm()
                if owner is None or (owner.source is not self and self.source_tag not in owner.get_tags()):
                    raise ValueError(f"IP '{address}' in VRF '{vrf.data['name']}' belongs to another source")
            ip_data = {"address": address, "vrf": vrf, "assigned_object_id": interface,
                       "assigned_object_type": NBVMInterface}
            if self.settings.tenant:
                ip_data["tenant"] = {"name": self.settings.tenant}
            if existing is None:
                existing = self.inventory.add_object(NBIPAddress, data=ip_data, source=self)
            else:
                existing.update(data=ip_data, source=self)
            discovered.append(existing)
            if eni.get("Attachment", {}).get("DeviceIndex") == 0 and primary:
                vm.update(data={f"primary_ip{parsed.version}": existing}, source=self)
        for old in interface.get_ip_addresses():
            if old not in discovered and (old.source is self or self.source_tag in old.get_tags()):
                old.remove_interface_association()
