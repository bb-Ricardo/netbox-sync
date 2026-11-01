# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2026 netbox-sync team. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import copy
from contextlib import ExitStack
from unittest.mock import Mock

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.stub import Stubber

from module.netbox import (
    NBCustomField, NBCluster, NBClusterGroup, NBIPAddress, NBTag, NBVirtualDisk, NBVM, NBVMInterface,
)
from module.sources import instantiate_sources
from module.sources.aws import client as aws_client
from module.sources.aws.client import AWSAccessCheck, AWSClient, AWSDiscoveryError
from test_aws_source import (
    ACCOUNT, INSTANCE, REGION, SECOND_ACCOUNT, VPC, assume_member, identity, inventory_data,
    make_source, org_sdk, organization_accounts, stub_region, windows_config_paths,
)
from test_orphaned_object_status import make_netbox_handler

FUNCTION_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:inventory-function"
CLUSTER_ARN = f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/inventory-cluster"


def service_data():
    data = inventory_data()
    data.update(
        Buckets=[{
            "Name": "inventory-bucket", "Region": REGION, "Versioning": "Enabled",
            "Encryption": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]},
            "Tags": [{"Key": "Environment", "Value": "test"}],
        }],
        Functions=[{
            "FunctionArn": FUNCTION_ARN, "FunctionName": "inventory-function", "MemorySize": 128,
            "State": "Active", "Runtime": "python3.12", "Timeout": 30, "Handler": "app.handler",
            "LastModified": "2026-10-27T00:00:00.000+0000", "Architectures": ["arm64"],
            "Tags": {"Environment": "test"},
        }],
        EKSClusters=[{
            "arn": CLUSTER_ARN, "name": "inventory-cluster", "version": "1.34", "status": "ACTIVE",
            "resourcesVpcConfig": {"vpcId": VPC}, "endpoint": "https://example.invalid",
            "nodegroups": [{
                "nodegroupName": "workers", "status": "ACTIVE",
                "resources": {"autoScalingGroups": [{"name": "eks-workers"}]},
            }],
            "fargateProfiles": [{"fargateProfileName": "serverless", "status": "ACTIVE"}],
            "tags": {"Environment": "test"},
        }],
    )
    data["instances"][0]["Tags"].append({"Key": "aws:autoscaling:groupName", "Value": "eks-workers"})
    return data


@pytest.mark.parametrize("version", ["3.7.0", "4.1.0", "4.3.0"])
def test_services_import_and_repeat(make_source, inventory, version):
    inventory.netbox_api_version = version
    data = service_data()
    source = make_source("sync_s3 = True\nsync_lambda = True\nsync_eks = True", data=data)
    source.apply()
    source.apply()
    vms = inventory.get_all_items(NBVM)
    assert len(vms) == 3
    worker = next(vm for vm in vms if vm.data["custom_fields"].get("aws_instance_id") == INSTANCE)
    function = next(vm for vm in vms if vm.data["custom_fields"].get("aws_service") == "lambda")
    holder = next(vm for vm in vms if vm.data["custom_fields"].get("aws_service") == "s3")
    assert worker.data["cluster"].data["type"].data["name"] == "AWS EKS"
    assert function.data["cluster"].data["type"].data["name"] == "AWS Lambda"
    assert function.data["memory"] == 128
    assert function.data["custom_fields"]["aws_resource_id"] == FUNCTION_ARN
    assert "vcpus" not in function.data and "vcpus" not in holder.data
    assert "memory" not in holder.data
    assert function.data["status"] == "active"
    assert "AWS:logical-lambda" in function.get_tags()
    assert "AWS:Environment=test" in function.get_tags()
    assert not inventory.get_all_interfaces(function)
    assert not inventory.get_all_interfaces(holder)
    bucket = next(disk for disk in inventory.get_all_items(NBVirtualDisk)
                  if disk.data["name"] == "inventory-bucket")
    assert bucket.data["virtual_machine"] is holder
    assert bucket.data["size"] == 0
    assert bucket.data["custom_fields"]["aws_metadata"]["usage_bytes"] is None
    assert bucket.data["custom_fields"]["aws_metadata"]["usage_status"] == "unknown"
    assert bucket.data["custom_fields"]["aws_metadata"]["Versioning"] == "Enabled"
    assert len(inventory.get_all_items(NBIPAddress)) == 3
    metadata = worker.data["cluster"].data["custom_fields"]["aws_metadata"]
    assert metadata["nodegroups"][0]["nodegroupName"] == "workers"
    assert metadata["fargateProfiles"][0]["fargateProfileName"] == "serverless"
    source.client.discover.assert_called_with(
        [REGION], None, None, services=("s3", "lambda", "eks"))
    field = inventory.get_by_data(NBCustomField, data={"name": "aws_metadata"})
    assert field.data.get("object_types", field.data.get("content_types")) == [
        NBVM.object_type, NBCluster.object_type, NBVirtualDisk.object_type,
    ]


def test_s3_requires_supported_netbox_before_discovery(make_source, inventory, caplog):
    inventory.netbox_api_version = "3.6.0"
    source = make_source("sync_s3 = True")
    with pytest.raises(SystemExit):
        source.apply()
    assert "requires NetBox 3.7" in caplog.text
    source.client.discover.assert_not_called()
    assert inventory.get_all_items(NBVM) == []


@pytest.mark.parametrize("key,setting", [
    ("Buckets", "sync_s3"), ("Functions", "sync_lambda"), ("EKSClusters", "sync_eks"),
])
def test_enabled_service_requires_complete_snapshot(make_source, inventory, key, setting, caplog):
    source = make_source(f"{setting} = True")
    source.sync_snapshot = Mock()
    with pytest.raises(SystemExit):
        source.apply()
    source.sync_snapshot.assert_not_called()
    assert f"Missing AWS {key} discovery" in caplog.text


def test_disabled_service_objects_are_retained(make_source, inventory):
    source = make_source("sync_s3 = True\nsync_lambda = True\nsync_eks = True", data=service_data())
    source.apply()
    managed = []
    for kind in (NBVM, NBVirtualDisk, NBCluster):
        for item in inventory.get_all_items(kind):
            if item.data.get("custom_fields", {}).get("aws_service"):
                item.add_tags(source.source_tag)
                item.source = None
                managed.append(item)
    source.settings.sync_s3 = False
    source.settings.sync_lambda = False
    source.settings.sync_eks = False
    source.apply()
    assert managed and all(item.source is source for item in managed)
    handler = make_netbox_handler()
    inventory.tag_all_the_things(handler)
    assert all(handler.orphaned_tag not in item.get_tags() for item in managed)
    worker = next(vm for vm in inventory.get_all_items(NBVM)
                  if vm.data["custom_fields"].get("aws_instance_id") == INSTANCE)
    assert worker.data["cluster"].data["type"].data["name"] == "AWS EC2"


def test_absent_enabled_resources_are_not_retained(make_source, inventory):
    data = service_data()
    source = make_source("sync_s3 = True\nsync_lambda = True", data=data)
    source.apply()
    resources = [item for kind in (NBVM, NBVirtualDisk) for item in inventory.get_all_items(kind)
                 if item.data.get("custom_fields", {}).get("aws_service")]
    for item in resources:
        item.add_tags([source.source_tag, make_netbox_handler().primary_tag])
        item.source = None
    data["Buckets"] = []
    data["Functions"] = []
    source.apply()
    assert resources and all(item.source is None for item in resources)
    handler = make_netbox_handler()
    inventory.tag_all_the_things(handler)
    assert all(handler.orphaned_tag in item.get_tags() for item in resources)


@pytest.mark.parametrize("state,status", [
    ("Pending", "planned"), ("Inactive", "offline"), ("Failed", "failed"),
])
def test_lambda_states(make_source, inventory, state, status):
    data = service_data()
    data["Functions"][0]["State"] = state
    make_source("sync_lambda = True", data=data).apply()
    function = next(vm for vm in inventory.get_all_items(NBVM)
                    if vm.data["custom_fields"].get("aws_service") == "lambda")
    assert function.data["status"] == status


def test_long_names_and_same_lambda_names_across_regions(make_source, inventory):
    data = service_data()
    function = data["Functions"][0]
    function["FunctionName"] = "x" * 64
    function["FunctionArn"] = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:{function['FunctionName']}"
    other = copy.deepcopy(data)
    other["instances"] = []
    other["Functions"][0]["FunctionArn"] = function["FunctionArn"].replace(REGION, "us-west-2")
    source = make_source("sync_lambda = True", data=data)
    source.client.discover.return_value = (ACCOUNT, {REGION: data, "us-west-2": other})
    source.apply()
    source.apply()
    functions = [vm for vm in inventory.get_all_items(NBVM)
                 if vm.data["custom_fields"].get("aws_service") == "lambda"]
    assert len(functions) == 2
    assert all(len(vm.data["name"]) <= 64 for vm in functions)
    assert len({vm.data["name"] for vm in functions}) == 2


def test_services_do_not_overwrite_unmanaged_objects(make_source, inventory, caplog):
    data = service_data()
    source = make_source("sync_lambda = True", data=data)
    source.apply()
    function = next(vm for vm in inventory.get_all_items(NBVM)
                    if vm.data["custom_fields"].get("aws_service") == "lambda")
    function.source = None
    before = copy.deepcopy(function.data["custom_fields"])
    data["Functions"][0]["MemorySize"] = 256
    source.apply()
    assert "belongs to another source" in caplog.text
    assert function.source is None
    assert function.data["custom_fields"] == before
    assert function.data["memory"] == 128


@pytest.mark.parametrize("conflict", ["name", "duplicate_identity", "other_source"])
def test_lambda_cluster_conflict_continues_and_retains_children(
        make_source, inventory, caplog, conflict):
    source = make_source("sync_lambda = True\nsync_private_ips = False", data=service_data())
    source.custom_fields()
    group = inventory.add_object(NBClusterGroup, data={"name": f"AWS {ACCOUNT}"})
    fields = {} if conflict == "name" else {"aws_resource_id": f"lambda/{ACCOUNT}/{REGION}"}
    cluster = inventory.add_object(NBCluster, data={
        "name": f"Lambda {ACCOUNT}/{REGION}", "group": group, "custom_fields": fields,
    })
    if conflict == "duplicate_identity":
        inventory.add_object(NBCluster, data={
            "name": "Duplicate", "group": group, "custom_fields": fields,
        })
    retained = inventory.add_object(NBVM, data={
        "name": "Existing function", "cluster": cluster, "tags": [source.source_tag],
    })
    source.apply()
    inventory.tag_all_the_things(make_netbox_handler())
    assert "Skipping" in caplog.text
    assert retained.source is source
    assert make_netbox_handler().orphaned_tag not in retained.get_tags()
    assert cluster.source is None
    assert cluster.data.get("custom_fields", {}) == fields
    assert any(vm.data.get("custom_fields", {}).get("aws_instance_id") == INSTANCE
               for vm in inventory.get_all_items(NBVM))
    assert not any(vm.data.get("custom_fields", {}).get("aws_resource_id") == FUNCTION_ARN
                   for vm in inventory.get_all_items(NBVM))


@pytest.mark.parametrize("stage", ["cluster", "holder", "bucket"])
def test_s3_conflict_skips_dependencies_but_continues(make_source, inventory, caplog, stage):
    data = service_data()
    data["Buckets"].append({**data["Buckets"][0], "Name": "second"})
    source = make_source("sync_s3 = True\nsync_private_ips = False", data=data)
    source.custom_fields()
    group = inventory.add_object(NBClusterGroup, data={"name": f"AWS {ACCOUNT}"})
    identity = f"s3/{ACCOUNT}/{REGION}"
    cluster = inventory.add_object(NBCluster, data={
        "name": f"S3 {ACCOUNT}/{REGION}", "group": group,
        "custom_fields": {} if stage == "cluster" else {"aws_resource_id": identity},
    }, source=source)
    if stage != "cluster":
        holder = inventory.add_object(NBVM, data={
            "name": f"S3 inventory {ACCOUNT}/{REGION}", "cluster": cluster,
            "custom_fields": {} if stage == "holder" else {"aws_resource_id": identity},
        }, source=source)
        if stage == "bucket":
            inventory.add_object(NBVirtualDisk, data={
                "name": "inventory-bucket", "virtual_machine": holder, "size": 0,
            })
    source.apply()
    assert ("name collision" in caplog.text) == (stage == "bucket")
    buckets = [disk.data["name"] for disk in inventory.get_all_items(NBVirtualDisk)
               if disk.data.get("custom_fields", {}).get("aws_service") == "s3"]
    assert buckets == (["second"] if stage == "bucket" else ["inventory-bucket", "second"])
    assert cluster.data["custom_fields"]["aws_resource_id"] == identity
    if stage == "holder":
        assert holder.data["custom_fields"]["aws_resource_id"] == identity
    assert any(vm.data.get("custom_fields", {}).get("aws_instance_id") == INSTANCE
               for vm in inventory.get_all_items(NBVM))


def test_eks_conflict_skips_workers_and_continues_next_region(make_source, inventory, caplog):
    data = service_data()
    source = make_source("sync_eks = True\nsync_private_ips = False", data=data)
    source.custom_fields()
    group = inventory.add_object(NBClusterGroup, data={"name": f"AWS {ACCOUNT}"})
    inventory.add_object(NBCluster, data={
        "name": f"EKS {ACCOUNT}/{REGION}/inventory-cluster", "group": group,
    })
    next_region = inventory_data()
    next_region["EKSClusters"] = []
    next_region["instances"][0]["InstanceId"] = "i-next-region"
    for eni in next_region["NetworkInterfaces"].values():
        eni["Attachment"]["InstanceId"] = "i-next-region"
    source.client.discover.return_value = ACCOUNT, {REGION: data, "us-west-2": next_region}
    source.apply()
    assert f"Skipping instance '{INSTANCE}'" in caplog.text
    instances = [vm.data.get("custom_fields", {}).get("aws_instance_id")
                 for vm in inventory.get_all_items(NBVM)]
    assert INSTANCE not in instances
    assert "i-next-region" in instances


@pytest.mark.parametrize("fields", [
    {},
    {"aws_resource_id": "", "aws_service": ""},
])
def test_eks_cluster_with_missing_identity_fields_is_reconciled(make_source, inventory, fields, caplog):
    source = make_source("sync_eks = True\nsync_private_ips = False", data=service_data())
    group = inventory.add_object(NBClusterGroup, data={"name": f"AWS {ACCOUNT}"})
    cluster = inventory.add_object(NBCluster, data={
        "name": f"EKS {ACCOUNT}/{REGION}/inventory-cluster",
        "group": group,
        "custom_fields": fields,
    })
    cluster.add_tags(source.source_tag)

    source.apply()

    assert not any("Skipping EKS cluster 'inventory-cluster'" in message
                   for message in caplog.messages)
    assert len([item for item in inventory.get_all_items(NBCluster)
                if item.data.get("name") == cluster.data["name"]]) == 1
    assert cluster.data["custom_fields"]["aws_resource_id"] == CLUSTER_ARN
    assert cluster.data["custom_fields"]["aws_service"] == "eks"
    worker = next(vm for vm in inventory.get_all_items(NBVM)
                  if vm.data.get("custom_fields", {}).get("aws_instance_id") == INSTANCE)
    assert worker.data["cluster"] is cluster


@pytest.mark.parametrize("fields", [
    {"aws_resource_id": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/another-cluster", "aws_service": "eks"},
    {"aws_resource_id": "", "aws_service": "lambda"},
])
def test_eks_cluster_does_not_adopt_conflicting_identity_fields(make_source, inventory, fields, caplog):
    source = make_source("sync_eks = True\nsync_private_ips = False", data=service_data())
    source.custom_fields()
    group = inventory.add_object(NBClusterGroup, data={"name": f"AWS {ACCOUNT}"})
    cluster = inventory.add_object(NBCluster, data={
        "name": f"EKS {ACCOUNT}/{REGION}/inventory-cluster",
        "group": group,
        "custom_fields": fields,
    })
    cluster.add_tags(source.source_tag)

    source.apply()

    assert "Skipping EKS cluster 'inventory-cluster'" in caplog.text
    assert cluster.data["custom_fields"] == fields
    assert not any(vm.data.get("custom_fields", {}).get("aws_instance_id") == INSTANCE
                   for vm in inventory.get_all_items(NBVM))


def test_lambda_function_conflict_continues_next_function(make_source, inventory, caplog):
    data = service_data()
    source = make_source("sync_lambda = True\nsync_private_ips = False", data=data)
    source.apply()
    cluster = next(item for item in inventory.get_all_items(NBCluster)
                   if item.data.get("custom_fields", {}).get("aws_service") == "lambda")
    inventory.add_object(NBVM, data={"name": "Lambda second", "cluster": cluster})
    data["Functions"].extend([
        {**data["Functions"][0], "FunctionName": name, "FunctionArn": f"{FUNCTION_ARN}-{name}"}
        for name in ("second", "third")
    ])
    source.apply()
    assert "name collision" in caplog.text
    assert any(vm.data["name"] == "Lambda third" for vm in inventory.get_all_items(NBVM))


def test_access_tracking_replaces_scope_and_records_operations(service_sdk):
    client = AWSClient()
    sts = service_sdk["sts"].client
    client.track_access(sts, ACCOUNT, None, "sts")
    client.track_access(sts, SECOND_ACCOUNT, None, "sts")
    params = {"RoleArn": f"arn:aws:iam::{SECOND_ACCOUNT}:role/reports", "RoleSessionName": "test"}
    service_sdk["sts"].add_client_error("assume_role", service_error_code="AccessDenied", expected_params=params)
    with client.tolerate_access_denied(SECOND_ACCOUNT, service="sts"):
        sts.assume_role(**params)
    assert client.access_checks == [
        AWSAccessCheck(SECOND_ACCOUNT, None, "sts", None, "AssumeRole", True)]


@pytest.mark.parametrize("scope", ["resource", "service", "account", "organization"])
def test_permission_tags_require_verified_recovery(make_source, inventory, scope):
    source = make_source("sync_s3 = True", data=service_data())
    source.apply()
    bucket = next(disk for disk in inventory.get_all_items(NBVirtualDisk)
                  if disk.data["name"] == "inventory-bucket")
    bucket.add_tags("user-tag")
    unmanaged = inventory.add_object(NBVirtualDisk, data={
        "name": "unmanaged", "virtual_machine": bucket.data["virtual_machine"], "size": 0,
    })
    account = None if scope == "organization" else ACCOUNT
    region = REGION if scope in ("resource", "service") else None
    service = {"resource": "s3", "service": "s3", "account": "sts",
               "organization": "organizations"}[scope]
    resource = "inventory-bucket" if scope == "resource" else None
    operation = "GetBucketTagging"
    tag = f"Sync:AccessDenied:{operation}"
    failure = AWSAccessCheck(account, region, service, resource, operation, True)
    success = AWSAccessCheck(account, region, service, resource, operation, False)
    source.client.access_checks = [failure]
    source.reconcile_access_tags()
    assert tag in bucket.get_tags()
    assert tag not in unmanaged.get_tags()
    for checks in ([], [AWSAccessCheck(ACCOUNT, REGION, "s3", "other", operation, False)],
                   [success, failure]):
        source.client.access_checks = checks
        source.reconcile_access_tags()
        assert tag in bucket.get_tags()
    source.client.access_checks = [success]
    source.reconcile_access_tags()
    assert tag not in bucket.get_tags()
    assert "user-tag" in bucket.get_tags()


def test_permission_tags_do_not_leak_across_scopes(make_source, inventory):
    source = make_source("sync_s3 = True", data=service_data())
    source.custom_fields()
    objects = []
    for account, region, service in (
            (ACCOUNT, REGION, "s3"), (SECOND_ACCOUNT, REGION, "s3"),
            (ACCOUNT, "us-west-2", "s3"), (ACCOUNT, REGION, "lambda")):
        objects.append(inventory.add_object(NBVM, data={
            "name": f"{account}-{region}-{service}", "tags": [source.source_tag],
            "custom_fields": {
                "aws_account_id": account, "aws_region": region, "aws_service": service,
            },
        }))
    source.client.access_checks = [AWSAccessCheck(ACCOUNT, REGION, "s3", None, "ListBuckets", True)]
    source.reconcile_access_tags()
    assert ["Sync:AccessDenied:ListBuckets" in item.get_tags() for item in objects] == [
        True, False, False, False]


def test_bucket_permission_tag_through_sdk_denial_and_recovery(service_sdk, inventory, load_config):
    load_config(f"""
[source/services]
type = aws
regions = {REGION}
sync_s3 = True
sync_private_ips = False
""")
    source, = instantiate_sources()
    tag = "Sync:AccessDenied:GetBucketTagging"
    for denied in (False, True, False):
        identity(service_sdk["sts"])
        stub_region(service_sdk["ec2"], inventory_data())
        if denied:
            service_sdk["s3"].add_response("list_buckets", {"Buckets": [{
                "Name": "inventory-bucket", "BucketRegion": REGION,
            }]}, {"BucketRegion": REGION})
            service_sdk["s3"].add_client_error("get_bucket_tagging", service_error_code="AccessDenied",
                                               expected_params={
                                                   "Bucket": "inventory-bucket", "ExpectedBucketOwner": ACCOUNT,
                                               })
        else:
            stub_s3(service_sdk["s3"])
        source.apply()
        inventory.tag_all_the_things(make_netbox_handler())
        bucket = next(disk for disk in inventory.get_all_items(NBVirtualDisk)
                      if disk.data["name"] == "inventory-bucket")
        assert (tag in bucket.get_tags()) is denied
        assert make_netbox_handler().orphaned_tag not in bucket.get_tags()
        assert any(check.operation == "GetBucketTagging" and check.resource == "inventory-bucket"
                   and check.denied is denied for check in source.client.access_checks)
        bucket.source = None


@pytest.mark.parametrize("failure", ["arn", "membership", "tag", "state"])
def test_invalid_service_metadata_aborts_before_sync(make_source, inventory, failure):
    data = service_data()
    if failure == "arn":
        data["Functions"][0]["FunctionArn"] = FUNCTION_ARN.replace(ACCOUNT, SECOND_ACCOUNT)
    elif failure == "membership":
        data["instances"][0]["Tags"].append({"Key": "eks:cluster-name", "Value": "different"})
    elif failure == "tag":
        data["Functions"][0]["Tags"]["Environment"] = "x" * 101
    else:
        data["Functions"][0]["State"] = "Unknown"
    source = make_source("sync_lambda = True\nsync_eks = True", data=data)
    source.sync_snapshot = Mock()
    with pytest.raises(SystemExit):
        source.apply()
    source.sync_snapshot.assert_not_called()


@pytest.fixture
def service_sdk(monkeypatch):
    session = boto3.Session(
        aws_access_key_id="test-only", aws_secret_access_key="test-only", region_name=REGION)
    clients = {service: session.client(service) for service in ("sts", "ec2", "s3", "lambda", "eks")}
    fake_session = Mock(region_name=REGION)
    fake_session.client.side_effect = lambda service, **kwargs: clients[service]
    monkeypatch.setattr(aws_client.boto3, "Session", Mock(return_value=fake_session))
    with ExitStack() as stack:
        stubs = {service: stack.enter_context(Stubber(client)) for service, client in clients.items()}
        yield stubs
        for stub in stubs.values():
            stub.assert_no_pending_responses()


def stub_s3(stub, present=True):
    stub.add_response("list_buckets", {
        "Buckets": [{"Name": "inventory-bucket", "BucketRegion": REGION}],
        "ContinuationToken": "next",
    }, {"BucketRegion": REGION})
    params = {"Bucket": "inventory-bucket", "ExpectedBucketOwner": ACCOUNT}
    if present:
        stub.add_response("get_bucket_tagging", {
            "TagSet": [{"Key": "Environment", "Value": "test"}],
        }, params)
    else:
        stub.add_client_error("get_bucket_tagging", service_error_code="NoSuchTagSet", expected_params=params)
    stub.add_response("get_bucket_versioning", {"Status": "Enabled"}, params)
    stub.add_response("get_bucket_encryption", {
        "ServerSideEncryptionConfiguration": {
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
        },
    }, params)
    stub.add_response("list_buckets", {"Buckets": []}, {"BucketRegion": REGION, "ContinuationToken": "next"})


def stub_lambda(stub):
    config = service_data()["Functions"][0]
    stub.add_response("list_functions", {
        "Functions": [{"FunctionName": config["FunctionName"], "FunctionArn": FUNCTION_ARN}],
        "NextMarker": "next",
    }, {})
    stub.add_response("get_function_configuration", {
        **{key: value for key, value in config.items() if key != "Tags"},
        "Environment": {"Variables": {"DO_NOT_IMPORT": "sensitive"}},
    }, {"FunctionName": FUNCTION_ARN})
    stub.add_response("list_tags", {"Tags": config["Tags"]}, {"Resource": FUNCTION_ARN})
    stub.add_response("list_functions", {"Functions": []}, {"Marker": "next"})


def stub_eks(stub):
    cluster = service_data()["EKSClusters"][0]
    name = cluster["name"]
    stub.add_response("list_clusters", {"clusters": [name], "nextToken": "next"}, {})
    stub.add_response("describe_cluster", {
        "cluster": {key: value for key, value in cluster.items() if key not in ("nodegroups", "fargateProfiles")},
    }, {"name": name})
    stub.add_response("list_nodegroups", {"nodegroups": ["workers"], "nextToken": "next"}, {"clusterName": name})
    stub.add_response("describe_nodegroup", {
        "nodegroup": {**cluster["nodegroups"][0], "clusterName": name},
    }, {"clusterName": name, "nodegroupName": "workers"})
    stub.add_response("list_nodegroups", {"nodegroups": []}, {"clusterName": name, "nextToken": "next"})
    stub.add_response("list_fargate_profiles", {
        "fargateProfileNames": ["serverless"], "nextToken": "next",
    }, {"clusterName": name})
    stub.add_response("describe_fargate_profile", {
        "fargateProfile": {**cluster["fargateProfiles"][0], "clusterName": name},
    }, {"clusterName": name, "fargateProfileName": "serverless"})
    stub.add_response("list_fargate_profiles", {"fargateProfileNames": []}, {"clusterName": name, "nextToken": "next"})
    stub.add_response("list_clusters", {"clusters": []}, {"nextToken": "next"})


def test_sdk_services_to_real_inventory(service_sdk, inventory, load_config):
    identity(service_sdk["sts"])
    stub_region(service_sdk["ec2"], service_data())
    stub_s3(service_sdk["s3"])
    stub_lambda(service_sdk["lambda"])
    stub_eks(service_sdk["eks"])
    load_config(f"""
[source/services]
type = aws
regions = {REGION}
sync_s3 = True
sync_lambda = True
sync_eks = True
sync_private_ips = False
""")
    source, = instantiate_sources()
    source.apply()
    assert len(inventory.get_all_items(NBVM)) == 3
    function = next(vm for vm in inventory.get_all_items(NBVM)
                    if vm.data["custom_fields"].get("aws_service") == "lambda")
    assert "Environment" not in function.data["custom_fields"]["aws_metadata"]
    assert "sensitive" not in str(function.data)


@pytest.mark.parametrize("service,operation", [
    ("s3", "list_buckets"), ("lambda", "list_functions"), ("eks", "list_clusters"),
])
def test_service_access_denied_continues(service_sdk, inventory, load_config, service, operation, caplog):
    identity(service_sdk["sts"])
    stub_region(service_sdk["ec2"], inventory_data())
    service_sdk[service].add_client_error(operation, service_error_code="AccessDenied", http_status_code=403)
    load_config(f"""
[source/services]
type = aws
regions = {REGION}
sync_{service} = True
""")
    source, = instantiate_sources()
    source.apply()
    assert "AccessDenied" in caplog.text and "Retaining all previously managed source objects" in caplog.text
    assert len(inventory.get_all_items(NBVM)) == 1
    assert source.client.access_denied == [(ACCOUNT, REGION, service)]


def test_optional_services_propagate_to_member_accounts(org_sdk, monkeypatch):
    organization_accounts(org_sdk)
    for account in (ACCOUNT, SECOND_ACCOUNT):
        assume_member(org_sdk, account)
    discovery = Mock(side_effect=lambda regions, account, discovery_region, **kwargs: (account, {}))
    monkeypatch.setattr(AWSClient, "discover", discovery)
    AWSClient().discover_organization([REGION], "NetBoxInventoryReadOnly", services=("s3", "lambda", "eks"))
    assert discovery.call_count == 2
    for call in discovery.call_args_list:
        assert call.kwargs == {"services": ("s3", "lambda", "eks")}


def test_service_defaults_preserve_ec2_only(make_source):
    source = make_source()
    assert not any((source.settings.sync_s3, source.settings.sync_lambda, source.settings.sync_eks))
    source.apply()
    source.client.discover.assert_called_once_with([REGION], None, None)


def test_incompatible_metadata_custom_field_aborts(make_source, inventory, caplog):
    inventory.add_object(NBCustomField, data={
        "id": 1, "name": "aws_metadata", "type": "text", "object_types": [NBVM.object_type],
    }, read_from_netbox=True)
    source = make_source("sync_s3 = True", data=service_data())
    with pytest.raises(SystemExit):
        source.apply()
    assert "must have type 'json'" in caplog.text
    assert inventory.get_all_items(NBVM) == []


def test_s3_holders_are_per_account_and_region(make_source, inventory, monkeypatch):
    data = service_data()
    data["instances"] = []
    second = copy.deepcopy(data)
    second["Buckets"][0]["Name"] = "second-account-bucket"
    other_region = copy.deepcopy(data)
    other_region["Buckets"][0].update(Name="other-region-bucket", Region="us-west-2")
    source = make_source("organization = True\nsync_s3 = True")
    monkeypatch.setattr(AWSClient, "discover_organization", Mock(return_value={
        ACCOUNT: {REGION: data, "us-west-2": other_region},
        SECOND_ACCOUNT: {REGION: second},
    }))
    source.apply()
    assert len(inventory.get_all_items(NBVM)) == 3
    assert len(inventory.get_all_items(NBVirtualDisk)) == 3
    assert {vm.data["name"] for vm in inventory.get_all_items(NBVM)} == {
        f"S3 inventory {ACCOUNT}/{REGION}", f"S3 inventory {ACCOUNT}/us-west-2",
        f"S3 inventory {SECOND_ACCOUNT}/{REGION}",
    }


@pytest.mark.parametrize("tag_name", ["AWS:Environment=Dev", "aws:environment=Dev"])
def test_service_tags_use_resolved_identity(make_source, inventory, tag_name):
    tag = inventory.add_object(NBTag, data={"name": tag_name})
    data = service_data()
    data["Buckets"][0]["Tags"] = [{"Key": "Environment", "Value": "dev"}]
    data["Functions"][0]["Tags"] = {"Environment": "dev"}
    data["EKSClusters"][0]["tags"] = {"Environment": "dev"}
    source = make_source("sync_s3 = True\nsync_lambda = True\nsync_eks = True", data=data)
    for _ in range(2):
        source.apply()
        tagged = [item for kind in (NBVM, NBVirtualDisk, NBCluster)
                  for item in inventory.get_all_items(kind)
                  if item.data.get("custom_fields", {}).get("aws_resource_id") in (
                      FUNCTION_ARN, CLUSTER_ARN, f"s3/{ACCOUNT}/inventory-bucket")]
        assert len(tagged) == 3
        assert all(tag in item.data["tags"] and tag_name in item.get_tags() for item in tagged)
    for item in tagged:
        item.add_tags("user-managed")
    data["Buckets"][0]["Tags"] = []
    data["Functions"][0]["Tags"] = {}
    data["EKSClusters"][0]["tags"] = {}
    source.apply()
    assert all(tag_name not in item.get_tags() and "user-managed" in item.get_tags() for item in tagged)


def test_optional_resource_tags_reconcile_without_losing_user_tags(make_source, inventory):
    data = service_data()
    source = make_source("sync_s3 = True\nsync_lambda = True\nsync_eks = True", data=data)
    source.apply()
    resources = [item for kind in (NBVM, NBVirtualDisk, NBCluster)
                 for item in inventory.get_all_items(kind)
                 if item.data.get("custom_fields", {}).get("aws_service") and
                 "AWS:Environment=test" in item.get_tags()]
    for item in resources:
        item.add_tags("user-managed")
    data["Buckets"][0]["Tags"] = []
    data["Functions"][0]["Tags"] = {}
    data["EKSClusters"][0]["tags"] = {}
    source.apply()
    assert len(resources) == 3
    assert all("AWS:Environment=test" not in item.get_tags() and "user-managed" in item.get_tags()
               for item in resources)


@pytest.mark.parametrize("key", [
    "aws:eks:cluster-name", "eks:cluster-name", "eksctl.cluster.k8s.io/v1alpha1/cluster-name",
])
def test_eks_worker_tag_membership(make_source, inventory, key):
    data = service_data()
    data["instances"][0]["Tags"] = [{"Key": key, "Value": "inventory-cluster"}]
    source = make_source("sync_eks = True", data=data)
    source.apply()
    worker, = inventory.get_all_items(NBVM)
    assert worker.data["cluster"].data["type"].data["name"] == "AWS EKS"


def test_enabling_eks_moves_existing_worker_without_duplication(make_source, inventory):
    source = make_source(data=service_data())
    source.apply()
    worker, = inventory.get_all_items(NBVM)
    disks = list(worker.get_virtual_disks())
    interfaces = list(inventory.get_all_items(NBVMInterface))
    assert worker.data["cluster"].data["type"].data["name"] == "AWS EC2"
    source.settings.sync_eks = True
    source.apply()
    same_worker, = inventory.get_all_items(NBVM)
    assert same_worker is worker
    assert same_worker.data["cluster"].data["type"].data["name"] == "AWS EKS"
    assert worker.get_virtual_disks() == disks
    assert inventory.get_all_items(NBVMInterface) == interfaces


def test_unknown_eks_cluster_tag_is_explicit(make_source, inventory, caplog):
    data = service_data()
    data["instances"][0]["Tags"] = [{"Key": "eks:cluster-name", "Value": "deleted-cluster"}]
    source = make_source("sync_eks = True", data=data)
    source.apply()
    worker, = inventory.get_all_items(NBVM)
    assert worker.data["cluster"].data["type"].data["name"] == "AWS EC2"
    assert "references undiscovered EKS cluster 'deleted-cluster'" in caplog.text


@pytest.mark.parametrize("missing", ["tags", "encryption", "versioning"])
def test_s3_absent_optional_configuration_is_explicit(service_sdk, missing):
    stub = service_sdk["s3"]
    stub.add_response("list_buckets", {"Buckets": [{"Name": "inventory-bucket"}]}, {"BucketRegion": REGION})
    params = {"Bucket": "inventory-bucket", "ExpectedBucketOwner": ACCOUNT}
    stub.add_response("get_bucket_location", {"LocationConstraint": ""}, params)
    if missing == "tags":
        stub.add_client_error("get_bucket_tagging", service_error_code="NoSuchTagSet", expected_params=params)
    else:
        stub.add_response("get_bucket_tagging", {"TagSet": []}, params)
    stub.add_response("get_bucket_versioning", {} if missing == "versioning" else {"Status": "Suspended"}, params)
    if missing == "encryption":
        stub.add_client_error(
            "get_bucket_encryption", service_error_code="ServerSideEncryptionConfigurationNotFoundError",
            expected_params=params)
    else:
        stub.add_response("get_bucket_encryption", {
            "ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
            },
        }, params)
    buckets = AWSClient().discover_s3(stub.client, REGION, ACCOUNT)
    bucket, = buckets
    assert bucket["Arn"] == "arn:aws:s3:::inventory-bucket"
    if missing == "tags":
        assert bucket["Tags"] == []
    elif missing == "encryption":
        assert bucket["Encryption"] == {}
    else:
        assert bucket["Versioning"] == "Unversioned"


def test_s3_wrong_region_fails_closed(service_sdk):
    service_sdk["s3"].add_response("list_buckets", {
        "Buckets": [{"Name": "inventory-bucket", "BucketRegion": "us-west-2"}],
    }, {"BucketRegion": REGION})
    with pytest.raises(AWSDiscoveryError, match="belongs to 'us-west-2'"):
        AWSClient().discover_s3(service_sdk["s3"].client, REGION, ACCOUNT)


def test_s3_metadata_access_denied_skips_bucket_not_empty_configuration(service_sdk, caplog):
    stub = service_sdk["s3"]
    stub.add_response("list_buckets", {
        "Buckets": [{"Name": "inventory-bucket", "BucketRegion": REGION}],
    }, {"BucketRegion": REGION})
    stub.add_client_error("get_bucket_tagging", service_error_code="AccessDenied", expected_params={
        "Bucket": "inventory-bucket", "ExpectedBucketOwner": ACCOUNT,
    })
    client = AWSClient()
    assert client.discover_s3(stub.client, REGION, ACCOUNT) == []
    assert client.access_denied == [(ACCOUNT, REGION, "s3")]
    assert "AccessDenied" in caplog.text


@pytest.mark.parametrize("operation", [
    "get_bucket_tagging", "get_bucket_versioning", "get_bucket_encryption",
])
def test_s3_denied_bucket_does_not_stop_next_bucket(service_sdk, operation, caplog):
    stub = service_sdk["s3"]
    stub.add_response("list_buckets", {"Buckets": [
        {"Name": "denied-bucket", "BucketRegion": REGION},
        {"Name": "accessible-bucket", "BucketRegion": REGION},
    ]}, {"BucketRegion": REGION})
    for name in ("denied-bucket", "accessible-bucket"):
        params = {"Bucket": name, "ExpectedBucketOwner": ACCOUNT}
        for request, response in (
            ("get_bucket_tagging", {"TagSet": []}),
            ("get_bucket_versioning", {}),
            ("get_bucket_encryption", {"ServerSideEncryptionConfiguration": {
                "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}],
            }}),
        ):
            if name == "denied-bucket" and request == operation:
                stub.add_client_error(request, service_error_code="AccessDenied", expected_params=params)
                break
            stub.add_response(request, response, params)
    client = AWSClient()
    buckets = client.discover_s3(stub.client, REGION, ACCOUNT)
    assert [bucket["Name"] for bucket in buckets] == ["accessible-bucket"]
    assert client.access_denied == [(ACCOUNT, REGION, "s3")]
    assert operation.replace("_", "").lower() in caplog.text.replace("_", "").lower()


def test_access_denied_retains_objects_and_preserves_eks_placement(make_source, inventory):
    data = service_data()
    source = make_source("sync_s3 = True\nsync_lambda = True\nsync_eks = True", data=data)
    source.apply()
    handler = make_netbox_handler()
    inventory.tag_all_the_things(handler)
    objects = [item for kind in (NBVM, NBVirtualDisk, NBVMInterface, NBIPAddress)
               for item in inventory.get_all_items(kind)]
    worker = next(vm for vm in inventory.get_all_items(NBVM)
                  if vm.data["custom_fields"].get("aws_instance_id") == INSTANCE)
    cluster = worker.data["cluster"]
    bucket = next(disk for disk in inventory.get_all_items(NBVirtualDisk)
                  if disk.data["name"] == "inventory-bucket")
    bucket_metadata = copy.deepcopy(bucket.data["custom_fields"]["aws_metadata"])
    for item in objects:
        item.source = None
    data["Buckets"] = []
    data.pop("Functions")
    data.pop("EKSClusters")
    data["InaccessibleServices"] = ["lambda", "eks"]
    data["instances"][0]["Tags"][0]["Value"] = "updated-worker"
    source.client.access_denied = [(ACCOUNT, REGION, "s3"), (ACCOUNT, REGION, "lambda"), (ACCOUNT, REGION, "eks")]
    source.apply()
    inventory.tag_all_the_things(handler)
    assert all(item.source is source and handler.orphaned_tag not in item.get_tags() for item in objects)
    assert bucket.data["custom_fields"]["aws_metadata"] == bucket_metadata
    assert "AWS:Environment=test" in bucket.get_tags()
    assert worker.data["cluster"] is cluster
    assert worker.data["name"] == "updated-worker"


def test_denied_organization_listing_retains_existing_objects(make_source, inventory):
    source = make_source(data=service_data())
    source.apply()
    handler = make_netbox_handler()
    inventory.tag_all_the_things(handler)
    vm, = inventory.get_all_items(NBVM)
    vm.source = None
    source.settings.organization = True
    source.client.discover_organization = Mock(return_value={})
    source.client.access_denied = [(None, None, "organizations")]
    source.apply()
    inventory.tag_all_the_things(handler)
    assert vm.source is source and handler.orphaned_tag not in vm.get_tags()


@pytest.mark.parametrize("code", ["ExpiredToken", "ThrottlingException", "InternalError"])
def test_non_authorization_errors_remain_fatal(service_sdk, code):
    identity(service_sdk["sts"])
    service_sdk["ec2"].add_client_error("describe_instances", service_error_code=code, expected_params={})
    with pytest.raises(AWSDiscoveryError, match=code):
        AWSClient().discover([REGION])


def test_denied_shared_network_owner_defers_dependent_instances(make_source, inventory):
    data = inventory_data()
    data["Vpcs"].clear()
    data["Subnets"].clear()
    source = make_source("organization = True")
    source.client.discover_organization = Mock(return_value={ACCOUNT: {REGION: data}})
    source.client.access_denied = [(SECOND_ACCOUNT, REGION, "ec2")]
    source.apply()
    assert inventory.get_all_items(NBVM) == []


def test_denied_identity_without_expected_account_is_nonfatal(service_sdk, inventory, load_config, caplog):
    service_sdk["sts"].add_client_error("get_caller_identity", service_error_code="AccessDenied", expected_params={})
    load_config(f"""
[source/services]
type = aws
regions = {REGION}
""")
    source, = instantiate_sources()
    source.apply()
    assert inventory.get_all_items(NBVM) == []
    assert "AccessDenied" in caplog.text
    assert source.client.access_denied == [(None, None, "account")]


def test_authorized_run_resumes_orphaning_after_denial(make_source, inventory):
    data = service_data()
    source = make_source("sync_s3 = True", data=data)
    source.apply()
    handler = make_netbox_handler()
    inventory.tag_all_the_things(handler)
    bucket = next(disk for disk in inventory.get_all_items(NBVirtualDisk)
                  if disk.data["name"] == "inventory-bucket")
    data["Buckets"] = []
    bucket.source = None
    source.client.access_denied = [(ACCOUNT, REGION, "s3")]
    source.apply()
    inventory.tag_all_the_things(handler)
    assert handler.orphaned_tag not in bucket.get_tags()
    bucket.source = None
    source.client.access_denied = []
    source.apply()
    inventory.tag_all_the_things(handler)
    assert handler.orphaned_tag in bucket.get_tags()


def test_ec2_denial_still_discovers_optional_services(service_sdk):
    identity(service_sdk["sts"])
    service_sdk["ec2"].add_client_error("describe_instances", service_error_code="UnauthorizedOperation",
                                       expected_params={})
    stub_s3(service_sdk["s3"])
    client = AWSClient()
    account, snapshots = client.discover([REGION], services=("s3",))
    assert account == ACCOUNT
    assert snapshots[REGION]["EC2AccessDenied"] is True
    assert snapshots[REGION]["Buckets"][0]["Name"] == "inventory-bucket"
    assert client.access_denied == [(ACCOUNT, REGION, "ec2")]
