from module.config import source_config_section_name
from module.config.base import ConfigBase
from module.config.option import ConfigOption


class HetznerConfig(ConfigBase):

    section_name = source_config_section_name

    def __init__(self):
        self.options = [

            ConfigOption(
                "enabled",
                bool,
                default_value=True,
                description="Enable or disable the Hetzner Cloud source."
            ),

            ConfigOption(
                "type",
                str,
                default_value="hetzner",
                description="Source type identifier. Must remain 'hetzner'."
            ),

            ConfigOption(
                "api_token",
                str,
                mandatory=True,
                description="Hetzner Cloud API token used to authenticate against the Hetzner Cloud API."
            ),
        ]

        super().__init__()