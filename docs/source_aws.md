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

By default each configured source handles one AWS account. Regions are discovered
automatically by default (`regions = auto`); an explicit list is also supported.
For multiple accounts, use organization mode below or configure separate named
sources/profiles. The optional
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
        "ec2:DescribeRegions",
        "sts:GetCallerIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

An assume-role profile additionally needs `sts:AssumeRole` permission on the
target role and a matching target-account trust policy (and external ID where
required). Single-account mode does not use Organizations permissions.

## Configuration

```ini
[source/production-aws]
type = aws
regions = auto
discovery_region = us-east-1
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

### Automatic or explicit regions

`regions = auto` (also the default when omitted) calls EC2 `DescribeRegions` and
queries regions whose opt-in status is `opt-in-not-required` or `opted-in`.
Disabled regions (`not-opted-in`) are excluded. Each resource is mapped to the
region that returned it, never to the bootstrap region.

Auto discovery requires a bootstrap endpoint: set `discovery_region`, or provide
a region through the AWS profile or `AWS_DEFAULT_REGION`. There is no hardcoded
commercial-region fallback; choose an endpoint in the appropriate AWS partition
(for example GovCloud or China when applicable). The account's enabled regions
in that partition are discovered. A missing bootstrap region fails explicitly.

To restrict scope, use `regions = us-east-1, us-west-2` or a YAML list. Explicit
mode does not call `DescribeRegions`, does not need that permission, and ignores
`discovery_region`. Do not combine `auto` and named regions in the same list.

Auto mode requires `ec2:DescribeRegions` in addition to the inventory permissions.
Failure to discover regions, an empty enabled-region result, or failure to query
any enabled region aborts synchronization before NetBox writes or pruning.
There is no silent fallback to a partial region list. Use explicit mode when
credentials are intentionally restricted to a subset of enabled regions.
Switching between modes changes discovery scope; inspect a dry run before
allowing writes, particularly if narrowing to an explicit list.

### Organization / multi-account discovery

For an organization with many member accounts, enable organization mode:

```ini
[source/aws-organization]
type = aws
profile = inventory-collector
organization = True
organization_role_name = NetBoxInventoryReadOnly
discovery_region = us-east-1
regions = auto
sync_private_ips = False
; Optional safety check: the account hosting the collector credentials
account_id = 123456789012
; Start with one or two member accounts before enabling the full organization:
;organization_account_ids = 111122223333, 444455556666
;organization_external_id = your-administrator-provided-external-id
;organization_include_management = False
```

The collector enumerates all account pages (including empty pages with a
continuation token), selects accounts with `State = ACTIVE`, and excludes the
organization's management account by default. It assumes the named role in each
selected account, verifies the resulting account identity, then discovers that
account's regions and resources. No EC2 permissions are needed on the collector
identity when management-account scanning is disabled.

`organization_account_ids` is an optional allowlist, not an account-ID guard.
Quote IDs in YAML lists. An explicitly empty list is invalid; omit the option to
select all active members. Unknown, inactive, or excluded IDs in that list cause
an error rather than silently reducing scope. To scan the management account, set
`organization_include_management = True` and deploy the same inventory role
there. A role path such as `service-accounts/NetBoxInventoryReadOnly` is supported.
Role ARNs use the collector's AWS partition; cross-partition discovery is not
supported.

All selected accounts are collected and validated before reconciliation. An
Organizations API error, role-assumption failure, account mismatch, or regional
discovery failure aborts the entire run before NetBox writes or pruning. No
inaccessible account is treated as empty. A selection with no eligible accounts
also fails. Account discovery and scanning are sequential. Fresh one-hour STS
credentials are obtained immediately before each account scan, so time spent
scanning earlier accounts does not consume later accounts' credential lifetime.
These member credentials are held only in memory and are not automatically
renewed within an individual account scan; expiration fails the run safely.
Collector profile credential refresh remains managed by boto3.

#### AWS profile

The profile supplies **collector credentials**, not a separate profile for every
member. No custom organization settings are needed in the AWS profile; the
organization options belong in the application's source section.

For an existing named credentials entry `inventory-collector`, the AWS shared
config can contain:

```ini
[profile inventory-collector]
region = us-east-1
```

Store its credentials in the standard AWS shared credentials file under
`[inventory-collector]`, or use an existing assume-role/SSO profile supported by
boto3. On Windows the standard files are `%USERPROFILE%\.aws\config` and
`%USERPROFILE%\.aws\credentials`; on Linux they are `~/.aws/config` and
`~/.aws/credentials`. Authenticate SSO profiles before an interactive test.
For unattended jobs prefer a workload IAM role or an automatically refreshable
credential provider, not an expiring interactive SSO login.

For example, if the collector itself uses an assume-role profile:

```ini
[profile inventory-collector]
role_arn = arn:aws:iam::123456789012:role/NetBoxOrganizationCollector
source_profile = existing-base-profile
region = us-east-1
```

The base profile must be authorized to assume that collector role, and the role
must trust it. The collector role then needs the permissions below. If the
collector runs with an attached IAM role or environment credentials, omit the
application `profile` setting entirely. Never store access keys in the source
configuration or commit credential files.

#### Complete example: reporting roles in management and member accounts

All account IDs, profile names, role names, and user names below are fictitious
examples. Replace them with your deployment's values.

In this example, an IAM user's keys assume the management account's
`ExampleInventoryRole`, which then assumes a role also named
`ExampleInventoryRole` in each member account:

```text
IAM user credentials (example-inventory-client profile)
  -> arn:aws:iam::123456789012:role/ExampleInventoryRole
  -> arn:aws:iam::<member-account-id>:role/ExampleInventoryRole
  -> member account EC2 discovery
```

Create these files for the OS user running the importer, on the machine or in
the container where it runs. Files on a workstation are not automatically
available to a remote host, service account, or container. Adjust account IDs
and role names for your deployment.

**1. AWS shared credentials** (`%USERPROFILE%\.aws\credentials` on Windows,
`~/.aws/credentials` on Linux):

```ini
[example-inventory-client]
aws_access_key_id = REPLACE_WITH_EXISTING_IAM_USER_ACCESS_KEY
aws_secret_access_key = REPLACE_WITH_EXISTING_IAM_USER_SECRET_KEY
; For temporary base credentials, also provide:
;aws_session_token = REPLACE_WITH_SESSION_TOKEN
```

Keep this file private and out of version control. The importer does not load
the standalone test script's `.env` file or its custom
`NETBOX_AWS_ACCESS_KEY_ID` / `NETBOX_AWS_SECRET_ACCESS_KEY` variables.
Alternatively, standard `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and
`AWS_SESSION_TOKEN` variables can supply base credentials; to use those for
the collector-role profile below, replace `source_profile` with
`credential_source = Environment`. Do not set both options.

**2. AWS shared config** (`%USERPROFILE%\.aws\config` on Windows,
`~/.aws/config` on Linux):

```ini
[profile example-inventory]
role_arn = arn:aws:iam::123456789012:role/ExampleInventoryRole
source_profile = example-inventory-client
region = us-east-1
```

The `source_profile` must match the credentials section from step 1. The
`profile` prefix belongs in the shared config section, not the credentials
section. This profile performs the first role assumption; the importer performs
the member-account role assumptions.

**3. Application settings** (add to your existing NetBox configuration):

```ini
[source/aws]
type = aws
profile = example-inventory
organization = True
organization_role_name = ExampleInventoryRole
account_id = 123456789012
regions = auto
discovery_region = us-east-1
sync_private_ips = False

; Optional: initially select one actual member account.
;organization_account_ids = 111122223333
```

`profile` identifies the management-account AWS profile.
`organization_role_name` identifies the role inside each member account, even
though the profile and role names are related here. Setting `account_id` is an
optional guard for the collector account; it does not select member accounts.
The management account is excluded from EC2 scanning by default.
`sync_private_ips = False` allows the initial VM/disk/interface test without
pre-created VPC VRFs; configure VRFs and permitted subnets before enabling IP
import.

Omit `organization_external_id` unless the member roles' trust policies require
an `sts:ExternalId` value. An external ID is not automatically the management
account ID. If the collector role itself requires an external ID, set
`external_id` in the AWS shared config profile; that is separate from the
application's member-role `organization_external_id`.

Before running the importer, verify the first role assumption with the AWS CLI
under the same OS user and environment:

```sh
aws sts get-caller-identity --profile example-inventory
```

The ARN should resemble
`arn:aws:sts::123456789012:assumed-role/ExampleInventoryRole/<session-name>`,
not the original IAM user's ARN. This checks only the collector identity, not
member-role trust or all inventory permissions. Then run a dry run:

```sh
python netbox-sync.py -n -c settings.ini
```

For this layout, adapt the policies below to use `ExampleInventoryRole` rather than
`NetBoxInventoryReadOnly`. The IAM user must be allowed to assume the management
role, and that role must trust the IAM user. The management role needs
Organizations access and permission to assume the member roles. Each member
role must trust
`arn:aws:iam::123456789012:role/ExampleInventoryRole`
and have the inventory read permissions (for example AWS-managed
`ReadOnlyAccess`). The importer does not create these roles or grants.

#### Collector permissions

Run from the management account or an appropriately delegated administrator
allowed to list organization accounts. Attach this policy to the collector user
or role, adjusting the role path and partition if needed:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "organizations:DescribeOrganization",
        "organizations:ListAccounts"
      ],
      "Resource": "*"
    },
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::*:role/NetBoxInventoryReadOnly"
    }
  ]
}
```

Restrict the role ARN account list to your member accounts where feasible.
AWS-managed `ReadOnlyAccess` covers the EC2 inventory permissions but does not
replace the required cross-account `sts:AssumeRole` grant.

#### Member-account inventory role

Deploy `NetBoxInventoryReadOnly` in every selected member account, for example
with CloudFormation StackSets. Attach the EC2 read policy above or AWS-managed
`ReadOnlyAccess`. Set its trust policy to allow the **exact collector principal**.
For an example IAM user acting directly as the collector, that policy is:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::123456789012:user/example-inventory-client"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

If using the collector-role profile above, replace the principal with
`arn:aws:iam::123456789012:role/NetBoxOrganizationCollector`.
If your administrator requires an external ID, add a
`Condition: {"StringEquals": {"sts:ExternalId": "<external-id>"}}` to the trust
statement and set the same `organization_external_id` in the application.
MFA-protected member-role assumption is not interactive; use an administrator-
approved unattended trust policy. SCPs and permissions boundaries must also
allow the calls. No roles or IAM policies are created by the importer.

Adding an allowlist, excluding management, or removing/inactivating an account
changes the source's authoritative scope and can orphan previously imported
objects under the existing pruning rules. Review a dry run before changing
scope. Avoid overlapping single-account and organization source definitions.

### VPC address-space isolation

The current NetBox integration reads VRFs and prefixes but does not create them.
Before enabling IP import, create a **distinct VRF for every VPC** in NetBox.
The default VRF name is:

```text
AWS <account-id>/<region>/<vpc-id>
```

For example: `AWS 123456789012/us-east-1/vpc-0123456789abcdef0`.
Use `vpc_vrf_relation` to select an existing differently named VRF. A mapping
key may be `account-id/region/vpc-id` in organization mode; this takes precedence
over a legacy `region/vpc-id` mapping. For example:

```ini
vpc_vrf_relation = 111122223333/us-east-1/vpc-0123456789abcdef0 = Production VPC
```

A mapping must not share a VRF between discovered VPCs. VRFs must remain distinct across
accounts and separately configured sources too. Organization mode validates
VRF isolation across all selected accounts before reconciliation.

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
