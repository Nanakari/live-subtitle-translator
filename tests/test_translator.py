from __future__ import annotations

import logging
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock, Mock

from src.gemini_live_translate import (
    GeminiLiveTranslator,
    WebSocketControlDiagnostics,
    WebSocketControlLogHandler,
)


class RecordingSink:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, message: str, *args) -> None:
        self.lines.append(("info", message % args))

    def warning(self, message: str, *args) -> None:
        self.lines.append(("warning", message % args))


class FakeSessionContext:
    def __init__(self) -> None:
        self.exit_calls = 0

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        self.exit_calls += 1


class MessageSession:
    def __init__(self, messages) -> None:
        self.messages = messages

    async def receive(self):
        for message in self.messages:
            yield message


class RotatingTranslator(GeminiLiveTranslator):
    def __init__(self, replacement_session, replacement_context) -> None:
        super().__init__(api_key="test")
        self.replacement_session = replacement_session
        self.replacement_context = replacement_context
        self.start_handles: list[str | None] = []

    async def start(self) -> None:
        if self._session is not None:
            return
        self.start_handles.append(self._resumption_handle)
        self._session = self.replacement_session
        self._session_cm = self.replacement_context
        self._closed = False


class TranslatorLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_releases_both_sdk_clients_and_is_idempotent(self) -> None:
        translator = GeminiLiveTranslator(api_key="test")
        client = SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock()), close=Mock())
        translator._client = client
        translator._session = object()
        translator._session_cm = FakeSessionContext()
        await translator.close()
        await translator.close()
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()
        self.assertIsNone(translator._client)

    async def test_cancelled_connect_closes_sdk_clients(self) -> None:
        import asyncio
        from google import genai
        client = SimpleNamespace(
            aio=SimpleNamespace(
                aclose=AsyncMock(),
                live=SimpleNamespace(connect=Mock(return_value=SimpleNamespace(
                    __aenter__=AsyncMock(side_effect=asyncio.CancelledError),
                ))),
            ),
            close=Mock(),
        )
        translator = GeminiLiveTranslator(api_key="test")
        with patch.object(genai, "Client", return_value=client):
            with self.assertRaises(asyncio.CancelledError):
                await translator.start()
        client.aio.aclose.assert_awaited_once()
        client.close.assert_called_once()

    async def test_old_session_error_cannot_close_a_replacement_session(self) -> None:
        translator = GeminiLiveTranslator(api_key="test")
        old_session = object()
        replacement_session = object()
        replacement_context = FakeSessionContext()
        translator._session = replacement_session
        translator._session_cm = replacement_context

        await translator._drop_session(expected_session=old_session)

        self.assertIs(translator._session, replacement_session)
        self.assertIs(translator._session_cm, replacement_context)
        self.assertEqual(replacement_context.exit_calls, 0)

    async def test_matching_session_is_closed(self) -> None:
        translator = GeminiLiveTranslator(api_key="test")
        session = object()
        context = FakeSessionContext()
        translator._session = session
        translator._session_cm = context

        await translator._drop_session(expected_session=session)

        self.assertIsNone(translator._session)
        self.assertIsNone(translator._session_cm)
        self.assertEqual(context.exit_calls, 1)

    async def test_disconnect_allows_the_translator_to_be_reused(self) -> None:
        translator = GeminiLiveTranslator(api_key="test")
        session = object()
        context = FakeSessionContext()
        translator._session = session
        translator._session_cm = context
        translator._closed = False

        await translator.disconnect()

        self.assertIsNone(translator._session)
        self.assertFalse(translator._closed)
        self.assertEqual(context.exit_calls, 1)

    async def test_control_messages_store_handle_and_report_go_away(self) -> None:
        translator = GeminiLiveTranslator(api_key="test")
        update_message = SimpleNamespace(
            session_resumption_update=SimpleNamespace(
                resumable=True,
                new_handle="resume-token",
            ),
            go_away=None,
        )
        go_away_message = SimpleNamespace(
            session_resumption_update=None,
            go_away=SimpleNamespace(time_left="50s"),
        )

        self.assertIsNone(translator._process_control_message(update_message))
        self.assertEqual(translator._resumption_handle, "resume-token")
        self.assertEqual(translator._process_control_message(go_away_message), "50s")

    async def test_turn_complete_is_carried_from_server_content(self) -> None:
        translator = GeminiLiveTranslator(api_key="test")
        message = SimpleNamespace(
            server_content=SimpleNamespace(
                input_transcription=SimpleNamespace(text="こんにちは"),
                output_transcription=SimpleNamespace(text="你好"),
                turn_complete=True,
                model_turn=None,
            )
        )

        event = translator._message_to_event(message)

        self.assertIsNotNone(event)
        self.assertTrue(event.turn_complete)
        self.assertEqual(event.input_text, "こんにちは")
        self.assertEqual(event.output_text, "你好")

    async def test_go_away_rotates_before_abort_and_uses_resumption_handle(self) -> None:
        update = SimpleNamespace(
            session_resumption_update=SimpleNamespace(
                resumable=True,
                new_handle="resume-token",
            ),
            go_away=None,
            server_content=None,
        )
        go_away = SimpleNamespace(
            session_resumption_update=None,
            go_away=SimpleNamespace(time_left="50s"),
            server_content=None,
        )
        transcript = SimpleNamespace(
            session_resumption_update=None,
            go_away=None,
            server_content=SimpleNamespace(
                input_transcription=SimpleNamespace(text="hello"),
                output_transcription=SimpleNamespace(text="你好"),
                model_turn=None,
            ),
        )
        old_context = FakeSessionContext()
        replacement_context = FakeSessionContext()
        translator = RotatingTranslator(
            MessageSession([transcript]),
            replacement_context,
        )
        translator._session = MessageSession([update, go_away])
        translator._session_cm = old_context

        events = translator.receive_translations()
        event = await events.__anext__()
        await events.aclose()
        await translator.close()

        self.assertEqual(event.input_text, "hello")
        self.assertEqual(event.output_text, "你好")
        self.assertEqual(old_context.exit_calls, 1)
        self.assertEqual(translator.start_handles, ["resume-token"])

    async def test_live_config_enables_resumption_and_context_compression(self) -> None:
        translator = GeminiLiveTranslator(api_key="test")
        translator._resumption_handle = "resume-token"

        config = translator._build_config()

        self.assertEqual(config.session_resumption.handle, "resume-token")
        self.assertIsNotNone(config.context_window_compression.sliding_window)

    async def test_websocket_keepalive_does_not_close_on_delayed_pong(self) -> None:
        translator = GeminiLiveTranslator(api_key="test")

        options = translator._build_http_options()

        self.assertEqual(options.async_client_args["ping_interval"], 20.0)
        self.assertIsNone(options.async_client_args["ping_timeout"])
        self.assertEqual(options.async_client_args["close_timeout"], 10.0)

    async def test_server_data_watchdog_requires_active_audio_and_stale_data(self) -> None:
        translator = GeminiLiveTranslator(
            api_key="test",
            server_data_timeout_seconds=120.0,
        )
        translator._session_started_at = 100.0

        self.assertFalse(translator._server_data_is_stalled(220.0))

        translator._last_audio_sent_at = 219.0
        self.assertTrue(translator._server_data_is_stalled(220.0))

        translator._last_server_message_at = 219.5
        self.assertFalse(translator._server_data_is_stalled(220.0))

    async def test_server_data_watchdog_ignores_inactive_audio(self) -> None:
        translator = GeminiLiveTranslator(
            api_key="test",
            server_data_timeout_seconds=120.0,
        )
        translator._session_started_at = 100.0
        translator._last_server_message_at = 100.0
        translator._last_audio_sent_at = 100.0

        self.assertFalse(translator._server_data_is_stalled(220.0))

    async def test_server_data_watchdog_drops_a_stalled_active_session(self) -> None:
        translator = GeminiLiveTranslator(
            api_key="test",
            server_data_timeout_seconds=0.5,
        )
        session = object()
        context = FakeSessionContext()
        now = time.monotonic()
        translator._session = session
        translator._session_cm = context
        translator._session_started_at = now - 1.0
        translator._last_audio_sent_at = now

        await translator._server_data_watchdog(session)

        self.assertIsNone(translator._session)
        self.assertEqual(context.exit_calls, 1)

    async def test_subtitle_watchdog_detects_control_only_connection(self) -> None:
        translator = GeminiLiveTranslator(
            api_key="test",
            subtitle_data_timeout_seconds=45.0,
        )
        translator._session_started_at = 100.0
        translator._last_server_message_at = 149.5
        translator._last_audio_sent_at = 149.8

        self.assertTrue(translator._subtitle_data_is_stalled(150.0))

        translator._last_subtitle_message_at = 149.7
        self.assertFalse(translator._subtitle_data_is_stalled(150.0))

    async def test_translation_watchdog_detects_input_without_output(self) -> None:
        translator = GeminiLiveTranslator(
            api_key="test",
            translation_output_timeout_seconds=45.0,
        )
        translator._last_audio_sent_at = 149.8
        translator._last_input_transcription_at = 149.5
        translator._translation_wait_started_at = 100.0

        self.assertTrue(translator._translation_output_is_stalled(150.0))

        translator._last_output_translation_at = 149.7
        translator._translation_wait_started_at = None
        self.assertFalse(translator._translation_output_is_stalled(150.0))

    async def test_control_logger_records_ping_pong_without_data_frames(self) -> None:
        sink = RecordingSink()
        diagnostics = WebSocketControlDiagnostics("test-1", sink)
        handler = WebSocketControlLogHandler(diagnostics)

        with patch(
            "src.gemini_live_translate.time.monotonic",
            side_effect=[100.0, 101.0, 102.0, 103.0],
        ):
            handler.emit(logging.LogRecord("ws", logging.DEBUG, "", 0, "> BINARY secret", (), None))
            handler.emit(logging.LogRecord("ws", logging.DEBUG, "", 0, "% sent keepalive ping", (), None))
            diagnostics.note_server_message(has_translation=False)
            diagnostics.note_server_message(has_translation=True)
            handler.emit(logging.LogRecord("ws", logging.DEBUG, "", 0, "% received keepalive pong", (), None))

        self.assertEqual(len(sink.lines), 2)
        self.assertIn("event=ping_sent", sink.lines[0][1])
        self.assertIn("event=pong_received", sink.lines[1][1])
        self.assertIn("rtt_ms=3000", sink.lines[1][1])
        self.assertIn("server_messages_since_ping=2", sink.lines[1][1])
        self.assertIn("translation_messages_since_ping=1", sink.lines[1][1])
        self.assertNotIn("secret", " ".join(line for _, line in sink.lines))

    async def test_pong_timeout_reports_translation_activity_during_wait(self) -> None:
        sink = RecordingSink()
        diagnostics = WebSocketControlDiagnostics("test-2", sink)
        handler = WebSocketControlLogHandler(diagnostics)

        with patch(
            "src.gemini_live_translate.time.monotonic",
            side_effect=[200.0, 201.0, 260.0],
        ):
            handler.emit(logging.LogRecord("ws", logging.DEBUG, "", 0, "% sent keepalive ping", (), None))
            diagnostics.note_server_message(has_translation=True)
            handler.emit(
                logging.LogRecord(
                    "ws",
                    logging.DEBUG,
                    "",
                    0,
                    "- timed out waiting for keepalive pong",
                    (),
                    None,
                )
            )

        timeout_line = sink.lines[-1][1]
        self.assertEqual(sink.lines[-1][0], "warning")
        self.assertIn("event=pong_timeout", timeout_line)
        self.assertIn("waited_ms=60000", timeout_line)
        self.assertIn("translation_messages_since_ping=1", timeout_line)
        self.assertIn("last_translation_age_ms=59000", timeout_line)


if __name__ == "__main__":
    unittest.main()
