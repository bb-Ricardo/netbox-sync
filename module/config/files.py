# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.


class ConfigFileINI:
    suffixes = [
        "ini"
    ]
    comment_prefix = ";"


class ConfigFileYAML:
    suffixes = [
        "yml",
        "yaml"
    ]
    comment_prefix = "#"


class ConfigFile:

    supported_config_file_types = [
        ConfigFileINI,
        ConfigFileYAML
    ]

    @classmethod
    def get_file_type(cls, config_file_name: str):

        suffix = cls.get_suffix(config_file_name)

        if suffix is None:
            return

        for possible_file_type in cls.supported_config_file_types:
            if suffix in possible_file_type.suffixes:
                return possible_file_type

    @classmethod
    def get_suffix(cls, config_file_name):

        if not isinstance(config_file_name, str):
            return

        return config_file_name.lower().split(".")[-1]
