from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import app
from src.desktop_runtime import DesktopControlState
from src.gemini_live_translate import GeminiLiveTranslator
from tests import test_desktop as desktop


async def eventually(predicate, timeout=1.5):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Expected state was not reached before the deadline")
        await asyncio.sleep(0.01)


class DesktopNetworkLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_cancels_pending_initial_connection(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Translator(desktop.WaitingTranslator):
            async def start(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        stop = threading.Event()
        with patch.object(app, "GeminiLiveTranslator", Translator):
            task = asyncio.create_task(app.run_translation({}, desktop.FakeWindow(), stop))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                stop.set()
                await asyncio.wait_for(task, 1)
                self.assertTrue(cancelled.is_set())
                self.assertTrue(Translator.instances[-1].closed)
            finally:
                stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_pause_cancels_connect_then_resume_establishes_a_new_connection(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Translator(desktop.RecordingTranslator):
            async def start(self):
                self.start_calls += 1
                if self.start_calls == 1:
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

        stop, controls = threading.Event(), DesktopControlState()
        with patch.object(app, "GeminiLiveTranslator", Translator), patch.object(app, "SystemAudioCapture", desktop.ContinuousCapture):
            task = asyncio.create_task(app.run_translation(
                {"audio": {"silence_sleep_enabled": False}}, desktop.FakeWindow(), stop, controls))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                controls.toggle_paused()
                translator = Translator.instances[-1]
                await eventually(lambda: translator.disconnect_calls == 1)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(translator.start_calls, 1)
                controls.toggle_paused()
                await eventually(lambda: bool(translator.audio))
                self.assertEqual(translator.start_calls, 2)
            finally:
                stop.set()
                await asyncio.wait_for(task, 1)

    async def test_pause_cancels_blocked_send_before_disconnect_and_can_resume(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Translator(desktop.RecordingTranslator):
            async def send_audio(self, chunk):
                if not entered.is_set():
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()
                else:
                    self.audio.append(chunk)

        stop, controls = threading.Event(), DesktopControlState()
        with patch.object(app, "GeminiLiveTranslator", Translator), patch.object(app, "SystemAudioCapture", desktop.ContinuousCapture):
            task = asyncio.create_task(app.run_translation(
                {"audio": {"silence_sleep_enabled": False}}, desktop.FakeWindow(), stop, controls))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                translator = Translator.instances[-1]
                controls.toggle_paused()
                await eventually(lambda: translator.disconnect_calls == 1)
                self.assertTrue(cancelled.is_set())
                self.assertEqual(translator.audio, [])
                controls.toggle_paused()
                await eventually(lambda: bool(translator.audio))
            finally:
                stop.set()
                await asyncio.wait_for(task, 1)

    async def test_stop_cancels_slow_disconnect(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()

        class Translator(desktop.WaitingTranslator):
            async def disconnect(self):
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

        stop, controls = threading.Event(), DesktopControlState()
        controls.toggle_paused()
        with patch.object(app, "GeminiLiveTranslator", Translator):
            task = asyncio.create_task(app.run_translation({}, desktop.FakeWindow(), stop, controls))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                stop.set()
                await asyncio.wait_for(task, 1)
                self.assertTrue(cancelled.is_set())
                self.assertTrue(Translator.instances[-1].closed)
            finally:
                stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_stop_interrupts_long_reconnect_backoff(self):
        entered = asyncio.Event()

        class Translator(desktop.WaitingTranslator):
            async def start(self):
                entered.set()
                raise OSError("simulated offline connection")

        stop = threading.Event()
        with patch.object(app, "GeminiLiveTranslator", Translator):
            task = asyncio.create_task(app.run_translation(
                {"gemini": {"reconnect_delay_seconds": 60}}, desktop.FakeWindow(), stop))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                stop.set()
                await asyncio.wait_for(task, 1)
                self.assertTrue(Translator.instances[-1].closed)
            finally:
                stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def test_resume_waits_for_pending_disconnect_before_connecting(self):
        entered, release = asyncio.Event(), asyncio.Event()

        class Translator(desktop.RecordingTranslator):
            instances = []

            async def disconnect(self):
                entered.set()
                await release.wait()
                self.disconnect_calls += 1

        stop, controls = threading.Event(), DesktopControlState()
        with patch.object(app, "GeminiLiveTranslator", Translator), patch.object(app, "SystemAudioCapture", desktop.ContinuousCapture):
            task = asyncio.create_task(app.run_translation(
                {"audio": {"silence_sleep_enabled": False}}, desktop.FakeWindow(), stop, controls))
            try:
                await eventually(lambda: bool(Translator.instances and Translator.instances[-1].audio))
                translator = Translator.instances[-1]
                controls.toggle_paused()
                await asyncio.wait_for(entered.wait(), 1)
                controls.toggle_paused()
                await asyncio.sleep(0.15)
                self.assertEqual(translator.start_calls, 1)
                release.set()
                await eventually(lambda: translator.start_calls == 2)
                self.assertEqual(translator.disconnect_calls, 1)
            finally:
                release.set()
                stop.set()
                await asyncio.wait_for(task, 1)


class TranslatorDeadlineTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        # The desktop imports the SDK during start(), before its first send.
        # Keep that one-off import cost outside the short I/O test deadlines.
        from google import genai  # noqa: F401

    async def test_setup_timeout_closes_clients_and_releases_connection_lock(self):
        from google import genai

        cancelled = asyncio.Event()

        async def hang():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        context = SimpleNamespace(__aenter__=AsyncMock(side_effect=hang))
        client = SimpleNamespace(
            aio=SimpleNamespace(live=SimpleNamespace(connect=Mock(return_value=context)), aclose=AsyncMock()),
            close=Mock(),
        )
        translator = GeminiLiveTranslator(api_key="test", connect_timeout_seconds=0.02)
        with patch.object(genai, "Client", return_value=client):
            with self.assertRaises(asyncio.TimeoutError) as raised:
                await asyncio.wait_for(translator.start(), 1)
        self.assertTrue(app.is_retryable_connection_error(raised.exception))
        self.assertTrue(cancelled.is_set())
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()
        self.assertIsNone(translator._session)
        self.assertIsNone(translator._client)
        self.assertFalse(translator._connect_lock.locked())

        # A timeout must not leave an unusable translator or held lock behind.
        replacement_context = SimpleNamespace(__aenter__=AsyncMock(return_value=object()), __aexit__=AsyncMock())
        replacement_client = SimpleNamespace(
            aio=SimpleNamespace(live=SimpleNamespace(connect=Mock(return_value=replacement_context)), aclose=AsyncMock()),
            close=Mock(),
        )
        with patch.object(genai, "Client", return_value=replacement_client):
            await asyncio.wait_for(translator.start(), 1)
        self.assertIsNotNone(translator._session)
        await asyncio.wait_for(translator.close(), 1)
        replacement_context.__aexit__.assert_awaited_once()
        replacement_client.aio.aclose.assert_awaited_once()

    async def test_send_timeout_discards_session_for_reconnect(self):
        async def hang(**kwargs):
            await asyncio.Event().wait()

        translator = GeminiLiveTranslator(api_key="test", send_timeout_seconds=0.02)
        translator._session = SimpleNamespace(send_realtime_input=AsyncMock(side_effect=hang))
        context = SimpleNamespace(__aexit__=AsyncMock())
        translator._session_cm = context
        await asyncio.wait_for(translator.send_audio(b"\0\0"), 1)
        context.__aexit__.assert_awaited_once()
        self.assertIsNone(translator._session)

    async def test_send_timeout_is_reported_when_reconnect_is_disabled(self):
        async def hang(**kwargs):
            await asyncio.Event().wait()

        translator = GeminiLiveTranslator(api_key="test", reconnect=False, send_timeout_seconds=0.02)
        translator._session = SimpleNamespace(send_realtime_input=AsyncMock(side_effect=hang))
        translator._session_cm = SimpleNamespace(__aexit__=AsyncMock())
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(translator.send_audio(b"\0\0"), 1)
        self.assertIsNone(translator._session)

    async def test_slow_socket_and_client_close_are_both_bounded(self):
        async def hang(*args):
            await asyncio.Event().wait()

        translator = GeminiLiveTranslator(api_key="test", cleanup_timeout_seconds=0.02)
        context = SimpleNamespace(__aexit__=AsyncMock(side_effect=hang))
        client = SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock(side_effect=hang)), close=Mock())
        translator._session = object()
        translator._session_cm = context
        translator._client = client
        await asyncio.wait_for(translator.close(), 1)
        context.__aexit__.assert_awaited_once()
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()
        self.assertIsNone(translator._session)
        self.assertIsNone(translator._client)
        self.assertFalse(translator._connect_lock.locked())


if __name__ == "__main__":
    unittest.main()
