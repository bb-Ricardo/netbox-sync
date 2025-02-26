# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import os

from module.config.formatter import DescriptionFormatterMixin
from module.config.group import ConfigOptionGroup
from module.config.option import ConfigOption
from module.common.config import CommonConfig
from module.netbox.config import NetBoxConfig
from module.sources.vmware.config import VMWareConfig
from module.sources.check_redfish.config import CheckRedfishConfig
from module.common.logging import get_logger
from module.config import default_config_file_path, source_config_section_name
from module.config.files import ConfigFile, ConfigFileINI, ConfigFileYAML
from module import __version__, __version_date__, __description__, __url__

log = get_logger()


class ConfigFileOutput(DescriptionFormatterMixin):

    base_config_list = [
        CommonConfig,
        NetBoxConfig
    ]

    source_config_list = [
        VMWareConfig,
        CheckRedfishConfig
    ]

    header = f"Welcome to the {__description__} configuration file."

    _description = """The values in this file override the default values used by the system if
                      a config option is not specified. The commented out lines are the configuration
                      field and the default value used. Uncommenting a line and changing the value
                      will change the value used at runtime when the process is restarted.
                      """

    source_description = """Controls the parameters of a defined source. The string past the slash
                         will be used as a sources name. Sources can be defined multiple times to
                         represent different sources.
                         """

    config_file_type = None
    indentation_level = 0
    lines = list()

    def __init__(self, args):

        if args is None or args.generate_config is False:
            return

        if len(args.config_files) == 0:
            self.output_file = default_config_file_path
        else:
            self.output_file = args.config_files[0]

        if os.path.exists(self.output_file):
            log.error(f'ERROR: Config file "{self.output_file}" already present')
            exit(1)

        self.config_file_type = ConfigFile.get_file_type(self.output_file)

        if self.config_file_type is None:
            log.error(f"ERROR: Unknown/Unsupported config file type "
                      f"'{ConfigFile.get_suffix(self.output_file)}' for {self.output_file}")
            exit(1)

        self.comment_prefix = self.config_file_type.comment_prefix

        self.format()

        self._set_indent_level(0)
        self._add_blank_line()
        self._add_line(f"{self.comment_prefix}EOF")
        self._add_blank_line()

        self.lines = [x.rstrip() for x in self.lines]

        try:
            with open(self.output_file, "w") as fp:
                fp.write('\n'.join(self.lines))
        except Exception as e:
            log.error(f"Error: Unable to write to file '{self.output_file}': {e}")
            exit(1)

        exit(0)

    def format(self):

        self._add_line(f"{self.comment_prefix*3} {self.header}\n")
        self._add_line(f"{self.comment_prefix*3} Version: {__version__} ({__version_date__})\n")
        self._add_line(f"{self.comment_prefix * 3} Project URL: {__url__}\n")
        self._add_blank_line()
        self._add_lines(self.config_description(prefix=self.comment_prefix).split("\n"))
        self._add_blank_line()

        for config_section in self.base_config_list:

            config_instance = config_section()

            self._format_section_description(config_instance)
            self._add_blank_line()

            if self.config_file_type is ConfigFileINI:
                self._add_line(f"[{config_instance.section_name}]")

            elif self.config_file_type is ConfigFileYAML:
                self._set_indent_level(0)
                self._add_line(f"{config_section.section_name}:")

            self._set_indent_level(1)
            self._format_options(config_instance.options)
            self._set_indent_level(0)

        # write out section description
        self._format_section_description(section_name="source/*", section_description=self.source_description)

        self._add_blank_line()

        if self.config_file_type is ConfigFileYAML:
            self._add_line(f"{source_config_section_name}:")

        for config_section in self.source_config_list:

            config_instance = config_section()

            self._format_section_description(config_instance)
            self._add_blank_line()

            if self.config_file_type is ConfigFileINI:
                self._add_line(f"[{config_instance.section_name}/{config_instance.source_name_example}]")

            elif self.config_file_type is ConfigFileYAML:
                self._set_indent_level(1)
                self._add_line(f"{config_instance.source_name_example}:")

            self._set_indent_level(2)
            self._format_options(config_instance.options)
            self._set_indent_level(1)

    def _add_line(self, line: str):
        indent = ""
        indent_size = 2

        if self.config_file_type is ConfigFileYAML:
            indent = " " * indent_size * self.indentation_level

        self.lines.append(f"{indent}{line}")

    def _add_lines(self, lines: list):
        for line in lines:
            self._add_line(line)

    def _set_indent_level(self, level: int):
        self.indentation_level = level

    def _format_options(self, option_list: list):

        for option in option_list:

            if isinstance(option, ConfigOption):
                self._format_config_option(option)
            elif isinstance(option, ConfigOptionGroup):
                self._format_config_option_group(option)

        self._add_blank_line()

    def _format_section_description(self,
                                    config_instance=None,
                                    section_name: str = None,
                                    section_description: str = None):

        wide_prefix = self.comment_prefix * 3

        if config_instance is not None:
            if section_description is None:
                section_description = config_instance.__doc__
            if section_name is None:
                section_name = f"{config_instance.section_name}"

        if section_description is not None and section_name is not None:
            section_formatter = DescriptionFormatterMixin()
            section_formatter._description = section_description

            self._add_blank_line()
            self._add_line(wide_prefix)
            self._add_line(f"{wide_prefix} [{section_name}]")
            self._add_line(wide_prefix)
            self._add_lines(section_formatter.config_description(prefix=wide_prefix).split("\n"))
            self._add_line(wide_prefix)

    def _add_blank_line(self):
        if len(self.lines) > 0 and self.lines[-1] != "":
            self.lines.append("")

    def _format_config_option(self, option):

        if option is None or option.removed is True or option.deprecated is True:
            return

        if len(option.description()) > 0:
            self._add_blank_line()
            self._add_lines(option.config_description(prefix=self.comment_prefix).split("\n"))

        option_key = option.key
        option_value = None
        if option.mandatory is False:
            option_key = f"{self.comment_prefix}{option_key}"
        if option_value is None and option.default_value is not None:
            option_value = option.default_value
        if option_value is None and option.config_example is not None:
            option_value = option.config_example
        if option_value is None:
            option_value = ""

        if self.config_file_type is ConfigFileINI:
            self._add_line(f"{option_key} = {option_value}")
        elif self.config_file_type is ConfigFileYAML:
            self._add_line(f"{option_key}: {option_value}")

    def _format_config_option_group(self, group):

        if group is None:
            return

        if isinstance(group.title, str) and len(group.title) > 0:
            self._add_blank_line()
            self._add_line(f"{self.comment_prefix} {group.title} options")

        if len(group.description()) > 0:
            self._add_blank_line()
            self._add_lines(group.config_description(prefix=self.comment_prefix).split("\n"))

        if isinstance(group.config_example, str) and len(group.config_example) > 0:
            formatter = DescriptionFormatterMixin()
            formatter._description = group.config_example
            self._add_line(self.comment_prefix)
            self._add_lines(formatter.config_description(prefix=self.comment_prefix).split("\n"))

        for option in group.options:
            self._format_config_option(option)
