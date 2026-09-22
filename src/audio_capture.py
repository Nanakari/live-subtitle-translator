from __future__ import annotations

import threading
import time
import warnings
from dataclasses import dataclass
from typing import Iterator

import numpy as np


class SystemAudioCaptureError(RuntimeError):
    pass


@dataclass
class SystemAudioCapture:
    sample_rate: int = 16000
    # None selects the device's native channel count. Legacy channels=1 is
    # also automatic: WASAPI single-channel recording can return corrupt data.
    channels: int | None = None
    chunk_ms: int = 100
    speaker_name: str = ""

    @staticmethod
    def list_speakers() -> list[str]:
        try:
            import soundcard as sc
        except ImportError as exc:
            raise SystemAudioCaptureError(
                "soundcard is not installed. Run: pip install -r requirements.txt"
            ) from exc
        return [str(speaker.name) for speaker in sc.all_speakers()]

    def stream_audio_chunks(
        self,
        stop_event: threading.Event | None = None,
        interrupt_event: threading.Event | None = None,
        pause_event: threading.Event | None = None,
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

        speaker = None
        if self.speaker_name:
            speaker = next(
                (
                    candidate
                    for candidate in sc.all_speakers()
                    if str(candidate.name) == self.speaker_name
                ),
                None,
            )
            if speaker is None:
                raise SystemAudioCaptureError(
                    f"Selected playback device is no longer available: {self.speaker_name}"
                )
        else:
            speaker = sc.default_speaker()
        if speaker is None:
            raise SystemAudioCaptureError(
                "No default speaker was found. Select a Windows playback device first."
            )

        try:
            loopback = sc.get_microphone(speaker.id, include_loopback=True)
            if not loopback.isloopback:
                raise SystemAudioCaptureError("Selected capture endpoint is not a loopback device.")
        except Exception as exc:
            devices = ", ".join(m.name for m in sc.all_microphones(include_loopback=True))
            raise SystemAudioCaptureError(
                "No usable loopback device was found for the default speaker. "
                f"Default speaker: {speaker.name!r}. Available capture devices: {devices or 'none'}"
            ) from exc

        available_channels = int(loopback.channels)
        requested_channels = int(self.channels) if self.channels is not None else 1
        capture_channels = available_channels if requested_channels == 1 else requested_channels
        if capture_channels < 2 or capture_channels > available_channels:
            raise SystemAudioCaptureError(
                "Audio capture stopped: a playback device with at least two capture channels "
                "is required for reliable WASAPI recording. "
                f"Device: {speaker.name!r}; available channels: {available_channels}; "
                f"requested channels: {capture_channels}. Select a stereo playback device "
                "or set audio.channels to null for automatic selection."
            )

        frames_per_chunk = max(1, int(self.sample_rate * self.chunk_ms / 1000))
        with loopback.recorder(samplerate=self.sample_rate, channels=capture_channels) as recorder:
            while True:
                if stop_event is not None and stop_event.is_set():
                    break
                if interrupt_event is not None and interrupt_event.is_set():
                    break
                if pause_event is not None and pause_event.is_set():
                    time.sleep(0.05)
                    continue
                data = recorder.record(numframes=frames_per_chunk)
                yield self._as_mono_float32(data)

    @staticmethod
    def _as_mono_float32(data: np.ndarray) -> np.ndarray:
        audio = np.asarray(data)
        if audio.ndim == 2:
            audio = audio.mean(axis=1)
        return np.asarray(audio, dtype=np.float32)
