# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import sys
import re


def grab(structure=None, path=None, separator=".", fallback=None):
    """
        get data from a complex object/json structure with a
        "." separated path information. If a part of a path
        is not present then this function returns the
        value of fallback (default: "None").

        example structure:
            data_structure = {
              "rows": [{
                "elements": [{
                  "distance": {
                    "text": "94.6 mi",
                    "value": 152193
                  },
                  "status": "OK"
                }]
              }]
            }
        example path:
            "rows.0.elements.0.distance.value"
        example return value:
            15193

        Parameters
        ----------
        structure: dict, list, object
            an object structure to extract data from
        path: str
            nested path to extract
        separator: str
            path separator to use. Helpful if a path element
            contains the default (.) separator.
        fallback: dict, list, str, int
            data to return if no match was found

        Returns
        -------
        str, dict, list
            the desired path element if found, otherwise None
    """

    max_recursion_level = 100

    current_level = 0
    levels = len(path.split(separator))

    if structure is None or path is None:
        return fallback

    # noinspection PyBroadException
    def traverse(r_structure, r_path):
        nonlocal current_level
        current_level += 1

        if current_level > max_recursion_level:
            return fallback

        for attribute in r_path.split(separator):
            if isinstance(r_structure, dict):
                r_structure = {k.lower(): v for k, v in r_structure.items()}

            try:
                if isinstance(r_structure, list):
                    data = r_structure[int(attribute)]
                elif isinstance(r_structure, dict):
                    data = r_structure.get(attribute.lower())
                else:
                    data = getattr(r_structure, attribute)

            except Exception:
                return fallback

            if current_level == levels:
                return data if data is not None else fallback
            else:
                return traverse(data, separator.join(r_path.split(separator)[1:]))

    return traverse(structure, path)


def dump(obj):
    """
    Dump content of a object to stdout

    Parameters
    ----------
    obj: object
        object to dump

    """

    for attr in dir(obj):
        if hasattr(obj, attr):
            print("obj.%s = %s" % (attr, getattr(obj, attr)))


def do_error_exit(log_text):
    """
    log an error and exit with return code 1

    Parameters
    ----------
    log_text : str
        the text to log as error
    """

    print(f"ERROR: {log_text}", file=sys.stderr)
    exit(1)


def get_relative_time(delta):
    """
    https://stackoverflow.com/a/13756038
    return a human readable string of a datetime object delta

    Parameters
    ----------
    delta:  datetime delta
        time delta to format

    Returns
    -------
    str: formatted string of time delta
    """

    seconds = int(delta.total_seconds())
    return_string = list()

    periods = [
        ('day', 60 * 60 * 24),
        ('hour', 60 * 60),
        ('minute', 60),
        ('second', 1)
    ]

    for period_name, period_seconds in periods:
        if seconds >= period_seconds:
            period_value, seconds = divmod(seconds, period_seconds)
            return_string.append(f"{period_value} {period_name}{plural(period_value)}")

    return ", ".join(return_string)


def get_string_or_none(text=None):
    """
    Only return stripped content of text if text is not None and not empty

    Parameters
    ----------
    text: str
        string to parse

    Returns
    -------
    (str, None): content of text
    """

    if text is not None and len(str(text).strip()) > 0:
        return str(text).strip()

    return None


def plural(length):
    """
    return "s" if length is not 1 else return empty string

    example:
    0 items
    1 item
    2 items

    Parameters
    ----------
    length: int
        length of item list

    Returns
    -------
    str: "s", ""
    """

    return "s" if length != 1 else ""


def quoted_split(string_to_split):
    """
        Splits a comma separated string into a list.
        It obeys quoted parts which could contain a comma as well.

        thanks to: https://stackoverflow.com/a/64333329

        Parameters
        ----------
        string_to_split: str
            the string to split

        Returns
        -------
        list
            of separated string parts
    """

    return_data = list()

    if not isinstance(string_to_split, str):
        return return_data

    for part in re.split(r",(?=(?:[^\"']*[\"'][^\"']*[\"'])*[^\"']*$)", string_to_split):
        return_data.append(part.strip(' "\''))

    return return_data

# EOF
