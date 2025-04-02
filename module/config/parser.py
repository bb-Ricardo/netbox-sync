# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.


import os
import configparser
from typing import Dict
import yaml

from module.common.logging import get_logger
from module.common.misc import grab, do_error_exit
from module.config import *
from module.config.files import ConfigFile, ConfigFileINI, ConfigFileYAML


log = get_logger()


class ConfigParser:
    """
    Singleton class to parse a given list of config files
    """

    file_list = list()
    content = dict()
    config_errors = list()
    config_warnings = list()
    parsing_finished = False

    def __new__(cls):
        it = cls.__dict__.get("__it__")
        if it is not None:
            return it
        cls.__it__ = it = object.__new__(cls)
        it.init()
        return it

    def init(self) -> None:
        pass

    def add_config_file(self, config_file_name: str) -> None:

        if isinstance(config_file_name, str) and len(config_file_name) > 0:
            self.file_list.append(self.get_config_file_path(config_file_name))

    def add_config_file_list(self, config_file_name_list: list) -> None:

        if not isinstance(config_file_name_list, list):
            return

        for config_file_name in config_file_name_list:
            self.add_config_file(config_file_name)

    def log_end_exit_on_errors(self) -> None:

        for error in self.config_errors:
            log.error(error)

        for warning in self.config_warnings:
            log.warning(warning)

        if len(self.config_errors) > 0:
            do_error_exit("Unable to open/parse one or more config files")

    def _add_error(self, message: str = "") -> None:

        if isinstance(message, str) and len(message) > 0:
            self.config_errors.append(message)

    def _add_warning(self, message: str = "") -> None:

        if isinstance(message, str) and len(message) > 0:
            self.config_warnings.append(message)

    def read_config(self) -> None:
        """
        Read config files in the given order
        """

        if self.parsing_finished is True:
            return

        default_config_file = self.get_config_file_path(default_config_file_path)

        # check if default config file actually exists
        # and add it to the list of files to parse
        if os.path.exists(default_config_file) and len(self.file_list) == 0:
            self.file_list.append(default_config_file)

        # check if config file exists
        for f in self.file_list:

            # check if file exists
            if not os.path.exists(f):
                self._add_error(f'Config file "{f}" not found')
                self.file_list.remove(f)
                continue

            # check if it's an actual file
            if not os.path.isfile(f):
                self._add_error(f'Config file "{f}" is not an actual file')
                self.file_list.remove(f)
                continue

            # check if config file is readable
            if not os.access(f, os.R_OK):
                self._add_error(f'Config file "{f}" not readable')
                self.file_list.remove(f)
                continue

        for config_file in self.file_list:

            config_file_type = ConfigFile.get_file_type(config_file)

            if config_file_type is None:
                self._add_error(f"Unknown/Unsupported config file type "
                                f"'{ConfigFile.get_suffix(config_file)}' for {config_file}")
                continue

            if config_file_type == ConfigFileINI:
                config_data = self._parse_ini(config_file)
            elif config_file_type == ConfigFileYAML:
                config_data = self._parse_yaml(config_file)
            else:
                continue

            self._add_config_data(config_data, config_file)

        # parse common and netbox config from env
        for section in [common_config_section_name, netbox_config_section_name]:
            env_config_data = self._parse_section_env_vars(section)
            self._add_config_data(env_config_data)

        # parse source data from env
        env_config_data = self._parse_source_env_vars()
        self._add_config_data(env_config_data)

        self.parsing_finished = True

    def _add_config_data(self, config_data: dict, config_file: str = "") -> None:

        if not isinstance(config_data, dict):
            self._add_error(f"Parsed config data from file '{config_file}' is not a dictionary")
            return

        for section, section_data in config_data.items():

            if section == source_config_section_name:
                if not isinstance(section_data, dict):
                    self._add_error(f"Parsed config data from file '{config_file}' for '{section}' is not a dictionary")
                    continue

                if self.content.get(section) is None:
                    self.content[section] = dict()

                for source_name, source_data in section_data.items():

                    current_data = grab(self.content, f"{section}|{source_name}", separator="|")

                    if current_data is None:
                        # source_name needs to be a string
                        self.content[section][str(source_name)] = source_data
                    else:
                        for key, value in source_data.items():
                            current_data[key] = value
            else:

                if section_data is None:
                    continue

                if not isinstance(section_data, dict):
                    self._add_error(f"Parsed config data from file '{config_file}' for '{section}' is not a dictionary")
                    continue

                if self.content.get(section) is None:
                    self.content[section] = dict()
                for key, value in section_data.items():
                    self.content[section][str(key)] = value

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

    def _parse_ini(self, config_file: str = "") -> Dict:

        if not isinstance(config_file, str):
            raise ValueError("value for 'config_file' of 'parse_ini' must be of type str")

        if len(config_file) == 0:
            raise ValueError(f"value for 'config_file' can't be empty")

        # setup config parser and read config
        config_handler = configparser.ConfigParser(strict=True, allow_no_value=True,
                                                   empty_lines_in_values=False, interpolation=None)

        return_data = {
            source_config_section_name: dict(dict())
        }

        try:
            config_handler.read_file(open(config_file))
        except configparser.Error as e:
            self._add_error(f"Problem while config file '{config_file}' parsing: {e}")
            return return_data
        except Exception as e:
            self._add_error(f"Unable to open file '{config_file}': {e}")
            return return_data

        for section in config_handler.sections():
            source_data = dict(config_handler.items(section))
            if section.startswith(f"{source_config_section_name}/"):
                return_data[source_config_section_name][section.replace(f"{source_config_section_name}/", "")] = \
                    source_data
            else:
                return_data[section] = source_data

        return return_data

    def _parse_yaml(self, config_file: str = "") -> Dict:

        if not isinstance(config_file, str):
            raise ValueError("value for 'config_file' of 'parse_yaml_or_toml' must be of type str")

        if len(config_file) == 0:
            raise ValueError(f"value for 'config_file' can't be empty")

        return_data = dict()

        with open(config_file, "r") as stream:
            try:
                return_data = yaml.safe_load(stream)
            except yaml.YAMLError as e:
                self._add_error(f"Problem while config file '{config_file}' parsing: {e}")
                return return_data
            except Exception as e:
                self._add_error(f"Unable to open file '{config_file}': {e}")
                return return_data

        if isinstance(return_data.get("sources"), dict) and return_data.get(source_config_section_name) is None:
            return_data[source_config_section_name] = return_data.get("sources")
            del return_data["sources"]

        return return_data

    @staticmethod
    def _parse_section_env_vars(section: str) -> Dict:

        return_data = {
            section: dict()
        }

        section_prefix = f"{env_var_prefix}_{section}".upper()
        for key, value in os.environ.items():
            if key.upper().startswith(section_prefix):
                return_data[section][key.replace(f"{section_prefix}_", "", 1).lower()] = value

        return return_data

    def _parse_source_env_vars(self) -> Dict:

        source_indexes = set()
        env_var_list = dict()
        env_var_names = dict()  # keep list of var names to point out possible config errors
        return_data = {
            source_config_section_name: dict(dict())
        }

        # compile dict of relevant env vars and values
        for key, value in os.environ.items():
            if key.upper().startswith(f"{env_var_source_prefix}_"):
                env_var_list[key.upper()] = value
                env_var_names[key.upper()] = key

        for env_var in env_var_list.keys():

            # try to find a var which contains the source name
            if env_var.endswith("_NAME"):
                source_indexes.add(env_var.replace(f"{env_var_source_prefix}_", "", 1).replace("_NAME", "", 1))

        for source_index in source_indexes:

            source_env_config = dict()
            source_prefix = f"{env_var_source_prefix}_{source_index}"

            source_name = env_var_list.get(f"{source_prefix}_NAME")
            if source_name is None:
                continue

            for key, value in env_var_list.items():

                if key != f"{source_prefix}_NAME":
                    source_env_config[key.replace(f"{source_prefix}_", "", 1).lower()] = value

                if key in env_var_names:
                    del env_var_names[key]

            if len(source_env_config) > 0:
                return_data[source_config_section_name][source_name] = source_env_config

        # point out possible env var config mistakes
        for _, key in env_var_names.items():
            self._add_warning(f"Found ENV var '{key}' which cannot be associated with any source due to "
                              f"missing '{env_var_source_prefix}_<index>_NAME' var")

        return return_data
