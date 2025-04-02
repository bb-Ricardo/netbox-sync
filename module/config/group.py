# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from module.config.option import ConfigOption
from module.config.formatter import DescriptionFormatterMixin


class ConfigOptionGroup(DescriptionFormatterMixin):

    def __init__(self,
                 title: str = "",
                 description: str = "",
                 config_example: str = "",
                 options: list = None):

        self.title = title
        self._description = description
        self.config_example = config_example
        self.options = options

        if not isinstance(self.options, list):
            raise AttributeError("parameter options is not a list of config options")
        else:
            for option in self.options:
                if not isinstance(option, ConfigOption):
                    raise AttributeError(f"option {option} needs to be of type {ConfigOption.__class__.__name__}")
