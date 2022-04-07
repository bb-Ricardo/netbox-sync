
# NetBox-Sync

This is a tool to sync data from different sources to a NetBox instance.

Available source types:
* VMware vCenter Server
* [bb-ricardo/check_redfish](https://github.com/bb-Ricardo/check_redfish) inventory files

**IMPORTANT: READ INSTRUCTIONS CAREFULLY BEFORE RUNNING THIS PROGRAM**

## Thanks
A BIG thank-you goes out to [Raymond Beaudoin](https://github.com/synackray) for creating
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
* NetBox >= 2.9
#### Source: VMWare (if used)
* VMWare vCenter >= 6.0
#### Source: check_redfish (if used)
* check_redfish >= 1.2.0

# Installing
* here we assume we install in ```/opt```

## RedHat based OS
* on RedHat/CentOS 7 you need to install python3.6 and pip from EPEL first
* on RedHat/CentOS 8 systems the package name changed to `python3-pip`
```shell
yum install python36-pip
```

## Ubuntu 18.04 & 20.04
```shell
apt-get update && apt-get install python3-venv
```

## Clone repo and install dependencies
* download and setup of virtual environment
```shell
cd /opt
git clone https://github.com/bb-Ricardo/netbox-sync.git
cd netbox-sync
python3 -m venv .venv
. .venv/bin/activate
pip3 install -r requirements.txt || pip install -r requirements.txt
```

### VMware tag sync (if necessary)
The `vsphere-automation-sdk` must be installed if tags should be synced from vCenter to NetBox
* assuming we are still in an activated virtual env
```shell
pip install --upgrade pip setuptools
pip install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git
```

## Docker

Run the application in a docker container. You can build it yourself or use the ones from docker hub.

Available here: bbricardo/netbox-sync

* The application working directory is ```/app```
* Required to mount your ```settings.ini```

To build it by yourself just run:
```shell
docker build -t bbricardo/netbox-sync:latest .
```

To start the container just use:
```shell
docker run --rm -it -v $(pwd)/settings.ini:/app/settings.ini bbricardo/netbox-sync:latest
```

## Kubernetes

Run the containerized application in a kubernetes cluster

 * Build the container image
 * Tag and push the image to a container registry you have access to
 * Create a secret from the settings.ini
 * Update the image field in the manifest
 * Deploy the manifest to your k8s cluster and check the job is running

 ```shell
 docker build -t netbox-vsphere-sync .
 docker image tag netbox-vsphere-sync your-registry.host/netbox-vsphere-sync:v1.2.0
 docker image push your-registry.host/netbox-vsphere-sync:v1.2.0

 kubectl create secret generic netbox-vsphere-sync --from-file=settings.ini
 kubectl apply -f netbox-vsphere-sync-cronjob.yaml
 ```

## NetBox API token
In order to updated data in NetBox you need a NetBox API token.
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

Version: 1.2.2 (2022-01-27)
Project URL: https://github.com/bb-ricardo/netbox-sync

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

## Setup
Copy the [settings-example.ini](settings-example.ini) sample settings file to `settings.ini`.
All options are described in the example file.

## Cron job
In Order to sync all items regularly you can add a cron job like this one
```
 # NetBox Sync
 23 */2 * * *  /opt/netbox-sync/.venv/bin/python3 /opt/netbox-sync/netbox-sync.py >/dev/null 2>&1
```

# How it works
**READ CAREFULLY**

## Basic structure
The program operates mainly like this
1. parsing and validating config
2. instantiating all sources and setting up connection to NetBox
3. read current data from NetBox
4. read data from all sources and add/update objects in memory
5. Update data in NetBox based on data from sources
6. Prune old objects

## NetBox connection
Request all current NetBox objects. Use caching whenever possible.
Objects must provide "last_updated" attribute to support caching for this object type.

Actually perform the request and retry x times if request times out.
Program will exit if all retries failed!

## Supported sources
Check out the documentations for the different sources
* [vmware](docs/source_vmware.md)
* [check_redfish](docs/source_check_redfish.md)

If you have multiple vCenter instances or check_redfish folders just add another source with the same type
in the **same** file.

Example:
```ini
[source/vcenter-BLN]

enabled = True
host_fqdn = vcenter1.berlin.example.com

[source/vcenter-NYC]

enabled = True
host_fqdn = vcenter2.new-york.example.com

[source/redfish-hardware]

type = check_redfish
inventory_file_path = /opt/redfish_inventory
```

**Developers**:
If you are interested in adding more source types please open an issue/discussion
because the documentation of implementing a new source hasn't been finished yet. 🤷

## Pruning
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
