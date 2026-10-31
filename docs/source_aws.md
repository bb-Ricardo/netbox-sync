# AWS source

The `aws` source imports EC2 instances, attached EBS volumes, attached network
interfaces (ENIs), MAC addresses, and permitted private IPv4 / IPv6 addresses.
Optional discovery adds S3 buckets, Lambda functions, and EKS clusters.
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
needed. Outbound HTTPS to the regional EC2 and STS endpoints is required,
plus the S3, Lambda, and EKS endpoints for enabled optional services.

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

### Additional permissions for optional services

The new services are **opt-in**. Add only the actions for the services you enable
to the single-account inventory identity or every selected member-account role.
For example, the following supplemental policy enables all three:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:ListAllMyBuckets",
        "s3:GetBucketLocation",
        "s3:GetBucketTagging",
        "s3:GetBucketVersioning",
        "s3:GetEncryptionConfiguration",
        "lambda:ListFunctions",
        "lambda:GetFunctionConfiguration",
        "lambda:ListTags",
        "eks:ListClusters",
        "eks:DescribeCluster",
        "eks:ListNodegroups",
        "eks:DescribeNodegroup",
        "eks:ListFargateProfiles",
        "eks:DescribeFargateProfile"
      ],
      "Resource": "*"
    }
  ]
}
```

Where your IAM policy supports resource-level permissions, restrict the
resource-specific actions to the intended buckets, functions, and EKS resources.
Listing actions still need their documented wildcard scope. No S3 object reads,
Lambda code downloads, Kubernetes credentials, or CloudWatch permissions are
required. Authorization denials are logged and skipped as described below;
other discovery failures still abort before NetBox writes and pruning.

### Authorization-denied discovery

Recognized authorization errors (`AccessDenied`, `AccessDeniedException`,
`UnauthorizedOperation`, `AuthorizationError`, `AuthorizationErrorException`,
`Forbidden`, and `ForbiddenException`) produce warnings and allow accessible
discovery to continue. Warnings include account, region, service, AWS operation,
and the bucket name when applicable.

* S3 bucket metadata denial skips that bucket and continues with subsequent
  buckets. Existing bucket metadata and tags are not cleared.
* Denied S3/Lambda/EKS service discovery skips that service in that region.
  Partially collected service results are discarded if listing or detail
  discovery fails outside the per-bucket handling.
* Denied EC2 discovery discards the regional EC2 snapshot, but still attempts
  optional services and other regions. Dependent instances whose shared
  network metadata is unavailable during denied organization discovery are
  deferred rather than imported with guessed masks.
* Denied member-role assumption skips that account and scans remaining
  accounts. Denied identity, region enumeration, or organization enumeration
  skips the affected scan without treating its inventory as empty.

**Any authorization denial conservatively retains all objects previously
managed by this AWS source for that run**, including children and shared-network
dependencies. Accessible objects still update. This also postpones orphaning
and pruning for genuinely deleted objects in otherwise accessible scopes of
this source; a later fully authorized run resumes normal lifecycle processing.
Objects from other sources are unaffected.

Existing EKS worker cluster placement is preserved when regional EKS discovery
is denied. Authentication/credential expiration, throttling exhaustion, service
errors, account mismatches, and invalid or conflicting data remain fatal.
Successful empty results are authoritative only when discovery has no denials.

Authorization failures also add operation-specific tags to matching managed
NetBox objects, for example `Sync:AccessDenied:GetBucketTagging`. Bucket-specific
failures tag the bucket's virtual disk. Failures without an individual resource
identifier tag existing managed objects in the affected account/region/service
scope; account/role or organization failures cover their broader scopes. Objects
not managed by this AWS source are not tagged. Resources that have never been
imported are not created as placeholders, so those failures remain log-only.

Permission tags clear automatically after the same operation succeeds for the
relevant resource or scope. A disabled service, unattempted operation, or success
for another resource does not clear a tag. A denial takes precedence over success
in the same applicable scope during a run. Other user tags are preserved.

### NetBox resource conflicts

Logical service resources with duplicate NetBox resource identities, ownership
by another source, or a NetBox name collision are logged and skipped, without
aborting unrelated resources, regions, or accounts. Conflicting objects are not
adopted or overwritten. A skipped cluster also skips its dependent resources,
including EKS workers; a skipped S3 inventory VM skips its buckets.

If a resource conflict occurs, all previously managed objects for this source
are retained for the run, protecting skipped resources and their children from
orphaning/pruning. Discovery and snapshot-validation errors retain their existing
failure handling.

### Troubleshooting incomplete network discovery

With private IP synchronization enabled, each attached ENI must have a discovered
VPC and subnet, and the subnet's VPC must match the ENI's VPC. If validation fails,
the error includes the account, region, instance, ENI, VPC, and subnet IDs. It
identifies resources missing from `DescribeVpcs` or `DescribeSubnets`, or reports
the conflicting VPC ID for a subnet mismatch. Multiple failures for the same ENI
are reported together.

Check those resource IDs in the reported account and region using the same role
or profile as discovery, including any cross-account network sharing setup.
In organization mode, VPCs and subnets discovered in another selected account
can satisfy these checks as described under shared VPCs below. Missing resources
must still be discovered somewhere in the selected organization scope.
Missing or mismatched network data normally aborts without NetBox writes or
pruning. When denied organization discovery leaves shared-network dependencies
unavailable, affected instance updates are instead deferred with a warning and
existing objects retained. Address-space checks are not bypassed for imports.
Earlier "Created new VRF object" log messages refer to in-memory inventory
objects created during validation, not completed NetBox API writes.

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
account_name = Production
```

All options also support the application's usual `NBS_SOURCE_<index>_*`
environment variables. `regions` and `tag_keys` accept YAML lists as well as
comma-separated strings. Quote the account ID in YAML so it remains a string.
`python netbox-sync.py -g` generates the full configuration.

### Optional S3, Lambda, and EKS discovery

These settings default to `False`, preserving existing EC2-only discovery and
permissions. Add them to the same AWS source for single-account or organization
mode:

```ini
sync_s3 = True
sync_lambda = True
sync_eks = True
```

All three follow the source's selected account and region scope. Pagination is
completed before reconciliation. Disabling an optional service retains its
previously managed logical VMs, disks, and clusters instead of orphaning them.
While a service is enabled, successfully discovered empty results are
authoritative when discovery has no denials: removed functions, buckets, and empty S3 holders follow the
normal orphaning/pruning policy. Clusters remain subject to the existing
non-pruning cluster policy.

#### S3 buckets

Requires **NetBox 3.7+**; older versions abort before discovery when `sync_s3`
is enabled. Each account/region with buckets gets:

* Cluster `S3 <account-id>/<region>`, type `AWS S3`.
* Logical VM `S3 inventory <account-id>/<region>`, tagged `AWS:logical-s3`.
* One virtual disk per general-purpose bucket, named by its bucket name.

The holder VM is explicitly an inventory container, not a server. It has no
invented CPU, memory, MAC address, or IP. Bucket metadata includes ARN, region,
versioning, and encryption configuration in the disk's `aws_metadata` JSON
custom field. Configured `tag_keys` are copied to the bucket disk.

**Disk size zero is a required-field placeholder, not a measurement.**
`aws_metadata.usage_status` is `unknown` and `usage_bytes` is JSON null; the
description also states that usage is unknown. Empty and unmeasured buckets
are therefore not treated as equivalent usage measurements. No provisioned
capacity or CloudWatch usage is inferred, and S3 disk totals must not be used
as storage-capacity reporting. Bucket contents, directory buckets, access
points, and S3-compatible non-AWS services are not discovered.

#### Lambda functions

Each account/region gets a logical `AWS Lambda` cluster and one logical VM per
function, tagged `AWS:logical-lambda`. Matching uses the unqualified function ARN,
not the display name. Long display names use a deterministic hash suffix to fit
the importer's name limit without losing resource identity.

Configured function memory is VM memory. vCPUs and interfaces are not invented,
and shared Lambda VPC ENIs are not assigned to a function VM. `aws_metadata`
records runtime, handler, architecture, timeout, modification time, package
type, VPC configuration, and AWS state when returned. Environment variables,
code, credentials, invocation instances, aliases, and published versions are
not imported.

State maps as follows: `Active` to `active`, `Pending` to `planned`, `Inactive`
to `offline`, and `Failed` to `failed`. These describe AWS function state, not
whether an invocation is currently executing.

#### EKS clusters and workers

Each real EKS cluster becomes a NetBox cluster of type `AWS EKS`. Its ARN is
the stable identity. `aws_metadata` records Kubernetes version, AWS status,
endpoint/VPC configuration, managed node groups (including scaling and Auto
Scaling group information), and Fargate profile metadata.

Existing discovered EC2 worker VMs move into that cluster; duplicate worker VMs
and fabricated control-plane VMs are not created. Worker membership is matched
using managed node-group Auto Scaling group names or the EC2 tags
`aws:eks:cluster-name`, `eks:cluster-name`, and
`eksctl.cluster.k8s.io/v1alpha1/cluster-name`. Conflicting membership aborts the
run. A reference to a cluster no longer discovered emits a warning and leaves
the instance in its regular EC2 cluster. Untagged self-managed workers without
a discovered node-group relationship remain ordinary EC2 VMs.

Disabling EKS discovery returns discovered EC2 workers to their normal EC2
clusters. Kubernetes API access, pods, workloads, and individual Fargate runtime
instances are outside this discovery scope.

Optional resources use `aws_service`, `aws_resource_id`, and `aws_metadata`
custom fields on VMs, clusters, and virtual disks. Existing unrelated tags are
preserved. These objects are logical documentation of AWS resources, not
claims that serverless services are virtual servers.

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
Authorization denial during region discovery is logged and preserves existing
objects. Other failures, or an empty enabled-region result, abort synchronization
before NetBox writes or pruning. There is no silent fallback to a partial region list. Use explicit mode when
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

All accessible selected accounts are collected and validated before reconciliation.
Authorization denials are logged and inaccessible scopes skipped with source-wide
retention. Other Organizations API errors, role-assumption failures, account
mismatches, or regional discovery failures abort before NetBox writes or pruning.
No inaccessible account is treated as empty. A selection with no eligible accounts
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

Each VPC uses a **distinct VRF**. When IP import encounters a VPC without an
explicit `vpc_vrf_relation`, the importer creates a VRF in NetBox using this
default name:

```text
AWS <account-id>/<region>/<vpc-id>
```

For example: `AWS 123456789012/us-east-1/vpc-0123456789abcdef0`.
Use `vpc_vrf_relation` to select an existing differently named VRF; mapped
VRFs must already exist, and a missing mapped VRF aborts the run rather than
creating one. A mapping key may be `account-id/region/vpc-id` in organization
mode; this takes precedence over a legacy `region/vpc-id` mapping. For example:

```ini
vpc_vrf_relation = 111122223333/us-east-1/vpc-0123456789abcdef0 = Production VPC
```

A mapping must not share a VRF between distinct discovered VPCs. VRFs must remain
distinct for different VPC owners and separately configured sources too.
Organization mode validates VRF isolation across all selected accounts before
reconciliation.

#### Shared VPCs in organization mode

With private IP synchronization enabled, organization mode resolves attached
ENIs' network resources across all selected account snapshots. A participant
account may omit the VPC or subnet from its EC2 responses if another selected
account discovers them in the **same region**. Resolution uses exact resource
IDs and AWS `OwnerId`, never IP overlap or guessed subnet masks. In the absence
of `OwnerId`, a resource remains associated with the account that discovered it;
an ambiguous cross-account match aborts the run.

All participants of the same shared VPC use **one owner-account VRF**:
`AWS <vpc-owner-account-id>/<region>/<vpc-id>`. Account-specific
`vpc_vrf_relation` mappings must use the **VPC owner's account ID**, not the
participant's. VM account custom fields and cluster membership still use the
instance account. The owner account can supply network metadata even when it
has no EC2 instances.

Include the network owner account in the same organization source when its
resources are not visible to participants. Resolution cannot borrow metadata
from another source or region. Missing resources, conflicting ownership or
CIDRs, and subnet/VPC mismatches still abort before reconciliation. If
authorization-denied discovery leaves network dependencies unavailable, affected
instance updates are deferred and existing objects retained instead.

This changes IP placement for shared VPCs previously imported into separate
participant-account VRFs. Review a dry run and existing mappings before enabling
pruning; previously imported IPs may remain in the old VRFs until reconciled
under the normal lifecycle rules.

There is no fallback to global address space. Subnet mask lengths come from AWS
subnet CIDRs; NetBox prefixes are neither required nor created. Identical IPs in
different VPCs remain separate NetBox IPs.

`permitted_subnets` is required to populate IP addresses; leaving it undefined
imports no IPs, matching other source behavior. `sync_private_ips = False`
imports VMs, disks, and interfaces without creating or requiring VRFs;
previously managed IPs on discovered interfaces are retained. DNS lookup
defaults to false because private AWS DNS is often inaccessible from the sync
host.

## Object mapping and lifecycle

| AWS | NetBox |
| --- | --- |
| Account name | Cluster group `AWS <account-name>` |
| Account name and region | Cluster `AWS <account-name>/<region>`, type `AWS EC2` |
| Region | Logical site `AWS <region>` |
| EC2 Name tag | VM name and `aws_name` custom field; falls back to instance ID when missing |
| Instance ID | `aws_instance_id` custom field; serial on NetBox 4.1+ |
| Instance type | CPU and memory, plus custom field |
| AWS account ID | `aws_account_id` custom field |
| Availability Zone | Custom field, not a physical site |
| ENI ID | VM interface name and custom field |
| ENI MAC | MAC object on NetBox 4.2+; legacy interface field on older versions |
| Private IPv4 / IPv6 | IP in the VPC's existing VRF, assigned to the ENI |
| EBS volume | Virtual disk named by volume ID |
| S3 bucket (opt-in) | Metadata-only virtual disk on an account/region logical VM |
| Lambda function (opt-in) | Logical VM in an `AWS Lambda` cluster |
| EKS cluster (opt-in) | `AWS EKS` cluster with node-group and Fargate profile metadata |
| EKS EC2 worker (opt-in) | Existing EC2 VM associated with its EKS cluster |

In single-account mode, set `account_name` to use a friendly label in the
cluster group and cluster names; it defaults to the account ID. Organization
mode uses the account name returned by AWS Organizations, falling back to the
account ID when unavailable. The account ID remains available on each VM as
`aws_account_id`. Region sites and clusters are logical containers, not claims
about physical AWS facilities or hypervisor placement.

VM names come from the EC2 `Name` tag, falling back to the instance ID if the
tag is missing or empty. If multiple instances in the same account and region
have the same Name tag, the instance ID is appended to each VM name to keep the
names distinct. Reconciliation uses the account ID and instance ID custom
fields, so a changed Name tag updates the existing VM rather than creating a
second one. Memory uses the instance type's MiB capacity; CPU uses instance CPU
options when reported, otherwise the type's default vCPU count. This is
provisioned capacity, not utilization.

Running instances are `active`; stopped, pending, stopping, and shutting-down
instances are `offline`. Terminated instances are excluded and subsequently
follow the existing NetBox orphaning/pruning configuration. An empty, successfully
queried account/region is authoritative when discovery has no denials. All accessible
configured regions and paginated responses are collected before reconciliation.
Authorization denials log warnings and retain existing objects. Other AWS failures, credential
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

An IP already assigned to another interface can be reassigned when its current
VM/device belongs to this source, or carries both `NetBox-synced` and
`NetBox-synced: Orphaned` tags. Reclamation from an orphaned owner logs a warning;
the orphan lifecycle removes source tags, so the former source may be different.
An active EC2 VM can also reclaim an IP from an `offline` owner managed by
netbox-sync (the current source's ownership or a `NetBox-synced` tag), even if
another source originally managed it. This logs a warning and clears the old
owner's primary-IP reference when applicable. An offline EC2 VM cannot take an
IP from an active managed owner, regardless of discovery order. Other non-active
statuses do not qualify as offline, and manually managed offline owners are not
eligible for this exception.
Other ownership conflicts, including unresolved owners, log an error and skip
that IP without aborting the sync. Its existing assignment is left untouched,
and it is not set as the discovered VM's primary IP. Normal write and pruning
phases still proceed.

Only explicitly selected `tag_keys` become tags (`AWS:<key>=<value>`). Their
combined names must fit NetBox's 100-character limit. Existing unrelated NetBox
tags are preserved. Metadata custom fields use the `aws_` prefix. Their names
should not conflict with user-defined fields of incompatible types.

Tag reconciliation uses the same inventory lookup as tag assignment. When that
lookup reuses an existing NetBox tag with different capitalization, its existing
name is preserved and the assigned tag is not removed as stale. This applies to
EC2 VMs, S3 bucket disks, Lambda VMs, and EKS clusters. Tags for configured keys
are still removed when they are no longer reported by AWS.

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
python -m pytest tests/test_aws_source.py tests/test_aws_services.py
```

Live AWS/NetBox validation is still required before production use. This source
does not inventory RDS, load balancers, ECS/Kubernetes workloads, security groups,
software, or guest operating-system configuration.
