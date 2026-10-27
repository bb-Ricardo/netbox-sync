# DigitalOcean source

The `digitalocean` source reads Droplets, attached block volumes, and managed
database clusters from the DigitalOcean API and reconciles them into NetBox.
API access is read-only; inventory changes are written to NetBox by the normal
netbox-sync process.

## Installation and access

The source uses the `requests` dependency already installed by netbox-sync; no
DigitalOcean SDK is required. Create a DigitalOcean API token with read access
to Droplets and volumes. If managed database ingestion is enabled, the token
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
`Frankfurt 1` and `fra1`). When only a region slug is provided by DigitalOcean,
it is also used as the name. Existing Regions are not renamed.

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

DigitalOcean reports Droplet and volume disk sizes in gigabytes; these are
converted to megabytes for NetBox using 1024 MB per reported GB. Database
clusters are represented as one VM-like record per cluster because this
project has no native managed-database object type. Database engine, version,
size, node count, endpoint host, and port are stored in `digitalocean_*` custom
fields. Connection credentials and connection URIs are never copied. Database
endpoint hostnames are not resolved into IP addresses.

DigitalOcean private and public network addresses are assigned to interfaces
named after their network type. The source does not create NetBox prefixes or
sites.

Droplets with the same name in different clusters remain distinct. Interfaces
and virtual disks are matched to their resolved parent VM, not just its name.
If an earlier run moved child objects between same-named VMs, inspect the next
dry run for interface, disk, and primary-IP changes before enabling writes.

IP assignments can be repaired automatically when the existing IP belongs to
the current source, either through this run's ownership or its persisted
`Source: <source name>` tag. Conflicting source ownership or uncertain ownership
stops the run before the normal NetBox write/prune phase instead of silently
skipping a discovered IP. Two interfaces claiming the same IP in one source run
also cause an explicit error. Source section names must remain stable for
persisted ownership tags to match. If an earlier run already removed an IP's
source tag or marked it orphaned, verify its owner and restore the correct tag
or repair its assignment manually before retrying.

## Validation

Start with a dry run:

```sh
python netbox-sync.py -n -c settings.ini
```

Confirm the discovered Droplet, volume, and database cluster counts and inspect
the resulting NetBox object changes before enabling writes or pruning.
