# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption
from module.config.group import ConfigOptionGroup
from module.sources.common.conifg import *


class VMWareConfig(ConfigBase):

    section_name = source_config_section_name
    source_name = None

    def __init__(self):
        self.options = [
            ConfigOption(**config_option_enabled_definition),

            ConfigOption("type",
                         str,
                         description="type of source. This defines which source handler to use",
                         config_example="vmware",
                         mandatory=True),

            ConfigOption("host_fqdn",
                         str,
                         description="host name / IP address of the vCenter",
                         config_example="my-netbox.local",
                         mandatory=True),

            ConfigOption("port",
                         int,
                         description="TCP port to connect to",
                         default_value=443,
                         mandatory=True),

            ConfigOption("username",
                         str,
                         description="username to use to log into vCenter",
                         config_example="vcenter-admin",
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
                         to 'true' to ensure a secure connection."""),

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
                              If more then one expression should match a '|' needs to be used
                              """,
                              config_example="""
                              (exclude all VMs with "replica" in their name and all VMs starting with "backup")
                                vm_exclude_filter = .*replica.*|^backup.*""",
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
            ConfigOptionGroup(title="relations",
                              options=[
                                ConfigOption("cluster_site_relation",
                                             str,
                                             description="""
                                             This option defines which vCenter cluster is part of a NetBox site.
                                             This is done with a comma separated key = value list.
                                               key: defines the cluster name as regex
                                               value: defines the NetBox site name (use quotes if name contains commas)
                                             This is a quite important config setting as IP addresses, prefixes, VLANs
                                             and VRFs are site dependent. In order to assign the correct prefix to an IP
                                             address it is important to pick the correct site.
                                             A VM always depends on the cluster site relation
                                             a cluster can be specified as "Cluster-name" or
                                             "Datacenter-name/Cluster-name" if multiple clusters have the same name
                                             """,
                                             config_example="Cluster_NYC = New York, Cluster_FFM.* = Frankfurt, Datacenter_TOKIO/.* = Tokio"),
                                ConfigOption("host_site_relation",
                                             str,
                                             description="""Same as cluster site but on host level.
                                             If unset it will fall back to cluster_site_relation""",
                                             config_example="nyc02.* = New York, ffm01.* = Frankfurt"),
                                ConfigOption("cluster_tenant_relation",
                                             str,
                                             description="""
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
                                ConfigOption("vm_platform_relation",
                                             str,
                                             description="""
                                             This option defines custom platforms if the VMWare created platforms are not suitable.
                                             Pretty much a mapping of VMWare platform name to your own platform name.
                                             This is done with a comma separated key = value list.
                                               key: defines a VMWare returned platform name
                                               value: defines the desired NetBox platform name""",
                                             config_example="centos-7.* = centos7, microsoft-windows-server-2016.* = Windows2016"),
                                ConfigOption("host_role_relation",
                                             str,
                                             description="""
                                             Define the NetBox device role used for hosts and VMs. The default is
                                             set to "Server". This is done with a comma separated key = value list.
                                               key: defines a hosts/VM name as regex
                                               value: defines the NetBox role name (use quotes if name contains commas)
                                             """,
                                             config_example=".* = Server"),
                                ConfigOption("vm_role_relation", str, config_example=".* = Server"),
                                ConfigOption("cluster_tag_relation",
                                             str,
                                             description="""
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
                         description="""
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
                         description="""
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
                              description="""
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
                              config_example="vm_tag_source = object, cluster",
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
                              description="""
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
            ConfigOption("disable_vlan_sync",
                         bool,
                         description="disables syncing of any VLANs visible in vCenter to NetBox",
                         default_value=False),
            ConfigOption("track_vm_host",
                         bool,
                         description="""enabling this option will add the ESXi host
                         this VM is running on to the VM details""",
                         default_value=False),
            ConfigOption("overwrite_device_interface_name",
                         bool,
                         description="""define if the name of the device interface discovered overwrites the
                         interface name in NetBox. The interface will only be matched by identical MAC address""",
                         default_value=False),
            ConfigOption("overwrite_vm_interface_name",
                         bool,
                         description="""define if the name of the VM interface discovered overwrites the
                         interface name in NetBox. The interface will only be matched by identical MAC address""",
                         default_value=False),
            ConfigOption("host_management_interface_match",
                         str,
                         description="""set a matching value for ESXi host management interface description
                         (case insensitive, comma separated). Used to figure out the ESXi primary IP address""",
                         default_value="management, mgmt"),
            ConfigOption("ip_tenant_inheritance_order",
                         str,
                         description="""
                         define in which order the IP address tenant will be assigned if tenant is undefined.
                         possible values:
                           * device : host or VM tenant will be assigned to the IP address
                           * prefix : if the IP address belongs to an existing prefix and this prefix has a tenant assigned, then this one is used
                           * disabled : no tenant assignment to the IP address will be performed
                         the order of the definition is important, the default is "device, prefix" which means:
                         If the device has a tenant then this one will be used. If not, the prefix tenant will be used if defined
                         """,
                         default_value="device, prefix"
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
                         deprecation_message="You need to switch to 'host_tag_source', 'vm_tag_source' or 'cluster_tag_source",
                         removed=True),
            ConfigOption("sync_parent_tags",
                         bool,
                         deprecation_message="You need to switch to 'host_tag_source', 'vm_tag_source' or 'cluster_tag_source",
                         removed=True)
        ]

        super().__init__()

    def validate_options(self):
        pass

#        for option in self.options:
#
#            pass
