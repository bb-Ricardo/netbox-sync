
# NetBox-Sync

This is a tool to sync data from different sources (currently only VMWare vCenter) to a NetBox instance.

**IMPORTANT: READ THIS INSTRUCTIONS CAREFULLY BEFORE RUNNING THIS PROGRAM**

## Thanks
A BIG thank you goes out to [Raymond Beaudoin](https://github.com/synackray) for creating
[vcenter-netbox-sync](https://github.com/synackray/vcenter-netbox-sync) which served as source of a lot
of ideas for this project.

## Principles

> copied from [Raymond Beaudoin](https://github.com/synackray)

The [NetBox documentation](https://netbox.readthedocs.io/en/stable/#serve-as-a-source-of-truth) makes it clear
the tool is intended to act as a "Source of Truth". The automated import of live network state is
strongly discouraged. While this is sound logic we've aimed to provide a middle-ground
solution for those who desire the functionality.

All objects collected from vCenter have a "lifecycle". Upon import, for supported object types,
they are tagged `NetBox-synced` to note their origin and distinguish them from other objects.
Using this tagging system also allows for the orphaning of objects which are no longer detected in vCenter.
This ensures stale objects are removed from NetBox keeping an accurate current state.

## Requirements
### Software
* python >= 3.6
* packaging
* requests==2.24.0
* pyvmomi==6.7.3
* aiodns==2.0.0

### Environment
* VMWare vCenter >= 6.0
* NetBox >= 2.9

# Installing
* here we assume we install in ```/opt```

## Ubuntu 18.04
```
sudo apt-get install virtualenv
cd /opt
git clone https://github.com/bb-Ricardo/netbox-sync.git
cd netbox-sync
virtualenv -p python3 .env
. .env/bin/activate
pip3 install -r requirements.txt
```

## RedHat based OS
* on RedHat/CentOS 7 you need to install python3.6 and pip from EPEL first
* on RedHat/CentOS 8 systems the package name changed to `python3-pip`
```
yum install python36-pip
```

* download and setup of virtual environment
```
cd /opt
git clone https://github.com/bb-Ricardo/netbox-sync.git
cd netbox-sync
virtualenv-3 .env || virtualenv .env
. .env/bin/activate
pip3 install -r requirements.txt || pip install -r requirements.txt
```

### Accounts and tokens
In order to read data from a vCenter and updated data in NetBox you need credentials in both instances.

### vCenter user
* User account with "Read-only" role on vCenter root scope. The "Propagate to children" setting must also be checked.

### NetBox API token
* API token with all permissions (read, write) except:
  * auth
  * secrets
  * users

A short description can be found [here](https://netbox.readthedocs.io/en/stable/rest-api/authentication/)

# Running the script

```
usage: netbox-sync.py [-h] [-c settings.ini]
                      [-l {DEBUG3,DEBUG2,DEBUG,INFO,WARNING,ERROR}] [-n] [-p]

Sync objects from various sources to NetBox

Version: 1.0.0-rc2 (2021-02-11)

optional arguments:
  -h, --help            show this help message and exit
  -c settings.ini, --config settings.ini
                        points to the config file to read config data from
                        which is not installed under the default path
                        './settings.ini'
  -l {DEBUG3,DEBUG2,DEBUG,INFO,WARNING,ERROR}, --log_level {DEBUG3,DEBUG2,DEBUG,INFO,WARNING,ERROR}
                        set log level (overrides config)
  -n, --dry_run         Operate as usual but don't change anything in NetBox.
                        Great if you want to test and see what would be
                        changed.
  -p, --purge           Remove (almost) all synced objects which were create
                        by this script. This is helpful if you want to start
                        fresh or stop using this script.
```

## TESTING
It is recommended to set log level to `DEBUG2` this way the program should tell you what is happening and why.
Also use the dry run option `-n` at the beginning to avoid changes directly in NetBox.

## Migration
If you migrate from [vcenter-netbox-sync](https://github.com/synackray/vcenter-netbox-sync) do following things:
* rename Tags:
    * Synced -> NetBox-synced
    * Orphaned -> NetBox-synced: Orphaned

## Setup
Copy the [settings-example.ini](settings-example.ini) sample settings file to `settings.ini`.
All options are described in the example file.

You should define the var `cluster_site_relation` which maps a vCenter cluster to an exiting Site in NetBox.
Otherwise a placeholder site will be created.

## Cron job
In Order to sync all items regularly you can add a cron job like this one
 # NetBox Sync
 23 */2 * * *  /opt/netbox-sync/.env/bin/python3 /opt/netbox-sync/netbox-sync.py >/dev/null 2>&1

# How it works
**READ CAREFULLY**

## Basic structure
The program operates mainly like this
1. parsing and validating config
2. instantiating all sources and setting up connection to NetBox
3. read current data from NetBox
4. read data from sources and add/update objects in memory
5. Update data in NetBox based on data from sources
6. Prune old objects

## NetBox connection
Request all current NetBox objects. Use caching whenever possible.
Objects must provide "last_updated" attribute to support caching for this object type.
Otherwise it's not possible to query only changed objects since last run. If attribute is
not present all objects will be requested (looking at you *Interfaces)

Actually perform the request and retry x times if request times out.
Program will exit if all retries failed!

## Sources
Currently only VMWare vCenter is an available source. But new sources can be added via a new
source class.

ToDo:
* add documentation of source implementation here

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
* If the current interface ia disabled and the new one enabled:
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

### Pruning
Prune objects in NetBox if they are no longer present in any source.
First they will be marked as Orphaned and after X (config option) days they will be
deleted from NetBox.

Objects subjected to pruning:
* devices
* VMs
* device interfaces
* VM interfaces
* IP addresses

All other objects created (i.e.: VLANs, cluster, manufacturers) will keep the
source tag but will not be deleted. Theses are "shared" objects might be used
by different NetBox objects

# License
>You can check out the full license [here](LICENSE.txt)

This project is licensed under the terms of the **MIT** license.
