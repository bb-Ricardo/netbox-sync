from hashlib import sha256
from ipaddress import ip_address, ip_interface, ip_network
from itertools import product

from packaging.version import Version

from module.common.logging import get_logger
from module.common.misc import do_error_exit
from module.common.support import normalize_mac_address
from module.netbox import (
    NBCustomField, NBCluster, NBClusterGroup, NBClusterType, NBIPAddress,
    NBMACAddress, NBSite, NBTag, NBTenant, NBVirtualDisk, NBVM, NBVMInterface, NBVRF,
)
from module.netbox.inventory import NetBoxInventory
from module.netbox.connection import NetBoxHandler
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
            if self.settings.sync_s3 and Version(self.inventory.netbox_api_version) < Version("3.7"):
                raise ValueError("S3 bucket inventory requires NetBox 3.7 or newer")
            services = tuple(service for service in ("s3", "lambda", "eks")
                             if getattr(self.settings, f"sync_{service}"))
            options = {"services": services} if services else {}
            if self.settings.organization:
                snapshots = self.client.discover_organization(
                    self.settings.regions, self.settings.organization_role_name,
                    expected_account=self.settings.account_id,
                    discovery_region=self.settings.discovery_region,
                    include_management=self.settings.organization_include_management,
                    external_id=self.settings.organization_external_id,
                    account_ids=self.settings.organization_account_ids, **options)
                account_names = getattr(self.client, "account_names", {})
            else:
                account, snapshot = self.client.discover(
                    self.settings.regions, self.settings.account_id, self.settings.discovery_region, **options)
                snapshots = {account: snapshot} if account is not None else {}
                account_names = {}
            if self.settings.organization and self.settings.sync_private_ips:
                self.resolve_shared_networks(snapshots)
                self.defer_inaccessible_network_instances(snapshots)
            vrfs = {}
            for account, snapshot in snapshots.items():
                self.validate_services(account, snapshot)
                self.validate_snapshot(account, snapshot, vrfs)
            self.retain_disabled_services()
            if getattr(self.client, "access_denied", []):
                self.retain_inaccessible_objects()
            account_labels = {
                account: account_names.get(account) or self.settings.account_name or account
                for account in snapshots
            }
            label_counts = {}
            for label in account_labels.values():
                key = label.casefold()
                label_counts[key] = label_counts.get(key, 0) + 1
            for account, label in account_labels.items():
                if label_counts[label.casefold()] > 1:
                    account_labels[account] = f"{label} ({account})"
            for account, snapshot in snapshots.items():
                self.sync_snapshot(account, snapshot, account_name=account_labels[account])
            self.reconcile_access_tags()
        except (AWSDiscoveryError, ValueError, KeyError) as exc:
            self.abort(exc)

    def validate_services(self, account, snapshot):
        for region, data in snapshot.items():
            inaccessible = data.get("InaccessibleServices", [])
            for enabled, key in (
                (self.settings.sync_s3 and "s3" not in inaccessible, "Buckets"),
                (self.settings.sync_lambda and "lambda" not in inaccessible, "Functions"),
                (self.settings.sync_eks and "eks" not in inaccessible, "EKSClusters"),
            ):
                if enabled and not isinstance(data.get(key), list):
                    raise ValueError(f"Missing AWS {key} discovery (account '{account}', region '{region}')")
                if enabled:
                    identity_key = {"Buckets": "Name", "Functions": "FunctionArn", "EKSClusters": "arn"}[key]
                    identities = [resource[identity_key] for resource in data[key]]
                    if len(set(identities)) != len(identities):
                        raise ValueError(f"Duplicate AWS {key} discovery (account '{account}', region '{region}')")
            for bucket in data["Buckets"] if self.settings.sync_s3 and "s3" not in inaccessible else []:
                if bucket["Region"] != region or not bucket["Name"] or len(bucket["Name"]) > 63:
                    raise ValueError(f"Invalid S3 bucket '{bucket['Name']}' for region '{region}'")
                self.service_tags({tag["Key"]: tag["Value"] for tag in bucket["Tags"]})
                if "Versioning" not in bucket or "Encryption" not in bucket:
                    raise ValueError(f"Incomplete S3 bucket metadata for '{bucket['Name']}'")
            for function in data["Functions"] if self.settings.sync_lambda and "lambda" not in inaccessible else []:
                self.validate_arn(function["FunctionArn"], "lambda", account, region)
                if function["State"] not in ("Pending", "Active", "Inactive", "Failed"):
                    raise ValueError(f"Unknown Lambda state '{function['State']}' for '{function['FunctionArn']}'")
                if not function["FunctionName"] or not isinstance(function["MemorySize"], int):
                    raise ValueError(f"Incomplete Lambda configuration for '{function['FunctionArn']}'")
                self.service_tags(function["Tags"])
            if self.settings.sync_eks and "eks" not in inaccessible:
                clusters = {cluster["name"]: cluster for cluster in data["EKSClusters"]}
                if len(clusters) != len(data["EKSClusters"]):
                    raise ValueError(f"Duplicate EKS cluster discovery in account '{account}', region '{region}'")
                asg_clusters = {}
                for cluster in data["EKSClusters"]:
                    self.validate_arn(cluster["arn"], "eks", account, region)
                    for field in ("status", "version", "resourcesVpcConfig", "nodegroups", "fargateProfiles"):
                        if field not in cluster:
                            raise ValueError(f"Incomplete EKS {field} for '{cluster['arn']}'")
                    self.service_tags(cluster.get("tags", {}))
                    for group in cluster["nodegroups"]:
                        for asg in group.get("resources", {}).get("autoScalingGroups", []):
                            name = asg["name"]
                            if name in asg_clusters and asg_clusters[name] != cluster["name"]:
                                raise ValueError(f"EKS Auto Scaling group '{name}' belongs to multiple clusters")
                            asg_clusters[name] = cluster["name"]
                data["EKSWorkers"] = {}
                for instance in data["instances"]:
                    tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                    names = {tags[key] for key in (
                        "aws:eks:cluster-name", "eks:cluster-name", "eksctl.cluster.k8s.io/v1alpha1/cluster-name",
                    ) if tags.get(key)}
                    asg_cluster = asg_clusters.get(tags.get("aws:autoscaling:groupName"))
                    if asg_cluster:
                        names.add(asg_cluster)
                    if len(names) > 1:
                        raise ValueError(f"Conflicting EKS cluster membership for instance '{instance['InstanceId']}'")
                    if names:
                        name = next(iter(names))
                        if name in clusters:
                            data["EKSWorkers"][instance["InstanceId"]] = name
                        else:
                            log.warning(
                                f"AWS instance '{instance['InstanceId']}' references undiscovered EKS cluster "
                                f"'{name}' (account '{account}', region '{region}'); retaining EC2 placement.")

    def access_scope(self, item):
        fields = item.data.get("custom_fields") or {}
        metadata = fields.get("aws_metadata") or {}
        identity = fields.get("aws_resource_id") or ""
        service = "ec2" if fields.get("aws_instance_id") else fields.get("aws_service")
        account = fields.get("aws_account_id") or metadata.get("account")
        region = fields.get("aws_region") or metadata.get("region")
        resource = None
        if identity.startswith("arn:"):
            parts = identity.split(":", 5)
            if len(parts) == 6:
                service, region, account, resource = parts[2:]
                if service == "lambda":
                    resource = identity
                elif service == "eks":
                    resource = resource.removeprefix("cluster/")
        elif identity.startswith("s3/"):
            parts = identity.split("/")
            account = parts[1]
            if isinstance(item, NBVirtualDisk):
                resource = parts[2]
            else:
                region = parts[2]
        for parent_key in ("virtual_machine", "cluster"):
            parent = item.data.get(parent_key)
            if parent is not None:
                parent_account, parent_region, parent_service, _ = self.access_scope(parent)
                account = account or parent_account
                region = region or parent_region
                service = service or parent_service
                break
        if isinstance(item, (NBIPAddress, NBMACAddress)):
            parent = item.get_device_vm()
            if parent is not None:
                account, region, service, _ = self.access_scope(parent)
        if account is None:
            group = item if isinstance(item, NBClusterGroup) else item.data.get("group")
            name = group.data.get("name", "") if group is not None else ""
            if name.startswith("AWS "):
                label = name[4:]
                account = next((key for key, value in getattr(self.client, "account_names", {}).items()
                                if label in (value, f"{value} ({key})")), None)
                if account is None and label.isdigit():
                    account = label
        if region is None:
            site = item.data.get("scope_id") or item.data.get("site")
            name = site.data.get("name", "") if isinstance(site, NBSite) else ""
            if name.startswith("AWS "):
                region = name[4:]
        return account, region, service or "ec2", resource

    def reconcile_access_tags(self):
        checks = getattr(self.client, "access_checks", [])
        if not checks:
            return
        prefix = "Sync:AccessDenied:"
        scopes = {}
        for check in checks:
            outcomes = scopes.setdefault(
                (check.account, check.region, check.service, check.resource), (set(), set()))
            outcomes[0 if check.denied else 1].add(check.operation)
        for object_type in self.dependent_netbox_objects:
            for item in self.inventory.get_all_items(object_type):
                if object_type in (NBTag, NBCustomField, NBTenant) or "tags" not in item.data_model or not (
                        item.source is self or self.source_tag in item.get_tags()):
                    continue
                account, region, service, resource = self.access_scope(item)
                denied, succeeded = set(), set()
                for key in product(
                        (None, account), (None, region), ("organizations", "sts", "account", service),
                        (None, resource)):
                    if key in scopes:
                        failures, successes = scopes[key]
                        denied.update(failures)
                        succeeded.update(successes)
                for operation in denied:
                    item.add_tags(f"{prefix}{operation}")
                for tag in item.get_tags():
                    if tag.startswith(prefix) and tag[len(prefix):] in succeeded - denied:
                        item.remove_tags(tag)

    def validate_arn(self, arn, service, account, region):
        parts = arn.split(":", 5)
        if len(parts) != 6 or parts[0] != "arn" or parts[2:5] != [service, region, account] or not parts[5]:
            raise ValueError(f"Invalid {service} resource ARN '{arn}' for account '{account}', region '{region}'")

    def service_tags(self, tags):
        result = [f"AWS:{key}={tags[key]}" for key in self.settings.tag_keys if key in tags]
        if any(len(tag) > 100 for tag in result):
            raise ValueError("AWS service tag exceeds the NetBox tag name limit (100 characters)")
        return result

    def reconcile_tags(self, item, tags):
        # Tag lookup can reuse an existing name with different capitalization.
        desired_tags = {self.inventory.get_by_data(NBTag, data={"name": name}) for name in tags}
        managed_prefixes = tuple(f"AWS:{key}=".casefold() for key in self.settings.tag_keys)
        for tag in item.data.get("tags", []):
            if tag.get_display_name().casefold().startswith(managed_prefixes) and tag not in desired_tags:
                item.remove_tags(tag)

    def retain_disabled_services(self):
        for object_type in (NBVM, NBVirtualDisk, NBCluster):
            for item in self.inventory.get_all_items(object_type):
                service = (item.data.get("custom_fields") or {}).get("aws_service")
                if service in ("s3", "lambda", "eks") and not getattr(self.settings, f"sync_{service}") and \
                        (item.source is self or self.source_tag in item.get_tags()):
                    item.set_source(self)

    def retain_source_objects(self):
        # Source-wide retention also protects children and cross-account shared-network dependencies.
        for object_type in self.dependent_netbox_objects:
            for item in self.inventory.get_all_items(object_type):
                if self.source_tag in item.get_tags():
                    item.set_source(self)

    def retain_inaccessible_objects(self):
        self.retain_source_objects()
        log.warning(
            f"AWS source '{self.name}': authorization-denied discovery is incomplete. "
            "Retaining all previously managed source objects this run; accessible resources will still be updated.")

    def defer_inaccessible_network_instances(self, snapshots):
        if not any(service in ("ec2", "account", "sts", "organizations")
                   for _, _, service in getattr(self.client, "access_denied", [])):
            return
        for account, snapshot in snapshots.items():
            for region, data in snapshot.items():
                retained = []
                for instance in data["instances"]:
                    missing_network = False
                    for reference in instance.get("NetworkInterfaces", []):
                        eni = data["NetworkInterfaces"].get(reference["NetworkInterfaceId"])
                        if eni is not None and (eni["VpcId"] not in data["Vpcs"] or
                                                eni["SubnetId"] not in data["Subnets"]):
                            missing_network = True
                    if missing_network:
                        log.warning(
                            f"AWS instance '{instance['InstanceId']}' (account '{account}', region '{region}') "
                            "has missing network dependencies during authorization-denied discovery. "
                            "Deferring its update and retaining existing objects.")
                    else:
                        retained.append(instance)
                data["instances"] = retained

    def resource_name(self, name, identity, limit):
        if len(name) <= limit:
            return name
        suffix = sha256(identity.encode("utf-8")).hexdigest()[:12]
        return f"{name[:limit - 13]}-{suffix}"

    def service_object(self, object_type, identity, data):
        matches = [item for item in self.inventory.get_all_items(object_type)
                   if (item.data.get("custom_fields") or {}).get("aws_resource_id") == identity]
        error = None
        if len(matches) > 1:
            error = f"Multiple NetBox {object_type.name} objects match AWS resource '{identity}'"
        elif matches:
            item = matches[0]
            if item.source is not self and self.source_tag not in item.get_tags():
                error = f"AWS resource '{identity}' belongs to another source"
        else:
            parent = "virtual_machine" if object_type is NBVirtualDisk else "cluster" if object_type is NBVM else "group"
            if any(item.data.get("name") == data["name"] and item.data.get(parent) is data.get(parent)
                   for item in self.inventory.get_all_items(object_type)):
                error = f"NetBox {object_type.name} name collision for AWS resource '{identity}'"
        if error is not None:
            log.error(
                f"AWS source '{self.name}': {error}. Skipping this resource and any dependent resources. "
                "Retaining all previously managed source objects this run to prevent pruning skipped resources.")
            self.retain_source_objects()
            return None
        if matches:
            item.update(data=data, source=self)
        else:
            item = self.inventory.add_object(object_type, data=data, source=self)
        self.reconcile_tags(item, data["tags"])
        return item

    def service_cluster(self, account, region, site, group, service, identity, name,
                        metadata=None, tags=None):
        data = {
            "name": self.resource_name(name, identity, 100), "group": group,
            "type": {"name": f"AWS {service.upper() if service != 'lambda' else 'Lambda'}"},
            "custom_fields": {"aws_service": service, "aws_resource_id": identity,
                              "aws_metadata": metadata or {"account": account, "region": region}},
            "tags": self.service_tags(tags or {}),
        }
        if Version(self.inventory.netbox_api_version) >= Version("4.2"):
            data.update(scope_type="dcim.site", scope_id=site)
        else:
            data["site"] = site
        return self.service_object(NBCluster, identity, data)

    def logical_vm(self, account, region, site, cluster, service, identity, name, metadata, tags, **attributes):
        data = {
            "name": self.resource_name(name, identity, 64), "cluster": cluster, "status": "active",
            "comments": f"Logical AWS {service} inventory object; not an EC2 instance.",
            "custom_fields": {
                "aws_account_id": account, "aws_region": region, "aws_service": service,
                "aws_resource_id": identity, "aws_metadata": metadata,
            },
            "tags": [f"AWS:logical-{service}", *self.service_tags(tags)],
            **attributes,
        }
        if Version(self.inventory.netbox_api_version) >= Version("3.3"):
            data["site"] = site
        if self.settings.tenant:
            data["tenant"] = {"name": self.settings.tenant}
        return self.service_object(NBVM, identity, data)

    def sync_lambda(self, account, region, site, cluster, function):
        status = {"Pending": "planned", "Active": "active", "Inactive": "offline", "Failed": "failed"}
        self.logical_vm(
            account, region, site, cluster, "lambda", function["FunctionArn"],
            f"Lambda {function['FunctionName']}",
            {key: value for key, value in function.items() if key != "Tags"},
            function["Tags"], memory=function["MemorySize"], status=status[function["State"]])

    def sync_s3(self, account, region, site, group, buckets):
        if not buckets:
            return
        identity = f"s3/{account}/{region}"
        cluster = self.service_cluster(
            account, region, site, group, "s3", identity, f"S3 {account}/{region}")
        if cluster is None:
            return
        holder = self.logical_vm(
            account, region, site, cluster, "s3", identity, f"S3 inventory {account}/{region}",
            {"kind": "bucket-inventory-holder", "usage_status": "unknown"}, {})
        if holder is None:
            return
        for bucket in buckets:
            bucket_id = f"s3/{account}/{bucket['Name']}"
            self.service_object(NBVirtualDisk, bucket_id, {
                "name": bucket["Name"], "virtual_machine": holder,
                # NetBox requires size; zero is explicitly unknown, never a usage measurement.
                "size": 0, "description": "AWS S3 bucket; usage unknown (size 0 is a placeholder, not measured).",
                "custom_fields": {
                    "aws_service": "s3", "aws_resource_id": bucket_id,
                    "aws_metadata": {**{key: value for key, value in bucket.items() if key != "Tags"},
                                     "usage_status": "unknown", "usage_bytes": None},
                },
                "tags": self.service_tags({tag["Key"]: tag["Value"] for tag in bucket["Tags"]}),
            })

    def resolve_shared_networks(self, snapshots):
        networks = {}
        for account, snapshot in snapshots.items():
            for region, data in snapshot.items():
                for kind in ("Vpcs", "Subnets"):
                    for resource_id, resource in data[kind].items():
                        owner = resource.get("OwnerId", account)
                        key = (kind, region, resource_id)
                        owners = networks.setdefault(key, {})
                        if resource.get("OwnerId") and any(
                                item.get("OwnerId") and previous_owner != owner
                                for previous_owner, item in owners.items()):
                            raise ValueError(
                                f"Conflicting AWS {kind} ownership for '{resource_id}' "
                                f"(region '{region}'): {', '.join(sorted(set(owners) | {owner}))}")
                        if owner in owners:
                            previous = owners[owner]
                            fields = ("VpcId", "CidrBlock")
                            if any(previous.get(field) != resource.get(field) for field in fields) or \
                                    set(self.ipv6_networks(previous)) != set(self.ipv6_networks(resource)):
                                raise ValueError(
                                    f"Conflicting AWS {kind} discovery for '{resource_id}' "
                                    f"(owner '{owner}', region '{region}')")
                        owners[owner] = resource
        for account, snapshot in snapshots.items():
            for region, data in snapshot.items():
                data["VpcOwners"] = {}
                for instance in data["instances"]:
                    for reference in instance.get("NetworkInterfaces", []):
                        eni = data["NetworkInterfaces"].get(reference["NetworkInterfaceId"])
                        if eni is None:
                            continue
                        vpc_id, subnet_id = eni["VpcId"], eni["SubnetId"]
                        vpcs = networks.get(("Vpcs", region, vpc_id), {})
                        local_vpc = data["Vpcs"].get(vpc_id)
                        owner = local_vpc.get("OwnerId", account) if local_vpc is not None else None
                        if owner is None and len(vpcs) == 1:
                            owner = next(iter(vpcs))
                        if owner is None and vpcs:
                            raise ValueError(
                                f"Ambiguous AWS VPC ownership for '{vpc_id}' "
                                f"(account '{account}', region '{region}', "
                                f"ENI '{eni['NetworkInterfaceId']}'): {', '.join(sorted(vpcs))}")
                        if owner is None:
                            continue
                        data["VpcOwners"][vpc_id] = owner
                        data["Vpcs"][vpc_id] = dict(vpcs[owner], OwnerId=owner)
                        subnets = networks.get(("Subnets", region, subnet_id), {})
                        if subnets and owner not in subnets:
                            raise ValueError(
                                f"Conflicting AWS subnet ownership for '{subnet_id}' "
                                f"(account '{account}', region '{region}', "
                                f"ENI '{eni['NetworkInterfaceId']}', VPC owner '{owner}'): "
                                f"{', '.join(sorted(subnets))}")
                        if owner in subnets:
                            data["Subnets"][subnet_id] = dict(subnets[owner], OwnerId=owner)

    def vrf_name(self, account, region, vpc):
        return self.settings.vpc_vrf_relation.get(
            f"{account}/{region}/{vpc}", self.settings.vpc_vrf_relation.get(
                f"{region}/{vpc}", f"AWS {account}/{region}/{vpc}"))

    def get_vrf(self, account, region, vpc):
        account_region_vpc = f"{account}/{region}/{vpc}"
        region_vpc = f"{region}/{vpc}"
        has_mapping = account_region_vpc in self.settings.vpc_vrf_relation or \
            region_vpc in self.settings.vpc_vrf_relation
        name = self.vrf_name(account, region, vpc)
        vrf = self.inventory.get_by_data(NBVRF, data={"name": name})
        if vrf is None:
            if has_mapping:
                raise ValueError(f"Create mapped NetBox VRF '{name}' for VPC '{vpc}' before importing its IPs")
            vrf = self.inventory.add_object(NBVRF, data={"name": name}, source=self)
        return vrf

    def ipv6_networks(self, resource):
        return [
            ip_network(entry["Ipv6CidrBlock"])
            for entry in resource.get("Ipv6CidrBlockAssociationSet", [])
            if entry.get("Ipv6CidrBlockState", {}).get("State") == "associated"
        ]

    def addresses(self, eni, subnet):
        networks = [ip_network(subnet["CidrBlock"]), *self.ipv6_networks(subnet)]
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
                    network_errors = []
                    if vpc not in data["Vpcs"]:
                        network_errors.append(f"VPC '{vpc}' was not returned by DescribeVpcs")
                    if subnet is None:
                        network_errors.append(
                            f"subnet '{eni['SubnetId']}' was not returned by DescribeSubnets")
                    elif subnet["VpcId"] != vpc:
                        network_errors.append(
                            f"subnet '{eni['SubnetId']}' belongs to VPC '{subnet['VpcId']}', "
                            f"not ENI VPC '{vpc}'")
                    if network_errors:
                        raise ValueError(
                            f"Incomplete VPC/subnet discovery for ENI '{eni['NetworkInterfaceId']}' "
                            f"(account '{account}', region '{region}', instance '{instance['InstanceId']}', "
                            f"VPC '{vpc}', subnet '{eni['SubnetId']}'): {'; '.join(network_errors)}")
                    if self.addresses(eni, subnet):
                        owner = data.get("VpcOwners", {}).get(vpc, account)
                        vrf = self.get_vrf(owner, region, vpc)
                        identity = (owner, region, vpc)
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
        if any((self.settings.sync_s3, self.settings.sync_lambda, self.settings.sync_eks)):
            types = [NBVM.object_type, NBCluster.object_type, NBVirtualDisk.object_type]
            for name, field_type in (
                ("aws_service", "text"), ("aws_resource_id", "text"),
                ("aws_metadata", "json"),
            ):
                field = self.add_update_custom_field({
                    "name": name, "label": name.replace("_", " ").upper(),
                    "object_types": types, "type": field_type,
                    "description": f"Reported by AWS ({self.name})",
                })
                existing_type = field.data.get("type")
                if isinstance(existing_type, dict):
                    existing_type = existing_type.get("value")
                if existing_type != field_type:
                    raise ValueError(
                        f"NetBox custom field '{name}' must have type '{field_type}', not '{existing_type}'")

    def sync_snapshot(self, account, snapshot, account_name=None):
        self.custom_fields()
        account_name = account_name or account
        group = self.inventory.add_update_object(
            NBClusterGroup, data={"name": f"AWS {account_name}"}, source=self)
        api_version = Version(self.inventory.netbox_api_version)
        for region, data in snapshot.items():
            site = self.inventory.add_update_object(
                NBSite, data={"name": f"AWS {region}"}, source=self)
            cluster_data = {
                "name": f"AWS {account_name}/{region}", "group": group,
                "type": {"name": "AWS EC2"},
            }
            if api_version >= Version("4.2"):
                cluster_data.update(scope_type="dcim.site", scope_id=site)
            else:
                cluster_data["site"] = site
            cluster = self.inventory.add_update_object(NBCluster, data=cluster_data, source=self)
            eks_clusters = {}
            inaccessible = data.get("InaccessibleServices", [])
            if self.settings.sync_eks and "eks" not in inaccessible:
                for resource in data["EKSClusters"]:
                    eks_cluster = self.service_cluster(
                        account, region, site, group, "eks", resource["arn"],
                        f"EKS {account}/{region}/{resource['name']}",
                        metadata={key: value for key, value in resource.items() if key != "tags"},
                        tags=resource.get("tags", {}))
                    eks_clusters[resource["name"]] = eks_cluster
            if self.settings.sync_lambda and "lambda" not in inaccessible:
                lambda_cluster = self.service_cluster(
                    account, region, site, group, "lambda", f"lambda/{account}/{region}",
                    f"Lambda {account}/{region}")
                if lambda_cluster is not None:
                    for function in data["Functions"]:
                        self.sync_lambda(account, region, site, lambda_cluster, function)
            if self.settings.sync_s3 and "s3" not in inaccessible:
                self.sync_s3(account, region, site, group, data["Buckets"])
            name_counts = {}
            for instance in data["instances"]:
                name = next((tag["Value"].strip() for tag in instance.get("Tags", [])
                             if tag["Key"] == "Name" and tag["Value"].strip()), instance["InstanceId"])
                key = name.casefold()
                name_counts[key] = name_counts.get(key, 0) + 1
            for instance in data["instances"]:
                instance_cluster = eks_clusters.get(
                    data.get("EKSWorkers", {}).get(instance["InstanceId"]), cluster)
                if instance_cluster is None:
                    log.error(
                        f"AWS source '{self.name}': Skipping instance '{instance['InstanceId']}' "
                        f"(account '{account}', region '{region}') because its EKS cluster was skipped.")
                    continue
                tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
                vm_name = tags.get("Name", "").strip() or instance["InstanceId"]
                if name_counts[vm_name.casefold()] > 1:
                    vm_name = f"{vm_name} ({instance['InstanceId']})"
                self.sync_instance(
                    account, region, instance, data, site,
                    instance_cluster,
                    api_version, vm_name=vm_name)

    def sync_instance(self, account, region, instance, data, site, cluster, api_version, vm_name=None):
        instance_id = instance["InstanceId"]
        if "eks" in data.get("InaccessibleServices", []):
            existing = next((item for item in self.inventory.get_all_items(NBVM)
                             if (item.data.get("custom_fields") or {}).get("aws_account_id") == account and
                             (item.data.get("custom_fields") or {}).get("aws_instance_id") == instance_id), None)
            if existing is not None and existing.data.get("cluster") is not None:
                cluster = existing.data["cluster"]
        tags = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
        spec = data["InstanceTypes"][instance["InstanceType"]]
        vm_data = {
            "name": vm_name or tags.get("Name", "").strip() or instance_id, "cluster": cluster,
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
        vm = next((item for item in self.inventory.get_all_items(NBVM)
                   if (item.data.get("custom_fields") or {}).get("aws_account_id") == account and
                   (item.data.get("custom_fields") or {}).get("aws_instance_id") == instance_id), None)
        if vm is None:
            vm = self.inventory.add_update_object(NBVM, data=vm_data, source=self)
        else:
            vm.update(data=vm_data, source=self)
        self.reconcile_tags(vm, vm_data["tags"])
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
            owner = data.get("VpcOwners", {}).get(eni["VpcId"], account)
            vrf = self.get_vrf(owner, region, eni["VpcId"])
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
                owner_tags = owner.get_tags() if owner is not None else []
                same_source = owner is not None and (
                    owner.source is self or self.source_tag in owner_tags)
                managed_owner = same_source or NetBoxHandler.primary_tag in owner_tags
                managed_orphan = NetBoxHandler.primary_tag in owner_tags and \
                    NetBoxHandler.orphaned_tag in owner_tags
                owner_status = owner.data.get("status") if owner is not None else None
                if isinstance(owner_status, dict):
                    owner_status = owner_status.get("value")
                if managed_owner and owner_status == "active" and vm.data["status"] == "offline":
                    log.error(
                        f"AWS source '{self.name}': IP '{address}' in VRF '{vrf.data['name']}' "
                        f"is assigned to active owner '{owner.get_display_name()}'. "
                        f"Skipping reassignment to offline ENI '{eni['NetworkInterfaceId']}'.")
                    continue
                offline_to_active = managed_owner and owner_status == "offline" and vm.data["status"] == "active"
                if not (same_source or managed_orphan or offline_to_active):
                    owner_name = owner.get_display_name() if owner is not None else "unresolved owner"
                    log.error(
                        f"AWS source '{self.name}': IP '{address}' in VRF '{vrf.data['name']}' "
                        f"belongs to another source (owner '{owner_name}'). "
                        f"Skipping reassignment to ENI '{eni['NetworkInterfaceId']}'.")
                    continue
                if managed_orphan or offline_to_active:
                    reason = "orphaned" if managed_orphan else "offline"
                    log.warning(
                        f"AWS source '{self.name}': Reclaiming IP '{address}' in VRF '{vrf.data['name']}' "
                        f"from {reason} owner '{owner.get_display_name()}' "
                        f"for ENI '{eni['NetworkInterfaceId']}'.")
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
