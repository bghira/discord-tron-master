import logging, os
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
logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL", "INFO"))
accel_logger = logging.getLogger("DeepSpeed")
accel_logger.setLevel(logging.WARNING)
new_handler = logging.StreamHandler()
new_handler.setFormatter(
    ColorizedFormatter("%(asctime)s [%(levelname)s] (%(name)s) %(message)s")
)
# Remove existing handlers
for handler in logger.handlers[:]:
    logger.removeHandler(handler)
if not logger.handlers:
    logger.addHandler(new_handler)