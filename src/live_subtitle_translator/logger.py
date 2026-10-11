from __future__ import annotations

import logging
import os
import re
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from .paths import resolve_app_path


_secrets: set[str] = set()
_file_handlers: dict[Path, RotatingFileHandler] = {}
_handler_lock = threading.RLock()


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
    *,
    app_root: Path | None = None,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
) -> logging.Logger:
    for variable in SecretFilter.SECRET_MARKERS:
        register_secret(os.getenv(variable))
    level = logging.DEBUG if debug else logging.INFO
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False

    formatter = SecretFormatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not any(getattr(handler, "_translator_console", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._translator_console = True
        handler.setFormatter(formatter)
        handler.addFilter(SecretFilter())
        logger.addHandler(handler)

    if log_file:
        path = resolve_app_path(log_file, app_root)
        try:
            with _handler_lock:
                file_handler = _file_handlers.get(path)
                if file_handler is None or file_handler._closed:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    file_handler = RotatingFileHandler(
                        path, maxBytes=max(1, int(max_bytes)),
                        backupCount=max(1, int(backup_count)), encoding="utf-8",
                    )
                    file_handler.setFormatter(formatter)
                    file_handler.addFilter(SecretFilter())
                    _file_handlers[path] = file_handler
                # One handler and lock per destination, shared by app/Gemini/UI.
                for old in logger.handlers[:]:
                    if isinstance(old, RotatingFileHandler) and old is not file_handler:
                        logger.removeHandler(old)
                if file_handler not in logger.handlers:
                    logger.addHandler(file_handler)
        except OSError:
            logger.warning("Cannot write the configured log file; continuing with console logging.")

    # Filtering belongs to each logger; a shared handler must not change level
    # when a second logger is configured.

    return logger
