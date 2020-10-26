

import configparser
from os.path import realpath

from module.common.misc import grab, do_error_exit
from module.common.logging import get_logger

log = get_logger()


def get_config_file(config_file):

    if config_file is None or config_file == "":
        do_error_exit("ERROR: Config file not defined.")

    base_dir = "/".join(__file__.split("/")[0:-3])
    if config_file[0] != "/":
        config_file = f"{base_dir}/{config_file}"

    return realpath(config_file)


def open_config_file(config_file):

    if config_file is None or config_file == "":
        do_error_exit("ERROR: Config file not defined.")

    # setup config parser and read config
    config_handler = configparser.ConfigParser(strict=True, allow_no_value=True)

    # noinspection PyBroadException
    try:
        config_handler.read_file(open(config_file))
    except configparser.Error as e:
        do_error_exit(f"ERROR: Problem while config file parsing: {e}")
    # noinspection PyBroadException
    except Exception:
        do_error_exit(f"ERROR: Unable to open file '{config_file}'")

    return config_handler


def get_config(config_handler=None, section=None, valid_settings=None):
    """parsing and basic validation of own config file
    Parameters
    ----------
    args : ArgumentParser object
    default_log_level: str
        default log level if log level is not set in config
    Returns
    -------
    dict
        a dictionary with all config options parsed from the config file
    """

    def get_config_option(section, item, default=None):

        if isinstance(default, bool):
            value = config_handler.getboolean(section, item, fallback=default)
        elif isinstance(default, int):
            value = config_handler.getint(section, item, fallback=default)
        else:
            value = config_handler.get(section, item, fallback=default)

        if value == "":
            value = None

        config_dict[item] = value

        # take care of logging sensitive data
        for sensitive_item in ["token", "pass"]:

            if sensitive_item.lower() in item.lower():
                value = value[0:3] + "***"

        log.debug(f"Config: {section}.{item} = {value}")


    config_dict = {}

    config_error = False

    if valid_settings is None:
        log.error("No valid settings passed to config parser!")

    # read specified section section
    if section is not None:
        if section not in config_handler.sections():
            log.error("Section '{section}' not found in config_file")
            config_error = True
        else:
            for config_item, default_value in valid_settings.items():
                get_config_option(section, config_item, default=default_value)


    return config_dict

# EOF

