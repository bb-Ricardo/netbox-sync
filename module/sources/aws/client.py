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


class AWSClient:
    def __init__(self, profile=None, *, session=None):
        if boto3 is None:
            raise AWSDiscoveryError("AWS SDK missing. Install dependencies from requirements-aws.txt.")
        try:
            self.session = session if session is not None else boto3.Session(profile_name=profile)
        except BotoCoreError as exc:
            raise AWSDiscoveryError(f"Unable to initialize AWS credentials: {exc}") from exc

    def discover_organization(self, regions, role_name, expected_account=None, discovery_region=None,
                              include_management=False, external_id=None, account_ids=None):
        """Collect all selected accounts; never return a partial organization."""
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
            clients.append(sts)
            identity = sts.get_caller_identity()
            if expected_account is not None and identity["Account"] != expected_account:
                raise AWSDiscoveryError(
                    f"AWS account mismatch: expected {expected_account}, authenticated as {identity['Account']}.")
            partition = identity["Arn"].split(":")[1]
            organizations = self.session.client("organizations", region_name=bootstrap, config=config)
            clients.append(organizations)
            management_account = organizations.describe_organization()["Organization"]["MasterAccountId"]
            accounts = set()
            for page in organizations.get_paginator("list_accounts").paginate():
                for account in page["Accounts"]:
                    account_id = account["Id"]
                    if account["State"] != "ACTIVE":
                        log.info(f"AWS organization: excluding account '{account_id}' in state '{account['State']}'.")
                        continue
                    if account_id == management_account and not include_management:
                        continue
                    accounts.add(account_id)
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
                credentials = sts.assume_role(**request)["Credentials"]
                member = AWSClient(session=boto3.Session(
                    aws_access_key_id=credentials["AccessKeyId"],
                    aws_secret_access_key=credentials["SecretAccessKey"],
                    aws_session_token=credentials["SessionToken"], region_name=bootstrap))
                _, snapshot = member.discover(regions, current_account, discovery_region)
                snapshots[current_account] = snapshot
            return snapshots
        except (BotoCoreError, ClientError, KeyError, AWSDiscoveryError) as exc:
            context = f" for account '{current_account}'" if current_account else ""
            raise AWSDiscoveryError(f"Incomplete AWS organization discovery{context}: {exc}") from exc
        finally:
            for client in clients:
                client.close()

    def discover(self, regions, expected_account=None, discovery_region=None):
        """Fully collect all configured regions before any inventory changes."""
        clients = []
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
            clients.append(sts)
            account = sts.get_caller_identity()["Account"]
            if expected_account is not None and account != expected_account:
                raise AWSDiscoveryError(
                    f"AWS account mismatch: expected {expected_account}, authenticated as {account}.")
            if automatic:
                discovery_client = self.session.client("ec2", region_name=bootstrap, config=config)
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
                clients.append(ec2)
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
                    for page in ec2.get_paginator("describe_instance_types").paginate(
                            InstanceTypes=names[start:start + 100]):
                        instance_types.update({item["InstanceType"]: item for item in page["InstanceTypes"]})
                data["InstanceTypes"] = instance_types
                snapshot[region] = data
            return account, snapshot
        except (BotoCoreError, ClientError, KeyError) as exc:
            raise AWSDiscoveryError(f"Incomplete AWS discovery; synchronization aborted: {exc}") from exc
        finally:
            for client in clients:
                client.close()
