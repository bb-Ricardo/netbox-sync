## Source: vmware

To match new/updated objects to existing objects in NetBox a few different steps are taken to find the correct object.


### Add/update device/VM object in inventory based on gathered data

Try to find object first based on the object data, interface MAC addresses and primary IPs.
1. try to find by name and cluster/site
2. try to find by mac addresses interfaces
3. try to find by serial number (1st) or asset tag (2nd) (ESXi host)
4. try to find by primary IP

#### IP addresses
First they will be checked and added if all checks pass.
* have to pass `permitted_subnets` config setting
* loop back addresses will be ignored
* link local addresses will be ignored

For each IP address a matching IP prefix will be searched for. First we look for the longest
matching IP Prefix in the same site. If this failed we try to find the longest matching global IP Prefix.

If a IP Prefix was found then we try to get the VRF and VLAN for this prefix. Now we compare
if interface VLAN and prefix VLAN match up and warn if they don't. We also compare the
length of the found prefix with the prefix length of the configured IP address.
A warning will also be issued if they don't match.

If the same IP address is found on a different interface (of a different device/VM) within the same realm
(both using same VRF or both are global) then following test are performed:
* If the current interface is enabled and the new one disabled:
  * IP stays at the current interface
* If the current interface is disabled and the new one enabled:
  * IP will be moved to the enabled interface
* Both interfaces are in the same state (disabled/enable)
  * IP will also stay at the current interface as it's unclear which one would be the correct one

Then we try to add data to the IP address if not already set:

* add prefix VRF if VRF for this IP is undefined
* add tenant if tenant for this IP is undefined
    1. try prefix tenant
    2. if prefix tenant is undefined try VLAN tenant

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

If the ratio is below 2.0 then None will be chosen. The probability is to low that
this one is the correct one.

#### 3. Try to find a NetBox object based on the primary IP (v4 or v6) address

If an exact matching NetBox object (device, vm) was found the object will be used
immediately without checking the other primary IP address (if defined).

### Try to match current NetBox object (device, vm) interfaces to discovered ones

This will be done by multiple approaches.
Order as following listing. Whatever matches first will be chosen.

by simple name:
* both interface names match exactly

by MAC address separated by physical and virtual NICs:
* MAC address of interfaces match exactly, distinguish between physical and virtual interfaces

by MAC regardless of interface type
* MAC address of interfaces match exactly, type of interface does not matter

If there are interfaces which don't match at all then the unmatched interfaces will be
matched 1:1. Sort both lists (unmatched current interfaces, unmatched new interfaces)
by name and assign them each other.
```
    ens1 > vNIC 1
    eth0 > vNIC 2
    eth1 > vNIC 3
    ...  > ...
```

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
