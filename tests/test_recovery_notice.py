from __future__ import annotations

import asyncio
import queue
import threading
import unittest
from contextlib import asynccontextmanager
from unittest.mock import Mock, patch

from live_subtitle_translator import app
from live_subtitle_translator.desktop_runtime import DesktopControlState
from live_subtitle_translator.gemini_live_translate import TranslationEvent
from live_subtitle_translator.recovery_notice import RecoveryNotice
from live_subtitle_translator.subtitle_state import SubtitleState
from live_subtitle_translator.subtitle_window import SubtitleWindow, SubtitleWindowConfig
from tests import test_desktop as desktop
from tests.test_network_lifecycle import eventually


class RecoveryNoticeTests(unittest.TestCase):
    def test_repeated_failures_share_deadline_and_show_only_once(self):
        show, clear = Mock(), Mock()
        notice = RecoveryNotice(show, clear, 10)
        notice.report("first failure", now=100)
        notice.update(now=109.9)
        show.assert_not_called()
        notice.report("another failure", now=109.9)
        notice.update(now=110)
        notice.report("third failure", now=111)
        notice.update(now=120)
        show.assert_called_once_with("another failure")
        notice.reset()
        clear.assert_called_once_with("another failure")

    def test_recovery_cancels_pending_notice_and_next_outage_starts_fresh(self):
        show, clear = Mock(), Mock()
        notice = RecoveryNotice(show, clear, 10)
        notice.report("brief failure", now=0)
        notice.reset()
        notice.update(now=20)
        show.assert_not_called()
        clear.assert_not_called()
        notice.report("new failure", now=20)
        notice.update(now=29.9)
        show.assert_not_called()
        notice.update(now=30)
        show.assert_called_once_with("new failure")

    def test_zero_delay_and_invalid_delays(self):
        show = Mock()
        notice = RecoveryNotice(show, Mock(), 0)
        notice.report("failure", now=100)
        notice.update(now=100)
        show.assert_called_once_with("failure")
        for delay in (-1, float("inf"), float("nan")):
            with self.subTest(delay=delay), self.assertRaisesRegex(ValueError, "error_display_delay_seconds"):
                RecoveryNotice(Mock(), Mock(), delay)


class RecoveryStatusTests(unittest.TestCase):
    def make_window(self):
        window = SubtitleWindow.__new__(SubtitleWindow)
        SubtitleState.__init__(window, SubtitleWindowConfig())
        window._queue = queue.Queue()
        window.root = Mock()
        window._render_history = Mock()
        window._history = [{"ja": "source", "zh": "translation"}]
        return window

    def test_recovery_redraws_preserved_subtitles_without_new_text(self):
        window = self.make_window()
        window.set_text("Gemini reconnecting: offline")
        window._poll_text()
        window._render_history.reset_mock()
        window.clear_status("Gemini reconnecting: offline")
        window._poll_text()
        self.assertEqual(window._status, "")
        self.assertEqual(window._history, [{"ja": "source", "zh": "translation"}])
        window._render_history.assert_called_once()

    def test_delayed_clear_cannot_erase_a_newer_status(self):
        for replacement in ("Paused. Disconnected.", "Gemini connection failed: invalid key"):
            with self.subTest(replacement=replacement):
                window = self.make_window()
                window.set_text("Gemini reconnecting: offline")
                window.set_text(replacement)
                window.clear_status("Gemini reconnecting: offline")
                window._poll_text()
                self.assertEqual(window._status, replacement)

    def test_empty_text_also_redraws_when_clearing_status(self):
        window = self.make_window()
        window._status = "Connected. Waiting for audio..."
        window.set_text("")
        window._poll_text()
        self.assertEqual(window._status, "")
        window._render_history.assert_called_once()


class RecordingWindow(desktop.FakeWindow):
    def __init__(self):
        super().__init__()
        self.cleared = []

    def clear_status(self, expected_text):
        self.cleared.append(expected_text)

    @property
    def notices(self):
        return [text for text in self.messages if "Retrying..." in text]


class DesktopRecoveryNoticeTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def running(self, translator, *, config=None, controls=None, capture=desktop.IdleCapture):
        clock = [100.0]

        class ManualClockNotice(RecoveryNotice):
            def report(self, message, now=None):
                super().report(message, now=clock[0])

            def update(self, now=None):
                super().update(now=clock[0])

        window, stop = RecordingWindow(), threading.Event()
        with patch.object(app, "GeminiLiveTranslator", translator), \
                patch.object(app, "SystemAudioCapture", capture), \
                patch.object(app, "RecoveryNotice", ManualClockNotice):
            task = asyncio.create_task(app.run_translation(config or {}, window, stop, controls))
            try:
                yield window, clock, task
            finally:
                stop.set()
                await asyncio.wait_for(task, 2)

    async def test_brief_receive_error_recovers_before_notice(self):
        recovered = asyncio.Event()

        class Translator(desktop.WaitingTranslator):
            async def receive_translations(self):
                yield TranslationEvent(error="brief network failure")
                yield TranslationEvent(session_id=1, session_started=True)
                recovered.set()
                await asyncio.Event().wait()

        async with self.running(Translator) as (window, clock, _):
            await asyncio.wait_for(recovered.wait(), 1)
            clock[0] += 20
            await asyncio.sleep(0.1)
            self.assertEqual(window.notices, [])

    async def test_single_receive_error_shows_while_retry_waits_and_clears_on_recovery(self):
        failed, recover, recovered = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Translator(desktop.WaitingTranslator):
            async def receive_translations(self):
                yield TranslationEvent(session_id=1, session_started=True)
                yield TranslationEvent(error="network failure")
                failed.set()
                await recover.wait()
                yield TranslationEvent(session_id=1, session_started=True)
                recovered.set()
                await asyncio.Event().wait()

        async with self.running(Translator) as (window, clock, _):
            await asyncio.wait_for(failed.wait(), 1)
            self.assertEqual(window.notices, [])
            clock[0] += 10
            await eventually(lambda: bool(window.notices))
            recover.set()
            await asyncio.wait_for(recovered.wait(), 1)
            self.assertEqual(window.cleared, window.notices)

    async def test_initial_retry_does_not_overwrite_persistent_notice(self):
        retrying, recover = asyncio.Event(), asyncio.Event()

        class Translator(desktop.WaitingTranslator):
            async def start(self):
                self.start_calls += 1
                if self.start_calls == 1:
                    raise OSError("offline")
                retrying.set()
                await recover.wait()

        async with self.running(Translator, config={"gemini": {"reconnect_delay_seconds": 0}}) as (window, clock, _):
            await asyncio.wait_for(retrying.wait(), 1)
            self.assertEqual(window.messages, ["Connecting to Gemini..."])
            clock[0] += 10
            await eventually(lambda: bool(window.notices))
            self.assertEqual(window.messages[-1], window.notices[0])
            recover.set()
            await eventually(lambda: "Connected. Waiting for audio..." in window.messages)
            self.assertEqual(window.cleared, window.notices)

    async def test_send_failure_is_deferred_even_during_long_retry_backoff(self):
        failed = asyncio.Event()

        class Translator(desktop.WaitingTranslator):
            async def send_audio(self, chunk):
                failed.set()
                raise OSError("send connection unavailable")

        config = {"audio": {"silence_sleep_enabled": False},
                  "gemini": {"reconnect_delay_seconds": 100}}
        async with self.running(Translator, config=config, capture=desktop.ContinuousCapture) as (window, clock, _):
            await asyncio.wait_for(failed.wait(), 1)
            self.assertEqual(window.notices, [])
            clock[0] += 10
            await eventually(lambda: bool(window.notices))
            self.assertEqual(len(window.notices), 1)

    async def test_pause_and_sleep_cancel_pending_notice(self):
        for sleeping in (False, True):
            with self.subTest(sleeping=sleeping):
                failed = asyncio.Event()
                controls = DesktopControlState()

                class Translator(desktop.WaitingTranslator):
                    async def receive_translations(self):
                        yield TranslationEvent(error="network failure")
                        failed.set()
                        await asyncio.Event().wait()

                async with self.running(Translator, controls=controls) as (window, clock, _):
                    await asyncio.wait_for(failed.wait(), 1)
                    if sleeping:
                        controls.set_auto_sleep(True)
                    else:
                        controls.toggle_paused()
                    prefix = "No audio." if sleeping else "Paused."
                    await eventually(lambda: any(text.startswith(prefix) for text in window.messages))
                    clock[0] += 20
                    await asyncio.sleep(0.1)
                    self.assertEqual(window.notices, [])

    async def test_fatal_or_disabled_retry_preserves_actual_error_immediately(self):
        for fatal, reconnect in ((True, True), (False, False)):
            with self.subTest(fatal=fatal, reconnect=reconnect):
                class Translator(desktop.WaitingTranslator):
                    async def receive_translations(self):
                        yield TranslationEvent(error="specific failure", fatal=fatal)

                async with self.running(Translator, config={"gemini": {"reconnect": reconnect}}) as (window, _, task):
                    await asyncio.wait_for(task, 1)
                    self.assertEqual(window.messages[-1], "Gemini connection failed: specific failure")
                    self.assertEqual(window.notices, [])


if __name__ == "__main__":
    unittest.main()
