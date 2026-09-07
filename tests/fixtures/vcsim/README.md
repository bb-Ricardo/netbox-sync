# vcsim inventory fixtures

Each `*.tar.gz` here is a vCenter inventory captured with `govc object.save` and
replayed by [vcsim](https://github.com/vmware/govmomi/tree/main/vcsim), the
simulator from the govmomi project. The integration tests start one vcsim per
archive and run the real VMware source handler against it, so they exercise the
actual pyVmomi code path without a live vCenter.

| archive | vCenter | hosts | VMs | notes |
| --- | --- | --- | --- | --- |
| `vchvr.tar.gz` | 6.7.0 | 2 | 28 | most VMs report running guest tools, so IP handling is covered |
| `vc001.tar.gz` | 8.0.3 | 3 | 34 | current API version, a cluster with vCLS VMs |

Both were contributed in [#474](https://github.com/bb-Ricardo/netbox-sync/issues/474).

## Adding a capture

Point `govc` at a vCenter and save its inventory:

```bash
export GOVC_URL="https://vcenter/sdk"
export GOVC_USERNAME="administrator@vsphere.local"
export GOVC_PASSWORD="..."
export GOVC_INSECURE=true

govc object.save -d my-vcenter
tar -czf my-vcenter.tar.gz my-vcenter
```

Drop the archive in this directory and the vcsim-backed tests pick it up: they
are parametrized over every archive found here, so a new capture is covered
without touching the test code.

The dumps contain host names, VM names, MAC addresses and guest IP addresses of
the source environment. Only capture inventories you are allowed to publish.
