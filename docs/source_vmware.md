# Source: vmware

## Setup
You need to have a source section in your `settings.ini` file with following type:
```ini
type = vmware
```
All options for this source are described in the [settings-example.ini](../settings-example.ini) file.

If you have multiple vCenter instances just add another source with the same type in the **same** file.

***IMPORTANT:*** For VMware source you should define the var `cluster_site_relation` which maps a vCenter cluster to an
exiting Site in NetBox. If undefined a placeholder site will be created.

### vCenter user
You need a user account with "Read-only" role on vCenter root scope.
The "Propagate to children" setting must also be checked.

## Adding objects from a vCenter to NetBox

To match new/updated objects to existing objects in NetBox a few different steps are taken to find the correct object.

You might be interested in this [description](common_concepts.md). This describes how discovered
IP addresses and interfaces will be added to NetBox.

### Add/update device/VM object in inventory based on gathered data

Try to find object first based on the object data, interface MAC addresses and primary IPs.
1. try to find by name and cluster/site
2. try to find by mac addresses interfaces
3. try to find by serial number (1st) or asset tag (2nd)
4. try to find by primary IP

### Finding hosts and VMs from discovered data

#### 1. try to find exact name match including cluster/site

If this NetBox object (device, vm) matches, the found object will be used.

#### 2. Try to find a NetBox object based on list of MAC addresses

Iterate over all NetBox interfaces of an object (device, vm) and compare MAC address with list
of MAC addresses discovered for this object. If a match was found, count for every object (device, vm)
how many MAC addresses are matching.

If exactly one NetBox object (device, vm) with matching interface MACs was found then this one will be used.

If two or more NetBox object (device, vm) with matching MACs were found, compare the two
NetBox object (device, vm) with the highest amount of matching interfaces. If the ratio of matching interface
MAC addresses exceeds 2.0 then the top matching NetBox object (device, vm) is chosen as desired object.

If the ratio is below 2.0 then None will be chosen. The probability is too low that
this one is the correct one.

#### 3. Try to find a NetBox object based on the primary IP (v4 or v6) address

If an exact matching NetBox object (device, vm) was found the object will be used
immediately without checking the other primary IP address (if defined).

### Objects read and parsed from vCenter

#### 1. Add a vCenter datacenter as a Cluster Group to NetBox
Simple, nothing special going on here

#### 2. Add a vCenter cluster as a Cluster to NetBox.

Cluster name is checked against `cluster_include_filter` and `cluster_exclude_filter config` setting.

#### 3. Parse distributed virtual port group
This is done to extract name and VLAN IDs from each port group

#### 4. Parse a vCenter (ESXi) host

First host is filtered:
* host has a cluster and this cluster is permitted
* skip host with same name and site, we already parsed it (use unique host names)
* does the host pass the `host_include_filter` and `host_exclude_filter`

Then all necessary host data will be collected:<br>
host model, manufacturer, serial, physical interfaces, virtual interfaces,
virtual switches, proxy switches, host port groups, interface VLANs, IP addresses

Primary IPv4/6 will be determined by
1. if the interface port group name contains "management" or "mngt"
2. interface with this IP is the default route of this host

#### 5. Parse a vCenter VM

Iterate over all VMs twice!

To handle VMs with the same name in a cluster we first iterate over all VMs and look only at the
active (online) ones and parse these first. Then we iterate a second time to catch the rest (also offline).

This has been implemented to support migration scenarios where you create/copy the same
machine with a different setup like a new version or something. This way NetBox will be
updated primarily with the actual active VM data.

First VM is filtered:
* VM has a cluster and is it permitted
* skip VMs with same name and cluster if already parsed
* does the VM pass the `vm_include_filter` and `vm_exclude_filter`

Then all necessary VM data will be collected:<br>
platform, virtual interfaces, virtual cpu/disk/memory interface VLANs, IP addresses

Primary IPv4/6 will be determined by interface that provides the default route for this VM.

**Note:**<br>
IP address information can only be extracted if guest tools are installed and running.

### Filtering VM Disk Information
VM disks are synchronized between vCenter and NetBox. Since NetBox 3.7.0, virtual disks are tracked as separate objects 
linked to VMs. In some scenarios, such as when temporary disks are attached to VMs during backup operations 
(e.g., "Independent-nonpersistent" disks from Veeam), you might want to exclude these changes from synchronization 
to avoid cluttering your NetBox change log.

You can use the following filter options to exclude disk synchronization for specific VMs:

1. **`vm_exclude_disk_sync`**: A regex pattern matching VM names where disk synchronization should be excluded.
   ```ini
   vm_exclude_disk_sync = backup-.*, veeam-.*
   ```

2. **`vm_exclude_disk_sync_by_tag`**: A comma-separated list of vCenter tags. VMs with any of these tags will have 
   their disk information excluded from synchronization.
   ```ini
   vm_exclude_disk_sync_by_tag = backup-vm, veeam-job
   ```

When a VM matches these filters, it will still be synchronized to NetBox with all its other information 
(CPU, memory, interfaces, IP addresses, etc.), but changes to disk information will be ignored.
