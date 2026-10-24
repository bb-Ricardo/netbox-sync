# OpenStack source

The `openstack` source reads availability zones, compute hypervisors, servers,
flavors, and Cinder volumes, then reconciles their inventory into NetBox.
OpenStack is queried read-only; inventory writes are made to NetBox.
All identifiers and credentials below are fictitious placeholders.

## Installation

The SDK is optional; other sources do not require it:

```sh
pip install -r requirements-openstack.txt
# Alternatively:
uv sync --extra openstack
```

The supported SDK range is `openstacksdk>=4.0,<5`. Selecting an OpenStack source
without the SDK logs an error and skips that source.

On the consolidated branch, Docker builds can enable the SDK with:

```sh
docker build --build-arg INSTALL_OPENSTACK=true -t netbox-sync:openstack .
```

## Authentication and permissions

The current source uses Keystone Identity v3 username/password authentication
with an explicit project, region, user domain, and project domain. It passes
these source settings directly to `openstacksdk`; it does not expose
`clouds.yaml` cloud selection or application-credential authentication.

The account must be allowed to read:

* Detailed Nova availability zones, including their host information.
* Detailed Nova hypervisors and servers.
* Nova flavors when server responses do not include their full specifications.
* Detailed Cinder volumes, including sizes for attached volumes.

`all_projects = True` is the default for server and volume discovery and
requires cross-project access, normally administrative or suitably delegated
read permissions. Exact roles depend on the cloud's Nova and Cinder policy.
`all_projects = False` requests servers and volumes within the authenticated
project, but does not remove the availability-zone or hypervisor queries;
project-only credentials may still lack the permissions needed by this source.

Use TLS certificate validation (the default). Configure the runtime's trusted
CA certificates for private endpoints rather than disabling verification.
The sync host needs outbound access to Keystone and the Nova/Cinder endpoints
advertised in the service catalog. No guest SSH access or guest agent is required
for addresses returned by Nova.

## Configuration

Add this section to the application's existing NetBox settings:

```ini
[source/example-openstack]
type = openstack
auth_url = https://identity.example.org:5000/v3
project = inventory
username = inventory-reader
password = REPLACE_WITH_PASSWORD
region = RegionOne
user_domain = Default
project_domain = Default
validate_tls_certs = True
all_projects = True
group_name = Example OpenStack
cluster_site_relation = .* = Example Cloud Site
permitted_subnets = 10.0.0.0/8, fd00::/8
dns_name_lookup = False
```

All seven authentication/scope fields (`auth_url`, `project`, `username`,
`password`, `region`, `user_domain`, and `project_domain`) are mandatory.
`group_name` defaults to `Openstack`. Use separate named sources for other
regions or clouds; there is no automatic region enumeration.

Keep real passwords out of version control and restrict access to configuration
files. The application also supports its standard source environment settings;
for example, a source defined entirely through environment variables can use:

```sh
NBS_SOURCE_1_NAME=example-openstack
NBS_SOURCE_1_TYPE=openstack
NBS_SOURCE_1_AUTH_URL=https://identity.example.org:5000/v3
NBS_SOURCE_1_PROJECT=inventory
NBS_SOURCE_1_USERNAME=inventory-reader
NBS_SOURCE_1_PASSWORD=REPLACE_WITH_PASSWORD
NBS_SOURCE_1_REGION=RegionOne
NBS_SOURCE_1_USER_DOMAIN=Default
NBS_SOURCE_1_PROJECT_DOMAIN=Default
```

These are example variable assignments; supply secrets through your runtime's
secret mechanism rather than storing this block in a repository.
Generate the complete supported configuration with:

```sh
python netbox-sync.py -g
```

### Site, network, and tenant relations

`cluster_site_relation` maps availability-zone names to NetBox sites using
regular expressions. `host_site_relation` can override sites for hypervisors.
Correct site mapping matters for prefix, VLAN, and VRF selection.

Only addresses permitted by `permitted_subnets` are imported. Pre-create the
NetBox prefixes, VRFs, and VLANs appropriate to your cloud's networks. Address
prefix matching prefers the mapped site and can fall back to other matching
prefixes; without a matching prefix, a bare IPv4/IPv6 address retains its
host-length mask. This source does not implement AWS-style per-VPC isolation:
carefully inspect overlapping tenant address spaces before permitting IP writes.

Interfaces and addresses come from Nova server address data, not a separate
Neutron topology inventory. Fixed and floating addresses may both be imported
when permitted; fixed addresses are preferred for primary IP selection.
`set_primary_ip`, `preserve_primary_ips`, `skip_fhrp_group_ips`,
`ip_tenant_inheritance_order`, interface exclusion filters, and
`disable_vlan_sync` control the shared network reconciliation behavior.

Tenants and tags are configured through relations rather than automatically
mirroring Keystone project names:

```ini
cluster_site_relation = ^zone-a$ = Example Cloud Site
vm_tenant_relation = ^production-.* = Example Tenant
vm_tag_relation = .* = OpenStack
```

Host, VM, and cluster site/tenant/role/tag options and platform mappings are
described in the generated configuration and [example settings](../settings-example.ini).

## Object mapping

| OpenStack | NetBox |
| --- | --- |
| Configured group name | Cluster group |
| Availability zone | Cluster |
| Hypervisor | Device and reported host interface/IP |
| Server | Virtual machine |
| Flavor vCPUs / RAM | VM CPU / memory |
| Flavor name | VM platform unless disabled or mapped |
| Image-backed root / ephemeral disks | VM disk capacity or virtual disks |
| Attached Cinder volume | VM disk capacity or virtual disk named by volume ID |
| Server network/MAC/address data | VM interfaces, MACs, and permitted IP addresses |

Servers with status `ACTIVE` are active in NetBox; other states map to offline.
By default VM names are used, with duplicate names within an availability zone
logged and skipped. Consider `set_vm_name_to_uuid = True` for cross-project
inventories where names repeat; the original name is then stored in the
`openstack_vm_name` custom field. Changing naming settings can affect matching,
so review a dry run before applying them to existing inventory.

`skip_vm_platform` disables flavor-to-platform mapping. `skip_vm_comments`
disables VM UUID comments. `strip_host_domain_name` and
`strip_vm_domain_name` control domain stripping.
`host_custom_object_attributes` and `vm_custom_object_attributes` copy selected
SDK resource attributes into custom fields; dotted paths are supported.

NetBox 3.7+ uses separate virtual disks; older versions retain aggregate VM
disk capacity. Disk units follow the existing convention: GiB before NetBox 4.1
and MiB from 4.1 onward. Missing attached-volume sizes are logged rather than
replaced with fabricated zero sizes. NetBox 4.2+ uses scoped cluster sites and
separate MAC address objects.

## Filters and lifecycle

Cluster, host, and VM include/exclude filters are regular expressions matched
from the start of the name. Include checks run before exclude checks.
Excluding an availability zone excludes its hosts and VMs; excluding a host
does not by itself exclude its VMs.

Objects participate in the application's normal source tagging, orphaning, and
pruning lifecycle. Changing project scope, filters, naming, or permitted networks
can change which objects are considered discovered. Avoid overlapping sources
and inspect the dry-run log before allowing writes or pruning. Do not assume
this source has the AWS implementation's collect-all-accounts failure contract.

## Validation

Start with a dry run using credentials with the intended scope:

```sh
python netbox-sync.py -n -c settings.ini
```

Check authentication, availability-zone coverage, hypervisor access, server and
volume counts, site mappings, duplicate names, and address/VRF assignments.
Unit tests use a fake SDK and the real in-memory NetBox models:

```sh
python -m pytest tests/test_openstack_source.py
```

These tests do not replace validation against your cloud's API versions and
authorization policies.
