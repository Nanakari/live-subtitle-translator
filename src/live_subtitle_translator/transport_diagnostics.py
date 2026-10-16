"""Bounded transport diagnostics without exception messages or payload data."""
from __future__ import annotations

import re

from websockets.exceptions import ConnectionClosed


_MISSING = object()
_MAX_CHAIN_LENGTH = 5


def _attribute(value: object, name: str) -> object:
    try:
        return getattr(value, name, _MISSING)
    except Exception:
        return _MISSING


def _integer(value: object, minimum: int, maximum: int) -> int | None:
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    # Bypass custom __str__/__int__ methods on integer subclasses, while still
    # accepting IntEnum close codes used by websockets.
    number = int.__int__(value)
    return number if minimum <= number <= maximum else None


def _close_code(frame: object) -> str:
    if frame is None:
        return "none"
    number = _integer(_attribute(frame, "code"), 0, 65535)
    return "unknown" if number is None else str(number)


def _exception_type(exc: BaseException) -> str:
    name = type.__getattribute__(type(exc), "__name__")
    # Class names are the only textual values included. Refuse arbitrary or
    # unbounded names from dynamically constructed exception classes.
    return name if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,79}", name) else "Exception"


def _next_exception(exc: BaseException) -> BaseException | None:
    cause = _attribute(exc, "__cause__")
    if isinstance(cause, BaseException):
        return cause
    if _attribute(exc, "__suppress_context__") is True:
        return None
    context = _attribute(exc, "__context__")
    return context if isinstance(context, BaseException) else None


def describe_transport_error(exc: BaseException) -> str:
    """Describe at most five exceptions using only types and numeric metadata.

    For WebSocket closures, an absent received Close frame is an abnormal EOF,
    even if the client sent a Close frame. A present frame with an invalid code
    is reported as ``unknown``. Exception messages, close reasons, and request
    data are never read or formatted.
    """
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and len(parts) < _MAX_CHAIN_LENGTH:
        if id(current) in seen:
            parts.append("[cycle]")
            break
        seen.add(id(current))
        fields: list[str] = []
        for name in ("errno", "winerror"):
            value = _attribute(current, name)
            if value is _MISSING or value is None:
                continue
            number = _integer(value, -(2**31), 2**32 - 1)
            fields.append(f"{name}={'unknown' if number is None else number}")
        if isinstance(current, ConnectionClosed):
            received = _attribute(current, "rcvd")
            sent = _attribute(current, "sent")
            fields.extend((
                f"received_close={_close_code(received)}",
                f"sent_close={_close_code(sent)}",
                "abnormal_eof=" + (
                    "unknown" if received is _MISSING else "true" if received is None else "false"
                ),
            ))
        elif isinstance(current, EOFError):
            fields.append("abnormal_eof=true")
        part = _exception_type(current)
        if fields:
            part += "[" + ",".join(fields) + "]"
        parts.append(part)
        current = _next_exception(current)
    else:
        if current is not None:
            parts.append("[cycle]" if id(current) in seen else "[truncated]")
    return "chain=" + " <- ".join(parts)


def is_expected_session_rotation(message: str | None) -> bool:
    """Preserve established rotation markers, excluding abnormal transport EOF."""
    if not isinstance(message, str):
        return False
    text = message.lower()
    if "no close frame received or sent" in text:
        return False
    return any(marker in text for marker in (
        "goaway",
        "session durat",
        "received 1008",
        "connection aborted because the client failed to close",
    ))
