
import os

from argparse import ArgumentParser, RawDescriptionHelpFormatter

from module.common.logging import valid_log_levels


def parse_command_line(version=None, self_description=None, version_date=None, default_config_file_path=None):
    """
    parse command line arguments, also add current version and version date to description

    Parameters
    ----------
    version: str
        version of this program
    self_description: str
        short self description of this program
    version_date: str
        release date of this version
    default_config_file_path: str
        path to default config file

    Returns
    -------
    ArgumentParser object: with parsed command line arguments
    """

    # define command line options
    description = f"{self_description}\nVersion: {version} ({version_date})"

    parser = ArgumentParser(
        description=description,
        formatter_class=RawDescriptionHelpFormatter)

    parser.add_argument("-c", "--config", default=default_config_file_path, dest="config_file",
                        help="points to the config file to read config data from " +
                             "which is not installed under the default path '" +
                             default_config_file_path + "'",
                        metavar="settings.ini")

    parser.add_argument("-l", "--log_level", choices=valid_log_levels, dest="log_level",
                        help="set log level (overrides config)")

    parser.add_argument("-p", "--purge", action="store_true",
                        help="Remove (almost) all synced objects which were create by this script. "
                             "This is helpful if you want to start fresh or stop using this script.")

    args = parser.parse_args()

    # fix supplied config file path
    if args.config_file != default_config_file_path and args.config_file[0] != os.sep:
        args.config_file = os.path.realpath(os.getcwd() + os.sep + args.config_file)

    return args

# EOF
