
import logging
from logging.handlers import RotatingFileHandler

from module.common.misc import do_error_exit


# define DEBUG2 and DEBUG3 log levels
DEBUG2 = 6  # extended messages

# define valid log levels
valid_log_levels = [ "DEBUG2", "DEBUG", "INFO", "WARNING", "ERROR"]

# add log level DEBUG2
logging.addLevelName(DEBUG2, "DEBUG2")
def debug2(self, message, *args, **kws):
    if self.isEnabledFor(DEBUG2):
        # Yes, logger takes its '*args' as 'args'.
        self._log(DEBUG2, message, args, **kws)
logging.Logger.debug2 = debug2


def get_logger():

    return logging.getLogger("Netbox-Sync")

def setup_logging(log_level=None, log_file=None):
    """Setup logging

    Parameters
    ----------
    args : ArgumentParser object

    default_log_level: str
        default log level if args.log_level is not set

    """

    if log_level is None or log_level == "":
        do_error_exit("ERROR: log level undefined or empty. Check config please.")

    # check set log level against self defined log level array
    if not log_level.upper() in valid_log_levels:
        do_error_exit(f"ERROR: Invalid log level: {log_level}")

    # check the provided log level
    if log_level == "DEBUG2":
        numeric_log_level = DEBUG2
    else:
        numeric_log_level = getattr(logging, log_level.upper(), None)

    log_format = logging.Formatter('%(asctime)s - %(levelname)s: %(message)s')

    # create logger instance
    logger = get_logger()

    logger.setLevel(numeric_log_level)

    # setup stream handler
    log_stream = logging.StreamHandler()
    log_stream.setFormatter(log_format)
    logger.addHandler(log_stream)

    # setup log file handler
    if log_file is not None:
        # base directory is three levels up
        base_dir = "/".join(__file__.split("/")[0:-3])
        if log_file[0] != "/":
            log_file = f"{base_dir}/{log_file}"

        try:
            log_file_handler = RotatingFileHandler(
                filename=log_file,
                maxBytes=10 * 1024 * 1024,  # Bytes to Megabytes
                backupCount=5
            )
        except Exception as e:
            do_error_exit(f"ERROR: Problems setting up log file: {e}")

        log_file_handler.setFormatter(log_format)
        logger.addHandler(log_file_handler)

    return logger
