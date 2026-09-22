from __future__ import annotations

import asyncio
import queue
import threading
import time
import unittest
from unittest.mock import patch

import app
import numpy as np
from src.desktop_runtime import DesktopControlState


class FakeWindow:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def set_text(self, text: str) -> None:
        self.messages.append(text)

    def set_bilingual(self, **_kwargs) -> None:
        return None


class WaitingTranslator:
    instances: list["WaitingTranslator"] = []

    def __init__(self, **_kwargs) -> None:
        self.closed = False
        self.start_calls = 0
        self.disconnect_calls = 0
        self.__class__.instances.append(self)

    async def start(self) -> None:
        self.start_calls += 1

    async def send_audio(self, _chunk) -> None:
        return None

    async def receive_translations(self):
        while not self.closed:
            await asyncio.sleep(0.01)
            if False:
                yield None

    async def close(self) -> None:
        self.closed = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1


class BrokenCapture:
    def __init__(self, **_kwargs) -> None:
        pass

    def stream_audio_chunks(
        self,
        stop_event=None,
        interrupt_event=None,
        pause_event=None,
    ):
        raise app.SystemAudioCaptureError("No usable loopback device was found")
        yield


class IdleCapture:
    def __init__(self, **_kwargs) -> None:
        pass

    def stream_audio_chunks(
        self,
        stop_event=None,
        interrupt_event=None,
        pause_event=None,
    ):
        while not stop_event.is_set() and not interrupt_event.is_set():
            stop_event.wait(0.01)
        return
        yield


class EndingTranslator(WaitingTranslator):
    async def receive_translations(self):
        if False:
            yield None


class PermanentFailureTranslator(WaitingTranslator):
    async def start(self) -> None:
        self.start_calls += 1
        raise RuntimeError(
            "Missing Gemini API key. Set gemini.api_key in config.yaml or set GEMINI_API_KEY."
        )


class RecordingTranslator(WaitingTranslator):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.audio: list[object] = []

    async def send_audio(self, chunk) -> None:
        self.audio.append(chunk)


class ContinuousCapture:
    instances: list["ContinuousCapture"] = []

    def __init__(self, speaker_name="", **_kwargs) -> None:
        self.speaker_name = speaker_name
        self.__class__.instances.append(self)

    def stream_audio_chunks(
        self,
        stop_event=None,
        interrupt_event=None,
        pause_event=None,
    ):
        while not stop_event.is_set() and not interrupt_event.is_set():
            time.sleep(0.01)
            yield b"\0\0"


class SwitchableCapture(ContinuousCapture):
    audible = False

    def stream_audio_chunks(
        self,
        stop_event=None,
        interrupt_event=None,
        pause_event=None,
    ):
        while not stop_event.is_set() and not interrupt_event.is_set():
            time.sleep(0.01)
            level = 0.1 if self.__class__.audible else 0.0
            yield np.full(160, level, dtype=np.float32)


class DesktopLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_wake_replays_first_audible_chunks_in_order(self) -> None:
        controls = DesktopControlState()
        stop_event = threading.Event()
        class WakeCapture:
            def __init__(self, **kwargs):
                pass

            def stream_audio_chunks(self, **kwargs):
                for _ in range(12):
                    time.sleep(0.01)
                    yield np.zeros(160, dtype=np.float32)
                for level in (0.1, 0.2, 0.3, 0.4):
                    time.sleep(0.01)
                    yield np.full(160, level, dtype=np.float32)
                while not stop_event.is_set():
                    stop_event.wait(0.01)

        RecordingTranslator.instances.clear()
        with patch.object(app, "GeminiLiveTranslator", RecordingTranslator), patch.object(app, "SystemAudioCapture", WakeCapture):
            task = asyncio.create_task(app.run_translation(
                {"audio": {"chunk_ms": 10, "silence_sleep_after_seconds": 0.1,
                           "silence_wake_chunks": 3}},
                FakeWindow(), stop_event, controls,
            ))
            try:
                # Windows runners can schedule the capture thread much later
                # than the event loop; wait for the observed audio, not ticks.
                deadline = asyncio.get_running_loop().time() + 5
                audio = []
                while asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                    if task.done():
                        await task
                        break
                    if RecordingTranslator.instances:
                        audio = RecordingTranslator.instances[-1].audio
                        if any(float(chunk[0]) > 0.35 for chunk in audio):
                            break
                audible = [round(float(chunk[0]), 1) for chunk in audio if float(chunk[0]) > 0]
                self.assertEqual(audible, [0.1, 0.2, 0.3, 0.4])
            finally:
                stop_event.set()
                await asyncio.wait_for(task, timeout=2)

    async def test_full_audio_queue_discards_oldest_chunk(self) -> None:
        audio_queue: queue.Queue[int] = queue.Queue(maxsize=3)
        for value in (1, 2, 3, 4):
            app.enqueue_latest(audio_queue, value)

        self.assertEqual(
            [audio_queue.get_nowait() for _ in range(audio_queue.qsize())],
            [2, 3, 4],
        )

    async def test_audio_capture_error_is_shown_before_desktop_worker_stops(self) -> None:
        window = FakeWindow()
        stop_event = threading.Event()
        with (
            patch.object(app, "GeminiLiveTranslator", WaitingTranslator),
            patch.object(app, "SystemAudioCapture", BrokenCapture),
        ):
            await asyncio.wait_for(
                app.run_translation(
                    {"gemini": {"reconnect": False}, "app": {}, "audio": {}},
                    window,
                    stop_event,
                ),
                timeout=1,
            )

        self.assertIn("No usable loopback device was found", window.messages)

    async def test_finished_receive_task_stops_fake_running_desktop(self) -> None:
        window = FakeWindow()
        stop_event = threading.Event()
        with (
            patch.object(app, "GeminiLiveTranslator", EndingTranslator),
            patch.object(app, "SystemAudioCapture", IdleCapture),
        ):
            await asyncio.wait_for(
                app.run_translation(
                    {"gemini": {"reconnect": False}, "app": {}, "audio": {}},
                    window,
                    stop_event,
                ),
                timeout=1,
            )

        self.assertTrue(
            any("Gemini receiver ended unexpectedly" in message for message in window.messages)
        )

    async def test_permanent_connection_failure_is_not_retried_forever(self) -> None:
        PermanentFailureTranslator.instances.clear()
        window = FakeWindow()
        stop_event = threading.Event()
        with (
            patch.object(app, "GeminiLiveTranslator", PermanentFailureTranslator),
            patch.object(app, "SystemAudioCapture", IdleCapture),
        ):
            await asyncio.wait_for(
                app.run_translation(
                    {"gemini": {"reconnect": True}, "app": {}, "audio": {}},
                    window,
                    stop_event,
                ),
                timeout=1,
            )

        translator = PermanentFailureTranslator.instances[-1]
        self.assertEqual(translator.start_calls, 1)
        self.assertTrue(
            any("Missing Gemini API key" in message for message in window.messages)
        )

    async def test_pause_drops_audio_until_translation_is_resumed(self) -> None:
        RecordingTranslator.instances.clear()
        controls = DesktopControlState()
        controls.toggle_paused()
        window = FakeWindow()
        stop_event = threading.Event()
        with (
            patch.object(app, "GeminiLiveTranslator", RecordingTranslator),
            patch.object(app, "SystemAudioCapture", ContinuousCapture),
        ):
            task = asyncio.create_task(
                app.run_translation(
                    {"gemini": {"reconnect": False}, "app": {}, "audio": {}},
                    window,
                    stop_event,
                    controls,
                )
            )
            await asyncio.sleep(0.12)
            translator = RecordingTranslator.instances[-1]
            self.assertEqual(translator.audio, [])
            controls.toggle_paused()
            # Connection completion is observed by the supervisor on its next
            # tick. Wait for the behavior rather than a scheduler-dependent delay.
            for _ in range(100):
                if translator.audio:
                    break
                await asyncio.sleep(0.01)
            stop_event.set()
            await asyncio.wait_for(task, timeout=1)

        self.assertGreater(len(translator.audio), 0)

    async def test_changing_device_restarts_capture_with_new_speaker(self) -> None:
        ContinuousCapture.instances.clear()
        controls = DesktopControlState("Speakers")
        window = FakeWindow()
        stop_event = threading.Event()
        with (
            patch.object(app, "GeminiLiveTranslator", WaitingTranslator),
            patch.object(app, "SystemAudioCapture", ContinuousCapture),
        ):
            task = asyncio.create_task(
                app.run_translation(
                    {"gemini": {"reconnect": False}, "app": {}, "audio": {}},
                    window,
                    stop_event,
                    controls,
                )
            )
            await asyncio.sleep(0.08)
            controls.select_audio_device("Headphones")
            await asyncio.sleep(0.12)
            stop_event.set()
            await asyncio.wait_for(task, timeout=1)

        self.assertEqual(
            [capture.speaker_name for capture in ContinuousCapture.instances[:2]],
            ["Speakers", "Headphones"],
        )

    async def test_silence_disconnects_and_sound_reconnects_gemini(self) -> None:
        WaitingTranslator.instances.clear()
        SwitchableCapture.audible = False
        controls = DesktopControlState()
        window = FakeWindow()
        stop_event = threading.Event()
        config = {
            "gemini": {"reconnect": False},
            "app": {},
            "audio": {
                "chunk_ms": 10,
                "silence_sleep_enabled": True,
                "silence_sleep_after_seconds": 0.04,
                "silence_rms_threshold": 0.0015,
                "silence_wake_chunks": 2,
            },
        }
        with (
            patch.object(app, "GeminiLiveTranslator", WaitingTranslator),
            patch.object(app, "SystemAudioCapture", SwitchableCapture),
        ):
            task = asyncio.create_task(
                app.run_translation(config, window, stop_event, controls)
            )
            try:
                await asyncio.sleep(0.02)
                translator = WaitingTranslator.instances[-1]
                for _ in range(50):
                    if translator.disconnect_calls:
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(controls.auto_sleep_event.is_set())
                self.assertGreaterEqual(translator.disconnect_calls, 1)

                SwitchableCapture.audible = True
                for _ in range(50):
                    if not controls.auto_sleep_event.is_set() and translator.start_calls >= 2:
                        break
                    await asyncio.sleep(0.02)
                self.assertFalse(controls.auto_sleep_event.is_set())
                self.assertGreaterEqual(translator.start_calls, 2)
            finally:
                stop_event.set()
                await asyncio.wait_for(task, timeout=1)

        self.assertTrue(
            any("Gemini is sleeping" in message for message in window.messages)
        )


if __name__ == "__main__":
    unittest.main()
