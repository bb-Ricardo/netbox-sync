
# WARNING
This Script syncs data from various sources to NetBox

WIP: THIS IS PRE ALPHA, DON'T USE IN PRODUCTION!!!

CURRENTLY TESTING ONLY

## Known Bugs
* assignment/reassignment of primary IP addresses
* --purge is completely untested

## Installation

```
git clone https://github.com/bb-Ricardo/netbox-sync.git
cd netbox-sync
virtualenv .env
. .env/bin/activate
pip install -r requirements.txt
```

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

# Licenses
 APACHE (probably)
