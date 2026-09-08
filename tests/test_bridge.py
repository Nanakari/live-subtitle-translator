from __future__ import annotations

import asyncio
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import chrome_server


EXTENSION_ID = "nkdahdhbljbeepadceehenadkldjicgn"
EXTENSION_ORIGIN = f"chrome-extension://{EXTENSION_ID}"


class FakeTranslator:
    instances: list["FakeTranslator"] = []

    def __init__(self, **_kwargs) -> None:
        self.reconnect = True
        self.reconnect_delay_seconds = 0.01
        self.audio: list[bytes] = []
        self.closed = False
        self.__class__.instances.append(self)

    async def start(self) -> None:
        return None

    async def send_audio(self, audio: bytes) -> None:
        self.audio.append(audio)

    async def receive_translations(self):
        while not self.closed:
            await asyncio.sleep(1)
            if False:
                yield None

    async def close(self) -> None:
        self.closed = True


class SlowStartTranslator(FakeTranslator):
    async def start(self) -> None:
        await asyncio.Event().wait()


class EndingTranslator(FakeTranslator):
    async def receive_translations(self):
        if False:
            yield None


class BlockedSender(FakeTranslator):
    sending = threading.Event()

    async def send_audio(self, audio: bytes) -> None:
        self.sending.set()
        await asyncio.Event().wait()


class BridgeSecurityTests(unittest.TestCase):
    def test_disconnect_cancels_blocked_audio_send(self) -> None:
        BlockedSender.sending.clear()
        with patch.object(chrome_server, "GeminiLiveTranslator", BlockedSender):
            with TestClient(chrome_server.app) as client:
                with client.websocket_connect(
                    "/ws/translate", headers={"origin": EXTENSION_ORIGIN}
                ) as websocket:
                    websocket.receive_json()
                    websocket.send_bytes(b"\0\0")
                    self.assertTrue(BlockedSender.sending.wait(timeout=1))
        self.assertTrue(BlockedSender.instances[-1].closed)
        self.assertFalse(chrome_server.BRIDGE_CONNECTION_LOCK.locked())

    def test_receiver_exit_closes_browser_and_releases_session(self) -> None:
        from starlette.websockets import WebSocketDisconnect
        with patch.object(chrome_server, "GeminiLiveTranslator", EndingTranslator):
            with TestClient(chrome_server.app) as client:
                with client.websocket_connect(
                    "/ws/translate", headers={"origin": EXTENSION_ORIGIN}
                ) as websocket:
                    self.assertEqual(websocket.receive_json()["status"], "gemini-connected")
                    self.assertIn("receiver ended", websocket.receive_json()["message"])
                    with self.assertRaises(WebSocketDisconnect):
                        websocket.receive_json()
        self.assertTrue(EndingTranslator.instances[-1].closed)
        self.assertFalse(chrome_server.BRIDGE_CONNECTION_LOCK.locked())

    def test_origin_allowlist_rejects_webpages_and_other_extensions(self) -> None:
        config = {"bridge": {"allowed_extension_ids": [EXTENSION_ID]}}
        self.assertTrue(
            chrome_server.is_allowed_extension_origin(EXTENSION_ORIGIN, config)
        )
        self.assertFalse(
            chrome_server.is_allowed_extension_origin("https://example.com", config)
        )
        self.assertFalse(
            chrome_server.is_allowed_extension_origin(
                "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", config
            )
        )

    def test_empty_allowlist_still_rejects_non_extension_origins(self) -> None:
        config = {"bridge": {"allowed_extension_ids": []}}
        self.assertTrue(
            chrome_server.is_allowed_extension_origin(EXTENSION_ORIGIN, config)
        )
        self.assertFalse(
            chrome_server.is_allowed_extension_origin("http://127.0.0.1", config)
        )

    def test_websocket_reports_ready_only_after_translator_start(self) -> None:
        FakeTranslator.instances.clear()
        with patch.object(chrome_server, "GeminiLiveTranslator", FakeTranslator):
            with TestClient(chrome_server.app) as client:
                with client.websocket_connect(
                    "/ws/translate", headers={"origin": EXTENSION_ORIGIN}
                ) as websocket:
                    self.assertEqual(
                        websocket.receive_json(),
                        {"type": "status", "status": "gemini-connected"},
                    )
                    websocket.send_bytes(b"\x00\x00" * 1600)
        self.assertEqual(len(FakeTranslator.instances), 1)
        self.assertTrue(FakeTranslator.instances[0].closed)

    def test_oversized_audio_frame_is_rejected(self) -> None:
        FakeTranslator.instances.clear()
        with patch.object(chrome_server, "GeminiLiveTranslator", FakeTranslator):
            with TestClient(chrome_server.app) as client:
                with client.websocket_connect(
                    "/ws/translate", headers={"origin": EXTENSION_ORIGIN}
                ) as websocket:
                    websocket.receive_json()
                    websocket.send_bytes(b"\x00" * 65537)
                    error = websocket.receive_json()
                    self.assertEqual(error["type"], "error")
                    self.assertIn("Audio frame exceeds", error["message"])

    def test_browser_disconnect_cancels_an_in_progress_gemini_connection(self) -> None:
        SlowStartTranslator.instances.clear()
        with patch.object(chrome_server, "GeminiLiveTranslator", SlowStartTranslator):
            with TestClient(chrome_server.app) as client:
                with client.websocket_connect(
                    "/ws/translate", headers={"origin": EXTENSION_ORIGIN}
                ):
                    pass
        self.assertEqual(len(SlowStartTranslator.instances), 1)
        self.assertTrue(SlowStartTranslator.instances[0].closed)


if __name__ == "__main__":
    unittest.main()
