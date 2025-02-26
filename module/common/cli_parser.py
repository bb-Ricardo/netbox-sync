# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import os

from argparse import ArgumentParser, RawDescriptionHelpFormatter

from module.common.logging import valid_log_levels
from module.config import default_config_file_path
from module import __version__, __version_date__, __url__


def parse_command_line(self_description=None):
    """
    parse command line arguments, also add current version and version date to description

    Parameters
    ----------
    self_description: str
        short self-description of this program

    Returns
    -------
    ArgumentParser object: with parsed command line arguments
    """

    # define command line options
    description = f"{self_description}\nVersion: {__version__} ({__version_date__})\nProject URL: {__url__}"

    parser = ArgumentParser(
        description=description,
        formatter_class=RawDescriptionHelpFormatter)

    parser.add_argument("-c", "--config", default=[], dest="config_files", nargs='+',
                        help=f"points to the config file to read config data from which is not installed "
                             f"under the default path '{default_config_file_path}'",
                        metavar=os.path.basename(default_config_file_path))

    parser.add_argument("-g", "--generate_config", action="store_true",
                        help="generates default config file.")

    parser.add_argument("-l", "--log_level", choices=valid_log_levels,
                        help="set log level (overrides config)")

    parser.add_argument("-n", "--dry_run", action="store_true",
                        help="Operate as usual but don't change anything in NetBox. Great if you want to test "
                             "and see what would be changed.")

    parser.add_argument("-p", "--purge", action="store_true",
                        help="Remove (almost) all synced objects which were create by this script. "
                             "This is helpful if you want to start fresh or stop using this script.")

    args = parser.parse_args()

    # fix supplied config file path
    fixed_config_files = list()
    for config_file in args.config_files:

        if len(config_file) == 0:
            continue

        if config_file != default_config_file_path and config_file[0] != os.sep:
            config_file = os.path.realpath(os.getcwd() + os.sep + config_file)
        fixed_config_files.append(config_file)

    args.config_files = fixed_config_files

    return args

# EOF
