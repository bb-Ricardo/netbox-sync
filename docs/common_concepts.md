# Concepts of approaches to add different data to NetBox

## IP addresses
First they will be checked and added if all checks pass.
* have to pass `permitted_subnets` config setting
* loop back addresses will be ignored
* link local addresses will be ignored

For each IP address a matching IP prefix will be searched for. First we look for the longest
matching IP Prefix in the same site. If this failed we try to find the longest matching global IP Prefix.

If an IP Prefix was found then we try to get the VRF and VLAN for this prefix. Now we compare
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

## Try to match current NetBox object (device, vm) interfaces to discovered ones

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