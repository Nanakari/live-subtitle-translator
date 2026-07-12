from __future__ import annotations

import threading
import warnings
from dataclasses import dataclass
from typing import Iterator

import numpy as np


class SystemAudioCaptureError(RuntimeError):
    pass


@dataclass
class SystemAudioCapture:
    sample_rate: int = 16000
    channels: int = 1
    chunk_ms: int = 100

    def stream_audio_chunks(
        self, stop_event: threading.Event | None = None
    ) -> Iterator[np.ndarray]:
        try:
            import soundcard as sc
        except ImportError as exc:
            raise SystemAudioCaptureError(
                "soundcard is not installed. Run: pip install -r requirements.txt"
            ) from exc
        warnings.filterwarnings(
            "ignore",
            message="data discontinuity in recording",
            module=r"soundcard\.mediafoundation",
        )

        speaker = sc.default_speaker()
        if speaker is None:
            raise SystemAudioCaptureError(
                "No default speaker was found. Select a Windows playback device first."
            )

        try:
            loopback = sc.get_microphone(speaker.name, include_loopback=True)
        except Exception as exc:
            devices = ", ".join(m.name for m in sc.all_microphones(include_loopback=True))
            raise SystemAudioCaptureError(
                "No usable loopback device was found for the default speaker. "
                f"Default speaker: {speaker.name!r}. Available capture devices: {devices or 'none'}"
            ) from exc

        frames_per_chunk = max(1, int(self.sample_rate * self.chunk_ms / 1000))
        with loopback.recorder(samplerate=self.sample_rate, channels=self.channels) as recorder:
            while stop_event is None or not stop_event.is_set():
                data = recorder.record(numframes=frames_per_chunk)
                yield self._as_mono_float32(data)

    @staticmethod
    def _as_mono_float32(data: np.ndarray) -> np.ndarray:
        audio = np.asarray(data)
        if audio.ndim == 2:
            audio = audio.mean(axis=1)
        return np.asarray(audio, dtype=np.float32)
