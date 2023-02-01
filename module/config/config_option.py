# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from typing import Any
from textwrap import wrap, fill, indent


class ConfigOption:

    def __init__(self,
                 key: str,
                 value_type: Any,
                 description: str = "",
                 default_value: Any = None,
                 config_example: Any = None,
                 mandatory: bool = False,
                 alt_key: str = None,
                 deprecated: bool = False,
                 deprecation_message: str = None):

        self.key = key
        self.value_type = value_type
        self._description = description
        self.default_value = default_value
        self.config_example = config_example
        self.mandatory = mandatory
        self.alt_key = alt_key
        self.deprecated = deprecated
        self.deprecation_message = deprecation_message

        if self.config_example is None:
            self.config_example = self.default_value

        if not isinstance(self._description, str):
            raise ValueError(f"value for 'description' of '{self.key}' must be of type str")

        if len(self._description) == 0:
            raise ValueError(f"value for 'description' of '{self.key}' can't be empty")

        if self.config_example is not None and not isinstance(self.config_example, self.value_type):
            raise ValueError(f"value for 'config_example' of '{self.key}' must be of '{self.value_type}'")

    def description(self, width: int = 80) -> str:

        if not isinstance(width, int):
            raise ValueError("value for 'width' must be of type int")

        return fill(" ".join(wrap(self._description)), width=width)

    def config_description(self, prefix: str = "#", width: int = 80) -> str:

        if not isinstance(width, int):
            raise ValueError("value for 'width' must be of type int")

        if not isinstance(prefix, str):
            raise ValueError("value for 'prefix' must be of type str")

        prefix += " "

        if width - len(prefix) < 3:
            width = 3
        else:
            width = width - len(prefix)

        return indent(self.description(width), prefix)
