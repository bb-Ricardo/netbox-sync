# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

from textwrap import fill, indent, dedent

default_output_width = 90


class DescriptionFormatterMixin:

    _description = ""

    def description(self, width: int = default_output_width) -> str:
        """
        return description as a string wrapped at 'width'

        SPECIAL: if self._description starts with a blank character,
                 the description will be strip of indentation and NO line wrapping will be applied.

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
            return dedent(self._description.rstrip())
        else:
            return fill(" ".join(self._description.split()), width=width)

    def config_description(self, prefix: str = "#", width: int = default_output_width) -> str:
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

        return indent(self.description(width), prefix, lambda line: True)
