# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2023 Ricardo Bartels. All rights reserved.
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
from module.common.logging import get_logger
from module.common.support import normalize_mac_address

log = get_logger()


class OVirtConfig(ConfigBase):

    section_name = source_config_section_name
    source_name = None
    source_name_example = "my-ovirt-example"

    def __init__(self):
        self.options = [
            ConfigOption(**config_option_enabled_definition),

            ConfigOption(**{**config_option_type_definition, "config_example": "ovirt"}),

            ConfigOption("url",
                         str,
                         description="host name / IP address of the oVirt API",
                         config_example="https://engine40.example.com/ovirt-engine/api",
                         mandatory=True),

            ConfigOption("username",
                         str,
                         description="username to use to log into oVirt",
                         config_example="ovirtuser",
                         mandatory=True),

            ConfigOption("password",
                         str,
                         description="password to use to log into oVirt",
                         config_example="supersecret",
                         sensitive=True,
                         mandatory=True),

            ConfigOption("ca_file",
                         str,
                         description="path to the CA file for oVirt",
                         config_example="ca.pem"),

            ConfigOption("validate_tls_certs",
                         bool,
                         description="""Enforces TLS certificate validation.
                         If oVirt uses a valid TLS certificate then this option should be set
                         to 'true' to ensure a secure connection.""",
                         default_value=False),

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
                                ConfigOption("vm_platform_relation",
                                             str,
                                             description="""\
                                             This option defines custom platforms if the VMWare created platforms are not suitable.
                                             Pretty much a mapping of VMWare platform name to your own platform name.
                                             This is done with a comma separated key = value list.
                                               key: defines a VMWare returned platform name
                                               value: defines the desired NetBox platform name""",
                                             config_example="centos-7.* = centos7, microsoft-windows-server-2016.* = Windows2016"),
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
                         description="do not set notes to the UUID or name of the VM",
                         default_value=False),
            ConfigOption("skip_vm_platform",
                         bool,
                         description="do not sync flavors from a VM in Openstack to the comments field on a VM in netbox",
                         default_value=False),
            ConfigOption("strip_host_domain_name",
                         bool,
                         description="strip domain part from host name before syncing device to NetBox",
                         default_value=False),
            ConfigOption("strip_vm_domain_name",
                         bool,
                         description="strip domain part from VM name before syncing VM to NetBox",
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
                                             config_example="uuid"),
                                ConfigOption("vm_custom_object_attributes",
                                             str,
                                             config_example="uuid")
                              ]),
            ConfigOption("set_source_name_as_cluster_group",
                         bool,
                         description="""this will set the sources name as cluster group name instead of the datacenter.
                         This works if the oVirt CP has ONLY ONE datacenter configured.
                         Otherwise it will rename all datacenters to the source name!""",
                         default_value=False),
            ConfigOption("set_vm_name_to_uuid",
                         bool,
                         description="Set the name in Netbox to the VM UUID instead of name",
                         default_value=False),

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

        for option in self.options:

            if option.value is None:
                continue

            if "filter" in option.key:

                re_compiled = None
                try:
                    re_compiled = re.compile(option.value)
                except Exception as e:
                    log.error(f"Problem parsing regular expression for '{self.source_name}.{option.key}': {e}")
                    self.set_validation_failed()

                option.set_value(re_compiled)

                continue

            if "relation" in option.key:

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

        permitted_subnets_option = self.get_option_by_name("permitted_subnets")

        if permitted_subnets_option is not None:
            permitted_subnets = PermittedSubnets(permitted_subnets_option.value)
            if permitted_subnets.validation_failed is True:
                self.set_validation_failed()

            permitted_subnets_option.set_value(permitted_subnets)
