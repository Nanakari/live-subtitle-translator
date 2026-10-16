from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import patch

from google.genai import live
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from live_subtitle_translator.config import apply_network_config, default_config
from live_subtitle_translator.gemini_live_translate import GeminiLiveTranslator, translator_options


class NetworkConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def config(self, proxy_url="", no_proxy=""):
        config = default_config()
        config["network"].update(proxy_url=proxy_url, no_proxy=no_proxy)
        config["gemini"].update(
            api_key="synthetic-routing-test", reconnect=False,
            connect_timeout_seconds=2, server_data_timeout_seconds=0,
            subtitle_data_timeout_seconds=0, translation_output_timeout_seconds=0,
        )
        return config

    @staticmethod
    def inherited_proxy():
        # Deliberately unusable routes; tests must never contact an external host.
        environment = {key: "http://127.0.0.1:1" for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy",
        )}
        # Windows' certificate-store loader needs these even though network
        # traffic is restricted to local fixtures.
        for name in ("SYSTEMROOT", "WINDIR"):
            if name in os.environ:
                environment[name] = os.environ[name]
        environment["no_proxy"] = "*"
        return environment

    async def receive_locally(self, config):
        async def handle(socket):
            await socket.recv()
            await socket.send(json.dumps({"setupComplete": {}}))
            await socket.send(json.dumps({"serverContent": {
                "outputTranscription": {"text": "routing works"},
                "turnComplete": True,
            }}))
            await socket.wait_closed()

        async with serve(handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]

            def local_connect(_uri, **kwargs):
                kwargs.pop("ssl", None)  # The fixture is plain local WS.
                return connect(f"ws://127.0.0.1:{port}", **kwargs)

            with patch.dict(os.environ, self.inherited_proxy(), clear=True):
                apply_network_config(config)
                translator = GeminiLiveTranslator(**translator_options(config))
                stream = translator.receive_translations()
                with patch.object(live, "ws_connect", side_effect=local_connect):
                    try:
                        started = await asyncio.wait_for(stream.__anext__(), 3)
                        self.assertTrue(started.session_started, started.error)
                        event = await asyncio.wait_for(stream.__anext__(), 3)
                        self.assertEqual(event.output_text, "routing works")
                    finally:
                        await stream.aclose()
                        await translator.close()

    async def test_empty_proxy_connects_directly_despite_inherited_proxies(self):
        await self.receive_locally(self.config())

    async def test_configured_no_proxy_still_bypasses_the_proxy(self):
        await self.receive_locally(self.config("http://127.0.0.1:1", "127.0.0.1"))

    async def test_configured_proxy_overrides_inherited_websocket_and_lowercase_routes(self):
        request_received = asyncio.Future()
        tunnel_finished = asyncio.Event()

        async def relay(reader, writer):
            try:
                while data := await reader.read(65536):
                    writer.write(data)
                    await writer.drain()
            finally:
                writer.close()

        async def tunnel(reader, writer):
            upstream_writer = None
            try:
                request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
                first_line = request.split(b"\r\n", 1)[0]
                target = first_line.split()[1].decode("ascii")
                host, target_port = target.rsplit(":", 1)
                self.assertEqual(host, "127.0.0.1")
                upstream_reader, upstream_writer = await asyncio.open_connection(host, int(target_port))
                if not request_received.done():
                    request_received.set_result(first_line)
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(relay(reader, upstream_writer), relay(upstream_reader, writer))
            finally:
                if upstream_writer is not None:
                    upstream_writer.close()
                    await upstream_writer.wait_closed()
                writer.close()
                await writer.wait_closed()
                tunnel_finished.set()

        async with await asyncio.start_server(tunnel, "127.0.0.1", 0) as proxy:
            port = proxy.sockets[0].getsockname()[1]
            config = self.config(f"http://127.0.0.1:{port}")
            await self.receive_locally(config)
            request = await asyncio.wait_for(request_received, 2)
            self.assertRegex(request.decode("ascii"), r"^CONNECT 127\.0\.0\.1:\d+ HTTP/1\.1$")
            await asyncio.wait_for(tunnel_finished.wait(), 2)


if __name__ == "__main__":
    unittest.main()
