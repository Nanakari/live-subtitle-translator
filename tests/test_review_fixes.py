"""Regression tests for source deltas, source deadlines, and manual pause.

No real audio, API calls, or private configuration is used.
"""
from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from live_subtitle_translator import app
from live_subtitle_translator.config import default_config
from live_subtitle_translator.desktop_runtime import DesktopControlState
from live_subtitle_translator.subtitle_state import SubtitleState
from live_subtitle_translator.subtitle_config import SubtitleWindowConfig
from tests.test_desktop import FakeWindow, WaitingTranslator
from tests.test_network_lifecycle import eventually


def source_window():
    window = SubtitleState(SubtitleWindowConfig.from_config(default_config()))
    window.config.subtitle_language = "source"
    window.config.skip_filler_subtitles = False
    return window


def source_after_turn(chunks):
    window = source_window()
    with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10):
        for chunk in chunks:
            window._consume_update("bilingual", chunk, None)
        window._consume_update("bilingual", None, None, turn_complete=True)
        window._finish_turn_boundary()
    return "".join(block["ja"] for block in window._history)


class SourceTextReview(unittest.TestCase):
    def test_truncated_source_context_preserves_space_before_continuation(self):
        window = source_window()
        window.config.subtitle_language = "bilingual"
        window.config.ja_pair_max_chars = 20
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10):
            window._add_ja_fragment("long prefix " * 4 + "hello ")
            context = window._ja_text_for_range(10, 10, 10)
            window._source_continuation = (10, context)
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=11):
            window._add_ja_fragment("world.")
            continued = window._ja_text_for_range(10, 11, 11)
        self.assertTrue(continued.endswith("hello world."), continued)

    def test_long_completed_source_is_not_truncated_by_pairing_limit(self):
        text = "long " * 100 + "sentence."
        self.assertEqual(source_after_turn([text]), text)

    def test_bilingual_source_also_preserves_delta_boundaries(self):
        window = source_window()
        window.config.subtitle_language = "bilingual"
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10):
            for piece in ("Hel", "lo ", "very ", "very ", "good."):
                window._consume_update("bilingual", piece, None)
            window._consume_update("bilingual", None, "很好的问候。", turn_complete=True)
            window._finish_turn_boundary()
        self.assertEqual(window._history[-1]["ja"], "Hello very very good.")

    def test_word_split_across_source_packets_is_preserved(self):
        self.assertEqual(source_after_turn(["Hel", "lo"]), "Hello")

    def test_repeated_spoken_words_are_preserved(self):
        self.assertEqual(source_after_turn(["very ", "very ", "good"]), "very very good")

    def test_continuous_source_commits_before_hard_deadline_without_loss(self):
        window = source_window()
        incoming = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉"
        for index, text in enumerate(incoming):
            with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10 + index * 0.5):
                window._consume_update("bilingual", text, None)
                window._flush_stale_stable_segment()
        self.assertTrue(
            window._history,
            "14.5 seconds elapsed, but the configured 8-second deadline committed nothing; "
            f"retained source={''.join(text for _, text in window._ja_fragments)!r}",
        )
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=25):
            window._consume_update("bilingual", None, None, turn_complete=True)
            window._finish_turn_boundary()
        self.assertEqual("".join(block["ja"] for block in window._history), incoming)


class PauseReview(unittest.IsolatedAsyncioTestCase):
    async def test_resume_does_not_send_audio_queued_before_user_pause(self):
        entered = asyncio.Event()
        captured = threading.Event()
        stop = threading.Event()
        controls = DesktopControlState()

        class Translator(WaitingTranslator):
            instances = []

            def __init__(self, **kwargs):
                super().__init__(**kwargs)
                self.audio = []

            async def send_audio(self, chunk):
                if not entered.is_set():
                    entered.set()
                    await asyncio.Event().wait()
                else:
                    self.audio.append(chunk)

        class Capture:
            def __init__(self, **kwargs):
                pass

            def stream_audio_chunks(self, **kwargs):
                for index in range(10):
                    yield f"before-pause-{index}".encode()
                captured.set()
                while not stop.is_set():
                    stop.wait(0.01)

        with patch.object(app, "GeminiLiveTranslator", Translator), patch.object(app, "SystemAudioCapture", Capture):
            task = asyncio.create_task(app.run_translation(
                {"audio": {"silence_sleep_enabled": False}}, FakeWindow(), stop, controls,
            ))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await eventually(captured.is_set)
                translator = Translator.instances[-1]
                controls.toggle_paused()
                await eventually(lambda: translator.disconnect_calls == 1)
                controls.toggle_paused()
                await eventually(lambda: translator.start_calls == 2)
                await asyncio.sleep(0.15)
                self.assertEqual(translator.audio, [], "No new audio was captured after pause")
            finally:
                stop.set()
                await asyncio.wait_for(task, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
