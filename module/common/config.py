# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2022 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.


from module.config.option import ConfigOption
from module.config.base import ConfigBase
from module.config import common_config_section_name


class CommonConfig(ConfigBase):
    """
    Controls the parameters for logging
    """

    section_name = common_config_section_name

    options = [
        ConfigOption("log_level",
                     str,
                     description="""\
                     Logs will always be printed to stdout/stderr.
                     Logging can be set to following log levels:
                       ERROR:      Fatal Errors which stops regular a run
                       WARNING:    Warning messages won't stop the syncing process but mostly worth
                                   to have a look at.
                       INFO:       Information about objects that will be create/updated/deleted in NetBox
                       DEBUG:      Will log information about retrieved information, changes in internal
                                   content structure and parsed config
                       DEBUG2:     Will also log information about how/why content is parsed or skipped.
                       DEBUG3:     Logs all source and NetBox queries/results to stdout. Very useful for
                                   troubleshooting, but will log any sensitive content contained within a query.
                    """,
                     default_value="INFO"),

        ConfigOption("log_to_file",
                     bool,
                     description="""Enabling this options will write all
                     logs to a log file defined in 'log_file'
                     """,
                     default_value=True),

        ConfigOption("log_file",
                     str,
                     description="""Destination of the log file if "log_to_file" is enabled.
                     Log file will be rotated maximum 5 times once the log file reaches size of 10 MB
                     """,
                     default_value="log/netbox_sync.log")
    ]
