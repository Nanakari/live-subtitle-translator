"""Local WebSocket bridge between the Chrome extension and Gemini Live."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import suppress
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from src.config import load_project_config
from src.gemini_live_translate import GeminiLiveTranslator


PROJECT_ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Gemini Live Translator Chrome bridge")
BRIDGE_STATS = {
    "active_connections": 0,
    "audio_chunks": 0,
    "audio_bytes": 0,
    "translation_events": 0,
    "gemini_connected": False,
    "last_error": "",
}
BRIDGE_CONNECTION_LOCK = asyncio.Lock()
CHROME_EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")


def is_retryable_connection_error(exc: Exception) -> bool:
    """Return whether a startup failure is likely caused by temporary network state."""
    text = str(exc).lower()
    return (
        isinstance(exc, (TimeoutError, OSError, ConnectionError))
        or "opening handshake" in text
        or "timed out" in text
        or "connection" in text
    )


def load_config() -> dict:
    return load_project_config(PROJECT_ROOT)


def is_allowed_extension_origin(origin: str, config: dict) -> bool:
    """Allow only configured Chrome extensions to use the local bridge."""
    bridge_cfg = config.get("bridge", {})
    allowed_ids = {
        str(extension_id).strip().lower()
        for extension_id in bridge_cfg.get("allowed_extension_ids", [])
        if str(extension_id).strip()
    }
    normalized_origin = (origin or "").strip().lower()
    if allowed_ids:
        return normalized_origin in {
            f"chrome-extension://{extension_id}" for extension_id in allowed_ids
        }
    return bool(CHROME_EXTENSION_ORIGIN.fullmatch(normalized_origin))


def apply_network_config(config: dict) -> None:
    network_cfg = config.get("network", {})
    proxy_url = (network_cfg.get("proxy_url") or "").strip()
    no_proxy = (network_cfg.get("no_proxy") or "").strip()
    if proxy_url:
        os.environ["HTTP_PROXY"] = proxy_url
        os.environ["HTTPS_PROXY"] = proxy_url
        os.environ["ALL_PROXY"] = proxy_url
    if no_proxy:
        os.environ["NO_PROXY"] = no_proxy


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/debug")
async def debug() -> dict:
    return dict(BRIDGE_STATS)


@app.websocket("/ws/translate")
async def translate(websocket: WebSocket) -> None:
    config = load_config()
    origin = websocket.headers.get("origin", "")
    if not is_allowed_extension_origin(origin, config):
        await websocket.close(code=1008, reason="Unauthorized extension origin")
        return
    if BRIDGE_CONNECTION_LOCK.locked():
        await websocket.close(code=1013, reason="A translation session is already active")
        return

    async with BRIDGE_CONNECTION_LOCK:
        await _translate_authorized(websocket, config)


async def _translate_authorized(websocket: WebSocket, config: dict) -> None:
    await websocket.accept()
    BRIDGE_STATS["active_connections"] += 1
    apply_network_config(config)
    gemini_cfg = config.get("gemini", {})
    bridge_cfg = config.get("bridge", {})
    max_audio_frame_bytes = int(bridge_cfg.get("max_audio_frame_bytes", 65536))
    audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
        maxsize=max(1, int(bridge_cfg.get("audio_queue_chunks", 50)))
    )
    client_disconnected = asyncio.Event()
    send_lock = asyncio.Lock()
    translator = GeminiLiveTranslator(
        model=gemini_cfg.get("model", "gemini-3.5-live-translate-preview"),
        target_language_code=gemini_cfg.get("target_language_code", "zh-Hans"),
        echo_target_language=bool(gemini_cfg.get("echo_target_language", False)),
        api_key=gemini_cfg.get("api_key") or None,
        api_key_env=gemini_cfg.get("api_key_env", "GEMINI_API_KEY"),
        # The extension always emits 16 kHz PCM, independent of desktop settings.
        sample_rate=16000,
        reconnect=bool(gemini_cfg.get("reconnect", True)),
        reconnect_delay_seconds=float(gemini_cfg.get("reconnect_delay_seconds", 2)),
        session_resumption=bool(gemini_cfg.get("session_resumption", True)),
        context_window_compression=bool(
            gemini_cfg.get("context_window_compression", True)
        ),
        websocket_ping_interval_seconds=float(
            gemini_cfg.get("websocket_ping_interval_seconds", 20)
        ),
        websocket_ping_timeout_seconds=(
            None
            if gemini_cfg.get("websocket_ping_timeout_seconds") is None
            else float(gemini_cfg["websocket_ping_timeout_seconds"])
        ),
        websocket_close_timeout_seconds=float(
            gemini_cfg.get("websocket_close_timeout_seconds", 10)
        ),
        websocket_control_logging=bool(
            gemini_cfg.get("websocket_control_logging", True)
        ),
        server_data_timeout_seconds=float(
            gemini_cfg.get("server_data_timeout_seconds", 120)
        ),
        subtitle_data_timeout_seconds=float(
            gemini_cfg.get("subtitle_data_timeout_seconds", 45)
        ),
        translation_output_timeout_seconds=float(
            gemini_cfg.get("translation_output_timeout_seconds", 45)
        ),
    )

    async def send_json(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    def queue_disconnect_sentinel() -> None:
        if audio_queue.full():
            with suppress(asyncio.QueueEmpty):
                audio_queue.get_nowait()
        with suppress(asyncio.QueueFull):
            audio_queue.put_nowait(None)

    async def read_browser_audio() -> None:
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    return
                audio = message.get("bytes")
                if audio is None:
                    continue
                if len(audio) > max_audio_frame_bytes:
                    raise ValueError(
                        f"Audio frame exceeds {max_audio_frame_bytes} bytes"
                    )
                BRIDGE_STATS["audio_chunks"] += 1
                BRIDGE_STATS["audio_bytes"] += len(audio)
                if audio_queue.full():
                    with suppress(asyncio.QueueEmpty):
                        audio_queue.get_nowait()
                audio_queue.put_nowait(audio)
        finally:
            client_disconnected.set()
            queue_disconnect_sentinel()

    async def connect_or_disconnect() -> bool:
        connect_task = asyncio.create_task(translator.start())
        disconnect_task = asyncio.create_task(client_disconnected.wait())
        try:
            done, _ = await asyncio.wait(
                {connect_task, disconnect_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_task in done:
                return False
            await connect_task
            return True
        finally:
            for task in (connect_task, disconnect_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(connect_task, disconnect_task, return_exceptions=True)

    async def forward_translations() -> None:
        async for event in translator.receive_translations():
            if event.error:
                BRIDGE_STATS["gemini_connected"] = False
                BRIDGE_STATS["last_error"] = event.error
                await send_json({"type": "error", "message": event.error})
                continue
            if event.input_text or event.output_text:
                if not BRIDGE_STATS["gemini_connected"]:
                    await send_json({"type": "status", "status": "gemini-connected"})
                BRIDGE_STATS["gemini_connected"] = True
                BRIDGE_STATS["last_error"] = ""
                BRIDGE_STATS["translation_events"] += 1
            await send_json(
                {"type": "translation", "input": event.input_text, "output": event.output_text, "error": event.error}
            )

    async def forward_audio() -> None:
        while True:
            audio = await audio_queue.get()
            if audio is None:
                return
            await translator.send_audio(audio)

    receive_task: asyncio.Task | None = None
    audio_task: asyncio.Task | None = None
    browser_task = asyncio.create_task(read_browser_audio())
    try:
        while not client_disconnected.is_set():
            try:
                if not await connect_or_disconnect():
                    return
                BRIDGE_STATS["gemini_connected"] = True
                break
            except Exception as exc:
                BRIDGE_STATS["gemini_connected"] = False
                BRIDGE_STATS["last_error"] = f"{type(exc).__name__}: {exc}"
                if not translator.reconnect or not is_retryable_connection_error(exc):
                    raise
                try:
                    await asyncio.wait_for(
                        client_disconnected.wait(),
                        timeout=translator.reconnect_delay_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
        if client_disconnected.is_set():
            return

        await send_json({"type": "status", "status": "gemini-connected"})
        receive_task = asyncio.create_task(forward_translations())
        audio_task = asyncio.create_task(forward_audio())
        done, _ = await asyncio.wait(
            {browser_task, receive_task, audio_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            await task
        if receive_task in done and not client_disconnected.is_set():
            raise RuntimeError("Gemini receiver ended unexpectedly")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        with suppress(Exception):
            await send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        BRIDGE_STATS["active_connections"] = max(0, BRIDGE_STATS["active_connections"] - 1)
        BRIDGE_STATS["gemini_connected"] = False
        browser_task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await browser_task
        for task in (receive_task, audio_task):
            if task is None:
                continue
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        try:
            await asyncio.wait_for(translator.close(), timeout=5)
        finally:
            with suppress(Exception):
                await websocket.close()
