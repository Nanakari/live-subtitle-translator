from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class SilenceDetector:
    """Detect sustained silence and a stable return of audible playback."""

    enabled: bool = True
    sleep_after_seconds: float = 20.0
    rms_threshold: float = 0.0015
    wake_chunks: int = 3
    sleeping: bool = False
    _silent_seconds: float = 0.0
    _audible_chunks: int = 0

    def update(self, audio_chunk: np.ndarray | bytes, chunk_seconds: float) -> str | None:
        if not self.enabled:
            return None
        audible = self._rms(audio_chunk) >= max(0.0, self.rms_threshold)
        if self.sleeping:
            if audible:
                self._audible_chunks += 1
                if self._audible_chunks >= max(1, self.wake_chunks):
                    self.sleeping = False
                    self._audible_chunks = 0
                    self._silent_seconds = 0.0
                    return "wake"
            else:
                self._audible_chunks = 0
            return None

        if audible:
            self._silent_seconds = 0.0
            return None
        self._silent_seconds += max(0.0, chunk_seconds)
        if self._silent_seconds >= max(0.1, self.sleep_after_seconds):
            self.sleeping = True
            self._silent_seconds = 0.0
            self._audible_chunks = 0
            return "sleep"
        return None

    @staticmethod
    def _rms(audio_chunk: np.ndarray | bytes) -> float:
        if isinstance(audio_chunk, bytes):
            if not audio_chunk:
                return 0.0
            audio = np.frombuffer(audio_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        else:
            audio = np.asarray(audio_chunk, dtype=np.float32)
        if audio.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
