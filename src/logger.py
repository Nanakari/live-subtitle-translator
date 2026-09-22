from __future__ import annotations

import logging
import os
import re
from typing import Optional


_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    if value:
        _secrets.add(value)


def redact(text: str) -> str:
    for secret in sorted(_secrets.copy(), key=len, reverse=True):
        text = text.replace(secret, "<redacted-api-key>")
    text = re.sub(r"(?:AQ\.[A-Za-z0-9_.-]{12,}|AIza[A-Za-z0-9_-]{20,})", "<redacted-api-key>", text)
    for marker in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        text = text.replace(marker, "<redacted-env-name>")
    return text


class SecretFormatter(logging.Formatter):
    """Redact the final message, including chained exceptions and stack info."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


class SecretFilter(logging.Filter):
    """Avoid leaking common API key environment names in log output."""

    SECRET_MARKERS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logger(
    debug: bool = False,
    name: Optional[str] = None,
    log_file: Optional[str] = None,
) -> logging.Logger:
    for variable in SecretFilter.SECRET_MARKERS:
        register_secret(os.getenv(variable))
    level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(
            SecretFormatter(
                "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
        handler.addFilter(SecretFilter())
        logger.addHandler(handler)

        if log_file:
            file_handler = logging.FileHandler(log_file, encoding="utf-8")
            file_handler.setFormatter(
                SecretFormatter(
                    "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            file_handler.addFilter(SecretFilter())
            logger.addHandler(file_handler)

    for handler in logger.handlers:
        handler.setLevel(level)

    return logger
