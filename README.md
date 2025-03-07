
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
* urllib3==2.2.1
* wheel
* requests==2.31.0
* pyvmomi==8.0.2.0.1
* aiodns==3.0.0
* pyyaml==6.0.1

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

## Ubuntu 18.04 & 20.04 && 22.04
```shell
apt-get update && apt-get install python3-venv
```

## Clone repo and install dependencies
* If you need to use python 3.6 then you would need `requirements_3.6.txt` to install requirements
* download and setup of virtual environment
```shell
cd /opt
git clone https://github.com/bb-Ricardo/netbox-sync.git
cd netbox-sync
python3 -m venv .venv
. .venv/bin/activate
pip3 install --upgrade pip || pip install --upgrade pip
pip3 install wheel || pip install wheel
pip3 install -r requirements.txt || pip install -r requirements.txt
```

### VMware tag sync (if necessary)
The `vsphere-automation-sdk` must be installed if tags should be synced from vCenter to NetBox
* assuming we are still in an activated virtual env
```shell
pip install --upgrade git+https://github.com/vmware/vsphere-automation-sdk-python.git
```

## NetBox API token
In order to updated data in NetBox you need a NetBox API token.
* API token with all permissions (read, write) except:
  * auth
  * secrets
  * users

A short description can be found [here](https://docs.netbox.dev/en/stable/integrations/rest-api/#authentication)

# Running the script

```
usage: netbox-sync.py [-h] [-c settings.ini [settings.ini ...]] [-g]
                      [-l {DEBUG3,DEBUG2,DEBUG,INFO,WARNING,ERROR}] [-n] [-p]

Sync objects from various sources to NetBox

Version: 1.8.0 (2025-03-07)
Project URL: https://github.com/bb-ricardo/netbox-sync

options:
  -h, --help            show this help message and exit
  -c settings.ini [settings.ini ...], --config settings.ini [settings.ini ...]
                        points to the config file to read config data from
                        which is not installed under the default path
                        './settings.ini'
  -g, --generate_config
                        generates default config file.
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

## Configuration
There are two ways to define configuration. Any combination of config file(s) and environment variables is possible.
* config files (the [default config](https://github.com/bb-Ricardo/netbox-sync/blob/main/settings-example.ini) file name is set to `./settings.ini`.)
* environment variables

The config from the environment variables will have precedence over the config file definitions.

### Config files
Following config file types are supported:
* ini
* yaml

There is also more than one config file permitted. Example (config file names are also just examples):
```bash
/opt/netbox-sync/netbox-sync.py -c common.ini all-sources.yaml additional-config.yaml
```

All files are parsed in order of the definition and options will overwrite the same options if defined in a
previous config file.

To get config file examples which include descriptions and all default values, the `-g` can be used:
```bash
# this will create an ini example
/opt/netbox-sync/netbox-sync.py -g -c settings-example.ini

# and this will create an example config file in yaml format
/opt/netbox-sync/netbox-sync.py -g -c settings-example.yaml 
```

### Environment variables
Each setting which can be defined in a config file can also be defined using an environment variable.

The prefix for all environment variables to be used in netbox-sync is: `NBS`

For configuration in the `common` and `netbox` section a variable is defined like this
```
<PREFIX>_<SECTION_NAME>_<CONFIG_OPTION_KEY>=value
```

Following example represents the same configuration:
```yaml
# yaml config example
common:
  log_level: DEBUG2
netbox:
  host_fqdn: netbox-host.example.com
  prune_enabled: true
```
```bash
# this variable definition is equal to the yaml config sample above
NBS_COMMON_LOG_LEVEL="DEBUG2"
NBS_netbox_host_fqdn="netbox-host.example.com"
NBS_NETBOX_PRUNE_ENABLED="true"
```

This way it is possible to expose for example the `NBS_NETBOX_API_KEY` only via an env variable.

The config definitions for `sources` need to be defined using an index. Following conditions apply:
* a single source needs to use the same index
* the index can be number or a name (but contain any special characters to support env var parsing)
* the source needs to be named with `_NAME` variable

Example of defining a source with config and environment variables.
```ini
; example for a source
[source/example-vcenter]
enabled = True
type = vmware
host_fqdn = vcenter.example.com
username = vcenter-readonly
```
```bash
# define the password on command line
# here we use '1' as index
NBS_SOURCE_1_NAME="example-vcenter"
NBS_SOURCE_1_PASSWORD="super-secret-and-not-saved-to-the-config-file"
NBS_SOURCE_1_custom_dns_servers="10.0.23.23, 10.0.42.42"
```

Even to just define one source variable like `NBS_SOURCE_1_PASSWORD` the `NBS_SOURCE_1_NAME` needs to be defined as
to associate to the according source definition.

## Cron job
In Order to sync all items regularly you can add a cron job like this one
```
 # NetBox Sync
 23 */2 * * *  /opt/netbox-sync/.venv/bin/python3 /opt/netbox-sync/netbox-sync.py >/dev/null 2>&1
```

## Docker

Run the application in a docker container. You can build it yourself or use the ones from docker hub.

Available here: [bbricardo/netbox-sync](https://hub.docker.com/r/bbricardo/netbox-sync)

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

* Create a config map with the default settings
* Create a secret witch only contains the credentials needed
* Adjust the provided [cronjob resource](https://github.com/bb-Ricardo/netbox-sync/blob/main/k8s-netbox-sync-cronjob.yaml) to your needs
* Deploy the manifest to your k8s cluster and check the job is running

config example saved as `settings.yaml`
```yaml
netbox:
  host_fqdn: netbox.example.com

source:
  my-vcenter-example:
    type: vmware
    host_fqdn: vcenter.example.com
    permitted_subnets: 172.16.0.0/12, 10.0.0.0/8, 192.168.0.0/16, fd00::/8
    cluster_site_relation: Cluster_NYC = New York, Cluster_FFM.* = Frankfurt, Datacenter_TOKIO/.* = Tokio
```

secrets example saved as `secrets.yaml`
```yaml
netbox:
  api_token: XYZXYZXYZXYZXYZXYZXYZXYZ
source:
  my-vcenter-example:
    username: vcenter-readonly
    password: super-secret
```

Create resource in your k8s cluster
 ```shell
kubectl create configmap netbox-sync-config --from-file=settings.yaml
kubectl create secret generic netbox-sync-secrets --from-file=secrets.yaml
kubectl apply -f k8s-netbox-sync-cronjob.yaml
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
* [vmware](https://github.com/bb-Ricardo/netbox-sync/blob/main/docs/source_vmware.md)
* [check_redfish](https://github.com/bb-Ricardo/netbox-sync/blob/main/docs/source_check_redfish.md)

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

If different sources overwrite the same attribute for ex. a host then the order of the sources should be considered.
The last source in order from top to bottom will prevail.

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
>You can check out the full license [here](https://github.com/bb-Ricardo/netbox-sync/blob/main/LICENSE.txt)

This project is licensed under the terms of the **MIT** license.
