import re

from module.common.logging import get_logger
from module.common.misc import quoted_split
from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption
from module.sources.common.config import (
    config_option_enabled_definition,
    config_option_permitted_subnets_definition,
    config_option_type_definition,
)
from module.sources.common.permitted_subnets import PermittedSubnets

log = get_logger()


class AWSConfig(ConfigBase):
    section_name = source_config_section_name
    source_name_example = "my-aws-example"
    source_name = None

    def __init__(self):
        self.options = [
            ConfigOption(**config_option_enabled_definition),
            ConfigOption(**{**config_option_type_definition, "config_example": "aws"}),
            ConfigOption("regions", str, default_value="auto",
                         description="""Use 'auto' to discover all account-enabled regions, or specify
                         regions to import (comma-separated or a YAML list). Do not mix auto with region names."""),
            ConfigOption("discovery_region", str,
                         description="""Bootstrap region for automatic region discovery. Defaults to the
                         AWS profile/environment region. This selects the API endpoint, not resource placement."""),
            ConfigOption("profile", str,
                         description="AWS SDK profile. Otherwise use the standard credential chain."),
            ConfigOption("account_id", str,
                         description="""Optional expected 12-digit credential account ID.
                         In organization mode this guards the collector account, not member accounts."""),
            ConfigOption("account_name", str,
                         description="Optional friendly account name for NetBox cluster labels in single-account mode."),
            ConfigOption("organization", bool, default_value=False,
                         description="Discover active AWS Organizations accounts and assume an inventory role in each."),
            ConfigOption("organization_role_name", str, default_value="NetBoxInventoryReadOnly",
                         description="Inventory role name (including any path) deployed in each selected account."),
            ConfigOption("organization_external_id", str, sensitive=True,
                         description="Optional external ID required by the member inventory roles' trust policies."),
            ConfigOption("organization_include_management", bool, default_value=False,
                         description="Also scan the organization management account using its inventory role."),
            ConfigOption("organization_account_ids", str,
                         description="""Optional account allowlist (comma-separated or YAML list).
                         Unset selects all active member accounts. Management inclusion is controlled separately."""),
            ConfigOption(**config_option_permitted_subnets_definition),
            ConfigOption("sync_private_ips", bool, default_value=True,
                         description="Import private IPv4 and IPv6 into pre-existing VPC-specific VRFs."),
            ConfigOption("vpc_vrf_relation", str,
                         description="""Comma-separated region/vpc-id = NetBox VRF name mappings.
                         Use account-id/region/vpc-id keys for account-specific mappings in organization mode.
                         Unmapped VPCs require a VRF named AWS <account-id>/<region>/<vpc-id>.
                         Each VPC must have a distinct VRF; global address space is never used.""",
                         config_example="us-east-1/vpc-0123456789abcdef0 = Production VPC"),
            ConfigOption("tag_keys", str,
                         description="AWS tag keys to copy as namespaced NetBox tags; unset imports none."),
            ConfigOption("tenant", str, description="Optional NetBox tenant for AWS VMs and IPs."),
            ConfigOption("dns_name_lookup", bool, default_value=False,
                         description="Perform reverse DNS lookups for imported private IPs."),
        ]
        super().__init__()

    def validate_options(self):
        for key in ("profile", "tenant", "discovery_region", "account_name",
                    "organization_role_name", "organization_external_id"):
            value = self.get_option_by_name(key).value
            if value is not None and not isinstance(value, str):
                log.error(f"AWS '{self.source_name}' option '{key}' must be a string.")
                self.set_validation_failed()

        role = self.get_option_by_name("organization_role_name").value
        if isinstance(role, str) and (
                len(role) > 512 or not re.fullmatch(r"(?:[A-Za-z0-9_+=,.@-]+/)*[A-Za-z0-9_+=,.@-]{1,64}", role)):
            log.error(f"AWS '{self.source_name}' organization_role_name must be a role name with an optional path.")
            self.set_validation_failed()
        external_id = self.get_option_by_name("organization_external_id").value
        if isinstance(external_id, str) and not re.fullmatch(r"[A-Za-z0-9_+=,.@:/-]{2,1224}", external_id):
            log.error(f"AWS '{self.source_name}' organization_external_id has an invalid format.")
            self.set_validation_failed()

        for key in ("regions", "tag_keys", "organization_account_ids"):
            option = self.get_option_by_name(key)
            value = option.value
            if value is not None and not isinstance(value, (str, list)):
                log.error(f"AWS '{self.source_name}' option '{key}' must be a string or list.")
                self.set_validation_failed()
                continue
            entries = value if isinstance(value, list) else quoted_split(value) if value else []
            if any(not isinstance(entry, str) or not entry.strip() for entry in entries):
                log.error(f"AWS '{self.source_name}' option '{key}' contains an invalid entry.")
                self.set_validation_failed()
                continue
            entries = list(dict.fromkeys(entry.strip() for entry in entries))
            if key == "regions" and not entries:
                log.error(f"AWS '{self.source_name}' requires 'auto' or at least one explicit region.")
                self.set_validation_failed()
            if key == "regions" and "auto" in entries and entries != ["auto"]:
                log.error(f"AWS '{self.source_name}' cannot mix 'auto' with explicit regions.")
                self.set_validation_failed()
            if key == "organization_account_ids" and any(
                    not re.fullmatch(r"[0-9]{12}", entry) for entry in entries):
                log.error(f"AWS '{self.source_name}' organization_account_ids must contain 12-digit strings.")
                self.set_validation_failed()
            if key == "organization_account_ids" and value is not None and not entries:
                log.error(f"AWS '{self.source_name}' organization_account_ids must be unset or nonempty.")
                self.set_validation_failed()
            option.set_value(entries)

        if not self.get_option_by_name("organization").value and (
                self.get_option_by_name("organization_account_ids").value or
                self.get_option_by_name("organization_external_id").value or
                self.get_option_by_name("organization_include_management").value):
            log.error(f"AWS '{self.source_name}' organization options require organization = True.")
            self.set_validation_failed()

        account = self.get_option_by_name("account_id").value
        if account is not None and (not isinstance(account, str) or len(account) != 12 or not account.isdigit()):
            log.error(f"AWS '{self.source_name}' account_id must be a 12-digit string.")
            self.set_validation_failed()

        relation = self.get_option_by_name("vpc_vrf_relation")
        mappings = {}
        relation_value = relation.value
        if relation_value is not None and not isinstance(relation_value, str):
            log.error(f"AWS '{self.source_name}' vpc_vrf_relation must be a string.")
            self.set_validation_failed()
            relation_value = None
        for entry in quoted_split(relation_value) if relation_value else []:
            key, separator, value = entry.partition("=")
            key, value = key.strip(' "\''), value.strip(' "\'')
            parts = key.split("/")
            if not separator or len(parts) not in (2, 3) or not all(parts) or not value or key in mappings or (
                    len(parts) == 3 and not re.fullmatch(r"[0-9]{12}", parts[0])):
                log.error(f"Malformed or duplicate AWS VPC/VRF mapping '{entry}'.")
                self.set_validation_failed()
                continue
            mappings[key] = value
        relation.set_value(mappings)

        permitted = self.get_option_by_name("permitted_subnets")
        subnets = PermittedSubnets(permitted.value)
        if subnets.validation_failed:
            self.set_validation_failed()
        permitted.set_value(subnets)
