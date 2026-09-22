from __future__ import annotations

import asyncio
import queue
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from google import genai
from google.genai.errors import ClientError
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

import app
from src.connection_errors import is_resumption_rejected, is_retryable_connection_error
from src.gemini_live_translate import GeminiLiveTranslator, TranslationEvent
from tests import test_desktop as desktop
from tests import test_subtitle_window as subtitles


def http_error(status):
    return InvalidStatus(Response(status, "test rejection", Headers()))


def message(source=None, output=None, language=None):
    return SimpleNamespace(server_content=SimpleNamespace(
        input_transcription=SimpleNamespace(text=source, language_code=language) if source else None,
        output_transcription=SimpleNamespace(text=output) if output else None,
        turn_complete=False,
    ))


class Session:
    def __init__(self, *items):
        self.items = list(items)
        self.send_realtime_input = AsyncMock()

    async def receive(self):
        while self.items:
            item = self.items.pop(0)
            if isinstance(item, Exception):
                raise item
            yield item
        await asyncio.Event().wait()


class SDK:
    """Exercise real translator lifecycle/configuration without network access."""
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.handles = []
        self.clients = []
        self.contexts = []

    def client(self, **kwargs):
        outcome = self.outcomes.pop(0)
        enter = AsyncMock(side_effect=outcome) if isinstance(outcome, Exception) else AsyncMock(return_value=outcome)
        context = SimpleNamespace(__aenter__=enter, __aexit__=AsyncMock())
        self.contexts.append(context)

        def connect(*, model, config):
            self.handles.append(config.session_resumption.handle if config.session_resumption else None)
            return context

        client = SimpleNamespace(aio=SimpleNamespace(
            live=SimpleNamespace(connect=connect), aclose=AsyncMock()), close=Mock())
        self.clients.append(client)
        return client


def translator(**kwargs):
    return GeminiLiveTranslator(api_key="test", reconnect_delay_seconds=0,
        server_data_timeout_seconds=0, subtitle_data_timeout_seconds=0,
        translation_output_timeout_seconds=0, **kwargs)


class RetryPolicyTests(unittest.TestCase):
    def test_permanent_http_statuses_never_retry_despite_connection_in_message(self):
        for status in (400, 401, 403, 404, 405, 422):
            with self.subTest(status=status):
                exc = http_error(status)
                self.assertIn("connection", str(exc))
                self.assertFalse(is_retryable_connection_error(exc))
                self.assertFalse(is_retryable_connection_error(ClientError(status, {"message": "connection failed"})))

    def test_transient_failures_remain_retryable(self):
        for exc in [http_error(c) for c in (408, 429, 500, 502, 503, 504)] + [
            OSError("offline"), asyncio.TimeoutError(),
            ConnectionClosedError(Close(1011, "server error"), None),
            ConnectionClosedError(Close(1008, "session duration exceeded"), None),
        ]:
            with self.subTest(exc=exc):
                self.assertTrue(is_retryable_connection_error(exc))

    def test_policy_violation_is_not_treated_as_scheduled_rotation(self):
        self.assertFalse(is_retryable_connection_error(
            ConnectionClosedError(Close(1008, "invalid model configuration"), None)))

    def test_only_explicit_session_rejection_allows_handle_fallback(self):
        for reason in ("BidiGenerateContent session not found", "Session has expired",
                       "Invalid session resumption handle", "resumption token is not valid"):
            self.assertTrue(is_resumption_rejected(RuntimeError(reason)), reason)
        for exc in (http_error(404), OSError("offline"),
                    ClientError(401, {"message": "invalid session resumption handle"}),
                    ClientError(403, {"message": "session not found"}),
                    ClientError(404, {"message": "model not found"})):
            self.assertFalse(is_resumption_rejected(exc))


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_permanent_failure_is_cached_across_concurrent_callers(self):
        sdk, t = SDK(http_error(401)), translator()
        with patch.object(genai, "Client", side_effect=sdk.client):
            results = await asyncio.gather(t.start(), t.start(), return_exceptions=True)
            self.assertTrue(all(isinstance(result, InvalidStatus) for result in results))
            self.assertEqual(len(sdk.clients), 1)
            await t.close()

    async def test_receive_reconnect_emits_fatal_once_and_stops(self):
        sdk, t = SDK(Session(ConnectionError("network reset")), http_error(403)), translator()
        with patch.object(genai, "Client", side_effect=sdk.client):
            stream = t.receive_translations()
            self.assertTrue((await stream.__anext__()).session_started)
            self.assertFalse((await stream.__anext__()).fatal)
            fatal = await stream.__anext__()
            self.assertTrue(fatal.fatal)
            with self.assertRaises(StopAsyncIteration):
                await stream.__anext__()
            with self.assertRaises(InvalidStatus):
                await t.send_audio(b"\0\0")
            self.assertEqual(len(sdk.clients), 2)
            await t.close()

    async def test_permanent_send_failure_blocks_a_reconnect(self):
        session = Session()
        session.send_realtime_input.side_effect = http_error(400)
        sdk, t = SDK(session), translator()
        with patch.object(genai, "Client", side_effect=sdk.client):
            await t.start()
            with self.assertRaises(InvalidStatus):
                await t.send_audio(b"\0\0")
            with self.assertRaises(InvalidStatus):
                await t.start()
            self.assertEqual(len(sdk.clients), 1)
            await t.close()

    async def test_rejected_handle_falls_back_once_to_a_new_logical_session(self):
        rejection = ConnectionClosedError(Close(1008, "BidiGenerateContent session not found"), None)
        sdk, t = SDK(rejection, Session()), translator()
        t._resumption_handle = "expired-handle"
        t._session_generation = 3
        with patch.object(genai, "Client", side_effect=sdk.client):
            await t.start()
            self.assertEqual(sdk.handles, ["expired-handle", None])
            self.assertEqual(t._session_generation, 4)
            sdk.clients[0].aio.aclose.assert_awaited_once()
            sdk.clients[0].close.assert_called_once()
            await t.close()

    async def test_fresh_fallback_failure_does_not_repeat_the_fallback(self):
        sdk = SDK(ClientError(400, {"message": "invalid session resumption handle"}), http_error(403))
        t = translator()
        t._resumption_handle = "expired-handle"
        with patch.object(genai, "Client", side_effect=sdk.client):
            for _ in range(2):
                with self.assertRaises(InvalidStatus):
                    await t.start()
            self.assertEqual(sdk.handles, ["expired-handle", None])
            await t.close()

    async def test_transient_outage_keeps_valid_handle_and_session_identity(self):
        sdk, t = SDK(OSError("offline"), Session()), translator()
        t._resumption_handle = "valid-handle"
        t._session_generation = 3
        with patch.object(genai, "Client", side_effect=sdk.client):
            with self.assertRaises(OSError):
                await t.start()
            await t.start()
            self.assertEqual(sdk.handles, ["valid-handle", "valid-handle"])
            self.assertEqual(t._session_generation, 3)
            await t.close()

    async def test_authentication_failure_does_not_discard_handle_or_fall_back(self):
        sdk, t = SDK(http_error(401)), translator()
        t._resumption_handle = "valid-handle"
        with patch.object(genai, "Client", side_effect=sdk.client):
            with self.assertRaises(InvalidStatus):
                await t.start()
            self.assertEqual(sdk.handles, ["valid-handle"])
            self.assertEqual(t._resumption_handle, "valid-handle")
            await t.close()

    async def test_desktop_auto_reconnect_resets_ui_before_first_new_translation(self):
        old = Session(message("old source", "OLD_UNFINISHED"), ConnectionError("offline"))
        new = Session(message(None, "NEW_TEXT"))
        sdk = SDK(old, new)
        stop = threading.Event()

        class Translator(GeminiLiveTranslator):
            def __init__(self, **kwargs):
                super().__init__(api_key="test", reconnect_delay_seconds=0,
                    server_data_timeout_seconds=0, subtitle_data_timeout_seconds=0,
                    translation_output_timeout_seconds=0)

        class Window(desktop.FakeWindow):
            def __init__(self):
                super().__init__()
                self.state = subtitles.SubtitleWindowTests().make_window()
                self.resets = []
            def reset_pairing(self, session_id=None):
                self.resets.append(session_id)
                self.state._consume_update("reset_pairing", None, None, session_id=session_id)
            def set_bilingual(self, input_text=None, output_text=None, turn_complete=False, session_id=None):
                self.state._consume_update("bilingual", input_text, output_text, turn_complete, session_id)
                if output_text == "NEW_TEXT":
                    stop.set()

        window = Window()
        with patch.object(genai, "Client", side_effect=sdk.client), patch.object(app, "GeminiLiveTranslator", Translator), patch.object(app, "SystemAudioCapture", desktop.IdleCapture):
            await asyncio.wait_for(app.run_translation(
                {"gemini": {"show_input_transcription": True}}, window, stop), 2)
        self.assertEqual(window.resets, [1, 2])
        self.assertEqual(window.state._current_zh, "NEW_TEXT")
        self.assertEqual(window.state._ja_fragments, [])


class WatchdogTests(unittest.TestCase):
    def make_translator(self, **kwargs):
        t = GeminiLiveTranslator(api_key="test", **kwargs)
        t._session_started_at = 100
        t._last_audio_sent_at = 150
        t._last_server_message_at = 150
        return t

    def test_target_language_or_unknown_language_does_not_arm_content_watchdogs(self):
        for language in (None, "zh", "zh-CN", "zh-Hans", "zh_TW"):
            with self.subTest(language=language):
                t = self.make_translator()
                for now in (100, 150):
                    t._observe_transcription(TranslationEvent(input_text="source", input_language_code=language), now)
                self.assertFalse(t._translation_output_is_stalled(150))
                self.assertFalse(t._subtitle_data_is_stalled(150))
                self.assertFalse(t._server_data_is_stalled(150))

    def test_music_without_transcription_does_not_arm_subtitle_watchdog(self):
        t = self.make_translator(echo_target_language=True)
        self.assertFalse(t._subtitle_data_is_stalled(150))

    def test_confirmed_foreign_language_or_echo_still_detects_stalled_translation(self):
        for echo, language in ((False, "ja"), (True, None), (True, "zh")):
            t = self.make_translator(echo_target_language=echo)
            for now in (100, 150):
                t._observe_transcription(TranslationEvent(input_text="source", input_language_code=language), now)
            self.assertTrue(t._translation_output_is_stalled(150))

    def test_language_switch_and_turn_end_disarm_previous_pending_output(self):
        t = self.make_translator()
        t._observe_transcription(TranslationEvent(input_text="foreign", input_language_code="ja"), 100)
        t._observe_transcription(TranslationEvent(input_text="target", input_language_code="zh"), 150)
        self.assertFalse(t._translation_output_is_stalled(150))
        t._observe_transcription(TranslationEvent(input_text="foreign", input_language_code="ja"), 160)
        self.assertEqual(t._translation_wait_started_at, 160)
        t._observe_transcription(TranslationEvent(turn_complete=True), 170)
        self.assertIsNone(t._translation_wait_started_at)
        self.assertFalse(t._translation_expected)

    def test_optional_language_code_survives_message_conversion(self):
        t = self.make_translator()
        self.assertEqual(t._message_to_event(message("source", language="ja")).input_language_code, "ja")
        event = t._message_to_event(SimpleNamespace(server_content={
            "inputTranscription": {"text": "source", "languageCode": "zh-CN"}}))
        self.assertEqual(event.input_language_code, "zh-CN")


class SubtitleSessionTests(unittest.TestCase):
    def make_window(self):
        w = subtitles.SubtitleWindowTests().make_window()
        w._queue = queue.Queue(maxsize=2)
        w.root = Mock()
        w._render_history = Mock()
        return w

    def test_explicit_reset_discards_all_unfinished_and_dedup_state(self):
        w = self.make_window()
        w._pending_zh_segments = [(10, 10, 10, "OLD_PENDING", False)]
        w._carry_ja, w._carry_zh, w._carry_started_at = "old", "old", 10
        w._history = [{"ja": "old", "zh": "old"}]
        w._recent_blocks = [("old", "old")]
        w._turn_complete_pending = True
        w._consume_update("reset_pairing", None, None)
        w._consume_update("bilingual", None, "NEW_TEXT!")
        self.assertEqual([s[3] for s in w._pending_zh_segments], ["NEW_TEXT!"])
        self.assertEqual(w._ja_fragments, [])
        self.assertEqual((w._carry_ja, w._carry_zh), ("", ""))
        self.assertIsNone(w._carry_started_at)
        self.assertFalse(w._turn_complete_pending)
        self.assertEqual(w._history, [])
        self.assertEqual(w._recent_blocks, [])

    def test_successful_resumption_keeps_pending_text_in_same_logical_session(self):
        w = self.make_window()
        w._consume_update("bilingual", "source", "FIRST", session_id=1)
        w._consume_update("bilingual", None, "SECOND", session_id=1)
        self.assertEqual(w._current_zh, "FIRSTSECOND")

    def test_session_tag_resets_even_when_overflow_drops_boundary_marker(self):
        w = self.make_window()
        w._consume_update("bilingual", "old", "OLD", session_id=1)
        w.reset_pairing(session_id=2)
        w.set_bilingual(output_text="NEW", session_id=2)
        w.set_bilingual(output_text="_TAIL", session_id=2)
        w._poll_text()
        self.assertEqual(w._current_zh, "NEW_TAIL")
        self.assertEqual(w._ja_fragments, [])

    def test_retired_session_packets_cannot_finish_or_contaminate_new_turn(self):
        w = self.make_window()
        w._consume_update("bilingual", "new source", "NEW", session_id=2)
        w.set_bilingual(input_text="old source", output_text="OLD!", turn_complete=True, session_id=1)
        w._poll_text()
        self.assertEqual(w._current_zh, "NEW")
        self.assertFalse(w._turn_complete_pending)
        self.assertEqual(w._pending_zh_segments, [])


if __name__ == "__main__":
    unittest.main()
