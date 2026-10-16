from __future__ import annotations

import asyncio
import logging
import io
import unittest
from unittest.mock import Mock, patch

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Frame, Opcode
from google import genai

from live_subtitle_translator import app
from live_subtitle_translator.gemini_live_translate import (
    GeminiLiveTranslator, WebSocketControlDiagnostics, WebSocketControlLogHandler,
)
from tests.test_translator import RecordingSink
from tests.test_connection_recovery import SDK, Session, translator
from live_subtitle_translator.logger import SecretFormatter


def record(message, *args):
    return logging.LogRecord("ws", logging.DEBUG, "", 0, message, args, None)


class ControlFrameDiagnosticTests(unittest.TestCase):
    def test_real_frame_messages_report_pong_without_waiter_and_without_duplicates(self):
        sink = RecordingSink()
        handler = WebSocketControlLogHandler(WebSocketControlDiagnostics("test", sink))
        payload = b"private-ping"
        with patch("live_subtitle_translator.gemini_live_translate.time.monotonic", return_value=100):
            handler.emit(record("> %s", Frame(Opcode.PING, payload)))
            handler.emit(record("% sent keepalive ping"))
        handler.emit(record("< %s", Frame(Opcode.PONG, b"unsolicited")))
        self.assertEqual(len(sink.lines), 1)
        with patch("live_subtitle_translator.gemini_live_translate.time.monotonic", return_value=100.25):
            handler.emit(record("< %s", Frame(Opcode.PONG, payload)))
            handler.emit(record("% received keepalive pong"))
        self.assertEqual(len(sink.lines), 2)
        self.assertIn("rtt_ms=250", sink.lines[-1][1])
        self.assertNotIn("private-ping", str(sink.lines))
        self.assertNotIn("unsolicited", str(sink.lines))

    def test_data_frames_and_headers_are_never_formatted(self):
        class PrivateFrame(Frame):
            def __str__(self):
                raise AssertionError("Must not format user data")

        class PrivateHeader:
            def __str__(self):
                raise AssertionError("Must not format credentials")

        sink = RecordingSink()
        handler = WebSocketControlLogHandler(WebSocketControlDiagnostics("test", sink))
        handler.emit(record("< %s", PrivateFrame(Opcode.TEXT, b"private transcript")))
        handler.emit(record("> %s", PrivateFrame(Opcode.BINARY, b"private audio")))
        handler.emit(record("> %s: %s", "Authorization", PrivateHeader()))
        handler.emit(record("< %s", Frame(Opcode.PONG, b"unsolicited")))
        self.assertEqual(sink.lines, [])

    def test_transport_failure_records_activity_before_session_reset(self):
        sink = RecordingSink()
        translator = GeminiLiveTranslator(api_key="test", logger=sink)
        translator._websocket_diagnostics = WebSocketControlDiagnostics("connection-7", sink)
        translator._session_started_at = 100
        translator._last_audio_sent_at = 119.9
        translator._last_server_message_at = 119.8
        failure = ConnectionClosedError(None, None)
        failure.__cause__ = ConnectionResetError(10054, "private host and credential")
        with patch("live_subtitle_translator.gemini_live_translate.time.monotonic", return_value=120):
            translator._log_transport_failure("receive", failure)
        level, line = sink.lines[0]
        self.assertEqual(level, "warning")
        for expected in ("stage=receive", "connection=connection-7", "errno=10054",
                         "session_age_ms=20000", "last_audio_age_ms=100",
                         "last_server_data_age_ms=200", "abnormal_eof=true"):
            self.assertIn(expected, line)
        self.assertNotIn("private", line)
        self.assertFalse(app.is_expected_reconnect_message(str(failure)))


class RealWebSocketDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    async def test_receive_error_does_not_leak_cause_text_through_a_second_log(self):
        failure = ConnectionClosedError(None, None)
        failure.__cause__ = EOFError("private-cause-body-that-is-not-an-api-key")
        sdk, client = SDK(Session(failure)), translator(reconnect=False)
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        handler.setFormatter(SecretFormatter("%(levelname)s %(message)s"))
        logger = logging.getLogger("tests.websocket.safe-failure")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
        client.logger = logger
        try:
            with patch.object(genai, "Client", side_effect=sdk.client):
                stream = client.receive_translations()
                self.assertTrue((await stream.__anext__()).session_started)
                self.assertEqual((await stream.__anext__()).error, str(failure))
                await stream.aclose()
                await client.close()
            self.assertIn("abnormal_eof=true", output.getvalue())
            self.assertIn("EOFError", output.getvalue())
            self.assertNotIn("private-cause-body", output.getvalue())
            self.assertNotIn("Traceback", output.getvalue())
        finally:
            logger.handlers.clear()

    async def test_pong_is_observed_with_timeout_disabled_on_real_local_socket(self):
        sink = RecordingSink()
        diagnostics = WebSocketControlDiagnostics("local", sink)
        handler = WebSocketControlLogHandler(diagnostics)
        logger = logging.getLogger("tests.websocket.frame-diagnostics")
        logger.handlers = [handler]
        logger.setLevel(logging.DEBUG)
        logger.propagate = False

        async def server_handler(socket):
            await socket.send("private server text")
            await socket.wait_closed()

        try:
            async with serve(server_handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                async with connect(f"ws://127.0.0.1:{port}", proxy=None,
                                   ping_interval=0.02, ping_timeout=None, logger=logger) as socket:
                    self.assertEqual(await socket.recv(), "private server text")
                    deadline = asyncio.get_running_loop().time() + 2
                    while not any("event=pong_received" in line for _, line in sink.lines):
                        if asyncio.get_running_loop().time() > deadline:
                            self.fail("No matching Pong diagnostic received")
                        await asyncio.sleep(0.01)
            self.assertNotIn("private server text", str(sink.lines))
        finally:
            logger.handlers.clear()


if __name__ == "__main__":
    unittest.main()
