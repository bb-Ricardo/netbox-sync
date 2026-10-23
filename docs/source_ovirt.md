# Source: Ovirt

## Installation

The SDK is optional: installations using other sources do not need it. Selecting
an oVirt source without the SDK logs an error and skips that source.

On Debian/Ubuntu, install the SDK's build dependencies before installing Python
packages:

```sh
sudo apt-get install gcc python3-dev libxml2-dev libcurl4-openssl-dev
pip install -r requirements-ovirt.txt
```

With uv, use `uv sync --extra ovirt` after installing the native libraries.
The SDK contains a native extension; a compiler and Python development headers
matching your Python version are needed when no wheel is available. The runtime
needs libxml2 and libcurl.

For Docker, enable the optional source in both build stages:

```sh
docker build --build-arg INSTALL_OVIRT=true -t netbox-sync:ovirt .
```

## Configuration

```ini
[source/production-ovirt]
type = ovirt
url = https://engine.example.com/ovirt-engine/api
username = inventory@internal
password = replace-me
validate_tls_certs = True
ca_file = /etc/pki/ovirt-engine/ca.pem
permitted_subnets = 10.0.0.0/8, fd00::/8
cluster_site_relation = DC/Production = Main Site
```

Use an account with read access to datacenters, clusters, hosts, VMs, disks,
interfaces, and reported devices. The existing source options remain supported,
including name filters, site/tenant/role/tag relations, domain stripping,
`set_vm_name_to_uuid`, `skip_vm_comments`, `skip_vm_platform`, and
`host_custom_object_attributes` / `vm_custom_object_attributes` (dot-separated
SDK attribute paths). Generate a full example with `python netbox-sync.py -g`.

Guest IP discovery requires the guest agent to report network devices. VMs
without agent data still receive their configured interfaces and disks. As with
other sources, bare guest IPs need a matching prefix in NetBox to determine their
prefix length. Only addresses allowed by `permitted_subnets` are imported.
`ip_tenant_inheritance_order`, `preserve_primary_ips`, `skip_fhrp_group_ips`, and
interface exclusion filters use the current shared network-sync behavior.

NetBox 4.2+ cluster sites use scoped relations and interfaces use MAC address
objects. NetBox 3.7+ disks are separate virtual disk objects, with the size units
appropriate to the NetBox version. Older versions retain aggregate VM disk size.
Missing CPU or disk size data is not replaced with fabricated values.

Begin with a dry run (`python netbox-sync.py -n`) and inspect the log before
allowing writes. Unit tests use a fake SDK and the real in-memory NetBox
inventory; they do not replace validation against your live oVirt Engine.
