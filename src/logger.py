from __future__ import annotations

import logging
import re
from typing import Optional


class SecretFilter(logging.Filter):
    """Avoid leaking common API key environment names in log output."""

    SECRET_MARKERS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        for marker in self.SECRET_MARKERS:
            if marker in message:
                record.msg = message.replace(marker, "<redacted-env-name>")
                record.args = ()
                message = record.msg
        redacted = re.sub(r"AQ\.[A-Za-z0-9_.-]{12,}", "<redacted-api-key>", message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def setup_logger(
    debug: bool = False,
    name: Optional[str] = None,
    log_file: Optional[str] = None,
) -> logging.Logger:
    level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.addFilter(SecretFilter())
        logger.addHandler(handler)

        if log_file:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            file_handler.addFilter(SecretFilter())
            logger.addHandler(file_handler)

    for handler in logger.handlers:
        handler.setLevel(level)

    return logger
