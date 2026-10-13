# -*- coding: utf-8 -*-
# Licensed under the MIT license.
from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption
from module.common.misc import quoted_split
from module.common.logging import get_logger
import re

log = get_logger()

class VMmanagerConfig(ConfigBase):
    section_name = source_config_section_name
    source_name_example = "my-vmmanager-example"

    def __init__(self):
        self.options = [
            ConfigOption("enabled", bool, default_value=True, description="Enable or disable this VMmanager source."),
            ConfigOption("type", str, default_value="vmmanager", description="Source type identifier. Must remain 'vmmanager'."),
            ConfigOption("api_url", str, mandatory=True, description="Base URL, e.g. https://vmmanager.example.org."),
            ConfigOption("email", str, mandatory=True, description="Email of the read-only VMmanager API account."),
            ConfigOption("password", str, mandatory=True, sensitive=True, description="Password of the read-only VMmanager API account."),
            ConfigOption("site_name", str, default_value="VMmanager", description="NetBox site name for imported VMs."),
            ConfigOption("cluster_name", str, default_value=None, description="Optional NetBox cluster name; defaults to VMmanager: <source name>."),
            ConfigOption("cluster_type_name", str, default_value="VMmanager", description="NetBox cluster type name."),
            ConfigOption("sync_clusters", bool, default_value=True, description="Import VMmanager clusters into NetBox."),
            ConfigOption("sync_nodes", bool, default_value=True, description="Import VMmanager hypervisor nodes as NetBox devices."),
            ConfigOption("exclude_dummy_clusters", bool, default_value=False, description="Skip VMmanager clusters whose virtualization_type is 'dummy'."),
            ConfigOption("hypervisor_device_type", str, default_value="VMmanager Hypervisor", description="NetBox device type model for imported hypervisor nodes."),
            ConfigOption("hypervisor_device_role", str, default_value="Hypervisor", description="NetBox device role for imported hypervisor nodes."),
            ConfigOption("vm_tenant_relation", str, description="Regex mapping from VM name to an existing NetBox tenant, e.g. .* = mz."),
            ConfigOption("vm_tag_relation", str, description="Regex mapping from VM name to NetBox tag(s), e.g. .* = mz. Separate multiple mappings with commas."),
            ConfigOption("verify_ssl", bool, default_value=True, description="Verify the VMmanager HTTPS certificate."),
            ConfigOption("timeout", int, default_value=30, description="HTTP timeout in seconds."),
        ]
        super().__init__()

    def validate_options(self):
        """Parse regex relations using the same `pattern = value` syntax as VMware."""
        for option in self.options:
            if option.key not in ("vm_tenant_relation", "vm_tag_relation") or not option.value:
                continue

            parsed = []
            for relation in quoted_split(option.value):
                if "=" not in relation:
                    log.error("Malformed relation '%s' in '%s.%s'", relation, self.source_name, option.key)
                    self.set_validation_failed()
                    continue
                object_regex, assigned_name = relation.split("=", 1)
                object_regex = object_regex.strip(' \"')
                assigned_name = assigned_name.strip(' \"')
                if not object_regex or not assigned_name:
                    log.error("Malformed relation '%s' in '%s.%s'", relation, self.source_name, option.key)
                    self.set_validation_failed()
                    continue
                try:
                    compiled = re.compile(object_regex)
                except re.error as exc:
                    log.error("Invalid regex '%s' in '%s.%s': %s", object_regex, self.source_name, option.key, exc)
                    self.set_validation_failed()
                    continue
                parsed.append({"object_regex": compiled, "assigned_name": assigned_name})
            option.set_value(parsed)
