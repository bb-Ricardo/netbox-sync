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
sync_volumes = True
sync_databases = True
```

`api_token` is required. `sync_volumes` and `sync_databases` default to `True`.
Set either to `False` when the token should not query that API resource.
Generate the complete configuration with:

```sh
python netbox-sync.py -g
```

## NetBox mapping

| DigitalOcean | NetBox |
| --- | --- |
| Region | Virtualization cluster; an existing NetBox region is used as its scope on NetBox 4.2+ |
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
named after their network type. The source does not create NetBox prefixes,
sites, or regions. Existing matching NetBox regions are used for cluster scope
where supported; otherwise clusters are left unscoped.

## Validation

Start with a dry run:

```sh
python netbox-sync.py -n -c settings.ini
```

Confirm the discovered Droplet, volume, and database cluster counts and inspect
the resulting NetBox object changes before enabling writes or pruning.
