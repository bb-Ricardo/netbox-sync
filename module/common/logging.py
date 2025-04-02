# -*- coding: utf-8 -*-
#  Copyright (c) 2020 - 2025 Ricardo Bartels. All rights reserved.
#
#  netbox-sync.py
#
#  This work is licensed under the terms of the MIT license.
#  For a copy, see file LICENSE.txt included in this
#  repository or visit: <https://opensource.org/licenses/MIT>.

import os
import sys
import logging
from logging.handlers import RotatingFileHandler

from module.common.misc import do_error_exit

# define DEBUG2 and DEBUG3 log levels
DEBUG2 = 6  # extended messages
DEBUG3 = 3  # extra extended messages

# define valid log levels
valid_log_levels = ["DEBUG3", "DEBUG2", "DEBUG", "INFO", "WARNING", "ERROR"]

# add log level DEBUG2
logging.addLevelName(DEBUG2, "DEBUG2")
# add log level DEBUG3
logging.addLevelName(DEBUG3, "DEBUG3")

log_file_max_size_in_mb = 10
log_file_max_rotation = 5


def debug2(self, message, *args, **kws):
    if self.isEnabledFor(DEBUG2):
        # Yes, logger takes its '*args' as 'args'.
        self._log(DEBUG2, message, args, **kws)


def debug3(self, message, *args, **kws):
    if self.isEnabledFor(DEBUG3):
        # Yes, logger takes its '*args' as 'args'.
        self._log(DEBUG3, message, args, **kws)


logging.Logger.debug2 = debug2
logging.Logger.debug3 = debug3


def get_logger():
    """
    common function to retrieve common log handler in project files

    Returns
    -------
    log handler
    """

    return logging.getLogger("NetBox-Sync")


def setup_logging(log_level=None, log_file=None):
    """
    Set up logging for the whole program and return a log handler

    Parameters
    ----------
    log_level: str
        valid log level to set logging to
    log_file: str
        name of the log file to log to

    Returns
    -------
    log handler to use for logging
    """

    log_format = '%(asctime)s - %(levelname)s: %(message)s'

    if log_level is None or log_level == "":
        do_error_exit("log level undefined or empty. Check config please.")

    # check set log level against self defined log level array
    if not log_level.upper() in valid_log_levels:
        do_error_exit(f"Passed invalid log level: {log_level}")

    # check the provided log level
    if log_level == "DEBUG2":
        numeric_log_level = DEBUG2
    elif log_level == "DEBUG3":
        numeric_log_level = DEBUG3
        logging.basicConfig(level=logging.DEBUG, format=log_format)
    else:
        numeric_log_level = getattr(logging, log_level.upper(), None)

    log_format = logging.Formatter(log_format)

    # create logger instance
    logger = get_logger()

    logger.setLevel(numeric_log_level)

    # setup stream handler
    # in DEBUG3 the root logger gets redefined, that would print every log message twice
    if log_level != "DEBUG3":
        log_stream = logging.StreamHandler(sys.stdout)
        log_stream.setFormatter(log_format)
        logger.addHandler(log_stream)

    # setup log file handler
    if log_file is not None:
        # base directory is three levels up
        base_dir = os.sep.join(__file__.split(os.sep)[0:-3])
        if log_file[0] != os.sep:
            log_file = f"{base_dir}{os.sep}{log_file}"

        log_file_handler = None
        try:
            log_file_handler = RotatingFileHandler(
                filename=log_file,
                maxBytes=log_file_max_size_in_mb * 1024 * 1024,  # Bytes to Megabytes
                backupCount=log_file_max_rotation
            )
        except Exception as e:
            do_error_exit(f"Problems setting up log file: {e}")

        log_file_handler.setFormatter(log_format)
        logger.addHandler(log_file_handler)

    return logger
