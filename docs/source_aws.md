# AWS EC2 source

The `aws` source imports EC2 instances, attached EBS volumes, attached network
interfaces (ENIs), MAC addresses, and permitted private IPv4 / IPv6 addresses.
AWS is queried read-only; writes are only made to NetBox. This branch is based
on current main and does not depend on the oVirt or OpenStack branches.

## Installation and authentication

```sh
pip install -r requirements-aws.txt
# Alternatively:
uv sync --extra aws
```

Other sources do not require boto3. An enabled AWS source without its SDK or
credentials fails explicitly rather than continuing with empty inventory.
For Docker, enable the SDK explicitly:

```sh
docker build --build-arg INSTALL_AWS=true -t netbox-sync:aws .
```

Use the standard boto3 credential chain:

* EC2 instance roles, ECS task roles, or EKS workload identity.
* A shared AWS profile (`profile` below), including an assume-role profile for
  cross-account access.
* `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`; temporary credentials also need
  `AWS_SESSION_TOKEN`. Prefer roles over stored access keys.

Each configured source handles one AWS account and an explicit region list.
Configure another named source/profile for another account. The optional
`account_id` guard verifies the account returned by STS before importing anything.
Profile-based assume-role credential refresh is handled by boto3. Credentials
must allow access to every configured region.

No SSH access, guest agents, inbound connections, or AWS write permissions are
needed. Outbound HTTPS to the regional EC2 and STS endpoints is required.

Example IAM policy (attach to a dedicated role or non-root identity):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ec2:DescribeInstances",
        "ec2:DescribeInstanceTypes",
        "ec2:DescribeNetworkInterfaces",
        "ec2:DescribeVolumes",
        "ec2:DescribeSubnets",
        "ec2:DescribeVpcs",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

An assume-role profile additionally needs `sts:AssumeRole` permission on the
target role and a matching target-account trust policy (and external ID where
required). No Organizations permissions are used.

## Configuration

```ini
[source/production-aws]
type = aws
regions = us-east-1, us-west-2
profile = inventory
account_id = 123456789012
permitted_subnets = 10.0.0.0/8, 2001:db8::/32
vpc_vrf_relation = us-east-1/vpc-0123456789abcdef0 = Production VPC
tag_keys = Environment, Owner
tenant = Operations
```

All options also support the application's usual `NBS_SOURCE_<index>_*`
environment variables. `regions` and `tag_keys` accept YAML lists as well as
comma-separated strings. Quote the account ID in YAML so it remains a string.
`python netbox-sync.py -g` generates the full configuration.

### VPC address-space isolation

The current NetBox integration reads VRFs and prefixes but does not create them.
Before enabling IP import, create a **distinct VRF for every VPC** in NetBox.
The default VRF name is:

```text
AWS <account-id>/<region>/<vpc-id>
```

For example: `AWS 123456789012/us-east-1/vpc-0123456789abcdef0`.
Use `vpc_vrf_relation` to select an existing differently named VRF. A mapping
must not share a VRF between discovered VPCs. VRFs must remain distinct across
separately configured sources too.

Missing VRFs for permitted addresses abort the run before inventory updates.
There is no fallback to global address space. Subnet mask lengths come from AWS
subnet CIDRs; NetBox prefixes are neither required nor created. Identical IPs in
different VPCs remain separate NetBox IPs.

`permitted_subnets` is required to populate IP addresses; leaving it undefined
imports no IPs, matching other source behavior. `sync_private_ips = False`
imports VMs, disks, and interfaces without requiring VRFs; previously managed
IPs on discovered interfaces are retained. DNS lookup defaults to false because
private AWS DNS is often inaccessible from the sync host.

## Object mapping and lifecycle

| AWS | NetBox |
| --- | --- |
| Account ID | Cluster group `AWS <account-id>` |
| Account and region | Cluster `AWS <account-id>/<region>`, type `AWS EC2` |
| Region | Logical site `AWS <region>` |
| Instance ID | VM name; serial on NetBox 4.1+ |
| Instance type | CPU and memory, plus custom field |
| Name tag / Availability Zone | Custom fields, not identity or physical sites |
| ENI ID | VM interface name and custom field |
| ENI MAC | MAC object on NetBox 4.2+; legacy interface field on older versions |
| Private IPv4 / IPv6 | IP in the VPC's existing VRF, assigned to the ENI |
| EBS volume | Virtual disk named by volume ID |

Region sites and clusters are logical containers, not claims about physical
AWS facilities or hypervisor placement. Instance IDs within account/region
clusters avoid duplicate or changing Name tags merging unrelated VMs. Memory
uses the instance type's MiB capacity; CPU uses instance CPU options when
reported, otherwise the type's default vCPU count. This is provisioned capacity,
not utilization.

Running instances are `active`; stopped, pending, stopping, and shutting-down
instances are `offline`. Terminated instances are excluded and subsequently
follow the existing NetBox orphaning/pruning configuration. An empty, successfully
queried account/region is authoritative. All configured regions and paginated
responses are collected before reconciliation. AWS access failures, credential
failures, missing attached resources, and missing VRF mappings abort the run
before the application's NetBox write/prune phase. Transient attachment changes
may therefore require retrying the run rather than importing partial data.

NetBox 3.7+ gets separate virtual disks; older versions get aggregate VM disk
size. Disk sizes use GiB before NetBox 4.1 and MiB from 4.1 onward, consistent
with the existing VMware source's binary-size convention. Only attached EBS
volumes are imported; instance-store disks and unattached volumes are excluded.

The primary IPv4 is the primary private address on device-index-zero ENI; a
reported primary IPv6 on that ENI is handled similarly. Public IPv4 mappings
are stored on the ENI as `aws_public_ipv4`, not assigned as guest IPs or made
primary. IPv6 addresses are actual interface addresses even when globally
routable. Delegated address prefixes are not imported.

Only explicitly selected `tag_keys` become tags (`AWS:<key>=<value>`). Their
combined names must fit NetBox's 100-character limit. Existing unrelated NetBox
tags are preserved. Metadata custom fields use the `aws_` prefix. Their names
should not conflict with user-defined fields of incompatible types.

Avoid overlapping source definitions for the same account/region. Narrowing
regions, disabling IP import, or changing permitted networks affects the
source's managed scope; review a dry run before making such changes. The
existing pruning settings determine what happens to objects no longer reported.

## Validation

Start with:

```sh
python netbox-sync.py -n -c settings.ini
```

Tests use botocore `Stubber` and the real in-memory NetBox inventory, without
AWS credentials or network calls:

```sh
pip install -r requirements-aws.txt pytest
python -m pytest tests/test_aws_source.py
```

Live AWS/NetBox validation is still required before production use. This source
does not inventory RDS, load balancers, ECS/EKS workloads, security groups,
software, or guest operating-system configuration.
