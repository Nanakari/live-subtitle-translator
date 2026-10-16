"""Keep short, recoverable outages from interrupting the subtitle display."""
from __future__ import annotations

import math
import time
from collections.abc import Callable


class RecoveryNotice:
    """Polled by the desktop loop; retries share one deadline until recovery."""

    def __init__(
        self,
        show: Callable[[str], None],
        clear: Callable[[str], None],
        delay_seconds: float,
    ) -> None:
        if not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("app.error_display_delay_seconds must be finite and non-negative")
        self._show = show
        self._clear = clear
        self.delay_seconds = delay_seconds
        self._started_at: float | None = None
        self._message = ""
        self._displayed = ""

    def report(self, message: str, now: float | None = None) -> None:
        if self._started_at is None:
            self._started_at = time.monotonic() if now is None else now
        self._message = message

    def update(self, now: float | None = None) -> None:
        if self._started_at is None or self._displayed:
            return
        now = time.monotonic() if now is None else now
        if now - self._started_at >= self.delay_seconds:
            self._displayed = self._message
            self._show(self._displayed)

    def reset(self) -> None:
        self._started_at = None
        self._message = ""
        if self._displayed:
            # Only remove our own notice, never a newer pause or fatal error.
            self._clear(self._displayed)
            self._displayed = ""
