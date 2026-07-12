from __future__ import annotations

import asyncio
import os
import warnings
from dataclasses import dataclass
from typing import Any, AsyncIterator

import numpy as np


MISSING_API_KEY_MESSAGE = (
    "Missing Gemini API key. Set gemini.api_key in config.yaml or set {env_name}."
)


@dataclass
class TranslationEvent:
    input_text: str | None = None
    output_text: str | None = None
    error: str | None = None


class GeminiLiveTranslator:
    def __init__(
        self,
        model: str = "gemini-3.5-live-translate-preview",
        target_language_code: str = "zh-Hans",
        echo_target_language: bool = False,
        api_key: str | None = None,
        api_key_env: str = "GEMINI_API_KEY",
        sample_rate: int = 16000,
        reconnect: bool = True,
        reconnect_delay_seconds: float = 2.0,
        logger: Any | None = None,
    ) -> None:
        self.model = model
        self.target_language_code = target_language_code
        self.echo_target_language = echo_target_language
        self.api_key = api_key or None
        self.api_key_env = api_key_env
        self.sample_rate = sample_rate
        self.reconnect = reconnect
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.logger = logger
        self._client = None
        self._session_cm = None
        self._session = None
        self._closed = False
        self._connect_lock = asyncio.Lock()

    async def start(self) -> None:
        api_key = self.api_key or os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(MISSING_API_KEY_MESSAGE.format(env_name=self.api_key_env))

        async with self._connect_lock:
            if self._session is not None:
                return
            with warnings.catch_warnings():
                warnings.filterwarnings("ignore", category=FutureWarning)
                warnings.filterwarnings(
                    "ignore",
                    message="Pydantic serializer warnings:*",
                    category=UserWarning,
                )
                from google import genai

                self._client = genai.Client(api_key=api_key)
                self._session_cm = self._client.aio.live.connect(
                    model=self.model,
                    config=self._build_config(),
                )
                try:
                    self._session = await self._session_cm.__aenter__()
                except Exception:
                    self._session = None
                    self._session_cm = None
                    raise
            self._closed = False
            self._log("info", "Connected to Gemini Live Translate model=%s", self.model)

    async def send_audio(self, audio_chunk: np.ndarray | bytes) -> None:
        if self._closed:
            return
        if self._session is None:
            await self.start()

        from google.genai import types

        pcm = self.audio_to_pcm16_bytes(audio_chunk)
        try:
            await self._session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={self.sample_rate}")
            )
        except Exception as exc:
            level = "info" if self._is_expected_reconnect_error(exc) else "warning"
            self._log(
                level,
                "Gemini send failed; closing current session: %s",
                exc,
                exc_info=(level == "warning"),
            )
            await self._drop_session()
            if not self.reconnect:
                raise

    async def receive_translations(self) -> AsyncIterator[TranslationEvent]:
        while not self._closed:
            if self._session is None:
                try:
                    await self.start()
                except Exception as exc:
                    yield TranslationEvent(error=str(exc))
                    if not self.reconnect:
                        return
                    await asyncio.sleep(self.reconnect_delay_seconds)
                    continue

            try:
                async for message in self._session.receive():
                    event = self._message_to_event(message)
                    if event:
                        yield event
                    if self._closed:
                        return
            except Exception as exc:
                level = "info" if self._is_expected_reconnect_error(exc) else "warning"
                self._log(
                    level,
                    "Gemini receive failed; reconnecting if enabled: %s",
                    exc,
                    exc_info=(level == "warning"),
                )
                await self._drop_session()
                yield TranslationEvent(error=str(exc))
                if not self.reconnect:
                    return
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def close(self) -> None:
        self._closed = True
        await self._drop_session()

    def _build_config(self) -> Any:
        from google.genai import types

        config = types.LiveConnectConfig(
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        # google-genai 1.47.0 does not expose TranslationConfig yet. Assigning
        # the raw dict after model creation preserves the preview field.
        config.generation_config = {
            "responseModalities": ["AUDIO"],
            "translationConfig": {
                "targetLanguageCode": self.target_language_code,
                "echoTargetLanguage": self.echo_target_language,
            },
        }
        return config

    def _message_to_event(self, message: Any) -> TranslationEvent | None:
        server_content = getattr(message, "server_content", None)
        if server_content is None:
            return None

        input_text = self._transcription_text(getattr(server_content, "input_transcription", None))
        output_text = self._transcription_text(getattr(server_content, "output_transcription", None))

        model_turn = getattr(server_content, "model_turn", None)
        if model_turn:
            for part in getattr(model_turn, "parts", []) or []:
                if getattr(part, "inline_data", None):
                    continue

        if input_text or output_text:
            return TranslationEvent(input_text=input_text, output_text=output_text)
        return None

    @staticmethod
    def _transcription_text(transcription: Any) -> str | None:
        if transcription is None:
            return None
        text = getattr(transcription, "text", None)
        if text:
            return str(text)
        if isinstance(transcription, dict):
            text = transcription.get("text")
            return str(text) if text else None
        return None

    @staticmethod
    def audio_to_pcm16_bytes(audio_chunk: np.ndarray | bytes) -> bytes:
        if isinstance(audio_chunk, bytes):
            return audio_chunk

        audio = np.asarray(audio_chunk)
        if audio.dtype == np.int16:
            return audio.tobytes()

        audio = np.asarray(audio, dtype=np.float32)
        if audio.ndim == 2:
            audio = audio.mean(axis=1)
        audio = np.clip(audio, -1.0, 1.0)
        return (audio * 32767).astype(np.int16).tobytes()

    async def _drop_session(self) -> None:
        async with self._connect_lock:
            session_cm = self._session_cm
            self._session = None
            self._session_cm = None
            if session_cm is not None:
                try:
                    await session_cm.__aexit__(None, None, None)
                except Exception:
                    self._log("debug", "Error while closing Gemini session", exc_info=True)

    def _log(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        if self.logger is not None:
            getattr(self.logger, level)(message, *args, **kwargs)

    @staticmethod
    def _is_expected_reconnect_error(exc: Exception) -> bool:
        text = str(exc).lower()
        expected_markers = (
            "goaway",
            "session durat",
            "no close frame received or sent",
            "received 1008",
            "connection aborted because the client failed to close",
        )
        return any(marker in text for marker in expected_markers)
