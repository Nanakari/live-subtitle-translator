from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import warnings
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, AsyncIterator

import numpy as np

from src.connection_errors import is_resumption_rejected, is_retryable_connection_error
from src.logger import register_secret


MISSING_API_KEY_MESSAGE = (
    "Missing Gemini API key. Set gemini.api_key in config.yaml or set {env_name}."
)


@dataclass
class TranslationEvent:
    input_text: str | None = None
    output_text: str | None = None
    error: str | None = None
    # Gemini sends this control bit when the current model turn is complete.
    # Keeping it alongside the transcription lets the UI commit a stable
    # sentence without waiting for a heuristic timeout.
    turn_complete: bool = False
    fatal: bool = False
    # Logical session identity is preserved across successful session resumption.
    session_id: int | None = None
    session_started: bool = False
    input_language_code: str | None = None


@dataclass
class WebSocketControlDiagnostics:
    connection_id: str
    sink: Any
    ping_sent_at: float | None = None
    last_server_message_at: float | None = None
    last_translation_at: float | None = None
    server_messages: int = 0
    translation_messages: int = 0
    server_messages_at_ping: int = 0
    translation_messages_at_ping: int = 0

    def note_server_message(self, has_translation: bool) -> None:
        now = time.monotonic()
        self.last_server_message_at = now
        self.server_messages += 1
        if has_translation:
            self.last_translation_at = now
            self.translation_messages += 1

    def log_opened(self, ping_interval: float, ping_timeout: float | None) -> None:
        self.sink.info(
            "WebSocket control | connection=%s event=opened ping_interval_s=%.1f ping_timeout_s=%s",
            self.connection_id,
            ping_interval,
            "disabled" if ping_timeout is None else f"{ping_timeout:.1f}",
        )

    def log_closed(self) -> None:
        self.sink.info(
            "WebSocket control | connection=%s event=closed server_messages=%d translation_messages=%d",
            self.connection_id,
            self.server_messages,
            self.translation_messages,
        )

    def handle_control_log(self, message: str) -> None:
        now = time.monotonic()
        if "sent keepalive ping" in message:
            self.ping_sent_at = now
            self.server_messages_at_ping = self.server_messages
            self.translation_messages_at_ping = self.translation_messages
            self.sink.info(
                "WebSocket control | connection=%s event=ping_sent last_server_data_age_ms=%s last_translation_age_ms=%s",
                self.connection_id,
                self._age_ms(now, self.last_server_message_at),
                self._age_ms(now, self.last_translation_at),
            )
        elif "received keepalive pong" in message:
            self.sink.info(
                "WebSocket control | connection=%s event=pong_received rtt_ms=%s server_messages_since_ping=%d translation_messages_since_ping=%d",
                self.connection_id,
                self._age_ms(now, self.ping_sent_at),
                self.server_messages - self.server_messages_at_ping,
                self.translation_messages - self.translation_messages_at_ping,
            )
            self.ping_sent_at = None
        elif "timed out waiting for keepalive pong" in message:
            self.sink.warning(
                "WebSocket control | connection=%s event=pong_timeout waited_ms=%s server_messages_since_ping=%d translation_messages_since_ping=%d last_server_data_age_ms=%s last_translation_age_ms=%s",
                self.connection_id,
                self._age_ms(now, self.ping_sent_at),
                self.server_messages - self.server_messages_at_ping,
                self.translation_messages - self.translation_messages_at_ping,
                self._age_ms(now, self.last_server_message_at),
                self._age_ms(now, self.last_translation_at),
            )

    @staticmethod
    def _age_ms(now: float, then: float | None) -> str:
        if then is None:
            return "none"
        return str(round((now - then) * 1000))


class WebSocketControlLogHandler(logging.Handler):
    """Keep only safe keepalive events from verbose websockets debug logs."""

    def __init__(self, diagnostics: WebSocketControlDiagnostics) -> None:
        super().__init__(level=logging.DEBUG)
        self.diagnostics = diagnostics

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if "keepalive ping" in message or "keepalive pong" in message:
            self.diagnostics.handle_control_log(message)


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
        session_resumption: bool = True,
        context_window_compression: bool = True,
        websocket_ping_interval_seconds: float = 20.0,
        websocket_ping_timeout_seconds: float | None = None,
        websocket_close_timeout_seconds: float = 10.0,
        websocket_control_logging: bool = True,
        server_data_timeout_seconds: float = 120.0,
        subtitle_data_timeout_seconds: float = 45.0,
        translation_output_timeout_seconds: float = 45.0,
        log_latency_metrics: bool = False,
        logger: Any | None = None,
        connect_timeout_seconds: float = 15.0,
        send_timeout_seconds: float = 5.0,
        cleanup_timeout_seconds: float = 2.0,
    ) -> None:
        for name, value in (
            ("connect_timeout_seconds", connect_timeout_seconds),
            ("send_timeout_seconds", send_timeout_seconds),
            ("cleanup_timeout_seconds", cleanup_timeout_seconds),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
        self.model = model
        self.target_language_code = target_language_code
        self.echo_target_language = echo_target_language
        self.api_key = api_key or None
        self.api_key_env = api_key_env
        self.sample_rate = sample_rate
        self.reconnect = reconnect
        self.reconnect_delay_seconds = reconnect_delay_seconds
        self.session_resumption = session_resumption
        self.context_window_compression = context_window_compression
        self.websocket_ping_interval_seconds = websocket_ping_interval_seconds
        self.websocket_ping_timeout_seconds = websocket_ping_timeout_seconds
        self.websocket_close_timeout_seconds = websocket_close_timeout_seconds
        self.websocket_control_logging = websocket_control_logging
        self.server_data_timeout_seconds = server_data_timeout_seconds
        self.subtitle_data_timeout_seconds = subtitle_data_timeout_seconds
        self.translation_output_timeout_seconds = translation_output_timeout_seconds
        self.log_latency_metrics = log_latency_metrics
        self.logger = logger
        self.connect_timeout_seconds = connect_timeout_seconds
        self.send_timeout_seconds = send_timeout_seconds
        self.cleanup_timeout_seconds = cleanup_timeout_seconds
        self._client = None
        self._session_cm = None
        self._session = None
        self._closed = False
        self._resumption_handle: str | None = None
        self._connection_sequence = 0
        self._websocket_diagnostics: WebSocketControlDiagnostics | None = None
        self._server_data_watchdog_task: asyncio.Task[None] | None = None
        self._session_started_at: float | None = None
        self._last_server_message_at: float | None = None
        self._last_subtitle_message_at: float | None = None
        self._last_input_transcription_at: float | None = None
        self._last_output_translation_at: float | None = None
        self._translation_wait_started_at: float | None = None
        self._last_audio_sent_at: float | None = None
        self._connect_lock = asyncio.Lock()
        self._terminal_error: Exception | None = None
        self._session_generation = 0
        self._translation_expected = False

    async def start(self) -> None:
        api_key = self.api_key or os.getenv(self.api_key_env)
        if not api_key:
            raise RuntimeError(MISSING_API_KEY_MESSAGE.format(env_name=self.api_key_env))
        register_secret(api_key)

        async with self._connect_lock:
            if self._terminal_error is not None:
                raise self._terminal_error
            if self._session is not None:
                return
            # Retry once without an explicitly rejected handle. Authentication,
            # model/configuration errors and network outages do not trigger this.
            for attempt in range(2):
                resume_handle = self._resumption_handle if self.session_resumption else None
                try:
                    await self._open_session(api_key, resume_handle)
                    return
                except Exception as exc:
                    if attempt == 0 and resume_handle and is_resumption_rejected(exc):
                        self._resumption_handle = None
                        self._log("info", "Session resumption was rejected; starting a fresh session")
                        continue
                    if not is_retryable_connection_error(exc):
                        self._terminal_error = exc
                    raise

    async def _open_session(self, api_key: str, resume_handle: str | None) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=FutureWarning)
            warnings.filterwarnings(
                "ignore",
                message="Pydantic serializer warnings:*",
                category=UserWarning,
            )
            from google import genai

            self._connection_sequence += 1
            diagnostics = self._new_websocket_diagnostics()
            self._client = genai.Client(
                api_key=api_key,
                http_options=self._build_http_options(diagnostics),
            )
            try:
                self._session_cm = self._client.aio.live.connect(
                    model=self.model,
                    config=self._build_config(),
                )
                # Covers the SDK setup response as well as the WebSocket
                # handshake. The transport's open_timeout covers only the latter.
                self._session = await asyncio.wait_for(
                    self._session_cm.__aenter__(), timeout=self.connect_timeout_seconds
                )
            except BaseException:
                self._session = None
                self._session_cm = None
                await self._close_client()
                if diagnostics is not None:
                    logging.getLogger(
                        f"gemini.websocket.control.{diagnostics.connection_id}"
                    ).handlers.clear()
                raise
        if not resume_handle or self._session_generation == 0:
            self._session_generation += 1
        self._translation_expected = False
        self._websocket_diagnostics = diagnostics
        self._session_started_at = time.monotonic()
        self._last_server_message_at = None
        self._last_subtitle_message_at = None
        self._last_input_transcription_at = None
        self._last_output_translation_at = None
        self._translation_wait_started_at = None
        self._last_audio_sent_at = None
        if diagnostics is not None:
            diagnostics.log_opened(
                self.websocket_ping_interval_seconds,
                self.websocket_ping_timeout_seconds,
            )
        self._closed = False
        if (
            self.server_data_timeout_seconds > 0
            or self.subtitle_data_timeout_seconds > 0
            or self.translation_output_timeout_seconds > 0
        ):
            self._server_data_watchdog_task = asyncio.create_task(
                self._server_data_watchdog(self._session)
            )
        self._log("info", "Connected to Gemini Live Translate model=%s", self.model)

    async def send_audio(self, audio_chunk: np.ndarray | bytes) -> None:
        if self._closed:
            return
        if self._session is None:
            await self.start()
        session = self._session
        if session is None:
            return

        from google.genai import types

        pcm = self.audio_to_pcm16_bytes(audio_chunk)
        try:
            await asyncio.wait_for(
                session.send_realtime_input(
                    audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={self.sample_rate}")
                ),
                timeout=self.send_timeout_seconds,
            )
            if self._session is session:
                self._last_audio_sent_at = time.monotonic()
                if self.log_latency_metrics:
                    self._log("info", "latency stage=audio_sent monotonic_s=%.6f", self._last_audio_sent_at)
        except Exception as exc:
            if self._session is not session:
                return
            fatal = not is_retryable_connection_error(exc)
            if fatal:
                self._terminal_error = exc
            level = "info" if self._is_expected_reconnect_error(exc) else "warning"
            self._log(
                level,
                "Gemini send failed; closing current session: %s",
                exc,
                exc_info=(level == "warning"),
            )
            await self._drop_session(expected_session=session, reason="audio_send_error")
            if fatal:
                raise
            if not self.reconnect:
                raise

    async def receive_translations(self) -> AsyncIterator[TranslationEvent]:
        announced_session_id: int | None = None
        while not self._closed:
            if self._session is None:
                try:
                    await self.start()
                except Exception as exc:
                    fatal = not is_retryable_connection_error(exc)
                    yield TranslationEvent(error=str(exc) or type(exc).__name__, fatal=fatal)
                    if fatal or not self.reconnect:
                        return
                    await asyncio.sleep(self.reconnect_delay_seconds)
                    continue

            try:
                session = self._session
                if session is None:
                    continue
                session_id = self._session_generation
                if announced_session_id != session_id:
                    announced_session_id = session_id
                    yield TranslationEvent(session_id=session_id, session_started=True)
                rotate_connection = False
                async for message in session.receive():
                    # Ignore any buffered packets (including resumption tokens)
                    # from a connection that a sender/watchdog already replaced.
                    if self._session is not session:
                        break
                    go_away_time_left = self._process_control_message(message)
                    if go_away_time_left is not None:
                        self._log(
                            "info",
                            "Gemini requested a graceful connection rotation; time_left=%s",
                            go_away_time_left or "unknown",
                        )
                        rotate_connection = True
                        break
                    event = self._message_to_event(message)
                    if event is not None and self.log_latency_metrics:
                        self._log("info", "latency stage=translation_received monotonic_s=%.6f", time.monotonic())
                    if self._session is session:
                        self._last_server_message_at = time.monotonic()
                        if event is not None:
                            self._last_subtitle_message_at = self._last_server_message_at
                            self._observe_transcription(event, self._last_server_message_at)
                    if self._websocket_diagnostics is not None:
                        self._websocket_diagnostics.note_server_message(
                            bool(event is not None and event.output_text)
                        )
                    if event:
                        event.session_id = session_id
                        yield event
                    if self._closed:
                        return
                if rotate_connection:
                    await self._drop_session(expected_session=session, reason="server_go_away")
                    continue
            except Exception as exc:
                if self._session is not session:
                    continue
                fatal = not is_retryable_connection_error(exc)
                if fatal:
                    self._terminal_error = exc
                level = "info" if self._is_expected_reconnect_error(exc) else "warning"
                self._log(
                    level,
                    "Gemini receive failed; reconnecting if enabled: %s",
                    exc,
                    exc_info=(level == "warning"),
                )
                await self._drop_session(expected_session=session, reason="receive_error")
                yield TranslationEvent(error=str(exc) or type(exc).__name__, fatal=fatal)
                if fatal or not self.reconnect:
                    return
                await asyncio.sleep(self.reconnect_delay_seconds)

    async def close(self) -> None:
        self._closed = True
        await self._drop_session(reason="shutdown")
        # A concurrent start() may have reset this flag before close acquired
        # the connection lock. Closing is authoritative for this instance.
        self._closed = True
        self._resumption_handle = None

    async def disconnect(self, preserve_resumption: bool = False) -> None:
        """Close only the active session so this translator can reconnect later."""
        await self._drop_session(reason="intentional_disconnect")
        if not preserve_resumption:
            self._resumption_handle = None

    def _build_config(self) -> Any:
        from google.genai import types

        config = types.LiveConnectConfig(
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        if self.session_resumption:
            config.session_resumption = types.SessionResumptionConfig(
                handle=self._resumption_handle,
            )
        if self.context_window_compression:
            config.context_window_compression = types.ContextWindowCompressionConfig(
                sliding_window=types.SlidingWindow(),
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

    def _build_http_options(
        self,
        diagnostics: WebSocketControlDiagnostics | None = None,
    ) -> Any:
        from google.genai import types

        websocket_args: dict[str, Any] = {
            "ping_interval": self.websocket_ping_interval_seconds,
            "ping_timeout": self.websocket_ping_timeout_seconds,
            "close_timeout": self.websocket_close_timeout_seconds,
        }
        if diagnostics is not None:
            websocket_logger = logging.getLogger(
                f"gemini.websocket.control.{diagnostics.connection_id}"
            )
            websocket_logger.handlers.clear()
            websocket_logger.setLevel(logging.DEBUG)
            websocket_logger.propagate = False
            websocket_logger.addHandler(WebSocketControlLogHandler(diagnostics))
            websocket_args["logger"] = websocket_logger

        return types.HttpOptions(
            async_client_args=websocket_args
        )

    def _new_websocket_diagnostics(self) -> WebSocketControlDiagnostics | None:
        if not self.websocket_control_logging or self.logger is None:
            return None
        return WebSocketControlDiagnostics(
            connection_id=f"{id(self):x}-{self._connection_sequence}",
            sink=self.logger,
        )

    def _process_control_message(self, message: Any) -> str | None:
        update = getattr(message, "session_resumption_update", None)
        if update is not None:
            resumable = getattr(update, "resumable", None)
            new_handle = getattr(update, "new_handle", None)
            if resumable and new_handle:
                self._resumption_handle = str(new_handle)
            elif resumable is False:
                self._resumption_handle = None

        go_away = getattr(message, "go_away", None)
        if go_away is None:
            return None
        return str(getattr(go_away, "time_left", None) or "")

    def _message_to_event(self, message: Any) -> TranslationEvent | None:
        server_content = getattr(message, "server_content", None)
        if server_content is None:
            return None

        input_transcription = getattr(server_content, "input_transcription", None)
        output_transcription = getattr(server_content, "output_transcription", None)
        turn_complete = bool(getattr(server_content, "turn_complete", False))
        if isinstance(server_content, dict):
            input_transcription = server_content.get("input_transcription", server_content.get("inputTranscription"))
            output_transcription = server_content.get("output_transcription", server_content.get("outputTranscription"))
            turn_complete = bool(
                server_content.get("turn_complete", server_content.get("turnComplete", False))
            )
        input_text = self._transcription_text(input_transcription)
        output_text = self._transcription_text(output_transcription)
        language = getattr(input_transcription, "language_code", None) or getattr(input_transcription, "languageCode", None)
        if isinstance(input_transcription, dict):
            language = input_transcription.get("language_code", input_transcription.get("languageCode"))

        model_turn = getattr(server_content, "model_turn", None)
        if model_turn:
            for part in getattr(model_turn, "parts", []) or []:
                if getattr(part, "inline_data", None):
                    continue

        if input_text or output_text or turn_complete:
            return TranslationEvent(
                input_text=input_text,
                output_text=output_text,
                turn_complete=turn_complete,
                input_language_code=str(language) if language else None,
            )
        return None

    def _observe_transcription(self, event: TranslationEvent, now: float) -> None:
        if event.input_text:
            self._last_input_transcription_at = now
            language = (event.input_language_code or "").lower().replace("_", "-").split("-")[0]
            target = self.target_language_code.lower().replace("_", "-").split("-")[0]
            self._translation_expected = self.echo_target_language or bool(language and language != target)
            if not self._translation_expected:
                self._translation_wait_started_at = None
            elif self._translation_wait_started_at is None:
                self._translation_wait_started_at = now
        if event.output_text:
            self._last_output_translation_at = now
            self._translation_wait_started_at = None
        if event.turn_complete:
            self._translation_expected = False
            self._translation_wait_started_at = None

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

    async def _drop_session(self, expected_session: Any | None = None, reason: str = "session_reset") -> None:
        watchdog_to_wait: asyncio.Task[None] | None = None
        async with self._connect_lock:
            if expected_session is not None and self._session is not expected_session:
                return
            session_cm = self._session_cm
            diagnostics = self._websocket_diagnostics
            watchdog = self._server_data_watchdog_task
            self._session = None
            self._session_cm = None
            self._websocket_diagnostics = None
            self._server_data_watchdog_task = None
            self._session_started_at = None
            self._last_server_message_at = None
            self._last_subtitle_message_at = None
            self._last_input_transcription_at = None
            self._last_output_translation_at = None
            self._translation_wait_started_at = None
            self._translation_expected = False
            self._last_audio_sent_at = None
            if watchdog is not None and watchdog is not asyncio.current_task():
                watchdog.cancel()
                watchdog_to_wait = watchdog
            if session_cm is not None:
                self._log("info", "Gemini session closing | reason=%s", reason)
                try:
                    await asyncio.wait_for(
                        session_cm.__aexit__(None, None, None),
                        timeout=self.cleanup_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    self._log("warning", "Timed out while closing Gemini WebSocket")
                except Exception:
                    self._log("debug", "Error while closing Gemini session", exc_info=True)
                finally:
                    await self._close_client()
            else:
                await self._close_client()
            if diagnostics is not None:
                diagnostics.log_closed()
                logging.getLogger(
                    f"gemini.websocket.control.{diagnostics.connection_id}"
                ).handlers.clear()
        if watchdog_to_wait is not None:
            with suppress(asyncio.CancelledError):
                await watchdog_to_wait

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            try:
                await asyncio.wait_for(
                    client.aio.aclose(), timeout=self.cleanup_timeout_seconds
                )
            except asyncio.TimeoutError:
                self._log("warning", "Timed out while closing async Gemini client")
            except Exception:
                self._log("debug", "Error while closing async Gemini client", exc_info=True)
        finally:
            try:
                client.close()
            except Exception:
                self._log("debug", "Error while closing Gemini client", exc_info=True)

    async def _server_data_watchdog(self, session: Any) -> None:
        enabled_timeouts = [
            value
            for value in (
                self.server_data_timeout_seconds,
                self.subtitle_data_timeout_seconds,
                self.translation_output_timeout_seconds,
            )
            if value > 0
        ]
        check_interval = min(5.0, max(0.1, min(enabled_timeouts) / 4))
        try:
            while self._session is session and not self._closed:
                await asyncio.sleep(check_interval)
                now = time.monotonic()
                if self._translation_output_is_stalled(now):
                    self._log(
                        "warning",
                        "Gemini translation-output watchdog expired; starting a fresh session | waiting_for_translation_ms=%s last_input_age_ms=%s timeout_s=%.1f",
                        self._age_ms(now, self._translation_wait_started_at),
                        self._age_ms(now, self._last_input_transcription_at),
                        self.translation_output_timeout_seconds,
                    )
                    self._resumption_handle = None
                elif self._subtitle_data_is_stalled(now):
                    reference = self._last_subtitle_message_at or self._session_started_at
                    self._log(
                        "warning",
                        "Gemini subtitle-data watchdog expired; starting a fresh session | no_subtitle_data_ms=%s last_audio_age_ms=%s timeout_s=%.1f",
                        self._age_ms(now, reference),
                        self._age_ms(now, self._last_audio_sent_at),
                        self.subtitle_data_timeout_seconds,
                    )
                    # Session resumption may preserve the same stalled model turn.
                    self._resumption_handle = None
                elif self._server_data_is_stalled(now):
                    reference = self._last_server_message_at or self._session_started_at
                    self._log(
                        "warning",
                        "Gemini server-data watchdog expired; reconnecting | no_server_data_ms=%s last_audio_age_ms=%s timeout_s=%.1f",
                        self._age_ms(now, reference),
                        self._age_ms(now, self._last_audio_sent_at),
                        self.server_data_timeout_seconds,
                    )
                else:
                    continue
                await self._drop_session(expected_session=session, reason="watchdog")
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            self._log("warning", "Gemini server-data watchdog failed", exc_info=True)

    def _server_data_is_stalled(self, now: float) -> bool:
        timeout = self.server_data_timeout_seconds
        if timeout <= 0 or self._last_audio_sent_at is None:
            return False
        # Do not rotate an intentionally idle connection. The watchdog becomes
        # active only while audio has been sent during the same timeout window.
        if now - self._last_audio_sent_at >= timeout:
            return False
        reference = self._last_server_message_at or self._session_started_at
        return reference is not None and now - reference >= timeout

    def _subtitle_data_is_stalled(self, now: float) -> bool:
        timeout = self.subtitle_data_timeout_seconds
        if (timeout <= 0 or self._last_audio_sent_at is None
                or not self._translation_expected or self._translation_wait_started_at is None):
            return False
        if now - self._last_audio_sent_at >= timeout:
            return False
        reference = self._last_subtitle_message_at or self._session_started_at
        return reference is not None and now - reference >= timeout

    def _translation_output_is_stalled(self, now: float) -> bool:
        timeout = self.translation_output_timeout_seconds
        if (
            timeout <= 0
            or not self._translation_expected
            or self._last_audio_sent_at is None
            or self._translation_wait_started_at is None
            or self._last_input_transcription_at is None
        ):
            return False
        if now - self._last_audio_sent_at >= timeout:
            return False
        # Trigger only while source transcription is still arriving. This
        # distinguishes a stuck translator from a naturally quiet input.
        if now - self._last_input_transcription_at >= timeout:
            return False
        return now - self._translation_wait_started_at >= timeout

    @staticmethod
    def _age_ms(now: float, then: float | None) -> str:
        if then is None:
            return "none"
        return str(round((now - then) * 1000))

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
