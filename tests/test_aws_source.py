import copy
import os
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

boto3 = pytest.importorskip("boto3")
from botocore.stub import Stubber

from module.config.file_output import ConfigFileOutput
from module.config.parser import ConfigParser
from module.netbox import (
    NBCluster, NBIPAddress, NBMACAddress, NBVirtualDisk, NBVM, NBVMInterface, NBVRF,
)
from module.netbox.connection import NetBoxHandler
from module.sources import instantiate_sources, valid_sources
from module.sources.aws import client as aws_client
from module.sources.aws.client import AWSClient
from module.sources.aws.config import AWSConfig
from module.sources.aws.connection import AWSHandler

ACCOUNT = "123456789012"
REGION = "us-east-1"
INSTANCE = "i-0123456789abcdef0"
VPC = "vpc-0123456789abcdef0"
SUBNET = "subnet-0123456789abcdef0"
ENI = "eni-0123456789abcdef0"
VOLUME = "vol-0123456789abcdef0"


def inventory_data():
    return {
        "instances": [{
            "InstanceId": INSTANCE, "InstanceType": "t3.small",
            "State": {"Name": "running"},
            "Placement": {"AvailabilityZone": "us-east-1a"},
            "VpcId": VPC, "SubnetId": SUBNET,
            "Tags": [{"Key": "Name", "Value": "web"}, {"Key": "Environment", "Value": "test"}],
            "NetworkInterfaces": [{"NetworkInterfaceId": ENI}],
            "BlockDeviceMappings": [{"DeviceName": "/dev/sda1", "Ebs": {"VolumeId": VOLUME}}],
        }],
        "InstanceTypes": {"t3.small": {
            "InstanceType": "t3.small", "VCpuInfo": {"DefaultVCpus": 2},
            "MemoryInfo": {"SizeInMiB": 2048},
        }},
        "NetworkInterfaces": {ENI: {
            "NetworkInterfaceId": ENI, "VpcId": VPC, "SubnetId": SUBNET,
            "Attachment": {"InstanceId": INSTANCE, "DeviceIndex": 0},
            "MacAddress": "02:aa:bb:cc:dd:ee",
            "PrivateIpAddresses": [
                {"PrivateIpAddress": "10.0.0.10", "Primary": True,
                 "Association": {"PublicIp": "198.51.100.10"}},
                {"PrivateIpAddress": "10.0.0.11", "Primary": False},
            ],
            "Ipv6Addresses": [{"Ipv6Address": "2001:db8::10", "IsPrimaryIpv6": True}],
        }},
        "Volumes": {VOLUME: {"VolumeId": VOLUME, "Size": 20, "VolumeType": "gp3"}},
        "Subnets": {SUBNET: {
            "SubnetId": SUBNET, "VpcId": VPC, "CidrBlock": "10.0.0.0/24",
            "Ipv6CidrBlockAssociationSet": [{
                "Ipv6CidrBlock": "2001:db8::/64", "Ipv6CidrBlockState": {"State": "associated"},
            }],
        }},
        "Vpcs": {VPC: {"VpcId": VPC}},
    }


@pytest.fixture(autouse=True)
def windows_config_paths(monkeypatch):
    if os.name == "nt":
        monkeypatch.setattr(ConfigParser, "get_config_file_path", staticmethod(os.path.abspath))


@pytest.fixture
def make_source(inventory, load_config, monkeypatch):
    def make(extra="", data=None, regions=REGION):
        load_config(f"""
[source/test-aws]
type = aws
regions = {regions}
permitted_subnets = 10.0.0.0/8, 2001:db8::/32
tag_keys = Environment
{extra}
""")
        discovery = Mock(return_value=(ACCOUNT, {REGION: data if data is not None else inventory_data()}))
        monkeypatch.setattr(AWSClient, "__init__", lambda self, profile=None: None)
        monkeypatch.setattr(AWSClient, "discover", discovery)
        sources = instantiate_sources()
        assert len(sources) == 1 and sources[0].init_successful
        return sources[0]
    return make


@pytest.fixture
def vpc_vrf(inventory):
    return inventory.add_object(NBVRF, data={
        "id": 1, "name": f"AWS {ACCOUNT}/{REGION}/{VPC}",
    }, read_from_netbox=True)


def test_registration():
    assert AWSHandler in valid_sources
    assert AWSConfig in ConfigFileOutput.source_config_list
    assert NBVRF in AWSHandler.dependent_netbox_objects
    assert NBMACAddress in AWSHandler.dependent_netbox_objects


@pytest.mark.parametrize("api_version", ["3.6.0", "3.7.0", "4.1.0", "4.3.0"])
def test_ec2_ebs_eni_import(make_source, inventory, vpc_vrf, api_version):
    inventory.netbox_api_version = api_version
    source = make_source("tenant = Operations")
    source.apply()
    vm, = inventory.get_all_items(NBVM)
    cluster, = inventory.get_all_items(NBCluster)
    interface, = inventory.get_all_items(NBVMInterface)
    assert vm.data["status"] == "active"
    assert vm.data["name"] == "web"
    assert vm.data["vcpus"] == 2
    assert vm.data["memory"] == 2048
    assert vm.data["custom_fields"]["aws_name"] == "web"
    assert vm.data["custom_fields"]["aws_instance_id"] == INSTANCE
    assert vm.data["tenant"].data["name"] == "Operations"
    assert "AWS:Environment=test" in vm.get_tags()
    assert interface.data["name"] == ENI
    assert interface.data["custom_fields"]["aws_public_ipv4"] == "198.51.100.10"
    ips = inventory.get_all_items(NBIPAddress)
    assert {item.data["address"] for item in ips} == {
        "10.0.0.10/24", "10.0.0.11/24", "2001:db8::10/64",
    }
    assert all(item.data["vrf"] is vpc_vrf for item in ips)
    assert vm.data["primary_ip4"].data["address"] == "10.0.0.10/24"
    assert vm.data["primary_ip6"].data["address"] == "2001:db8::10/64"
    if api_version >= "4.2":
        assert cluster.data["scope_type"] == "dcim.site"
        assert cluster.data["scope_id"].data["name"] == f"AWS {REGION}"
        mac, = inventory.get_all_items(NBMACAddress)
        assert mac is interface.data["primary_mac_address"]
    if api_version < "3.7":
        assert vm.data["disk"] == 20
    else:
        disk, = inventory.get_all_items(NBVirtualDisk)
        assert disk.data["name"] == VOLUME
        assert disk.data["size"] == (20 if api_version < "4.1" else 20480)
    source.apply()
    assert len(inventory.get_all_items(NBVM)) == 1
    assert len(inventory.get_all_items(NBVMInterface)) == 1
    assert len(inventory.get_all_items(NBIPAddress)) == 3


def test_overlapping_vpcs_have_distinct_ips(make_source, inventory, vpc_vrf):
    data = inventory_data()
    second = copy.deepcopy(data)
    other_instance, other_eni = "i-11111111111111111", "eni-11111111111111111"
    other_vpc, other_subnet = "vpc-11111111111111111", "subnet-11111111111111111"
    vm = second["instances"][0]
    vm.update(InstanceId=other_instance, VpcId=other_vpc, SubnetId=other_subnet,
              NetworkInterfaces=[{"NetworkInterfaceId": other_eni}], BlockDeviceMappings=[])
    eni = second["NetworkInterfaces"].pop(ENI)
    eni.update(NetworkInterfaceId=other_eni, VpcId=other_vpc, SubnetId=other_subnet,
               MacAddress="02:aa:bb:cc:dd:ff")
    eni["Attachment"]["InstanceId"] = other_instance
    subnet = second["Subnets"].pop(SUBNET)
    subnet.update(SubnetId=other_subnet, VpcId=other_vpc)
    data["instances"].append(vm)
    data["NetworkInterfaces"][other_eni] = eni
    data["Subnets"][other_subnet] = subnet
    data["Vpcs"][other_vpc] = {"VpcId": other_vpc}
    inventory.add_object(NBVRF, data={
        "id": 2, "name": f"AWS {ACCOUNT}/{REGION}/{other_vpc}",
    }, read_from_netbox=True)
    source = make_source(data=data)
    source.apply()
    assert len(inventory.get_all_items(NBVM)) == 2
    assert len(inventory.get_all_items(NBIPAddress)) == 6


def test_missing_vrf_aborts_before_inventory_changes(make_source, inventory, caplog):
    source = make_source()
    with pytest.raises(SystemExit):
        source.apply()
    assert inventory.get_all_items(NBVM) == []
    assert inventory.get_all_items(NBCluster) == []
    assert "Create NetBox VRF" in caplog.text


def test_missing_volume_aborts_before_inventory_changes(make_source, inventory, vpc_vrf):
    data = inventory_data()
    data["Volumes"].clear()
    source = make_source(data=data)
    with pytest.raises(SystemExit):
        source.apply()
    assert inventory.get_all_items(NBVM) == []


def test_stopped_instance_without_public_ip_is_retained(make_source, inventory, vpc_vrf):
    data = inventory_data()
    data["instances"][0]["State"]["Name"] = "stopped"
    data["NetworkInterfaces"][ENI]["PrivateIpAddresses"][0].pop("Association")
    source = make_source(data=data)
    source.apply()
    vm, = inventory.get_all_items(NBVM)
    assert vm.data["status"] == "offline"
    assert len(inventory.get_all_items(NBIPAddress)) == 3
    assert inventory.get_all_items(NBVMInterface)[0].data["custom_fields"]["aws_public_ipv4"] == ""


def test_name_tag_rename_does_not_duplicate_vm(make_source, inventory, vpc_vrf):
    data = inventory_data()
    source = make_source(data=data)
    source.apply()
    data["instances"][0]["Tags"][0]["Value"] = "renamed"
    source.apply()
    vm, = inventory.get_all_items(NBVM)
    assert vm.data["name"] == "renamed"
    assert vm.data["custom_fields"]["aws_name"] == "renamed"


def test_missing_name_tag_uses_instance_id(make_source, inventory, vpc_vrf):
    data = inventory_data()
    data["instances"][0]["Tags"] = [{"Key": "Environment", "Value": "test"}]
    make_source(data=data).apply()
    vm, = inventory.get_all_items(NBVM)
    assert vm.data["name"] == INSTANCE
    assert vm.data["custom_fields"]["aws_instance_id"] == INSTANCE


def test_duplicate_name_tags_include_instance_ids(make_source, inventory, vpc_vrf):
    data = inventory_data()
    duplicate = copy.deepcopy(data["instances"][0])
    duplicate_id = "i-11111111111111111"
    duplicate["InstanceId"] = duplicate_id
    duplicate["NetworkInterfaces"] = []
    duplicate["BlockDeviceMappings"] = []
    data["instances"].append(duplicate)
    make_source(data=data).apply()
    assert {vm.data["name"] for vm in inventory.get_all_items(NBVM)} == {
        f"web ({INSTANCE})", f"web ({duplicate_id})",
    }


def test_account_name_is_used_for_cluster_labels(make_source, inventory, vpc_vrf):
    make_source("account_name = Production", data=inventory_data()).apply()
    cluster, = inventory.get_all_items(NBCluster)
    assert cluster.data["name"] == f"AWS Production/{REGION}"


def test_duplicate_organization_account_names_remain_distinct(make_source, inventory):
    snapshot = {REGION: inventory_data()}
    source = make_source("organization = True\nsync_private_ips = False")
    source.client.account_names = {ACCOUNT: "Shared", SECOND_ACCOUNT: "Shared"}
    source.client.discover_organization = Mock(return_value={
        ACCOUNT: snapshot, SECOND_ACCOUNT: snapshot,
    })
    source.apply()
    assert {cluster.data["name"] for cluster in inventory.get_all_items(NBCluster)} == {
        f"AWS Shared ({ACCOUNT})/{REGION}", f"AWS Shared ({SECOND_ACCOUNT})/{REGION}",
    }
    assert len(inventory.get_all_items(NBVM)) == 2


def test_removed_aws_tag_is_reconciled_but_user_tag_is_kept(make_source, inventory, vpc_vrf):
    data = inventory_data()
    source = make_source(data=data)
    source.apply()
    vm, = inventory.get_all_items(NBVM)
    vm.add_tags("user-managed")
    data["instances"][0]["Tags"] = [{"Key": "Environment", "Value": "production"}]
    source.apply()
    assert "AWS:Environment=test" not in vm.get_tags()
    assert "AWS:Environment=production" in vm.get_tags()
    assert "user-managed" in vm.get_tags()


def test_removed_secondary_ip_and_mac_are_detached(make_source, inventory, vpc_vrf):
    data = inventory_data()
    source = make_source(data=data)
    source.apply()
    secondary = next(ip for ip in inventory.get_all_items(NBIPAddress)
                     if ip.data["address"] == "10.0.0.11/24")
    old_mac, = inventory.get_all_items(NBMACAddress)
    data["NetworkInterfaces"][ENI]["PrivateIpAddresses"].pop()
    data["NetworkInterfaces"][ENI]["MacAddress"] = "02:00:00:00:00:01"
    source.apply()
    assert "assigned_object_id" in secondary.unset_items
    assert "assigned_object_id" in old_mac.unset_items
    interface, = inventory.get_all_items(NBVMInterface)
    assert interface.data["primary_mac_address"].data["mac_address"] == "02:00:00:00:00:01"


def test_primary_address_removal_clears_primary_ip(make_source, inventory, vpc_vrf):
    data = inventory_data()
    source = make_source(data=data)
    source.apply()
    vm, = inventory.get_all_items(NBVM)
    data["NetworkInterfaces"][ENI]["PrivateIpAddresses"] = []
    data["NetworkInterfaces"][ENI]["Ipv6Addresses"] = []
    source.apply()
    assert "primary_ip4" in vm.unset_items
    assert "primary_ip6" in vm.unset_items


def test_ip_owned_by_other_source_is_not_reassigned(make_source, inventory, vpc_vrf):
    source = make_source()
    other = inventory.add_object(NBVM, data={"name": "other", "status": "active"})
    other_interface = inventory.add_object(NBVMInterface, data={"name": "eth0", "virtual_machine": other})
    existing = inventory.add_object(NBIPAddress, data={
        "address": "10.0.0.10/24", "vrf": vpc_vrf, "assigned_object_id": other_interface,
        "assigned_object_type": NBVMInterface,
    })
    with pytest.raises(SystemExit):
        source.apply()
    assert existing.get_interface() is other_interface


def test_tagging_and_successful_empty_discovery_use_existing_orphan_lifecycle(
        make_source, inventory, vpc_vrf):
    data = inventory_data()
    source = make_source(data=data)
    source.apply()
    handler = object.__new__(NetBoxHandler)
    handler.settings = SimpleNamespace(
        prune_enabled=True, ignore_unknown_source_object_pruning=False,
        orphaned_device_status=None, orphaned_vm_status=None,
    )
    inventory.tag_all_the_things(handler)
    vm, = inventory.get_all_items(NBVM)
    assert source.source_tag in vm.get_tags()
    assert handler.primary_tag in vm.get_tags()
    for items in inventory.base_structure.values():
        for item in items:
            item.source = None
    data["instances"] = []
    source.apply()
    inventory.tag_all_the_things(handler)
    assert handler.orphaned_tag in vm.get_tags()


def test_customized_cpu_options_override_type_default(make_source, inventory, vpc_vrf):
    data = inventory_data()
    data["instances"][0]["CpuOptions"] = {"CoreCount": 1, "ThreadsPerCore": 1}
    source = make_source(data=data)
    source.apply()
    vm, = inventory.get_all_items(NBVM)
    assert vm.data["vcpus"] == 1


def test_multiple_enis_only_primary_eni_sets_primary_ip(make_source, inventory, vpc_vrf):
    data = inventory_data()
    secondary_id = "eni-11111111111111111"
    secondary = copy.deepcopy(data["NetworkInterfaces"][ENI])
    secondary.update(NetworkInterfaceId=secondary_id, MacAddress="02:00:00:00:00:01")
    secondary["Attachment"]["DeviceIndex"] = 1
    secondary["PrivateIpAddresses"] = [{"PrivateIpAddress": "10.0.0.20", "Primary": True}]
    secondary["Ipv6Addresses"] = []
    data["NetworkInterfaces"][secondary_id] = secondary
    data["instances"][0]["NetworkInterfaces"].append({"NetworkInterfaceId": secondary_id})
    source = make_source(data=data)
    source.apply()
    vm, = inventory.get_all_items(NBVM)
    assert len(inventory.get_all_items(NBVMInterface)) == 2
    assert vm.data["primary_ip4"].data["address"] == "10.0.0.10/24"


def test_optional_network_import_does_not_require_vrf(make_source, inventory):
    source = make_source("sync_private_ips = False")
    source.apply()
    assert len(inventory.get_all_items(NBVMInterface)) == 1
    assert inventory.get_all_items(NBIPAddress) == []


def test_explicit_vrf_mapping(make_source, inventory):
    inventory.add_object(NBVRF, data={"id": 1, "name": "Production"}, read_from_netbox=True)
    source = make_source(f"vpc_vrf_relation = {REGION}/{VPC} = Production")
    source.apply()
    assert {item.data["vrf"].data["name"] for item in inventory.get_all_items(NBIPAddress)} == {"Production"}


def test_quoted_vrf_name_with_comma(make_source, inventory):
    inventory.add_object(NBVRF, data={"id": 1, "name": "Production, New York"}, read_from_netbox=True)
    source = make_source(f'vpc_vrf_relation = {REGION}/{VPC} = "Production, New York"')
    source.apply()
    assert {item.data["vrf"].data["name"] for item in inventory.get_all_items(NBIPAddress)} == {
        "Production, New York",
    }


def test_disallowed_addresses_are_not_imported(make_source, inventory, vpc_vrf):
    source = make_source()
    from module.sources.common.permitted_subnets import PermittedSubnets
    source.settings.permitted_subnets = PermittedSubnets("192.168.0.0/16")
    source.apply()
    assert inventory.get_all_items(NBIPAddress) == []
    assert len(inventory.get_all_items(NBVMInterface)) == 1


def test_unmapped_subnet_address_aborts_before_changes(make_source, inventory, vpc_vrf):
    data = inventory_data()
    data["NetworkInterfaces"][ENI]["PrivateIpAddresses"][0]["PrivateIpAddress"] = "10.1.1.1"
    source = make_source(data=data)
    with pytest.raises(SystemExit):
        source.apply()
    assert inventory.get_all_items(NBCluster) == []


def test_missing_sdk_aborts_enabled_source_but_disabled_source_is_safe(
        inventory, load_config, monkeypatch, caplog):
    monkeypatch.setattr(aws_client, "boto3", None)
    load_config(f"""
[source/test-aws]
type = aws
regions = {REGION}
enabled = False
""")
    assert instantiate_sources() == []
    load_config(f"""
[source/test-aws]
type = aws
regions = {REGION}
""")
    with pytest.raises(SystemExit):
        instantiate_sources()
    assert "requirements-aws.txt" in caplog.text


def test_yaml_lists_and_account_guard_parse(inventory, load_config, monkeypatch):
    monkeypatch.setattr(AWSClient, "__init__", lambda self, profile=None: None)
    load_config(f"""
source:
  test-aws:
    type: aws
    regions: [us-east-1, us-west-2]
    account_id: "{ACCOUNT}"
    tag_keys: [Environment, Owner]
""", "settings.yaml")
    source, = instantiate_sources()
    assert source.settings.regions == ["us-east-1", "us-west-2"]
    assert source.settings.tag_keys == ["Environment", "Owner"]
    assert source.settings.account_id == ACCOUNT


@pytest.mark.parametrize("extra", [
    "account_id = not-an-account",
    "vpc_vrf_relation = malformed",
    f"vpc_vrf_relation = {REGION}/{VPC} = One, {REGION}/{VPC} = Two",
])
def test_invalid_config_fails(make_source, extra):
    with pytest.raises(SystemExit):
        make_source(extra)


@pytest.fixture
def sdk(monkeypatch):
    session = boto3.Session(
        aws_access_key_id="test-only", aws_secret_access_key="test-only", region_name=REGION)
    sts = session.client("sts")
    ec2 = session.client("ec2")
    sts_stub, ec2_stub = Stubber(sts), Stubber(ec2)
    fake_session = Mock()
    fake_session.region_name = REGION
    fake_session.client.side_effect = lambda service, **kwargs: sts if service == "sts" else ec2
    monkeypatch.setattr(aws_client.boto3, "Session", Mock(return_value=fake_session))
    with sts_stub, ec2_stub:
        yield sts_stub, ec2_stub
        sts_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()


def identity(stub, account=ACCOUNT, partition="aws"):
    stub.add_response("get_caller_identity", {
        "Account": account, "Arn": f"arn:{partition}:iam::{account}:user/test", "UserId": "test",
    }, {})


def stub_region(stub, data):
    stub.add_response("describe_instances", {"Reservations": [{
        "Instances": data["instances"],
    }], "NextToken": "next-page"}, {})
    stub.add_response("describe_instances", {"Reservations": []}, {"NextToken": "next-page"})
    for operation, result_key in (
        ("describe_network_interfaces", "NetworkInterfaces"),
        ("describe_volumes", "Volumes"), ("describe_subnets", "Subnets"),
        ("describe_vpcs", "Vpcs"),
    ):
        stub.add_response(operation, {result_key: list(data[result_key].values())}, {})
    if data["instances"]:
        stub.add_response("describe_instance_types", {
            "InstanceTypes": list(data["InstanceTypes"].values()),
        }, {"InstanceTypes": ["t3.small"]})


def test_sdk_paginated_discovery(sdk):
    sts, ec2 = sdk
    identity(sts)
    data = inventory_data()
    stub_region(ec2, data)
    account, snapshot = AWSClient().discover([REGION], ACCOUNT)
    assert account == ACCOUNT
    assert snapshot[REGION]["instances"] == data["instances"]
    assert snapshot[REGION]["Volumes"] == data["Volumes"]


def test_stubbed_sdk_to_real_inventory(sdk, inventory, load_config, vpc_vrf):
    sts, ec2 = sdk
    identity(sts)
    stub_region(ec2, inventory_data())
    load_config(f"""
[source/test-aws]
type = aws
regions = {REGION}
account_id = {ACCOUNT}
permitted_subnets = 10.0.0.0/8, 2001:db8::/32
""")
    source, = instantiate_sources()
    source.apply()
    vm, = inventory.get_all_items(NBVM)
    assert vm.data["name"] == INSTANCE
    assert vm.data["primary_ip4"].data["vrf"] is vpc_vrf
    assert len(inventory.get_all_items(NBVirtualDisk)) == 1


def test_profile_is_forwarded_to_sdk(sdk):
    AWSClient(profile="inventory")
    aws_client.boto3.Session.assert_called_once_with(profile_name="inventory")


def test_sdk_error_after_first_region_aborts_entire_apply(sdk, inventory, load_config, caplog):
    sts, ec2 = sdk
    identity(sts)
    stub_region(ec2, inventory_data())
    ec2.add_client_error("describe_instances", service_error_code="UnauthorizedOperation",
                         service_message="not permitted", http_status_code=403, expected_params={})
    load_config(f"""
[source/test-aws]
type = aws
regions = {REGION}, us-west-2
sync_private_ips = False
""")
    source, = instantiate_sources()
    with pytest.raises(SystemExit):
        source.apply()
    assert inventory.get_all_items(NBVM) == []
    assert inventory.get_all_items(NBCluster) == []
    assert "No NetBox writes or pruning" in caplog.text


def test_wrong_account_aborts(sdk):
    sts, _ = sdk
    identity(sts)
    with pytest.raises(aws_client.AWSDiscoveryError, match="account mismatch"):
        AWSClient().discover([REGION], "999999999999")


def test_terminated_instances_are_excluded(sdk):
    sts, ec2 = sdk
    identity(sts)
    terminated = inventory_data()["instances"][0]
    terminated["State"]["Name"] = "terminated"
    ec2.add_response("describe_instances", {"Reservations": [{"Instances": [terminated]}]}, {})
    for operation, result_key in (
        ("describe_network_interfaces", "NetworkInterfaces"), ("describe_volumes", "Volumes"),
        ("describe_subnets", "Subnets"), ("describe_vpcs", "Vpcs"),
    ):
        ec2.add_response(operation, {result_key: []}, {})
    _, snapshot = AWSClient().discover([REGION])
    assert snapshot[REGION]["instances"] == []


def test_missing_sdk_fails_explicitly(monkeypatch):
    monkeypatch.setattr(aws_client, "boto3", None)
    with pytest.raises(aws_client.AWSDiscoveryError, match="requirements-aws.txt"):
        AWSClient()


def region_response(stub, regions):
    stub.add_response("describe_regions", {"Regions": [
        {"RegionName": name, "OptInStatus": status} for name, status in regions
    ]}, {"AllRegions": True})


def test_auto_discovers_enabled_regions_and_preserves_region_identity(sdk):
    sts, ec2 = sdk
    identity(sts)
    region_response(ec2, [
        ("us-west-2", "opted-in"),
        (REGION, "opt-in-not-required"),
        ("ap-east-1", "not-opted-in"),
        (REGION, "opt-in-not-required"),
    ])
    stub_region(ec2, inventory_data())
    stub_region(ec2, inventory_data())
    client = AWSClient()
    account, snapshot = client.discover(["auto"], ACCOUNT)
    assert account == ACCOUNT
    assert list(snapshot) == [REGION, "us-west-2"]
    ec2_regions = [
        call.kwargs["region_name"] for call in client.session.client.call_args_list
        if call.args[0] == "ec2"
    ]
    assert ec2_regions == [REGION, REGION, "us-west-2"]


def test_auto_bootstrap_override(sdk):
    sts, ec2 = sdk
    identity(sts)
    region_response(ec2, [(REGION, "opt-in-not-required")])
    stub_region(ec2, inventory_data())
    client = AWSClient()
    client.session.region_name = None
    client.discover(["auto"], discovery_region="us-west-2")
    assert client.session.client.call_args_list[0].kwargs["region_name"] == "us-west-2"
    assert client.session.client.call_args_list[1].kwargs["region_name"] == "us-west-2"
    assert client.session.client.call_args_list[2].kwargs["region_name"] == REGION


def test_auto_without_bootstrap_fails_explicitly(sdk):
    client = AWSClient()
    client.session.region_name = None
    with pytest.raises(aws_client.AWSDiscoveryError, match="AWS_DEFAULT_REGION"):
        client.discover(["auto"])
    client.session.client.assert_not_called()


@pytest.mark.parametrize("regions", [[], [("ap-east-1", "not-opted-in")]])
def test_auto_no_enabled_regions_aborts(sdk, regions):
    sts, ec2 = sdk
    identity(sts)
    region_response(ec2, regions)
    with pytest.raises(aws_client.AWSDiscoveryError, match="no enabled regions"):
        AWSClient().discover(["auto"])


def test_auto_permission_failure_aborts_before_inventory_changes(
        sdk, inventory, load_config, caplog):
    sts, ec2 = sdk
    identity(sts)
    ec2.add_client_error("describe_regions", service_error_code="UnauthorizedOperation",
                         service_message="not permitted", http_status_code=403,
                         expected_params={"AllRegions": True})
    load_config("""
[source/test-aws]
type = aws
sync_private_ips = False
""")
    source, = instantiate_sources()
    assert source.settings.regions == ["auto"]
    with pytest.raises(SystemExit):
        source.apply()
    assert inventory.get_all_items(NBVM) == []
    assert inventory.get_all_items(NBCluster) == []
    assert "No NetBox writes or pruning" in caplog.text


def test_auto_later_region_failure_does_not_return_partial_inventory(
        sdk, inventory, load_config):
    sts, ec2 = sdk
    identity(sts)
    region_response(ec2, [(REGION, "opt-in-not-required"), ("us-west-2", "opted-in")])
    stub_region(ec2, inventory_data())
    ec2.add_client_error("describe_instances", service_error_code="UnauthorizedOperation",
                         http_status_code=403, expected_params={})
    load_config("""
[source/test-aws]
type = aws
regions = auto
sync_private_ips = False
""")
    source, = instantiate_sources()
    with pytest.raises(SystemExit):
        source.apply()
    assert inventory.get_all_items(NBVM) == []
    assert inventory.get_all_items(NBCluster) == []


@pytest.mark.parametrize("regions", ["auto, us-east-1", "us-east-1, auto"])
def test_auto_cannot_be_mixed_with_explicit_regions(make_source, regions):
    with pytest.raises(SystemExit):
        make_source(regions=regions)


def test_auto_yaml_list_parses(inventory, load_config, monkeypatch):
    monkeypatch.setattr(AWSClient, "__init__", lambda self, profile=None: None)
    load_config("""
source:
  test-aws:
    type: aws
    regions: [auto]
    discovery_region: us-west-2
""", "settings.yaml")
    source, = instantiate_sources()
    assert source.settings.regions == ["auto"]
    assert source.settings.discovery_region == "us-west-2"


def test_explicit_regions_do_not_need_bootstrap_or_describe_regions(sdk):
    sts, ec2 = sdk
    identity(sts)
    stub_region(ec2, inventory_data())
    client = AWSClient()
    client.session.region_name = None
    _, snapshot = client.discover([REGION], discovery_region="us-west-2")
    assert list(snapshot) == [REGION]
    assert client.session.client.call_args_list[0].kwargs["region_name"] == REGION


MANAGEMENT = "999999999999"
SECOND_ACCOUNT = "234567890123"


@pytest.fixture
def org_sdk(monkeypatch):
    real_session = boto3.Session(
        aws_access_key_id="test-only", aws_secret_access_key="test-only", region_name=REGION)
    stubs = []

    def make_session(organization=False):
        services = ("sts", "organizations") if organization else ("sts", "ec2")
        clients = {service: real_session.client(service) for service in services}
        session = Mock(region_name=REGION)
        session.client.side_effect = lambda service, **kwargs: clients[service]
        bundle = SimpleNamespace(session=session)
        for service, client in clients.items():
            stub = Stubber(client)
            stubs.append(stub)
            setattr(bundle, service, stub)
        return bundle

    central = make_session(organization=True)
    members = {account: make_session() for account in (MANAGEMENT, ACCOUNT, SECOND_ACCOUNT)}
    factory = Mock(side_effect=lambda **kwargs: (
        members[kwargs["aws_session_token"]].session if "aws_session_token" in kwargs else central.session))
    monkeypatch.setattr(aws_client.boto3, "Session", factory)
    with ExitStack() as stack:
        for stub in stubs:
            stack.enter_context(stub)
        yield SimpleNamespace(central=central, members=members, factory=factory)
        for stub in stubs:
            stub.assert_no_pending_responses()


def organization_accounts(sdk, accounts=None, partition="aws"):
    identity(sdk.central.sts, MANAGEMENT, partition)
    sdk.central.organizations.add_response("describe_organization", {
        "Organization": {"MasterAccountId": MANAGEMENT},
    }, {})
    # An empty page with a continuation token must not terminate discovery.
    sdk.central.organizations.add_response("list_accounts", {
        "Accounts": [], "NextToken": "next-page",
    }, {})
    sdk.central.organizations.add_response("list_accounts", {
        "Accounts": accounts if accounts is not None else [
            {"Id": MANAGEMENT, "Name": "Management", "State": "ACTIVE"},
            {"Id": ACCOUNT, "Name": "Production", "State": "ACTIVE"},
            {"Id": SECOND_ACCOUNT, "Name": "Development", "State": "ACTIVE"},
            {"Id": "345678901234", "Name": "Suspended", "State": "SUSPENDED"},
        ],
    }, {"NextToken": "next-page"})


def assume_member(sdk, account, external_id=None, partition="aws", role="NetBoxInventoryReadOnly"):
    request = {"RoleArn": f"arn:{partition}:iam::{account}:role/{role}",
               "RoleSessionName": "netbox-sync", "DurationSeconds": 3600}
    if external_id:
        request["ExternalId"] = external_id
    sdk.central.sts.add_response("assume_role", {"Credentials": {
        "AccessKeyId": "ASIA" + "A" * 16, "SecretAccessKey": "test-only-secret",
        "SessionToken": account, "Expiration": datetime(2099, 1, 1, tzinfo=timezone.utc),
    }}, request)
    return sdk.members[account]


def organization_source(load_config, extra=""):
    load_config(f"""
[source/test-aws]
type = aws
organization = True
regions = auto
account_id = {MANAGEMENT}
sync_private_ips = False
{extra}
""")
    source, = instantiate_sources()
    return source


def test_organization_scans_members_not_management(org_sdk, inventory, load_config):
    organization_accounts(org_sdk)
    for account in (ACCOUNT, SECOND_ACCOUNT):
        member = assume_member(org_sdk, account)
        identity(member.sts, account)
        region_response(member.ec2, [(REGION, "opt-in-not-required")])
        stub_region(member.ec2, inventory_data())
    source = organization_source(load_config)
    source.apply()
    vms = inventory.get_all_items(NBVM)
    assert len(vms) == 2
    assert {vm.data["custom_fields"]["aws_account_id"] for vm in vms} == {ACCOUNT, SECOND_ACCOUNT}
    assert {vm.data["cluster"].data["name"] for vm in vms} == {
        f"AWS Production/{REGION}", f"AWS Development/{REGION}",
    }
    assert len(inventory.get_all_items(NBVMInterface)) == 2
    assert len(inventory.get_all_items(NBVirtualDisk)) == 2
    assert {item.data["virtual_machine"] for item in inventory.get_all_items(NBVMInterface)} == set(vms)
    assert {item.data["virtual_machine"] for item in inventory.get_all_items(NBVirtualDisk)} == set(vms)
    org_sdk.members[MANAGEMENT].session.client.assert_not_called()
    assert [call.args[0] for call in org_sdk.central.session.client.call_args_list] == [
        "sts", "organizations",
    ]
    # Each account gets fresh temporary credentials, not the collector's IAM-user credentials.
    assert [call.kwargs["aws_session_token"] for call in org_sdk.factory.call_args_list[1:]] == [
        ACCOUNT, SECOND_ACCOUNT,
    ]
    for account in (ACCOUNT, SECOND_ACCOUNT):
        source.sync_snapshot(account, {REGION: inventory_data()})
    assert len(inventory.get_all_items(NBVMInterface)) == 2
    assert len(inventory.get_all_items(NBVirtualDisk)) == 2


def test_organization_allowlist_role_path_external_id_and_partition(org_sdk):
    organization_accounts(org_sdk, partition="aws-us-gov")
    member = assume_member(org_sdk, SECOND_ACCOUNT, external_id="inventory-test",
                           partition="aws-us-gov", role="service-accounts/Inventory")
    identity(member.sts, SECOND_ACCOUNT, partition="aws-us-gov")
    stub_region(member.ec2, inventory_data())
    snapshots = AWSClient().discover_organization(
        [REGION], "service-accounts/Inventory", external_id="inventory-test",
        account_ids=[SECOND_ACCOUNT])
    assert list(snapshots) == [SECOND_ACCOUNT]
    org_sdk.members[ACCOUNT].session.client.assert_not_called()


def test_organization_can_include_management(org_sdk):
    organization_accounts(org_sdk)
    member = assume_member(org_sdk, MANAGEMENT)
    identity(member.sts, MANAGEMENT)
    stub_region(member.ec2, inventory_data())
    snapshots = AWSClient().discover_organization(
        [REGION], "NetBoxInventoryReadOnly", include_management=True, account_ids=[MANAGEMENT])
    assert list(snapshots) == [MANAGEMENT]


@pytest.mark.parametrize("failure", ["assume", "scan", "identity"])
def test_organization_later_account_failure_prevents_all_mutations(
        org_sdk, inventory, load_config, failure, caplog):
    organization_accounts(org_sdk)
    first = assume_member(org_sdk, ACCOUNT)
    identity(first.sts, ACCOUNT)
    region_response(first.ec2, [(REGION, "opt-in-not-required")])
    stub_region(first.ec2, inventory_data())
    if failure == "assume":
        org_sdk.central.sts.add_client_error(
            "assume_role", service_error_code="AccessDenied", http_status_code=403,
            expected_params={"RoleArn": f"arn:aws:iam::{SECOND_ACCOUNT}:role/NetBoxInventoryReadOnly",
                             "RoleSessionName": "netbox-sync", "DurationSeconds": 3600})
    else:
        second = assume_member(org_sdk, SECOND_ACCOUNT)
        identity(second.sts, ACCOUNT if failure == "identity" else SECOND_ACCOUNT)
        if failure == "scan":
            region_response(second.ec2, [(REGION, "opt-in-not-required")])
            second.ec2.add_client_error("describe_instances", service_error_code="UnauthorizedOperation",
                                       http_status_code=403, expected_params={})
    source = organization_source(load_config)
    sync = Mock()
    source.sync_snapshot = sync
    with pytest.raises(SystemExit):
        source.apply()
    sync.assert_not_called()
    assert inventory.get_all_items(NBVM) == []
    assert inventory.get_all_items(NBCluster) == []
    assert SECOND_ACCOUNT in caplog.text
    assert "No NetBox writes or pruning" in caplog.text


def test_organization_listing_failure_aborts(org_sdk):
    identity(org_sdk.central.sts, MANAGEMENT)
    org_sdk.central.organizations.add_response("describe_organization", {
        "Organization": {"MasterAccountId": MANAGEMENT},
    }, {})
    org_sdk.central.organizations.add_client_error(
        "list_accounts", service_error_code="AccessDeniedException", http_status_code=403, expected_params={})
    with pytest.raises(aws_client.AWSDiscoveryError, match="AccessDeniedException"):
        AWSClient().discover_organization([REGION], "NetBoxInventoryReadOnly")


@pytest.mark.parametrize("selection", [[MANAGEMENT], ["999999999999"], ["345678901234"]])
def test_organization_invalid_allowlist_is_not_silently_ignored(org_sdk, selection):
    organization_accounts(org_sdk)
    with pytest.raises(aws_client.AWSDiscoveryError, match="not active/eligible"):
        AWSClient().discover_organization([REGION], "NetBoxInventoryReadOnly", account_ids=selection)


def test_organization_no_eligible_accounts_aborts(org_sdk):
    organization_accounts(org_sdk, accounts=[{"Id": MANAGEMENT, "State": "ACTIVE"}])
    with pytest.raises(aws_client.AWSDiscoveryError, match="no eligible accounts"):
        AWSClient().discover_organization([REGION], "NetBoxInventoryReadOnly")


def test_organization_wrong_collector_account_aborts_before_listing(org_sdk):
    identity(org_sdk.central.sts, MANAGEMENT)
    with pytest.raises(aws_client.AWSDiscoveryError, match="account mismatch"):
        AWSClient().discover_organization([REGION], "NetBoxInventoryReadOnly", expected_account=ACCOUNT)
    assert org_sdk.central.session.client.call_count == 1


def test_organization_missing_bootstrap_aborts(org_sdk):
    org_sdk.central.session.region_name = None
    with pytest.raises(aws_client.AWSDiscoveryError, match="AWS_DEFAULT_REGION"):
        AWSClient().discover_organization(["auto"], "NetBoxInventoryReadOnly")
    org_sdk.central.session.client.assert_not_called()


def test_organization_all_accounts_validated_before_sync(make_source, inventory, monkeypatch):
    source = make_source("organization = True")
    invalid = inventory_data()
    invalid["Volumes"] = {}
    monkeypatch.setattr(AWSClient, "discover_organization", Mock(return_value={
        ACCOUNT: {REGION: inventory_data()}, SECOND_ACCOUNT: {REGION: invalid},
    }))
    source.sync_snapshot = Mock()
    with pytest.raises(SystemExit):
        source.apply()
    source.sync_snapshot.assert_not_called()
    assert inventory.get_all_items(NBVM) == []


def test_organization_rejects_vrf_reuse_across_accounts(make_source, vpc_vrf, monkeypatch):
    source = make_source(f"""
organization = True
vpc_vrf_relation = {REGION}/{VPC} = {vpc_vrf.data['name']}
""")
    monkeypatch.setattr(AWSClient, "discover_organization", Mock(return_value={
        ACCOUNT: {REGION: inventory_data()}, SECOND_ACCOUNT: {REGION: inventory_data()},
    }))
    source.sync_snapshot = Mock()
    with pytest.raises(SystemExit):
        source.apply()
    source.sync_snapshot.assert_not_called()


def test_organization_account_specific_vrf_mapping(make_source, inventory, vpc_vrf, monkeypatch):
    second_vrf = inventory.add_object(NBVRF, data={"id": 2, "name": "Second VPC"}, read_from_netbox=True)
    source = make_source(f"""
organization = True
vpc_vrf_relation = {SECOND_ACCOUNT}/{REGION}/{VPC} = Second VPC
""")
    monkeypatch.setattr(AWSClient, "discover_organization", Mock(return_value={
        ACCOUNT: {REGION: inventory_data()}, SECOND_ACCOUNT: {REGION: inventory_data()},
    }))
    source.apply()
    assert {address.data["vrf"] for address in inventory.get_all_items(NBIPAddress)} == {
        vpc_vrf, second_vrf,
    }


@pytest.mark.parametrize("extra", [
    "organization = True\norganization_role_name = arn:aws:iam::123456789012:role/Inventory",
    "organization = True\norganization_role_name = invalid role",
    "organization = True\norganization_external_id = x",
    "organization = True\norganization_account_ids = 123",
    "organization = True\norganization_account_ids = 123456789012, invalid",
    "organization_account_ids = 123456789012",
    "organization_include_management = True",
    "organization_external_id = test-id",
])
def test_organization_invalid_config(make_source, extra):
    with pytest.raises(SystemExit):
        make_source(extra)


def test_organization_yaml_options(inventory, load_config, monkeypatch):
    monkeypatch.setattr(AWSClient, "__init__", lambda self, profile=None: None)
    load_config(f"""
source:
  test-aws:
    type: aws
    organization: true
    organization_account_ids: ["{ACCOUNT}", "{SECOND_ACCOUNT}"]
    organization_role_name: service-accounts/Inventory
""", "settings.yaml")
    source, = instantiate_sources()
    assert source.settings.organization_account_ids == [ACCOUNT, SECOND_ACCOUNT]
    assert source.settings.organization_role_name == "service-accounts/Inventory"


def test_organization_empty_member_does_not_stop_next_account(org_sdk):
    organization_accounts(org_sdk)
    empty = inventory_data()
    empty["instances"] = []
    for account, data in ((ACCOUNT, empty), (SECOND_ACCOUNT, inventory_data())):
        member = assume_member(org_sdk, account)
        identity(member.sts, account)
        stub_region(member.ec2, data)
    snapshots = AWSClient().discover_organization([REGION], "NetBoxInventoryReadOnly")
    assert snapshots[ACCOUNT][REGION]["instances"] == []
    assert snapshots[SECOND_ACCOUNT][REGION]["instances"][0]["InstanceId"] == INSTANCE


def test_organization_paginates_150_accounts(org_sdk, monkeypatch):
    accounts = [str(111100000000 + index) for index in range(150)]
    identity(org_sdk.central.sts, MANAGEMENT)
    org_sdk.central.organizations.add_response("describe_organization", {
        "Organization": {"MasterAccountId": MANAGEMENT},
    }, {})
    for start in range(0, len(accounts), 20):
        response = {"Accounts": [
            {"Id": account, "State": "ACTIVE"} for account in accounts[start:start + 20]
        ]}
        if start + 20 < len(accounts):
            response["NextToken"] = f"page-{start + 20}"
        params = {"NextToken": f"page-{start}"} if start else {}
        org_sdk.central.organizations.add_response("list_accounts", response, params)
    for account in accounts:
        org_sdk.central.sts.add_response("assume_role", {"Credentials": {
            "AccessKeyId": "ASIA" + "A" * 16, "SecretAccessKey": "test-only-secret",
            "SessionToken": account, "Expiration": datetime(2099, 1, 1, tzinfo=timezone.utc),
        }}, {"RoleArn": f"arn:aws:iam::{account}:role/NetBoxInventoryReadOnly",
             "RoleSessionName": "netbox-sync", "DurationSeconds": 3600})
    org_sdk.factory.side_effect = lambda **kwargs: org_sdk.central.session
    discovery = Mock(side_effect=lambda regions, expected_account, discovery_region: (
        expected_account, {REGION: inventory_data()}))
    monkeypatch.setattr(AWSClient, "discover", discovery)
    snapshots = AWSClient().discover_organization([REGION], "NetBoxInventoryReadOnly")
    assert list(snapshots) == accounts
    assert discovery.call_count == 150
    assert org_sdk.factory.call_count == 151


def test_organization_source_forwards_options(make_source, monkeypatch):
    source = make_source(f"""
organization = True
organization_include_management = True
organization_role_name = service-accounts/Inventory
organization_external_id = inventory-test
organization_account_ids = {ACCOUNT}, {SECOND_ACCOUNT}
discovery_region = us-west-2
""")
    discovery = Mock(return_value={ACCOUNT: {}})
    monkeypatch.setattr(AWSClient, "discover_organization", discovery)
    source.apply()
    discovery.assert_called_once_with(
        [REGION], "service-accounts/Inventory", expected_account=None,
        discovery_region="us-west-2", include_management=True, external_id="inventory-test",
        account_ids=[ACCOUNT, SECOND_ACCOUNT])


@pytest.mark.parametrize("accounts", ["[]", "[123456789012]", "123456789012", "true"])
def test_organization_invalid_yaml_allowlist(inventory, load_config, monkeypatch, accounts):
    monkeypatch.setattr(AWSClient, "__init__", lambda self, profile=None: None)
    load_config(f"""
source:
  test-aws:
    type: aws
    organization: true
    organization_account_ids: {accounts}
""", "settings.yaml")
    with pytest.raises(SystemExit):
        instantiate_sources()
