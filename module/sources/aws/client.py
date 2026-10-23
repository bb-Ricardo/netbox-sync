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
    def __init__(self, profile=None):
        if boto3 is None:
            raise AWSDiscoveryError("AWS SDK missing. Install dependencies from requirements-aws.txt.")
        try:
            self.session = boto3.Session(profile_name=profile)
        except BotoCoreError as exc:
            raise AWSDiscoveryError(f"Unable to initialize AWS credentials: {exc}") from exc

    def discover(self, regions, expected_account=None):
        """Fully collect all configured regions before any inventory changes."""
        clients = []
        try:
            config = Config(retries={"mode": "standard", "max_attempts": 5},
                            connect_timeout=10, read_timeout=60)
            sts = self.session.client("sts", region_name=regions[0], config=config)
            clients.append(sts)
            account = sts.get_caller_identity()["Account"]
            if expected_account is not None and account != expected_account:
                raise AWSDiscoveryError(
                    f"AWS account mismatch: expected {expected_account}, authenticated as {account}.")
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
