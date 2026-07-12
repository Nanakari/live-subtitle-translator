"""Local WebSocket bridge between the Chrome extension and Gemini Live."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from src.config import load_project_config
from src.gemini_live_translate import GeminiLiveTranslator


PROJECT_ROOT = Path(__file__).resolve().parent
app = FastAPI(title="Gemini Live Translator Chrome bridge")


def load_config() -> dict:
    return load_project_config(PROJECT_ROOT)


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


@app.websocket("/ws/translate")
async def translate(websocket: WebSocket) -> None:
    await websocket.accept()
    config = load_config()
    apply_network_config(config)
    audio_cfg = config.get("audio", {})
    gemini_cfg = config.get("gemini", {})
    translator = GeminiLiveTranslator(
        model=gemini_cfg.get("model", "gemini-3.5-live-translate-preview"),
        target_language_code=gemini_cfg.get("target_language_code", "zh-Hans"),
        echo_target_language=bool(gemini_cfg.get("echo_target_language", False)),
        api_key=gemini_cfg.get("api_key") or None,
        api_key_env=gemini_cfg.get("api_key_env", "GEMINI_API_KEY"),
        sample_rate=int(audio_cfg.get("sample_rate", 16000)),
        reconnect=bool(gemini_cfg.get("reconnect", True)),
        reconnect_delay_seconds=float(gemini_cfg.get("reconnect_delay_seconds", 2)),
    )

    async def forward_translations() -> None:
        async for event in translator.receive_translations():
            await websocket.send_json(
                {"type": "translation", "input": event.input_text, "output": event.output_text, "error": event.error}
            )

    receive_task: asyncio.Task | None = None
    try:
        await translator.start()
        receive_task = asyncio.create_task(forward_translations())
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if audio := message.get("bytes"):
                await translator.send_audio(audio)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        with suppress(Exception):
            await websocket.send_json({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        if receive_task:
            receive_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await receive_task
        await translator.close()
