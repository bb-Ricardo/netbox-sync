from contextlib import contextmanager
from dataclasses import dataclass

from module.common.logging import get_logger

log = get_logger()

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError
except ModuleNotFoundError as exc:
    if exc.name != "boto3":
        raise
    boto3 = None


class AWSDiscoveryError(RuntimeError):
    """Discovery failed; do not reconcile this run with NetBox."""


@dataclass(frozen=True)
class AWSAccessCheck:
    account: str | None
    region: str | None
    service: str
    resource: str | None
    operation: str
    denied: bool


class AWSClient:
    def __init__(self, profile=None, *, session=None):
        if boto3 is None:
            raise AWSDiscoveryError("AWS SDK missing. Install dependencies from requirements-aws.txt.")
        try:
            self.session = session if session is not None else boto3.Session(profile_name=profile)
        except BotoCoreError as exc:
            raise AWSDiscoveryError(f"Unable to initialize AWS credentials: {exc}") from exc
        self.account_names = {}
        self.access_denied = []
        self.access_checks = []

    def track_access(self, client, account, region, service):
        def capture_resource(params, context, **kwargs):
            context["netbox_sync_resource"] = next((
                params[key] for key in ("Bucket", "clusterName", "FunctionName", "Resource", "name")
                if key in params
            ), None)

        def record_access(model, parsed, context, **kwargs):
            code = parsed.get("Error", {}).get("Code")
            denied = code in (
                "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                "AuthorizationError", "AuthorizationErrorException", "Forbidden", "ForbiddenException")
            if code is None or denied or code in (
                    "NoSuchTagSet", "ServerSideEncryptionConfigurationNotFoundError"):
                check_account = parsed.get("Account", account) if model.name == "GetCallerIdentity" else account
                self.access_checks.append(AWSAccessCheck(
                    check_account, region, service, context.get("netbox_sync_resource"), model.name, denied))

        for event, handler in (
                ("before-parameter-build.*.*", capture_resource), ("after-call.*.*", record_access)):
            unique_id = f"netbox-sync-access-{event}"
            client.meta.events.unregister(event, unique_id=unique_id)
            client.meta.events.register(event, handler, unique_id=unique_id)

    @contextmanager
    def tolerate_access_denied(self, account=None, region=None, service=None, resource=None):
        try:
            yield
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in (
                    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
                    "AuthorizationError", "AuthorizationErrorException", "Forbidden", "ForbiddenException"):
                raise AWSDiscoveryError(f"Incomplete AWS discovery: {exc}") from exc
            self.access_denied.append((account, region, service))
            log.warning(
                f"AWS access denied (account '{account}', region '{region}', service '{service}', "
                f"resource '{resource}'): {exc}. "
                "Skipping inaccessible discovery; existing source objects will be retained.")

    def discover_organization(self, regions, role_name, expected_account=None, discovery_region=None,
                              include_management=False, external_id=None, account_ids=None, services=()):
        """Collect selected accounts, recording authorization-denied scopes for retention."""
        self.access_denied = []
        self.access_checks = []
        clients = []
        current_account = None
        try:
            bootstrap = (discovery_region or self.session.region_name) if regions == ["auto"] else regions[0]
            if not bootstrap:
                raise AWSDiscoveryError(
                    "Organization discovery requires discovery_region or an AWS profile/environment region "
                    "(AWS_DEFAULT_REGION).")
            config = Config(retries={"mode": "standard", "max_attempts": 5},
                            connect_timeout=10, read_timeout=60)
            sts = self.session.client("sts", region_name=bootstrap, config=config)
            self.track_access(sts, None, None, "sts")
            clients.append(sts)
            identity = sts.get_caller_identity()
            if expected_account is not None and identity["Account"] != expected_account:
                raise AWSDiscoveryError(
                    f"AWS account mismatch: expected {expected_account}, authenticated as {identity['Account']}.")
            partition = identity["Arn"].split(":")[1]
            organizations = self.session.client("organizations", region_name=bootstrap, config=config)
            self.track_access(organizations, None, None, "organizations")
            clients.append(organizations)
            management_account = organizations.describe_organization()["Organization"]["MasterAccountId"]
            accounts = set()
            account_names = {}
            for page in organizations.get_paginator("list_accounts").paginate():
                for account in page["Accounts"]:
                    account_id = account["Id"]
                    if account["State"] != "ACTIVE":
                        log.info(f"AWS organization: excluding account '{account_id}' in state '{account['State']}'.")
                        continue
                    if account_id == management_account and not include_management:
                        continue
                    accounts.add(account_id)
                    account_names[account_id] = account.get("Name") or account_id
            if account_ids:
                missing = set(account_ids) - accounts
                if missing:
                    raise AWSDiscoveryError(
                        f"Requested organization accounts are not active/eligible: {', '.join(sorted(missing))}.")
                accounts.intersection_update(account_ids)
            if not accounts:
                raise AWSDiscoveryError("Organization discovery returned no eligible accounts.")
            snapshots = {}
            for current_account in sorted(accounts):
                role_arn = f"arn:{partition}:iam::{current_account}:role/{role_name}"
                log.info(f"AWS organization: discovering account '{current_account}' using '{role_arn}'.")
                request = {"RoleArn": role_arn, "RoleSessionName": "netbox-sync", "DurationSeconds": 3600}
                if external_id:
                    request["ExternalId"] = external_id
                credentials = None
                self.track_access(sts, current_account, None, "sts")
                with self.tolerate_access_denied(current_account, service="sts"):
                    credentials = sts.assume_role(**request)["Credentials"]
                if credentials is None:
                    continue
                member = AWSClient(session=boto3.Session(
                    aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"],
                    aws_session_token=credentials["SessionToken"], region_name=bootstrap))
                options = {"services": services} if services else {}
                _, snapshot = member.discover(regions, current_account, discovery_region, **options)
                self.access_denied.extend(member.access_denied)
                self.access_checks.extend(member.access_checks)
                snapshots[current_account] = snapshot
            self.account_names = account_names
            return snapshots
        except ClientError as exc:
            with self.tolerate_access_denied(service="organizations"):
                raise exc
            return {}
        except (BotoCoreError, KeyError, AWSDiscoveryError) as exc:
            context = f" for account '{current_account}'" if current_account else ""
            raise AWSDiscoveryError(f"Incomplete AWS organization discovery{context}: {exc}") from exc
        finally:
            for client in clients:
                client.close()

    def discover(self, regions, expected_account=None, discovery_region=None, services=()):
        """Collect accessible regions and record denied scopes before inventory changes."""
        clients = []
        self.access_denied = []
        self.access_checks = []
        account = expected_account
        try:
            config = Config(retries={"mode": "standard", "max_attempts": 5},
                            connect_timeout=10, read_timeout=60)
            automatic = regions == ["auto"]
            bootstrap = (discovery_region or self.session.region_name) if automatic else regions[0]
            if not bootstrap:
                raise AWSDiscoveryError(
                    "Automatic region discovery requires discovery_region or an AWS profile/environment region "
                    "(AWS_DEFAULT_REGION).")
            sts = self.session.client("sts", region_name=bootstrap, config=config)
            self.track_access(sts, account, None, "account")
            clients.append(sts)
            account = sts.get_caller_identity()["Account"]
            if expected_account is not None and account != expected_account:
                raise AWSDiscoveryError(
                    f"AWS account mismatch: expected {expected_account}, authenticated as {account}.")
            if automatic:
                discovery_client = self.session.client("ec2", region_name=bootstrap, config=config)
                self.track_access(discovery_client, account, None, "account")
                clients.append(discovery_client)
                response = discovery_client.describe_regions(AllRegions=True)
                regions = sorted({
                    entry["RegionName"] for entry in response["Regions"]
                    if entry["OptInStatus"] in ("opt-in-not-required", "opted-in")
                })
                if not regions:
                    raise AWSDiscoveryError("Automatic region discovery returned no enabled regions.")
                log.info(f"AWS account '{account}' enabled regions: {', '.join(regions)}")
            snapshot = {}
            for region in regions:
                ec2 = self.session.client("ec2", region_name=region, config=config)
                self.track_access(ec2, account, region, "ec2")
                clients.append(ec2)
                data = None
                with self.tolerate_access_denied(account, region, "ec2"):
                    data = self.discover_ec2(ec2)
                if data is None:
                    data = {"instances": [], "InstanceTypes": {}, "Vpcs": {}, "Subnets": {},
                            "NetworkInterfaces": {}, "Volumes": {}, "EC2AccessDenied": True}
                data["InaccessibleServices"] = []
                for service in services:
                    service_client = self.session.client(service, region_name=region, config=config)
                    self.track_access(service_client, account, region, service)
                    clients.append(service_client)
                    try:
                        with self.tolerate_access_denied(account, region, service):
                            if service == "s3":
                                data["Buckets"] = self.discover_s3(service_client, region, account)
                            elif service == "lambda":
                                data["Functions"] = self.discover_lambda(service_client)
                            elif service == "eks":
                                data["EKSClusters"] = self.discover_eks(service_client)
                            else:
                                raise AWSDiscoveryError(f"Unsupported AWS service '{service}'")
                        key = {"s3": "Buckets", "lambda": "Functions", "eks": "EKSClusters"}[service]
                        if key not in data:
                            data["InaccessibleServices"].append(service)
                    except (BotoCoreError, ClientError, KeyError) as exc:
                        raise AWSDiscoveryError(
                            f"Incomplete {service} discovery (account '{account}', region '{region}'): {exc}") from exc
                snapshot[region] = data
            return account, snapshot
        except ClientError as exc:
            with self.tolerate_access_denied(account, service="account"):
                raise exc
            return account, {}
        except (BotoCoreError, KeyError) as exc:
            raise AWSDiscoveryError(f"Incomplete AWS discovery; synchronization aborted: {exc}") from exc
        finally:
            for client in clients:
                client.close()

    def discover_ec2(self, ec2):
        instances = [
            instance
            for page in ec2.get_paginator("describe_instances").paginate()
            for reservation in page.get("Reservations", [])
            for instance in reservation.get("Instances", [])
            if instance["State"]["Name"] != "terminated"
        ]
        data = {"instances": instances}
        for operation, result_key, key in (
            ("describe_network_interfaces", "NetworkInterfaces", "NetworkInterfaceId"),
            ("describe_volumes", "Volumes", "VolumeId"),
            ("describe_subnets", "Subnets", "SubnetId"),
            ("describe_vpcs", "Vpcs", "VpcId"),
        ):
            data[result_key] = {
                item[key]: item
                for page in ec2.get_paginator(operation).paginate()
                for item in page.get(result_key, [])
            }
        instance_types = {}
        names = sorted({item["InstanceType"] for item in instances})
        for start in range(0, len(names), 100):
            for page in ec2.get_paginator("describe_instance_types").paginate(InstanceTypes=names[start:start + 100]):
                instance_types.update({item["InstanceType"]: item for item in page["InstanceTypes"]})
        data["InstanceTypes"] = instance_types
        return data

    def discover_s3(self, client, region, account):
        buckets = []
        for page in client.get_paginator("list_buckets").paginate(BucketRegion=region):
            for bucket in page["Buckets"]:
                with self.tolerate_access_denied(account, region, "s3", bucket["Name"]):
                    buckets.append(self.discover_s3_bucket(client, bucket, region, account))
        return buckets

    def discover_s3_bucket(self, client, bucket, region, account):
        name = bucket["Name"]
        bucket_region = bucket.get("BucketRegion")
        if bucket_region is None:
            location = client.get_bucket_location(Bucket=name, ExpectedBucketOwner=account)
            bucket_region = location["LocationConstraint"] or "us-east-1"
            if bucket_region == "EU":
                bucket_region = "eu-west-1"
        if bucket_region != region:
            raise AWSDiscoveryError(
                f"S3 bucket '{name}' returned for region '{region}' belongs to '{bucket_region}'")
        request = {"Bucket": name, "ExpectedBucketOwner": account}
        try:
            tags = client.get_bucket_tagging(**request)["TagSet"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "NoSuchTagSet":
                raise
            tags = []
        versioning = client.get_bucket_versioning(**request).get("Status", "Unversioned")
        try:
            encryption = client.get_bucket_encryption(**request)["ServerSideEncryptionConfiguration"]
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "ServerSideEncryptionConfigurationNotFoundError":
                raise
            encryption = {}
        return {
            "Name": name, "Region": region, "Tags": tags, "Versioning": versioning,
            "Encryption": encryption, "Arn": f"arn:{client.meta.partition}:s3:::{name}",
        }

    def discover_lambda(self, client):
        functions = []
        for page in client.get_paginator("list_functions").paginate():
            for reference in page["Functions"]:
                config = client.get_function_configuration(FunctionName=reference["FunctionArn"])
                # Do not retain environment variables or other unneeded configuration.
                function = {key: config[key] for key in (
                    "FunctionArn", "FunctionName", "MemorySize", "State", "Runtime", "Handler",
                    "Timeout", "LastModified", "Architectures", "PackageType", "VpcConfig",
                ) if key in config}
                if function["FunctionArn"] != reference["FunctionArn"]:
                    raise AWSDiscoveryError(f"Lambda function identity changed for '{reference['FunctionArn']}'")
                function["Tags"] = client.list_tags(Resource=function["FunctionArn"]).get("Tags", {})
                functions.append(function)
        return functions

    def discover_eks(self, client):
        clusters = []
        for page in client.get_paginator("list_clusters").paginate():
            for name in page["clusters"]:
                cluster = client.describe_cluster(name=name)["cluster"]
                if cluster["name"] != name:
                    raise AWSDiscoveryError(f"EKS cluster identity changed for '{name}'")
                groups = []
                for group_page in client.get_paginator("list_nodegroups").paginate(clusterName=name):
                    for group_name in group_page["nodegroups"]:
                        group = client.describe_nodegroup(clusterName=name, nodegroupName=group_name)["nodegroup"]
                        if group["clusterName"] != name or group["nodegroupName"] != group_name:
                            raise AWSDiscoveryError(f"EKS node group identity changed for '{group_name}'")
                        groups.append({key: group[key] for key in (
                            "nodegroupName", "nodegroupArn", "status", "version", "capacityType",
                            "instanceTypes", "scalingConfig", "subnets", "resources",
                        ) if key in group})
                profiles = []
                for profile_page in client.get_paginator("list_fargate_profiles").paginate(clusterName=name):
                    for profile_name in profile_page["fargateProfileNames"]:
                        profile = client.describe_fargate_profile(
                            clusterName=name, fargateProfileName=profile_name)["fargateProfile"]
                        if profile["clusterName"] != name or profile["fargateProfileName"] != profile_name:
                            raise AWSDiscoveryError(f"EKS Fargate profile identity changed for '{profile_name}'")
                        profiles.append({key: profile[key] for key in (
                            "fargateProfileName", "fargateProfileArn", "status", "subnets", "selectors",
                        ) if key in profile})
                clusters.append({
                    **{key: cluster[key] for key in (
                        "name", "arn", "status", "version", "endpoint", "resourcesVpcConfig", "tags",
                    ) if key in cluster},
                    "nodegroups": sorted(groups, key=lambda group: group["nodegroupName"]),
                    "fargateProfiles": sorted(profiles, key=lambda profile: profile["fargateProfileName"]),
                })
        return clusters
