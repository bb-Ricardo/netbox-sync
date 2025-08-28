This document is intended to be an overview of how the code is working.

1. source list is validated.
2. sources are instantiated.
	the sources are returned as a source handler list.
3. netbox data is queried and cached
	All data types in NBInventory class are:
		"FHRP group": [],
		"IP address": [],
		"IP prefix": [],
		"MAC address": [],
		"VLAN": [],
		"VLANGroup": [],
		"VRF": [],
		"Virtual Disk": [],
		"cluster": [],
		"cluster group": [],
		"cluster type": [],
		"custom field": [],
		"device": [],
		"device role": [],
		"device type": [],
		"interface": [],
		"inventory item": [],
		"manufacturer": [],
		"platform": [],
		"power port": [],
		"site": [],
		"site group": [],
		"tag": [],
		"tenant": [],
		"virtual machine": [],
		"virtual machine interface": []
	these are all of the possible, valid inventory entries. obtained from adding 'log.debug(inventory)' to module/sources/__init__.py, line 84.
4. source handler 'apply()' method is called, which retrieves vmware data. In this method, 'view handers' are called, which individually add each data type (e.g. data center, cluster) to the 'object_mapping' dict.
	object_mapping entries are:
		"datacenter": {
			"view_type": vim.Datacenter,
			"view_handler": self.add_datacenter
		},
		"cluster": {
			"view_type": vim.ClusterComputeResource,
			"view_handler": self.add_cluster
		},
		"single host cluster": {
			"view_type": vim.ComputeResource,
			"view_handler": self.add_cluster
		},
		"network": {
			"view_type": vim.dvs.DistributedVirtualPortgroup,
			"view_handler": self.add_port_group
		},
		"host": {
			"view_type": vim.HostSystem,
			"view_handler": self.add_host
		},
		"virtual machine": {
			"view_type": vim.VirtualMachine,
			"view_handler": self.add_virtual_machine
		},
		"offline virtual machine": {
			"view_type": vim.VirtualMachine,
			"view_handler": self.add_virtual_machine
		}
5. the queried data is added to the cache under the view handler methods.
	the view handler methods are responsible for adding all the individual data to the cache and inventory
6. in the vm view handler, all the extra data from the vm's is collected and parsed, and then passed into the add_device_vm_to_inventory method. currently i believe this is where the ip address data is coming from. the params for the above method are:
	NBVM, object_data=vm_data, vnic_data=nic_data, nic_ips=nic_ips, p_ipv4=vm_primary_ip4, p_ipv6=vm_primary_ip6, vmware_object=obj, disk_data=disk_data
7. in this file /module/sources/vmware/connection.py, line 956 is the method add_device_vm_to_inventory. this is where vm's are added to the inventory, and the corresponding objects (vm attributes, like site, device, primary ip address) are matched or created.
8. after looking through the add_update_interface method (/module/sources/common/source_base.py, line 234) within the previous method, vmware does not appear to be providing any fhrp group related data. itRequest logs is possible this is because i don't have any fhrp group data to sync.
	interface data provided (in the form ([ip address], {assigned object})):
		([], {'name': 'vmnic0', 'device': None, 'mac_address': '0C:4D:E9:99:EE:51', 'enabled': True, 'description': '1Gb/s  pNIC (vSwitch0)', 'type': '1000base-t', 'mtu': 1500, 'speed': 1000000, 'duplex': 'full', 'mode': 'access'})
		(['192.168.11.143/255.255.255.0'], {'name': 'vmk0', 'device': None, 'mac_address': '0C:4D:E9:99:EE:51', 'enabled': True, 'mtu': 1500, 'type': 'virtual', 'mode': 'access', 'description': 'Management Network (vSwitch0, vlan ID: 0)'})
		(['192.168.11.147/24'], {'name': 'vNIC 1 (VM Network)', 'virtual_machine': None, 'mac_address': '00:0C:29:F3:3C:69', 'description': 'Network adapter 1 (VirtualVmxnet3) (vlan ID: 0)', 'enabled': True, 'mtu': 1500, 'mode': 'access'})
	this method is where ip address objects are created and added to the inventory if new and updated if not.
	the method also returns the ip addresses as a list (as well as the interface object)
