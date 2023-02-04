# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.


import os
import configparser
from typing import List, Dict
import yaml
import toml

from module.common.logging import get_logger
from module.common.misc import do_error_exit

log = get_logger()


class ConfigFilesParser:
    """
    parses a given list of config files
    """

    names = list()
    content = dict()
    config_file_errors = False

    def __init__(self, config_file_list: List, default_config_file: str = None):
        """
        Read config files in the given order

        Parameters
        ----------
        config_file_list: list
            list of paths of config files to parse
        default_config_file: str
            path to default config file
        """

        self.default_config_file = self.get_config_file_path(default_config_file)

        # check if default config file actually exists
        # and add it to the list of files to parse
        if os.path.exists(self.default_config_file) and len(config_file_list) == 0:
            self.names.append(self.default_config_file)
        else:
            self.names = config_file_list

        # check if config file exists
        for f in self.names:

            f = self.get_config_file_path(f)

            # check if file exists
            if not os.path.exists(f):
                log.error(f'Config file "{f}" not found')
                self.config_file_errors = True
                continue

            # check if it's an actual file
            if not os.path.isfile(f):
                log.error(f'Config file "{f}" is not an actual file')
                self.config_file_errors = True
                continue

            # check if config file is readable
            if not os.access(f, os.R_OK):
                log.error(f'Config file "{f}" not readable')
                self.config_file_errors = True
                continue

        config_file_type_parser_methods = {
            "ini": self.parse_ini,
            "yaml": self.parse_yaml,
            "yml": self.parse_yaml,
            "toml": self.parse_toml
        }

        for config_file in self.names:

            suffix = config_file.lower().split(".")[-1]

            parser_method = config_file_type_parser_methods.get(suffix)

            if parser_method is None:
                log.error(f"Unknown/Unsupported config file type '{suffix}' for {config_file}")
                self.config_file_errors = True
                continue

            # noinspection PyArgumentList
            self.add_config_data(parser_method(config_file=config_file), config_file)

        if self.config_file_errors:
            do_error_exit("Unable to open/parse one or more config files.")

        log.info("Done reading config files")

    def add_config_data(self, config_data: dict, config_file: str) -> None:

        if not isinstance(config_data, dict):
            log.error(f"Parsed config data from file '{config_file}' is not a directory")
            self.config_file_errors = True
            return

        for section, section_data in config_data.items():

            if section == "sources":
                if not isinstance(section_data, list):
                    log.error(f"Parsed config data from file '{config_file}' for '{section}' is not a list")
                    self.config_file_errors = True
                    continue

                if self.content.get(section) is None:
                    self.content[section] = list()

                for source in section_data:

                    current_data = None
                    for current_sources in self.content.get(section):
                        # find source by name
                        if current_sources.get("name") == source.get("name"):
                            current_data = current_sources
                            break

                    if current_data is None:
                        self.content[section].append(source)
                    else:
                        for key, value in source.items():
                            current_data[key] = value
            else:

                if not isinstance(section_data, dict):
                    log.error(f"Parsed config data from file '{config_file}' for '{section}' is not a directory")
                    self.config_file_errors = True
                    continue

                if self.content.get(section) is None:
                    self.content[section] = dict()
                for key, value in section_data.items():
                    self.content[section][key] = value

    @staticmethod
    def get_config_file_path(config_file: str) -> str:
        """
        get absolute path to provided config file string

        Parameters
        ----------
        config_file: str
            config file path

        Returns
        -------
        str: absolute path to config file
        """

        if not isinstance(config_file, str):
            raise ValueError("value for 'config_file' of 'parse_ini' must be of type str")

        if len(config_file) == 0:
            raise ValueError(f"value for 'config_file' can't be empty")

        base_dir = os.sep.join(__file__.split(os.sep)[0:-3])
        if config_file[0] != os.sep:
            config_file = f"{base_dir}{os.sep}{config_file}"

        return os.path.realpath(config_file)

    def parse_ini(self, config_file: str = "") -> Dict:

        if not isinstance(config_file, str):
            raise ValueError("value for 'config_file' of 'parse_ini' must be of type str")

        if len(config_file) == 0:
            raise ValueError(f"value for 'config_file' can't be empty")

        # setup config parser and read config
        config_handler = configparser.ConfigParser(strict=True, allow_no_value=True,
                                                   empty_lines_in_values=False, interpolation=None)

        return_data = dict()

        try:
            config_handler.read_file(open(config_file))
        except configparser.Error as e:
            log.error(f"Problem while config file '{config_file}' parsing: {e}")
            self.config_file_errors = True
            return return_data
        except Exception as e:
            log.error(f"Unable to open file '{config_file}': {e}")
            self.config_file_errors = True
            return return_data

        for section in config_handler.sections():
            if section.startswith("source/"):
                if return_data.get("sources") is None:
                    return_data["sources"] = list()

                source_data = dict(config_handler.items(section))
                source_data["name"] = section.replace("source/", "")
                return_data["sources"].append(source_data)

            else:
                return_data[section] = dict(config_handler.items(section))

        return return_data

    def parse_yaml_or_toml(self, config_file: str = "", config_type: str = "yaml") -> Dict:

        if not isinstance(config_file, str):
            raise ValueError("value for 'config_file' of 'parse_yaml_or_toml' must be of type str")

        if len(config_file) == 0:
            raise ValueError(f"value for 'config_file' can't be empty")

        return_data = dict()
        if config_type == "yaml":
            parser = yaml.safe_load
        elif config_type == "toml":
            parser = toml.load
        else:
            log.error(f"Unknown config type '{config_type}' for config file '{config_file}'.")
            self.config_file_errors = True
            return return_data

        with open(config_file, "r") as stream:
            try:
                return_data = parser(stream)
            except (yaml.YAMLError, toml.TomlDecodeError) as e:
                log.error(f"Problem while config file '{config_file}' parsing: {e}")
                self.config_file_errors = True
                return return_data
            except Exception as e:
                log.error(f"Unable to open file '{config_file}': {e}")
                self.config_file_errors = True
                return return_data

        if isinstance(return_data.get("source"), list) and return_data.get("sources") is None:
            return_data["sources"] = return_data.get("source")
            del return_data["source"]

        return return_data

    def parse_yaml(self, config_file: str = "") -> Dict:
        return self.parse_yaml_or_toml(config_file, "yaml")

    def parse_toml(self, config_file: str = "") -> Dict:
        return self.parse_yaml_or_toml(config_file, "toml")
