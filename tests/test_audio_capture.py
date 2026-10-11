from __future__ import annotations

import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, MagicMock, patch

import numpy as np

from live_subtitle_translator.audio_capture import SystemAudioCapture, SystemAudioCaptureError


class AudioCaptureTests(unittest.TestCase):
    def make_backend(self, channels: int, data: np.ndarray):
        speaker = SimpleNamespace(id="playback-id", name="Speakers")
        recorder = MagicMock()
        recorder.__enter__.return_value.record.return_value = data
        loopback = SimpleNamespace(
            channels=channels, isloopback=True, recorder=Mock(return_value=recorder)
        )
        backend = SimpleNamespace(
            default_speaker=Mock(return_value=speaker),
            get_microphone=Mock(return_value=loopback),
        )
        return backend, loopback

    def test_native_stereo_is_captured_and_downmixed_before_delivery(self):
        stereo = np.array([[0.8, 0.2], [-0.2, -0.6]], dtype=np.float32)
        backend, loopback = self.make_backend(2, stereo)
        with patch.dict(sys.modules, {"soundcard": backend}):
            stream = SystemAudioCapture().stream_audio_chunks()
            try:
                audio = next(stream)
            finally:
                stream.close()
        loopback.recorder.assert_called_once_with(samplerate=16000, channels=2)
        backend.get_microphone.assert_called_once_with("playback-id", include_loopback=True)
        np.testing.assert_allclose(audio, [0.5, -0.4])
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(audio.ndim, 1)

    def test_legacy_mono_setting_uses_all_native_surround_channels(self):
        surround = np.array([[0, 0, 0.6, 0, 0, 0]], dtype=np.float32)
        backend, loopback = self.make_backend(6, surround)
        with patch.dict(sys.modules, {"soundcard": backend}):
            stream = SystemAudioCapture(channels=1).stream_audio_chunks()
            try:
                audio = next(stream)
            finally:
                stream.close()
        loopback.recorder.assert_called_once_with(samplerate=16000, channels=6)
        np.testing.assert_allclose(audio, [0.1])

    def test_explicit_multichannel_override_is_supported(self):
        backend, loopback = self.make_backend(6, np.zeros((2, 2), dtype=np.float32))
        with patch.dict(sys.modules, {"soundcard": backend}):
            stream = SystemAudioCapture(channels=2).stream_audio_chunks()
            try:
                next(stream)
            finally:
                stream.close()
        loopback.recorder.assert_called_once_with(samplerate=16000, channels=2)

    def test_mono_only_device_fails_before_opening_an_unreliable_recorder(self):
        backend, loopback = self.make_backend(1, np.zeros((2, 1), dtype=np.float32))
        with patch.dict(sys.modules, {"soundcard": backend}):
            with self.assertRaisesRegex(SystemAudioCaptureError, "at least two"):
                next(SystemAudioCapture().stream_audio_chunks())
        loopback.recorder.assert_not_called()

    def test_unsupported_channel_count_is_reported_before_recording(self):
        backend, loopback = self.make_backend(2, np.zeros((2, 2), dtype=np.float32))
        with patch.dict(sys.modules, {"soundcard": backend}):
            with self.assertRaisesRegex(SystemAudioCaptureError, "available channels: 2"):
                next(SystemAudioCapture(channels=6).stream_audio_chunks())
        loopback.recorder.assert_not_called()

    def check_pause_reopens_recorder(self, rapid_resume: bool):
        paused = threading.Event()
        reset = threading.Event()
        backend, loopback = self.make_backend(2, np.ones((2, 2), dtype=np.float32))
        first = loopback.recorder.return_value
        fresh = MagicMock()
        fresh.__enter__.return_value.record.return_value = np.full((2, 2), 0.25, dtype=np.float32)
        loopback.recorder.side_effect = [first, fresh]

        def pause_during_record(**kwargs):
            paused.set()
            reset.set()
            if rapid_resume:
                paused.clear()
            return np.ones((2, 2), dtype=np.float32)

        def resume_after_recorder_closed(_seconds):
            first.__exit__.assert_called_once()
            paused.clear()

        first.__enter__.return_value.record.side_effect = pause_during_record
        with patch.dict(sys.modules, {"soundcard": backend}), patch(
            "live_subtitle_translator.audio_capture.time.sleep", side_effect=resume_after_recorder_closed
        ):
            stream = SystemAudioCapture().stream_audio_chunks(pause_event=paused, reset_event=reset)
            try:
                audio = next(stream)
            finally:
                stream.close()
        self.assertEqual(loopback.recorder.call_count, 2)
        first.__exit__.assert_called_once()
        fresh.__exit__.assert_called_once()
        np.testing.assert_allclose(audio, [0.25, 0.25])
        self.assertFalse(reset.is_set())

    def test_pause_discards_inflight_audio_and_closes_recorder_before_waiting(self):
        self.check_pause_reopens_recorder(rapid_resume=False)

    def test_rapid_pause_resume_still_discards_backend_buffer(self):
        self.check_pause_reopens_recorder(rapid_resume=True)


if __name__ == "__main__":
    unittest.main()
