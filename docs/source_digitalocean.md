# DigitalOcean source

The `digitalocean` source reads Droplets, attached block volumes, and managed
database clusters from the DigitalOcean API and reconciles them into NetBox.
API access is read-only; inventory changes are written to NetBox by the normal
netbox-sync process.

## Installation and access

The source uses the `requests` dependency already installed by netbox-sync; no
DigitalOcean SDK is required. Create a DigitalOcean API token with read access
to regions, Droplets, and volumes. If managed database ingestion is enabled, the token
must also be able to read databases.

Keep the API token out of version control and provide it through a protected
configuration file or the application's standard source environment variables.

## Configuration

Add a source section to the existing settings file:

```ini
[source/example-digitalocean]
type = digitalocean
api_token = REPLACE_WITH_READ_ONLY_API_TOKEN
region_mapping = fra1=Germany, nyc1=United States
sync_volumes = True
sync_databases = True
; Optional isolation for overlapping private addresses across teams:
;private_ip_vrf = DigitalOcean Team-A
```

`api_token` is required. `sync_volumes` and `sync_databases` default to `True`.
Set either to `False` when the token should not query that API resource.
Accounts without managed databases can leave `sync_databases` enabled:
both `{"databases": []}` and `{"databases": null}` are treated as empty
database collections. Missing collection fields and other malformed values
remain errors rather than being treated as empty inventory.
Generate the complete configuration with:

```sh
python netbox-sync.py -g
```

`region_mapping` is optional. It accepts comma-separated, exact
`DigitalOcean region slug=NetBox Region name` pairs. Quote destination names
containing commas, for example `nyc1="New York, USA"`. Duplicate source slugs,
empty entries, and Region names longer than 100 characters are rejected.

On NetBox 4.2+, the source reuses a Region with the destination name, then
tries its generated slug, and creates it if neither exists. Explicit mappings
take precedence over Regions matching the DigitalOcean region. Without a
mapping, the DigitalOcean region name and slug are used (for example
`Frankfurt 1` and `fra1`). Region slugs (including those returned for databases)
are resolved through `GET /v2/regions` once per run, so cluster names do not
depend on whether Droplets exist in the region. If a slug is absent from that
response, it is used as the name. Existing Regions are not renamed.
Regions previously created by this source retain their source ownership tags;
reused, unmanaged Regions are not claimed by the source.

Multiple DigitalOcean regions can map to the same NetBox Region; their
virtualization clusters remain separate and retain their DigitalOcean names.
Existing clusters in the source's cluster group are updated in place when
their scope changes, including previously unscoped clusters. Ambiguous
duplicate clusters in that group cause an explicit error instead of choosing
one arbitrarily.

The NetBox token needs permission to add Regions when they do not already
exist. On NetBox versions before 4.2, Region creation and mapping are not
applied; clusters remain unscoped.

## NetBox mapping

### Private address isolation

`private_ip_vrf` optionally names a NetBox VRF for this source's private
network addresses (IPv4 and IPv6). The source reuses a VRF with that exact
name or creates it if missing; names must be non-empty and at most 100
characters. The NetBox token needs permission to create VRFs if missing.
An ambiguous VRF name stops the run rather than selecting one arbitrarily.
Referenced VRFs inherit source ownership through the normal inventory logic
and are not pruned.

Use distinct names for isolated teams, for example:

```ini
[source/team-a]
type = digitalocean
api_token = REPLACE_WITH_TEAM_A_READ_ONLY_TOKEN
private_ip_vrf = DigitalOcean Team-A

[source/team-b]
type = digitalocean
api_token = REPLACE_WITH_TEAM_B_READ_ONLY_TOKEN
private_ip_vrf = DigitalOcean Team-B
```

Both teams can then have `10.108.0.6` in the same region without sharing an
IP record. IP matching and per-run conflict checks use the address and VRF;
prefix-length changes update the same record. Public network addresses remain
in the global table, regardless of their numerical address range.
When unset, private addresses also remain global, preserving existing behavior.
Each run logs the source name and whether private addresses use the global
table or the configured VRF, making it possible to verify the active settings.

Enabling or changing this option does **not** move existing IPs between VRFs.
It creates or reuses records in the configured VRF and selects discovered
primary IPs from the appropriate table, preferring public addresses.
Existing global records and assignments remain unchanged; review and clean up
obsolete records manually after confirming ownership in a dry run.
Previously source-tagged global IPs may become orphaned and eligible for
normal pruning once their replacements are in a VRF. Disable pruning during
the transition and review the dry-run changes before enabling writes.
Turning the option off likewise does not move VRF records back to global.
This is per-source isolation, not per-VPC isolation: if a team has multiple
VPCs that reuse addresses, per-VPC isolation would be needed instead of this
setting.

| DigitalOcean | NetBox |
| --- | --- |
| Region | Virtualization cluster scoped to a reused or automatically created NetBox Region on NetBox 4.2+ |
| Droplet | Virtual machine |
| Droplet size | VM vCPUs and memory |
| Droplet image distribution/name | VM platform |
| Droplet public/private IPv4 and IPv6 | VM interfaces and IP addresses |
| Droplet root disk | VM disk capacity on older NetBox; virtual disk on NetBox 3.7+ |
| Attached block volume | Additional virtual disk on NetBox 3.7+ |
| DigitalOcean resource tags | NetBox tags |
| Managed database cluster | VM inventory record with `db-` name prefix and metadata custom fields |

DigitalOcean reports Droplet and volume disk sizes in gigabytes. NetBox 4.1+
receives megabytes using 1024 MB per reported GB; earlier NetBox versions
receive gigabytes. Database storage, reported in MiB, is converted to whole
gigabytes on earlier NetBox versions. VM serial numbers are only sent on
NetBox 4.1+. Database
clusters are represented as one VM-like record per cluster because this
project has no native managed-database object type. Database engine, version,
size, node count, endpoint host, and port are stored in `digitalocean_*` custom
fields. Connection credentials and connection URIs are never copied. Database
endpoint hostnames are not resolved into IP addresses.

DigitalOcean private and public network addresses are assigned to interfaces
named after their network type. The source does not create NetBox prefixes or
sites.

VMs are matched by `digitalocean_resource_id` before falling back to name and
cluster for records without a resource ID. Duplicate VM names within a region
(case-insensitive, including database `db-` prefixes) have ` (<resource id>)`
appended, with the original name shortened as needed to fit NetBox's limit.
Resource IDs preserve VM identity across renames and region changes.
If multiple VMs carry the same resource ID, the source uses the sole
non-orphaned match when all other matches have the `NetBox-synced: Orphaned`
tag. It logs the ignored orphaned records and leaves them unchanged for normal
orphan pruning. A single orphaned match can still be rediscovered.
If multiple non-orphaned matches, or only multiple orphaned matches, remain,
the source exits with the matching NetBox IDs, names, and clusters rather than
choosing arbitrarily.
Droplets with the same name in different clusters remain distinct. Interfaces
and virtual disks are matched to their resolved parent VM, not just its name.
If an earlier run moved child objects between same-named VMs, inspect the next
dry run for interface, disk, and primary-IP changes before enabling writes.

IP assignments can be repaired automatically when the existing IP belongs to
the current source, either through this run's ownership or its persisted
`Source: <source name>` tag. Conflicting or uncertain ownership is logged and
the assignment is skipped without stopping the run or changing the existing
assignment. Other addresses, droplets, disks, and databases continue syncing.
Skipped IPs are not selected as the discovered VM's primary IP. An existing IP
tagged for this source is retained for the run, preventing the skipped assignment
from causing orphaning/pruning; ownership tags are preserved.
Two interfaces claiming the same IP in one source run also produce an error log:
the first assignment is kept and subsequent conflicting assignments are skipped.
Source section names must remain stable for
persisted ownership tags to match. If an earlier run already removed an IP's
source tag or marked it orphaned, verify its owner and restore the correct tag
or repair its assignment manually before retrying.
Ownership-conflict errors include the existing NetBox IP ID, VRF, assigned
interface, IP source tags, current-run source, and assigned VM tags to help
identify stale assignments. VM tags alone do not authorize moving an IP.

## Validation

Start with a dry run:

```sh
python netbox-sync.py -n -c settings.ini
```

Confirm the discovered Droplet, volume, and database cluster counts and inspect
the resulting NetBox object changes before enabling writes or pruning.
API failures stop discovery before resource inventory is modified, close the
API session, and report the HTTP status and DigitalOcean error message when
available.
