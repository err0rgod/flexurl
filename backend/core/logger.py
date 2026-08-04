import logging
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOGS_DIR, exist_ok=True)
log_file = os.path.join(LOGS_DIR, "app.log")

formatter = logging.Formatter("%(levelname)-8s [%(asctime)s] %(name)s: %(message)s")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, LOG_LEVEL, logging.INFO)

file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setFormatter(formatter)
file_handler.setLevel(numeric_level)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(numeric_level)

logger = logging.getLogger("flexurl")
logger.setLevel(numeric_level)

if logger.hasHandlers():
    logger.handlers.clear()

logger.addHandler(file_handler)
logger.addHandler(console_handler)
