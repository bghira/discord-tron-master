import logging
from colorama import Fore, Back, Style, init
from discord_tron_master.classes.app_config import AppConfig
config = AppConfig()

class ColorizedFormatter(logging.Formatter):
    level_colors = {
        logging.DEBUG: Fore.CYAN,
        logging.INFO: Fore.GREEN,
        logging.WARNING: Fore.YELLOW,
        logging.ERROR: Fore.RED,
        logging.CRITICAL: Fore.RED + Back.WHITE + Style.BRIGHT,
    }

    def format(self, record):
        level_color = self.level_colors.get(record.levelno, '')
        reset_color = Style.RESET_ALL
        message = super().format(record)
        return f"{level_color}{message}{reset_color}"

# Initialize colorama
init(autoreset=True)

# Set up logging with the custom formatter
logger = logging.getLogger()
logger.setLevel(config.get_log_level())

handler = logging.StreamHandler()
handler.setFormatter(ColorizedFormatter("%(asctime)s [%(levelname)s] %(message)s"))

logger.addHandler(handler)