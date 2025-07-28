# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import re
from ipaddress import ip_address

from module.common.misc import quoted_split
from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption
from module.config.group import ConfigOptionGroup
from module.sources.common.config import *
from module.sources.common.permitted_subnets import PermittedSubnets
from module.sources.common.handle_vlan import FilterVLANByID, FilterVLANByName
from module.common.logging import get_logger
from module.common.support import normalize_mac_address

log = get_logger()


class VMWareConfig(ConfigBase):

    section_name = source_config_section_name
    source_name = None
    source_name_example = "my-vcenter-example"

    def __init__(self):
        self.options = [
            ConfigOption(**config_option_enabled_definition),

            ConfigOption(**{**config_option_type_definition, "config_example": "vmware"}),

            ConfigOption("host_fqdn",
                         str,
                         description="host name / IP address of the vCenter",
                         config_example="vcenter.example.com",
                         mandatory=True),

            ConfigOption("port",
                         int,
                         description="TCP port to connect to",
                         default_value=443),

            ConfigOption("username",
                         str,
                         description="username to use to log into vCenter",
                         config_example="vcenter-readonly",
                         mandatory=True),

            ConfigOption("password",
                         str,
                         description="password to use to log into vCenter",
                         config_example="super-secret",
                         sensitive=True,
                         mandatory=True),

            ConfigOption("validate_tls_certs",
                         bool,
                         description="""Enforces TLS certificate validation.
                         If vCenter uses a valid TLS certificate then this option should be set
                         to 'true' to ensure a secure connection.""",
                         default_value=False),

            ConfigOption("proxy_host",
                         str,
                         description="""EXPERIMENTAL: Connect to a vCenter using a proxy server
                         (socks proxies are not supported). define a host name or an IP address""",
                         config_example="10.10.1.10"),

            ConfigOption("proxy_port",
                         int,
                         description="""EXPERIMENTAL: Connect to a vCenter using a proxy server
                         (socks proxies are not supported).
                         define proxy server port number""",
                         config_example=3128),

            ConfigOption(**config_option_permitted_subnets_definition),

            ConfigOptionGroup(title="filter",
                              description="""filters can be used to include/exclude certain objects from importing
                              into NetBox. Include filters are checked first and exclude filters after.
                              An object name has to pass both filters to be synced to NetBox.
                              If a filter is unset it will be ignored. Filters are all treated as regex expressions!
                              If more then one expression should match, a '|' needs to be used
                              """,
                              config_example="""Example: (exclude all VMs with "replica" in their name 
                              and all VMs starting with "backup"): vm_exclude_filter = .*replica.*|^backup.*""",
                              options=[
                                ConfigOption("cluster_exclude_filter",
                                             str,
                                             description="""If a cluster is excluded from sync then ALL VMs and HOSTS
                                             inside the cluster will be ignored! a cluster can be specified
                                             as "Cluster-name" or "Datacenter-name/Cluster-name" if
                                             multiple clusters have the same name"""),
                                ConfigOption("cluster_include_filter", str),
                                ConfigOption("host_exclude_filter",
                                             str,
                                             description="""This will only include/exclude the host,
                                             not the VM if Host is part of a multi host cluster"""),
                                ConfigOption("host_include_filter", str),
                                ConfigOption("vm_exclude_filter",
                                             str, description="simply include/exclude VMs"),
                                ConfigOption("vm_include_filter", str)
                              ]),
            ConfigOption("vm_exclude_by_tag_filter",
                         str,
                         description="""defines a comma separated list of vCenter tags which (if assigned to a VM)
                         will exclude this VM from being synced to NetBox. The config option 'vm_tag_source'
                         determines which tags are collected for VMs.
                         """,
                         config_example="tag-a, tag-b"
                         ),

            ConfigOptionGroup(title="relations",
                              options=[
                                ConfigOption("cluster_site_relation",
                                             str,
                                             description="""\
                                             This option defines which vCenter cluster is part of a NetBox site.
                                             This is done with a comma separated key = value list.
                                               key: defines the cluster name as regex
                                               value: defines the NetBox site name (use quotes if name contains commas)
                                             This is a quite important config setting as IP addresses, prefixes, VLANs
                                             and VRFs are site dependent. In order to assign the correct prefix to an IP
                                             address it is important to pick the correct site.
                                             A VM always depends on the cluster site relation
                                             a cluster can be specified as "Cluster-name" or
                                             "Datacenter-name/Cluster-name" if multiple clusters have the same name.
                                             When a vCenter cluster consists of hosts from multiple NetBox sites,
                                             it is possible to leave the site for a NetBox cluster empty. All VMs from
                                             this cluster will then also have no site reference.
                                             The keyword "<NONE>" can be used as a value for this.
                                             """,
                                             config_example="Cluster_NYC = New York, Cluster_FFM.* = Frankfurt, Datacenter_TOKIO/.* = Tokio, Cluster_MultiSite = <NONE>"),
                                ConfigOption("host_site_relation",
                                             str,
                                             description="""Same as cluster site but on host level.
                                             If unset it will fall back to cluster_site_relation""",
                                             config_example="nyc02.* = New York, ffm01.* = Frankfurt"),
                                ConfigOption("cluster_tenant_relation",
                                             str,
                                             description="""\
                                             This option defines which cluster/host/VM belongs to which tenant.
                                             This is done with a comma separated key = value list.
                                               key: defines a hosts/VM name as regex
                                               value: defines the NetBox tenant name (use quotes if name contains commas)
                                             a cluster can be specified as "Cluster-name" or
                                             "Datacenter-name/Cluster-name" if multiple clusters have the same name
                                             """,
                                             config_example="Cluster_NYC.* = Customer A"),
                                ConfigOption("host_tenant_relation", str, config_example="esxi300.* = Infrastructure"),
                                ConfigOption("vm_tenant_relation", str, config_example="grafana.* = Infrastructure"),
                                ConfigOption("host_platform_relation",
                                             str,
                                             description="""\
                                             This option defines custom platforms if the VMWare created platforms are not suitable.
                                             Pretty much a mapping of VMWare platform name to your own platform name.
                                             This is done with a comma separated key = value list.
                                               key: defines a VMWare returned platform name as regex
                                               value: defines the desired NetBox platform name""",
                                             config_example="VMware ESXi 7.0.3 = VMware ESXi 7.0 Update 3o"),
                                ConfigOption("vm_platform_relation", str, config_example="centos-7.* = centos7, microsoft-windows-server-2016.* = Windows2016"),
                                ConfigOption("host_role_relation",
                                             str,
                                             description="""\
                                             Define the NetBox device role used for hosts. The default is
                                             set to "Server". This is done with a comma separated key = value list.
                                               key: defines host(s) name as regex
                                               value: defines the NetBox role name (use quotes if name contains commas)
                                             """,
                                             default_value=".* = Server"),
                                ConfigOption("vm_role_relation",
                                             str,
                                             description="""\
                                             Define the NetBox device role used for VMs. This is done with a
                                             comma separated key = value list, same as 'host_role_relation'.
                                               key: defines VM(s) name as regex
                                               value: defines the NetBox role name (use quotes if name contains commas)
                                             """,
                                             config_example=".* = Server"),
                                ConfigOption("cluster_tag_relation",
                                             str,
                                             description="""\
                                             Define NetBox tags which are assigned to a cluster, host or VM. This is
                                             done with a comma separated key = value list.
                                               key: defines a hosts/VM name as regex
                                               value: defines the NetBox tag (use quotes if name contains commas)
                                             a cluster can be specified as "Cluster-name" or
                                             "Datacenter-name/Cluster-name" if multiple clusters have the same name""",
                                             config_example="Cluster_NYC.* = Infrastructure"),
                                ConfigOption("host_tag_relation", str, config_example="esxi300.* = Infrastructure"),
                                ConfigOption("vm_tag_relation", str, config_example="grafana.* = Infrastructure")
                              ]),
            ConfigOption("match_host_by_serial",
                         bool,
                         description="""Try to find existing host based on serial number. This can cause issues
                         with blade centers if VMWare does not report the blades serial number properly.""",
                         default_value=True),
            ConfigOption("collect_hardware_asset_tag",
                         bool,
                         description="Attempt to collect asset tags from vCenter hosts",
                         default_value=True),
            ConfigOption("collect_hardware_serial",
                         bool,
                         description="Attempt to collect serials from vCenter hosts",
                         default_value=True),
            ConfigOption("dns_name_lookup",
                         bool,
                         description="""Perform a reverse lookup for all collected IP addresses.
                         If a dns name was found it will be added to the IP address object in NetBox
                         """,
                         default_value=True),
            ConfigOption("custom_dns_servers",
                         str,
                         description="use custom DNS server to do the reverse lookups",
                         config_example="192.168.1.11, 192.168.1.12"),
            ConfigOption("set_primary_ip",
                         str,
                         description="""\
                         define how the primary IPs should be set
                         possible values:

                           always:     will remove primary IP from the object where this address is
                                       currently set as primary and moves it to new object

                           when-undefined:
                                       only sets primary IP if undefined, will cause ERRORs if same IP is
                                       assigned more then once to different hosts and IP is set as the
                                       objects primary IP

                           never:      don't set any primary IPs, will cause the same ERRORs
                                       as "when-undefined"
                         """,
                         default_value="when-undefined"),
            ConfigOption("skip_vm_comments",
                         bool,
                         description="Do not sync notes from a VM in vCenter to the comments field on a VM in netbox",
                         default_value=False),
            ConfigOption("skip_vm_templates",
                         bool,
                         description="Do not sync template VMs",
                         default_value=True),
            ConfigOption("skip_offline_vms",
                         bool,
                         description="""\
                         Skip virtual machines which are reported as offline.
                         ATTENTION: this option will keep purging stopped VMs if activated!
                         """,
                         default_value=False),
            ConfigOption("skip_srm_placeholder_vms",
                         bool,
                         description="""If the VMware Site Recovery Manager is used to can skip syncing
                         placeholder/replicated VMs from fail-over site to NetBox.""",
                         default_value=False),
            ConfigOption("strip_host_domain_name",
                         bool,
                         description="strip domain part from host name before syncing device to NetBox",
                         default_value=False),
            ConfigOption("strip_vm_domain_name",
                         bool,
                         description="strip domain part from VM name before syncing VM to NetBox",
                         default_value=False),
            ConfigOptionGroup(title="tag source",
                              description="""\
                              sync tags assigned to clusters, hosts and VMs in vCenter to NetBox
                              INFO: this requires the installation of the 'vsphere-automation-sdk',
                              see docs about installation possible values:
                                * object : the host or VM itself
                                * parent_folder_1 : the direct folder this object is organized in (1 level up)
                                * parent_folder_2 : the indirect folder this object is organized in (2 levels up)
                                * cluster : the cluster this object is organized in
                                * datacenter : the datacenter this object is organized in
                              this is a comma separated list of options. example: vm_tag_source = object, cluster
                              """,
                              config_example="Example: vm_tag_source = object, cluster",
                              options=[
                                ConfigOption("cluster_tag_source", str),
                                ConfigOption("host_tag_source", str),
                                ConfigOption("vm_tag_source", str)
                              ]),
            ConfigOption("sync_custom_attributes",
                         bool,
                         description="""sync custom attributes defined for hosts and VMs
                         in vCenter to NetBox as custom fields""",
                         default_value=False),
            ConfigOptionGroup(title="custom object attributes",
                              description="""\
                              add arbitrary host/vm object attributes as custom fields to NetBox.
                              multiple attributes can be defined comma separated.
                              to get a list of available attributes use '-l DEBUG3' as cli param (CAREFUL: output might be long)
                              and here 'https://gist.github.com/bb-Ricardo/538768487bdac4efafabe56e005cb4ef' can be seen how to
                              access these attributes
                              """,
                              options=[
                                ConfigOption("host_custom_object_attributes",
                                             str,
                                             config_example="summary.runtime.bootTime"),
                                ConfigOption("vm_custom_object_attributes",
                                             str,
                                             config_example="config.uuid")
                              ]),
            ConfigOption("set_source_name_as_cluster_group",
                         bool,
                         description="""this will set the sources name as cluster group name instead of the datacenter.
                         This works if the vCenter has ONLY ONE datacenter configured.
                         Otherwise it will rename all datacenters to the source name!""",
                         default_value=False),
            ConfigOption("sync_vm_dummy_interfaces",
                         bool,
                         description="""activating this option will also include "dummy/virtual" interfaces
                         which are only visible inside the VM and are exposed through VM guest tools.
                         Dummy interfaces without an IP address will be skipped.""",
                         default_value=False),
            ConfigOptionGroup(title="VLAN syncing",
                              description="""\
                              These options control if VLANs are sync to NetBox or if some VLANs are excluded from sync.
                              The exclude options can contain the site name as well (site-name/vlan). Site names and VLAN
                              names can be regex expressions. VLAN IDs can be single IDs or ranges.
                              """,
                              options=[
                                  ConfigOption("disable_vlan_sync",
                                               bool,
                                               description="disables syncing of any VLANs visible in vCenter to NetBox",
                                               default_value=False),
                                  ConfigOption("vlan_sync_exclude_by_name",
                                               str,
                                               config_example="New York/Storage, Backup, Tokio/DMZ, Madrid/.*"),
                                  ConfigOption("vlan_sync_exclude_by_id",
                                               str,
                                               config_example="Frankfurt/25, 1023-1042"),
                                  ConfigOption("vlan_group_relation_by_name",
                                               str,
                                               description="""adds a relation to assign VLAN groups to matching VLANs
                                               by name. Same matching rules as the exclude_by_name option uses are applied.
                                               If name and id relations are defined, the name relation takes precedence.
                                               Fist match wins. Only newly discovered VLANs which are not present in
                                               NetBox will be assigned a VLAN group. Supported scopes for a VLAN group
                                               are "site", "site-group", "cluster" and "cluster-group". Scopes are buggy
                                               in NetBox https://github.com/netbox-community/netbox/issues/18706
                                               """,
                                               config_example="London/Vlan_.* = VLAN Group 1, Tokio/Vlan_.* = VLAN Group 2"),
                                  ConfigOption("vlan_group_relation_by_id",
                                               str,
                                               description="""adds a relation to assign VLAN groups to matching VLANs by ID.
                                               Same matching rules as the exclude_by_id option uses are applied.
                                               Fist match wins.  Only newly discovered VLANs which are not present in
                                               NetBox will be assigned a VLAN group.
                                               """,
                                               config_example="1023-1042 = VLAN Group 1, Tokio/2342 = VLAN Group 2")
                              ]),

            ConfigOption("track_vm_host",
                         bool,
                         description="""enabling this option will add the ESXi host
                         this VM is running on to the VM details""",
                         default_value=False),
            ConfigOption("overwrite_device_interface_name",
                         bool,
                         description="""define if the name of the device interface discovered overwrites the
                         interface name in NetBox. The interface will only be matched by identical MAC address""",
                         default_value=True),
            ConfigOption("overwrite_vm_interface_name",
                         bool,
                         description="""define if the name of the VM interface discovered overwrites the
                         interface name in NetBox. The interface will only be matched by identical MAC address""",
                         default_value=True),
            ConfigOption("overwrite_device_platform",
                         bool,
                         description="""define if the platform of the device discovered overwrites the device
                         platform in NetBox.""",
                         default_value=True),
            ConfigOption("overwrite_vm_platform",
                         bool,
                         description="""define if the platform of the VM discovered overwrites the VM
                         platform in NetBox.""",
                         default_value=True),
            ConfigOption("host_management_interface_match",
                         str,
                         description="""set a matching value for ESXi host management interface description
                         (case insensitive, comma separated). Used to figure out the ESXi primary IP address""",
                         default_value="management, mgmt"),
            ConfigOption(**config_option_ip_tenant_inheritance_order_definition),
            ConfigOption("sync_vm_interface_mtu",
                         bool,
                         description="""Usually netbox-sync grabs the MTU size for the VM interface from the
                         ESXi hosts vSwitch. If this is not fitting or incorrect it is possible to disable the
                         synchronisation by setting this option to 'False'
                         """,
                         default_value=True),
            ConfigOption("host_nic_exclude_by_mac_list",
                         str,
                         description="""defines a comma separated list of MAC addresses which should be excluded
                         from sync. Any host NIC with a matching MAC address will be excluded from sync.
                         """,
                         config_example="AA:BB:CC:11:22:33, 66:77:88:AA:BB:CC"
                         ),
            ConfigOption("custom_attribute_exclude",
                         str,
                         description="""defines a comma separated list of custom attribute which should be excluded
                         from sync. Any custom attribute with a matching attribute key will be excluded from sync.
                         """,
                         config_example="VB_LAST_BACKUP, VB_LAST_BACKUP2"
                         ),
            ConfigOption("vm_disk_and_ram_in_decimal",
                         bool,
                         description="""In NetBox version 4.1.0 and newer the VM disk and RAM values are displayed
                         in power of 10 instead of power of 2. If this values is set to true 4GB of RAM will be
                         set to a value of 4000 megabyte. If set to false 4GB of RAM will be reported as 4096MB.
                         The same behavior also applies for VM disk sizes.""",
                         default_value=True
                         ),
            ConfigOption("skip_host_nics",
                         bool,
                         description="""Skip creating or updating host physical nics in Netobx. Normal operation
                         will maintain all phisical nics in netbox. This option will skip this part.""" ,
                         default_value=False
                         ),

            # removed settings
            ConfigOption("netbox_host_device_role",
                         str,
                         deprecation_message="You need to switch to 'host_role_relation'.",
                         removed=True),
            ConfigOption("netbox_vm_device_role",
                         str,
                         deprecation_message="You need to switch to 'vm_role_relation'.",
                         removed=True),
            ConfigOption("sync_tags",
                         bool,
                         deprecation_message="You need to switch to 'host_tag_source', " +
                                             "'vm_tag_source' or 'cluster_tag_source'",
                         removed=True),
            ConfigOption("sync_parent_tags",
                         bool,
                         deprecation_message="You need to switch to 'host_tag_source', " +
                                             "'vm_tag_source' or 'cluster_tag_source'",
                         removed=True)
        ]

        super().__init__()

    def validate_options(self):

        valid_tag_sources = [
            "object", "parent_folder_1", "parent_folder_2", "cluster", "datacenter"
        ]

        for option in self.options:

            if option.value is None:
                continue

            if "filter" in option.key and "vm_exclude_by_tag_filter" not in option.key:

                re_compiled = None
                try:
                    re_compiled = re.compile(option.value)
                except Exception as e:
                    log.error(f"Problem parsing regular expression for '{self.source_name}.{option.key}': {e}")
                    self.set_validation_failed()

                option.set_value(re_compiled)

                continue

            if option.key == "vm_exclude_by_tag_filter":

                option.set_value(quoted_split(option.value))

                continue

            if "relation" in option.key and "vlan_group_relation" not in option.key:

                relation_data = list()

                relation_type = option.key.split("_")[1]

                for relation in quoted_split(option.value):

                    object_name = relation.split("=")[0].strip(' "')
                    relation_name = relation.split("=")[1].strip(' "')

                    if len(object_name) == 0 or len(relation_name) == 0:
                        log.error(f"Config option '{relation}' malformed got '{object_name}' for "
                                  f"object name and '{relation_name}' for {relation_type} name.")
                        self.set_validation_failed()
                        continue

                    try:
                        re_compiled = re.compile(object_name)
                    except Exception as e:
                        log.error(f"Problem parsing regular expression '{object_name}' for '{relation}': {e}")
                        self.set_validation_failed()
                        continue

                    relation_data.append({
                        "object_regex": re_compiled,
                        "assigned_name": relation_name
                    })

                option.set_value(relation_data)

                continue

            if "custom_object_attributes" in option.key:

                option.set_value(quoted_split(option.value))

                continue

            if "tag_source" in option.key:

                option.set_value(quoted_split(option.value))

                for tag_source_option in option.value:
                    if tag_source_option not in valid_tag_sources:
                        log.error(f"Tag source '{tag_source_option}' for '{option.key}' option invalid.")
                        self.set_validation_failed()

                continue

            if option.key == "set_primary_ip":
                if option.value not in ["always", "when-undefined", "never"]:
                    log.error(f"Primary IP option '{option.key}' value '{option.value}' invalid.")
                    self.set_validation_failed()

            if option.key == "custom_dns_servers":

                dns_name_lookup = self.get_option_by_name("dns_name_lookup")

                if not isinstance(dns_name_lookup, ConfigOption) or dns_name_lookup.value is False:
                    continue

                custom_dns_servers = quoted_split(option.value)

                tested_custom_dns_servers = list()
                for custom_dns_server in custom_dns_servers:
                    try:
                        tested_custom_dns_servers.append(str(ip_address(custom_dns_server)))
                    except ValueError:
                        log.error(f"Config option 'custom_dns_servers' value '{custom_dns_server}' "
                                  f"does not appear to be an IP address.")
                        self.set_validation_failed()

                option.set_value(tested_custom_dns_servers)

                continue

            if option.key == "host_management_interface_match":

                option.set_value(quoted_split(option.value))

                continue

            if option.key == "ip_tenant_inheritance_order":

                option.set_value(quoted_split(option.value))

                for ip_tenant_inheritance in option.value:
                    if ip_tenant_inheritance not in ["device", "prefix", "disabled"]:
                        log.error(f"Config value '{ip_tenant_inheritance}' invalid for "
                                  f"config option 'ip_tenant_inheritance_order'!")
                        self.set_validation_failed()

                if len(option.value) > 2:
                    log.error("Config option 'ip_tenant_inheritance_order' can contain only 2 items max")
                    self.set_validation_failed()

            if option.key == "host_nic_exclude_by_mac_list":

                value_list = list()

                for mac_address in quoted_split(option.value) or list():

                    normalized_mac_address = normalize_mac_address(mac_address)

                    if len(f"{normalized_mac_address}") != 17:
                        log.error(f"MAC address '{mac_address}' for 'host_nic_exclude_by_mac_list' invalid.")
                        self.set_validation_failed()
                    else:
                        value_list.append(normalized_mac_address)

                option.set_value(value_list)

            if option.key == "custom_attribute_exclude":

                option.set_value(quoted_split(option.value))

                continue

            if option.key in [ "vlan_sync_exclude_by_name", "vlan_sync_exclude_by_id",
                               "vlan_group_relation_by_name", "vlan_group_relation_by_id" ]:

                if option.key == "vlan_sync_exclude_by_name":
                    filter_class = FilterVLANByName
                    filter_type = "exclude"
                elif option.key == "vlan_group_relation_by_name":
                    filter_class = FilterVLANByName
                    filter_type = "group relation"
                elif option.key == "vlan_sync_exclude_by_id":
                    filter_class = FilterVLANByID
                    filter_type = "exclude"
                elif option.key == "vlan_group_relation_by_id":
                    filter_class = FilterVLANByID
                    filter_type = "group relation"
                else:
                    raise ValueError(f"unhandled config option {option.key}")

                value_list = list()

                for single_option_value in quoted_split(option.value) or list():

                    relation_name = None
                    object_name = single_option_value.split("=")[0].strip(' "')

                    if "relation" in option.key:

                        if "=" not in single_option_value:
                            log.error(f"Config option '{option.key}' malformed, got {single_option_value} but "
                                      f"needs key = value relation.")
                            self.set_validation_failed()
                            continue

                        relation_name = single_option_value.split("=")[1].strip(' "')

                        if relation_name is not None and len(relation_name) == 0:
                            log.error(f"Config option '{option.key}' malformed, got '{object_name}' as "
                                      f"object name and relation name was empty.")
                            self.set_validation_failed()
                            continue

                    vlan_filter = filter_class(object_name, filter_type)

                    if not vlan_filter.is_valid():
                        self.set_validation_failed()
                        continue

                    if "relation" in option.key:
                        value_list.append((vlan_filter, relation_name))
                    else:
                        value_list.append(vlan_filter)

                option.set_value(value_list)

        permitted_subnets_option = self.get_option_by_name("permitted_subnets")

        if permitted_subnets_option is not None:
            permitted_subnets = PermittedSubnets(permitted_subnets_option.value)
            if permitted_subnets.validation_failed is True:
                self.set_validation_failed()

            permitted_subnets_option.set_value(permitted_subnets)
