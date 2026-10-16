from __future__ import annotations

import asyncio
import queue
import threading
import unittest
from unittest.mock import Mock, patch

from live_subtitle_translator import app
from live_subtitle_translator.subtitle_state import SubtitleState
from live_subtitle_translator.subtitle_window import SubtitleWindow, SubtitleWindowConfig
from tests import test_desktop as desktop


DEVICE_ERROR = "Selected playback device is no longer available: Offline speaker"
EXPECTED_STATUS = f"Audio capture stopped: {DEVICE_ERROR}"


class MissingDeviceCapture(desktop.IdleCapture):
    def stream_audio_chunks(self, **kwargs):
        raise app.SystemAudioCaptureError(DEVICE_ERROR)
        yield


class AudioErrorTranslator(desktop.WaitingTranslator):
    async def send_audio(self, chunk):
        raise app.SystemAudioCaptureError(DEVICE_ERROR)


class AudioErrorStatusTests(unittest.IsolatedAsyncioTestCase):
    async def check_status(self, translator, capture):
        for language in ("source", "bilingual", "translation"):
            with self.subTest(language=language):
                window = SubtitleWindow.__new__(SubtitleWindow)
                SubtitleState.__init__(window, SubtitleWindowConfig(subtitle_language=language))
                window._queue = queue.Queue()
                window.root = Mock()
                window._render_history = Mock()
                history = [{"ja": "previous source", "zh": "previous translation"}]
                window._history = list(history)
                stop = threading.Event()
                with patch.object(app, "GeminiLiveTranslator", translator), patch.object(app, "SystemAudioCapture", capture):
                    await asyncio.wait_for(app.run_translation(
                        {"app": {"error_display_delay_seconds": 999},
                         "audio": {"silence_sleep_enabled": False}},
                        window, stop), 1)
                # Use the real UI queue and state consumer: the fatal error
                # must display on the first tick, without sentence timeouts.
                window._poll_text()
                self.assertTrue(stop.is_set())
                self.assertEqual(window._status, EXPECTED_STATUS)
                self.assertEqual(window._build_render_items(), [(EXPECTED_STATUS, "status")])
                self.assertEqual(window._compact_track_text(), ("", EXPECTED_STATUS))
                self.assertEqual(window._history, history)
                self.assertEqual(window._current_zh, "")
                self.assertEqual(window._pending_zh_segments, [])
                window._render_history.assert_called_once()

    async def test_missing_capture_device_is_immediate_status_in_all_languages(self):
        await self.check_status(desktop.WaitingTranslator, MissingDeviceCapture)

    async def test_queued_audio_failure_is_immediate_status_in_all_languages(self):
        await self.check_status(AudioErrorTranslator, desktop.ContinuousCapture)


if __name__ == "__main__":
    unittest.main()
