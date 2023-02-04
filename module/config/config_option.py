# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from typing import Any
from textwrap import wrap, fill, indent, dedent

from module.common.logging import get_logger

log = get_logger()


class ConfigOption:
    """
    handles all attributes of a single config option
    """

    def __init__(self,
                 key: str,
                 value_type: Any,
                 description: str = "",
                 default_value: Any = None,
                 config_example: Any = None,
                 mandatory: bool = False,
                 alt_key: str = None,
                 deprecated: bool = False,
                 deprecation_message: str = "",
                 removed: bool = False,
                 sensitive: bool = False):

        self.key = key
        self._value = None
        self.value_type = value_type
        self._description = description
        self.default_value = default_value
        self.config_example = config_example
        self.mandatory = mandatory
        self.alt_key = alt_key
        self.deprecated = deprecated
        self.deprecation_message = deprecation_message
        self.removed = removed
        self.sensitive = sensitive

        if self.config_example is None:
            self.config_example = self.default_value

        if self.default_value is not None:
            self.set_value(self.default_value)

        if not isinstance(self._description, str):
            raise ValueError(f"value for 'description' of '{self.key}' must be of type str")

        if not isinstance(self.deprecation_message, str):
            raise ValueError(f"value for 'deprecation_message' of '{self.key}' must be of type str")

        if len(self._description) == 0:
            raise ValueError(f"value for 'description' of '{self.key}' can't be empty")

        if self.config_example is not None and not isinstance(self.config_example, self.value_type):
            raise ValueError(f"value for 'config_example' of '{self.key}' must be of '{self.value_type}'")

    def __repr__(self):
        return f"{self.key}: {self._value}"

    @property
    def value(self):
        return self._value

    @property
    def sensitive_value(self):

        if self.sensitive is True:
            return self._value[0:3] + "***"

        return self._value

    def set_value(self, value):

        if value is None:
            return

        if self.value_type == bool:
            try:
                config_value = self.to_bool(value)
            except ValueError:
                log.error(f"Unable to parse '{value}' for '{self.key}' as bool")
                return

        elif self.value_type == int:
            try:
                config_value = int(value)
            except ValueError:
                log.error(f"Unable to parse '{value}' for '{self.key}' as int")
                return
        else:
            config_value = value

        self._value = config_value

    @staticmethod
    def to_bool(value):
        """
            converts a string to a boolean
        """
        valid = {
             'true': True, 't': True, '1': True, 'yes': True,
             'false': False, 'f': False, '0': False, 'no': False
             }

        if isinstance(value, bool):
            return value

        elif isinstance(value, str):
            if value.lower() in valid:
                return valid[value.lower()]

        raise ValueError

    def description(self, width: int = 80) -> str:
        """
        return description as a string wrapped at 'width'

        SPECIAL: if self._description starts with a blank character,
                 the description will be dedented and NO line wrapping will be applied.

        Parameters
        ----------
        width: int
            max characters per line

        Returns
        -------
        str: single string containing new line characters
        """

        if not isinstance(width, int):
            raise ValueError("value for 'width' must be of type int")

        if self._description.startswith(" "):
            return dedent(self._description)
        else:
            return fill(" ".join(wrap(self._description)), width=width)

    def config_description(self, prefix: str = "#", width: int = 80) -> str:
        """
        prefixes each description line with one or more 'prefix' characters
        and a blank between prefix and description line text

        Parameters
        ----------
        prefix: str
            string to prefix each line with
        width: int
            max characters per line

        Returns
        -------
        str: single string containing new line characters
        """

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
