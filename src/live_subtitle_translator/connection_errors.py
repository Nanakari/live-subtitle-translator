"""Shared retry policy for initial connections and Live session reconnects."""

from __future__ import annotations

import asyncio
import re

from websockets.exceptions import ConnectionClosed, InvalidMessage


def http_status(exc: Exception) -> int | None:
    if isinstance(exc, ConnectionClosed):
        return None  # Its deprecated .code is a WebSocket close code, not HTTP.
    response = getattr(exc, "response", None)
    for value in (
        getattr(response, "status_code", None),
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
    ):
        if isinstance(value, int) and 100 <= value <= 599:
            return value
    return None


def is_resumption_rejected(exc: Exception) -> bool:
    """Only explicit session/handle rejection permits a fresh-session fallback."""
    status = http_status(exc)
    if status is not None and status not in {400, 404, 410}:
        return False
    text = str(exc).lower()
    if re.search(r"\bsession (?:not found|has expired|expired|does not exist)\b", text):
        return True
    return bool(
        re.search(r"\b(?:resumption|resume|session)[ _-]*(?:handle|token)\b|\bsession resumption\b", text)
        and re.search(r"\binvalid\b|\bexpired\b|\bnot found\b|\bnot valid\b|\bcannot resume\b", text)
    )


def is_retryable_connection_error(exc: Exception) -> bool:
    status = http_status(exc)
    if status is not None:
        return status in {408, 429} or 500 <= status < 600
    # An invalid handle without a handle to discard is a permanent error.
    if is_resumption_rejected(exc):
        return False
    if isinstance(exc, ConnectionClosed):
        close = exc.rcvd or exc.sent
        if close is None:
            return True
        if close.code == 1008:
            reason = close.reason.lower()
            return any(marker in reason for marker in ("session duration", "session durat", "goaway"))
        return close.code in {1000, 1001, 1006, 1011, 1012, 1013, 1014}
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, OSError, InvalidMessage)):
        return True
    # Some proxy/transport implementations expose only text. Avoid broad words
    # such as "connection", which also appear in permanent handshake errors.
    return any(marker in str(exc).lower() for marker in (
        "opening handshake timed out", "connection reset", "connection refused",
        "connection aborted", "temporarily unavailable", "no close frame received or sent",
    ))
