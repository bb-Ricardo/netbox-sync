# Source: check_redfish

This program is able to import inventory files created via
[bb-ricardo/check_redfish](https://github.com/bb-Ricardo/check_redfish#inventory-data)

## Setup
You need to have a source section in your `settings.ini` file with following type:
```ini
type = check_redfish
```
All options for this source are described in the [settings-example.ini](../settings-example.ini) file.

If you have multiple redfish import folders just add another source with the same type.

## Source data
### Example: creating a source inventory file
```
/opt/check_redfish/check_redfish.py \
    -H 10.5.42.23 \
    -f /etc/icinga2/ilo_credentials \
    --inventory --all \
    --inventory_file /tmp/inventory_files/my-server.json \
    --inventory_id 23
```

### Requirements
* inventory file needs to have a `.json` suffix
* A matching device needs to be already present in NetBox
  * the inventory needs to have either a `invenotry_id` which matches a NetBox device id or
  * the system serial needs to match a device in NetBox

## Adding objects from a check_redfish inventory file to NetBox

You might be interested in this [description](common_concepts.md). This describes how discovered
IP addresses and interfaces will be added to NetBox.

### Custom fields

At first some custom fields will be added if not already present.
* firmware: stores the item firmware if present
* inventory-type: the type of the inventory item (i.e.: Power Supply)
* inventory-size: the "size" of the inventory item
  * DIMM: module size in GB
  * Physical Drive: drive size in GB
  * CPU: number of cores and threads
  * ...
* inventory-speed: the "speed" of the inventory item
  * Physical Drive (non SSD): rotational speed in RPM
  * DIMM: Module speed in MHz
  * CPU: speed in GHz
  * NIC: Interface speed in GBit/s (or MBit/s for 100Base-T)
  * ...
* health: the last discovered health status of the inventory item, usually OK

### Parsing inventory files

#### Example inventory
```json
{
    "inventory": {
        "chassi": [],
        "fan": [],
        "firmware": [],
        "logical_drive": [],
        "manager": [],
        "memory": [],
        "network_adapter": [],
        "network_port": [],
        "physical_drive": [],
        "power_supply": [
            {
                "bay": 1,
                "capacity_in_watt": 500,
                "chassi_ids": [
                    1
                ],
                "firmware": "1.03",
                "health_status": "OK",
                "id": "0",
                "input_voltage": 224,
                "last_power_output": 110,
                "model": "XXXXXX-B21",
                "name": "HpeServerPowerSupply",
                "operation_status": "Enabled",
                "part_number": "XXXXXX-001",
                "serial": "XXXXXXX",
                "type": "AC",
                "vendor": "CHCNY"
            },
            {
                "bay": 2,
                "capacity_in_watt": 500,
                "chassi_ids": [
                    1
                ],
                "firmware": "1.03",
                "health_status": "OK",
                "id": "1",
                "input_voltage": 228,
                "last_power_output": 110,
                "model": "XXXXXX-B21",
                "name": "HpeServerPowerSupply",
                "operation_status": "Enabled",
                "part_number": "XXXXXX-001",
                "serial": "XXXXXXX",
                "type": "AC",
                "vendor": "CHCNY"
            }
        ],
        "processor": [],
        "storage_controller": [],
        "storage_enclosure": [],
        "system": [],
        "temperature": []
    },
    "meta": {
        "data_retrieval_issues": {},
        "duration_of_data_collection_in_seconds": 1.002901,
        "host_that_collected_inventory": "inventory-collector.example.com",
        "inventory_id": 23,
        "inventory_layout_version": "1.2.0",
        "script_version": "1.2.0",
        "start_of_data_collection": "2021-04-01T09:09:07+02:00"
    }
}
```

### Parsing files and finding the correct device

First the source will try to open the directory defined in `inventory_file_path` and find all files with the
suffix `.json`.
Then it will iterate over all inventory files and try to add the Inventory to NetBox.

The id in `meta.invenotry_id` is used to find a matching NetBox device with the same ID.
If this was unsuccessful the inventory systems serial is used to find a matching device.
If that failed as well this inventory files is skipped.

### Items added/updated in NetBox

inventory_class    |inventory-type    |NetBox object
-------------------|------------------|-------------
system             |N/A               |dcim/devices
power_supply       |Power Supply      |dcim/power-ports<br>dcim/inventory-items
fan                |Fan               |dcim/inventory-items
memory             |DIMM              |dcim/inventory-items
proc               |CPU               |dcim/inventory-items
physical_drive     |Physical Drive    |dcim/inventory-items
storage_controller |Storage Controller|dcim/inventory-items
storage_enclosure  |Storage Enclosure |dcim/inventory-items
network_adapter    |NIC               |dcim/inventory-items
network_port       |N/A               |dcim/interfaces
manager            |Manager           |dcim/inventory-items

