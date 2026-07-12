from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TextDeduplicator:
    max_chars: int = 80
    repeat_window_seconds: float = 1.5
    _last_text: str = ""
    _last_time: float = 0.0
    _recent_fragments: dict[str, float] = field(default_factory=dict)

    def update(self, text: str) -> str | None:
        normalized = " ".join((text or "").split())
        if not normalized:
            return None

        now = time.monotonic()
        if normalized == self._last_text:
            return None

        self._prune(now)
        if normalized in self._recent_fragments:
            return None

        stable = self._fit(normalized)
        self._last_text = stable
        self._last_time = now
        self._recent_fragments[stable] = now
        return stable

    def _fit(self, text: str) -> str:
        if self.max_chars <= 0 or len(text) <= self.max_chars:
            return text

        punctuation = "。！？!?；;，,"
        tail = text[-self.max_chars :]
        cut_points = [tail.rfind(mark) for mark in punctuation]
        cut_at = max(cut_points)
        if cut_at > 0 and cut_at < len(tail) - 1:
            return tail[cut_at + 1 :].strip()
        return tail.strip()

    def _prune(self, now: float) -> None:
        expired = [
            text
            for text, seen_at in self._recent_fragments.items()
            if now - seen_at > self.repeat_window_seconds
        ]
        for text in expired:
            self._recent_fragments.pop(text, None)
