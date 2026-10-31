# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

"""
boto3-backed discovery client for the AWS source.

AWS is queried read-only; this module never performs a write call against AWS.
All discovery failures that are not a recognized authorization denial are
raised as AWSDiscoveryError, which the source handler turns into a fatal exit
(no NetBox writes or pruning for that run). Recognized authorization denials
are tolerated: they are logged, recorded, and discovery of everything else
continues - see tolerate_access_denied() and docs/source_aws.md.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - exercised via monkeypatching boto3 = None
    boto3 = None
    Config = None
    ClientError = Exception

from module.common.logging import get_logger

log = get_logger()

# Error codes AWS uses for an authorization denial, across the services this
# source talks to. Anything else (expired/invalid credentials, throttling,
# malformed requests, ...) is left fatal.
AUTH_ERROR_CODES = {
    "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
    "AuthorizationError", "AuthorizationErrorException",
    "Forbidden", "ForbiddenException",
}

MAX_ITEMS_PER_PAGE = 20

# Every client retries throttled and transient failures with backoff and gives up on an
# unreachable endpoint after 10 seconds instead of the botocore default of 60.
BOTO_CONFIG = Config(
    retries={"mode": "standard", "max_attempts": 5}, connect_timeout=10, read_timeout=60
) if Config is not None else None


class AWSDiscoveryError(Exception):
    """A fatal AWS discovery failure. The caller must not write to NetBox."""


@dataclass(frozen=True)
class AWSAccessCheck:
    account: Optional[str]
    region: Optional[str]
    service: str
    resource: Optional[str]
    operation: str
    denied: bool


class AWSClient:
    # class-level fallbacks: tests monkeypatch __init__ to a no-op and still
    # read these before any discovery call sets a real per-instance list.
    access_denied = []
    access_checks = []
    account_names = {}

    def __init__(self, profile=None):
        if boto3 is None:
            raise AWSDiscoveryError(
                "the 'boto3' package is required for the AWS source; "
                "install it with 'pip install -r requirements-aws.txt'"
            )
        self.session = boto3.Session(profile_name=profile) if profile else boto3.Session()
        self.access_denied = []
        self.access_checks = []
        self.account_names = {}
        self._tracked_scopes = {}

    # -- access tracking ----------------------------------------------------

    def track_access(self, client, account, region, service):
        """
        Record every call made through `client` as an AWSAccessCheck, scoped
        to (account, region, service). Calling this again for the same client
        instance replaces the scope used for subsequent calls; it does not
        stack. Used where one client is reused across many scopes in a loop
        (member-account assume-role calls during organization discovery).
        """
        key = id(client)
        self._tracked_scopes[key] = (account, region, service)
        if key not in getattr(self, "_hooked_clients", set()):
            if not hasattr(self, "_hooked_clients"):
                self._hooked_clients = set()
            self._hooked_clients.add(key)

            def after_call(http_response, parsed, model, context, **kwargs):
                # AWS service-level errors (including AccessDenied) surface here as a
                # normal parsed response with an "Error" key; botocore raises the
                # actual exception to the caller only after this event fires.
                # after-call-error is for transport failures (DNS, timeouts, ...),
                # not service-side denials, so it is deliberately not hooked here.
                scope = self._tracked_scopes.get(key, (account, region, service))
                operation = model.name if model else None
                if not operation:
                    return
                error_code = (parsed or {}).get("Error", {}).get("Code")
                denied = error_code in AUTH_ERROR_CODES
                self.access_checks.append(AWSAccessCheck(*scope, None, operation, denied))

            client.meta.events.register("after-call.*.*", after_call)

    @contextmanager
    def tolerate_access_denied(self, account, region=None, service=None, resource=None, operation=None):
        """
        Run a single AWS call, tolerating a recognized authorization denial:
        logs it, records it in access_denied (coarse, deduplicated) and, when
        `resource` or `operation` is given, in access_checks (fine-grained,
        for permission-tag reconciliation), then suppresses the exception so
        discovery of everything else continues. Any other ClientError is
        re-raised as a fatal AWSDiscoveryError.
        """
        try:
            yield
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            op = operation or exc.operation_name
            if code in AUTH_ERROR_CODES:
                where = " ".join(filter(None, [
                    f"account '{account}'" if account else None,
                    f"region '{region}'" if region else None,
                    f"service '{service}'" if service else None,
                    f"resource '{resource}'" if resource else None,
                ]))
                log.warning(f"AWS {code} on {op}{(' for ' + where) if where else ''}: "
                            f"{exc.response.get('Error', {}).get('Message', '')}")
                scope = (account, region, service)
                if scope not in self.access_denied:
                    self.access_denied.append(scope)
                if resource is not None or operation is not None:
                    self.access_checks.append(AWSAccessCheck(account, region, service, resource, op, True))
                return
            raise AWSDiscoveryError(f"{code or type(exc).__name__}: {exc}") from exc
        else:
            if resource is not None or operation is not None:
                self.access_checks.append(
                    AWSAccessCheck(account, region, service, resource, operation or "", False))

    def _tolerated(self, operation, account, region=None, service=None, resource=None):
        """Call `operation` (a zero-arg callable), tolerating denial. Returns the
        result, or None if the call was denied and tolerated."""
        result = None
        with self.tolerate_access_denied(account, region, service, resource, operation.__name__
                                         if hasattr(operation, "__name__") else None):
            result = operation()
        return result

    # -- identity / regions ---------------------------------------------------

    def _bootstrap_region(self, regions, discovery_region):
        if regions == ["auto"]:
            region = discovery_region or self.session.region_name
            if not region:
                raise AWSDiscoveryError(
                    "AWS region auto-discovery needs a bootstrap region: set 'discovery_region', "
                    "AWS_DEFAULT_REGION, or a region in the AWS profile/config.")
            return region
        return regions[0]

    def _get_identity(self, bootstrap_region, account, sts=None):
        sts = sts or self.session.client("sts", region_name=bootstrap_region, config=BOTO_CONFIG)
        identity_account = None
        with self.tolerate_access_denied(None, None, "account"):
            response = sts.get_caller_identity()
            identity_account = response["Account"]
            # arn:<partition>:iam::<account>:user/...
            self._partition = response["Arn"].split(":")[1]
        if identity_account is None:
            return None
        if account is not None and identity_account != account:
            raise AWSDiscoveryError(
                f"AWS credential account mismatch: expected '{account}', got '{identity_account}'")
        return identity_account

    def _enabled_regions(self, bootstrap_region, account):
        ec2 = self.session.client("ec2", region_name=bootstrap_region, config=BOTO_CONFIG)
        enabled = set()
        denied_scope = (account, None, "ec2")
        with self.tolerate_access_denied(*denied_scope):
            response = ec2.describe_regions(AllRegions=True)
            for entry in response["Regions"]:
                if entry.get("OptInStatus") in ("opt-in-not-required", "opted-in"):
                    enabled.add(entry["RegionName"])
        if not enabled:
            if denied_scope in self.access_denied:
                return []
            raise AWSDiscoveryError("AWS region auto-discovery found no enabled regions")
        return sorted(enabled)

    # -- single-account discovery --------------------------------------------

    def discover(self, regions, account=None, discovery_region=None, services=()):
        # Starts a fresh access_denied/access_checks run so a prior denial does
        # not linger forever once access is restored. Member clients created by
        # discover_organization() share one access_denied/access_checks list
        # across accounts and must not reset it on every member's call.
        if not getattr(self, "_is_org_member", False):
            self.access_denied = []
            self.access_checks = []
        bootstrap_region = self._bootstrap_region(regions, discovery_region)
        identity_account = self._get_identity(bootstrap_region, account)
        if identity_account is None:
            return account, {}
        if regions == ["auto"]:
            regions_to_scan = self._enabled_regions(bootstrap_region, identity_account)
        else:
            regions_to_scan = regions
        snapshot = {}
        for region in regions_to_scan:
            snapshot[region] = self._discover_region(region, identity_account, services)
        return identity_account, snapshot

    def _discover_region(self, region, account, services):
        ec2 = self.session.client("ec2", region_name=region, config=BOTO_CONFIG)
        data = {
            "instances": [], "InstanceTypes": {}, "NetworkInterfaces": {},
            "Volumes": {}, "Subnets": {}, "Vpcs": {},
        }
        ec2_ok = False
        with self.tolerate_access_denied(account, region, "ec2"):
            data["instances"] = self._describe_instances(ec2)
            data["NetworkInterfaces"] = {
                item["NetworkInterfaceId"]: item
                for item in self._paginate(ec2.describe_network_interfaces, "NetworkInterfaces")
            }
            data["Volumes"] = {item["VolumeId"]: item for item in self._paginate(ec2.describe_volumes, "Volumes")}
            data["Subnets"] = {item["SubnetId"]: item for item in self._paginate(ec2.describe_subnets, "Subnets")}
            data["Vpcs"] = {item["VpcId"]: item for item in self._paginate(ec2.describe_vpcs, "Vpcs")}
            if data["instances"]:
                types = {instance["InstanceType"] for instance in data["instances"]}
                response = ec2.describe_instance_types(InstanceTypes=sorted(types))
                data["InstanceTypes"] = {item["InstanceType"]: item for item in response["InstanceTypes"]}
            ec2_ok = True
        if not ec2_ok:
            data["EC2AccessDenied"] = True

        inaccessible = []
        if "s3" in services:
            buckets = self._tolerated_list(self.discover_s3, account, region, "s3", ec2.meta.region_name
                                            if False else None,
                                            args=(self.session.client("s3", region_name=region, config=BOTO_CONFIG), region, account))
            if buckets is None:
                inaccessible.append("s3")
                data["Buckets"] = []
            else:
                data["Buckets"] = buckets
        if "lambda" in services:
            functions = self._tolerated_list(self.discover_lambda, account, region, "lambda", None,
                                             args=(self.session.client("lambda", region_name=region, config=BOTO_CONFIG), region, account))
            if functions is None:
                inaccessible.append("lambda")
                data["Functions"] = []
            else:
                data["Functions"] = functions
        if "eks" in services:
            clusters = self._tolerated_list(self.discover_eks, account, region, "eks", None,
                                            args=(self.session.client("eks", region_name=region, config=BOTO_CONFIG), region, account))
            if clusters is None:
                inaccessible.append("eks")
                data["EKSClusters"] = []
            else:
                data["EKSClusters"] = clusters
        if inaccessible:
            data["InaccessibleServices"] = inaccessible
        return data

    def _tolerated_list(self, func, account, region, service, _unused, args):
        try:
            with self.tolerate_access_denied(account, region, service):
                return func(*args)
        except AWSDiscoveryError:
            raise

    def _describe_instances(self, ec2):
        instances = []
        for item in self._paginate_reservations(ec2):
            if item.get("State", {}).get("Name") != "terminated":
                instances.append(item)
        return instances

    def _paginate_reservations(self, ec2):
        token = None
        while True:
            kwargs = {"NextToken": token} if token else {}
            response = ec2.describe_instances(**kwargs)
            for reservation in response.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    yield instance
            token = response.get("NextToken")
            if not token:
                break

    def _paginate(self, func, key):
        token = None
        while True:
            kwargs = {"NextToken": token} if token else {}
            response = func(**kwargs)
            for item in response.get(key, []):
                yield item
            token = response.get("NextToken")
            if not token:
                break

    # -- S3 -------------------------------------------------------------------

    def discover_s3(self, client, region, account):
        results = []
        token = None
        while True:
            params = {"BucketRegion": region}
            if token:
                params["ContinuationToken"] = token
            response = client.list_buckets(**params)
            for bucket in response.get("Buckets", []):
                result = self._discover_one_bucket(client, bucket, region, account)
                if result is not None:
                    results.append(result)
            token = response.get("ContinuationToken")
            if not token:
                break
        return results

    def _discover_one_bucket(self, client, bucket, region, account):
            name = bucket["Name"]
            bucket_region = bucket.get("BucketRegion")
            if bucket_region is None:
                location = client.get_bucket_location(Bucket=name, ExpectedBucketOwner=account)
                bucket_region = location.get("LocationConstraint") or "us-east-1"
            if bucket_region != region:
                raise AWSDiscoveryError(
                    f"AWS S3 bucket '{name}' belongs to '{bucket_region}', not the requested region '{region}'")

            denied_on = None
            params = {"Bucket": name, "ExpectedBucketOwner": account}
            tags = []
            try:
                response = client.get_bucket_tagging(**params)
                tags = response.get("TagSet", [])
                self.access_checks.append(AWSAccessCheck(account, region, "s3", name, "GetBucketTagging", False))
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code == "NoSuchTagSet":
                    tags = []
                elif code in AUTH_ERROR_CODES:
                    denied_on = "GetBucketTagging"
                else:
                    raise AWSDiscoveryError(f"{code}: {exc}") from exc

            versioning = "Unversioned"
            if denied_on is None:
                try:
                    response = client.get_bucket_versioning(**params)
                    versioning = response.get("Status", "Unversioned")
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code in AUTH_ERROR_CODES:
                        denied_on = "GetBucketVersioning"
                    else:
                        raise AWSDiscoveryError(f"{code}: {exc}") from exc

            encryption = {}
            if denied_on is None:
                try:
                    response = client.get_bucket_encryption(**params)
                    encryption = response.get("ServerSideEncryptionConfiguration", {})
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    if code == "ServerSideEncryptionConfigurationNotFoundError":
                        encryption = {}
                    elif code in AUTH_ERROR_CODES:
                        denied_on = "GetBucketEncryption"
                    else:
                        raise AWSDiscoveryError(f"{code}: {exc}") from exc

            if denied_on is not None:
                log.warning(f"AWS AccessDenied on {denied_on} for account '{account}', "
                            f"region '{region}', bucket '{name}'")
                scope = (account, region, "s3")
                if scope not in self.access_denied:
                    self.access_denied.append(scope)
                self.access_checks.append(AWSAccessCheck(account, region, "s3", name, denied_on, True))
                return None

            return {
                "Name": name, "Region": region, "Arn": f"arn:aws:s3:::{name}",
                "Tags": tags, "Versioning": versioning, "Encryption": encryption,
            }

    # -- Lambda -----------------------------------------------------------------

    def discover_lambda(self, client, region, account):
        results = []
        token = None
        while True:
            kwargs = {"Marker": token} if token else {}
            response = client.list_functions(**kwargs)
            for entry in response.get("Functions", []):
                arn = entry["FunctionArn"]
                config = client.get_function_configuration(FunctionName=arn)
                config.pop("Environment", None)
                config.pop("ResponseMetadata", None)
                tags = client.list_tags(Resource=arn).get("Tags", {})
                results.append({**config, "FunctionArn": arn, "Tags": tags})
            token = response.get("NextMarker")
            if not token:
                break
        return results

    # -- EKS ----------------------------------------------------------------

    def discover_eks(self, client, region, account):
        clusters = []
        token = None
        while True:
            kwargs = {"nextToken": token} if token else {}
            response = client.list_clusters(**kwargs)
            for name in response.get("clusters", []):
                clusters.append(self._discover_one_eks_cluster(client, name))
            token = response.get("nextToken")
            if not token:
                break
        return clusters

    def _discover_one_eks_cluster(self, client, name):
        cluster = client.describe_cluster(name=name)["cluster"]

        cluster["nodegroups"] = []
        token = None
        while True:
            kwargs = {"nextToken": token} if token else {}
            response = client.list_nodegroups(clusterName=name, **kwargs)
            for group_name in response.get("nodegroups", []):
                nodegroup = client.describe_nodegroup(clusterName=name, nodegroupName=group_name)["nodegroup"]
                cluster["nodegroups"].append(nodegroup)
            token = response.get("nextToken")
            if not token:
                break

        cluster["fargateProfiles"] = []
        token = None
        while True:
            kwargs = {"nextToken": token} if token else {}
            response = client.list_fargate_profiles(clusterName=name, **kwargs)
            for profile_name in response.get("fargateProfileNames", []):
                profile = client.describe_fargate_profile(
                    clusterName=name, fargateProfileName=profile_name)["fargateProfile"]
                cluster["fargateProfiles"].append(profile)
            token = response.get("nextToken")
            if not token:
                break

        return cluster

    def _paginate_token(self, func, key, token_key):
        token = None
        while True:
            kwargs = {token_key: token} if token else {}
            response = func(**kwargs)
            for item in response.get(key, []):
                yield item
            token = response.get(token_key)
            if not token:
                break

    # -- organization ---------------------------------------------------------

    def discover_organization(self, regions, role_name, expected_account=None, discovery_region=None,
                               include_management=False, external_id=None, account_ids=None, services=()):
        self.access_denied = []
        self.access_checks = []
        bootstrap_region = self._bootstrap_region(regions, discovery_region)
        sts = self.session.client("sts", region_name=bootstrap_region, config=BOTO_CONFIG)
        management_account = self._get_identity(bootstrap_region, expected_account, sts=sts)
        if management_account is None:
            return {}

        organizations = self.session.client("organizations", region_name=bootstrap_region, config=BOTO_CONFIG)
        org_info = None
        with self.tolerate_access_denied(management_account, None, "organizations"):
            org_info = organizations.describe_organization()["Organization"]
        if org_info is None:
            return {}
        master_account = org_info["MasterAccountId"]

        accounts = None
        with self.tolerate_access_denied(management_account, None, "organizations"):
            accounts = list(self._paginate(organizations.list_accounts, "Accounts"))
        if accounts is None:
            return {}

        active = {entry["Id"]: entry for entry in accounts if entry.get("State") == "ACTIVE"}
        self.account_names.update({account_id: entry.get("Name", account_id)
                                   for account_id, entry in active.items()})

        if account_ids:
            missing = [account_id for account_id in account_ids if account_id not in active]
            if missing or (not include_management and any(a == master_account for a in account_ids)):
                bad = [a for a in account_ids if a not in active or (a == master_account and not include_management)]
                raise AWSDiscoveryError(
                    f"AWS organization account(s) {', '.join(bad)} are not active/eligible for discovery")
            selection = list(account_ids)
        else:
            selection = [account_id for account_id in active
                        if account_id != master_account or include_management]
        if not selection:
            raise AWSDiscoveryError("AWS organization discovery found no eligible accounts")

        snapshots = {}
        for account_id in selection:
            member_client = self._assume_member(sts, account_id, role_name, external_id, management_account)
            if member_client is None:
                continue
            options = {"services": services} if services else {}
            discovered_account, snapshot = member_client.discover(
                regions, account_id, discovery_region, **options)
            if discovered_account != account_id:
                continue
            snapshots[account_id] = snapshot
        return snapshots

    def _assume_member(self, sts, account_id, role_name, external_id, management_account):
        role_arn = self._role_arn(account_id, role_name)
        kwargs = {"RoleArn": role_arn, "RoleSessionName": "netbox-sync", "DurationSeconds": 3600}
        if external_id:
            kwargs["ExternalId"] = external_id
        credentials = None
        with self.tolerate_access_denied(account_id, None, "sts", operation="AssumeRole"):
            response = sts.assume_role(**kwargs)
            credentials = response["Credentials"]
        if credentials is None:
            return None
        member = AWSClient.__new__(AWSClient)
        member.session = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )
        member.access_denied = self.access_denied
        member.access_checks = self.access_checks
        member.account_names = self.account_names
        member._tracked_scopes = {}
        member._is_org_member = True
        return member

    def _role_arn(self, account_id, role_name):
        partition = getattr(self, "_partition", "aws")
        return f"arn:{partition}:iam::{account_id}:role/{role_name}"
