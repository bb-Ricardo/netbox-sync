
# WARNING
This Script syncs data from various sources to NetBox

WIP: THIS IS STILL UNDER DEVELOPMENT, DON'T USE IN PRODUCTION!!!

BUT TESTING IS MORE THEN WELCOME


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

## Setup
Now copy the sample settings file to `settings.ini`

You should define the var `cluster_site_relation` which maps a vCenter Cluster to an exiting Site in NetBox.
Otherwise a placeholder Site will be created.

# Running the script

```
usage: netbox-sync.py [-h] [-c settings.ini]
                      [-l {DEBUG3,DEBUG2,DEBUG,INFO,WARNING,ERROR}] [-p]

Sync objects from various sources to Netbox

Version: 0.0.1 (2020-10-01)

optional arguments:
  -h, --help            show this help message and exit
  -c settings.ini, --config settings.ini
                        points to the config file to read config data from
                        which is not installed under the default path
                        './settings.ini'
  -l {DEBUG3,DEBUG2,DEBUG,INFO,WARNING,ERROR}, --log_level {DEBUG3,DEBUG2,DEBUG,INFO,WARNING,ERROR}
                        set log level (overrides config)
  -p, --purge           Remove (almost) all synced objects which were create
                        by this script. This is helpful if you want to start
                        fresh or stop using this script.
```

## Migration
If you migrate from https://github.com/synackray/vcenter-netbox-sync do following things:
* rename Tags:
    * Synced -> NetBox-synced
    * Orphaned -> NetBox-synced: Orphaned

## TESTING
It is recommended to set log level to "DEBUG2" this way the program should tell you what is happening and why

# License
>You can check out the full license [here](LICENSE.txt)

This project is licensed under the terms of the **MIT** license.
