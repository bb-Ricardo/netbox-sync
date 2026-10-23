import copy
import os
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
    assert vm.data["name"] == INSTANCE
    assert vm.data["status"] == "active"
    assert vm.data["vcpus"] == 2
    assert vm.data["memory"] == 2048
    assert vm.data["custom_fields"]["aws_name"] == "web"
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
    assert vm.data["name"] == INSTANCE
    assert vm.data["custom_fields"]["aws_name"] == "renamed"


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
    fake_session.client.side_effect = lambda service, **kwargs: sts if service == "sts" else ec2
    monkeypatch.setattr(aws_client.boto3, "Session", Mock(return_value=fake_session))
    with sts_stub, ec2_stub:
        yield sts_stub, ec2_stub
        sts_stub.assert_no_pending_responses()
        ec2_stub.assert_no_pending_responses()


def identity(stub):
    stub.add_response("get_caller_identity", {
        "Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/test", "UserId": "test",
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
