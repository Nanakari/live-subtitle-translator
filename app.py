from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from pathlib import Path

from src.audio_capture import SystemAudioCapture, SystemAudioCaptureError
from src.config import load_project_config
from src.gemini_live_translate import GeminiLiveTranslator
from src.logger import setup_logger
from src.subtitle_window import SubtitleWindow, SubtitleWindowConfig


PROJECT_ROOT = Path(__file__).resolve().parent


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


def is_expected_reconnect_message(message: str | None) -> bool:
    text = (message or "").lower()
    expected_markers = (
        "goaway",
        "session durat",
        "no close frame received or sent",
        "received 1008",
        "connection aborted because the client failed to close",
    )
    return any(marker in text for marker in expected_markers)


async def run_translation(config: dict, window: SubtitleWindow, stop_event: threading.Event) -> None:
    apply_network_config(config)
    app_cfg = config.get("app", {})
    logger = setup_logger(
        app_cfg.get("debug", False),
        "app",
        app_cfg.get("log_file"),
    )
    audio_cfg = config.get("audio", {})
    gemini_cfg = config.get("gemini", {})
    subtitle_cfg = config.get("subtitle", {})
    reconnect = bool(gemini_cfg.get("reconnect", True))
    reconnect_delay = float(gemini_cfg.get("reconnect_delay_seconds", 2))

    capture = SystemAudioCapture(
        sample_rate=int(audio_cfg.get("sample_rate", 16000)),
        channels=int(audio_cfg.get("channels", 1)),
        chunk_ms=int(audio_cfg.get("chunk_ms", 100)),
    )
    translator = GeminiLiveTranslator(
        model=gemini_cfg.get("model", "gemini-3.5-live-translate-preview"),
        target_language_code=gemini_cfg.get("target_language_code", "zh-Hans"),
        echo_target_language=bool(gemini_cfg.get("echo_target_language", False)),
        api_key=gemini_cfg.get("api_key") or None,
        api_key_env=gemini_cfg.get("api_key_env", "GEMINI_API_KEY"),
        sample_rate=int(audio_cfg.get("sample_rate", 16000)),
        reconnect=reconnect,
        reconnect_delay_seconds=reconnect_delay,
        logger=setup_logger(
            app_cfg.get("debug", False),
            "gemini",
            app_cfg.get("log_file"),
        ),
    )
    audio_queue: queue.Queue = queue.Queue(maxsize=20)

    def put_audio_item(item: object) -> None:
        try:
            audio_queue.put_nowait(item)
        except queue.Full:
            try:
                audio_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                audio_queue.put_nowait(item)
            except queue.Full:
                logger.debug("Could not enqueue audio item because the queue stayed full.")

    def audio_capture_worker() -> None:
        try:
            for chunk in capture.stream_audio_chunks(stop_event=stop_event):
                if stop_event.is_set():
                    break
                try:
                    audio_queue.put(chunk, timeout=0.5)
                except queue.Full:
                    logger.debug("Dropping audio chunk because the send queue is full.")
        except Exception as exc:
            put_audio_item(exc)
            stop_event.set()

    async def send_audio_loop() -> None:
        while not stop_event.is_set():
            try:
                chunk = await asyncio.to_thread(audio_queue.get)
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk
                await translator.send_audio(chunk)
            except SystemAudioCaptureError as exc:
                logger.error(str(exc))
                window.set_text(str(exc))
                stop_event.set()
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Audio sender paused after error: %s", exc, exc_info=True)
                window.set_text(f"Audio/Gemini error: {type(exc).__name__}. Retrying...")
                if not reconnect:
                    stop_event.set()
                    break
                await asyncio.sleep(reconnect_delay)

    async def receive_text_loop() -> None:
        show_input = bool(gemini_cfg.get("show_input_transcription", False))
        log_transcriptions = bool(gemini_cfg.get("log_transcriptions", False))

        async for event in translator.receive_translations():
            if stop_event.is_set():
                break
            if event.error:
                log_level = logging.INFO if is_expected_reconnect_message(event.error) else logging.WARNING
                logger.log(log_level, "Gemini event error: %s", event.error)
                window.set_text(f"Gemini reconnecting: {event.error or 'connection error'}")
                continue
            input_text = event.input_text if show_input else None
            output_text = None
            if log_transcriptions and show_input and event.input_text:
                logger.debug("Input transcription: %s", event.input_text)
            if event.output_text:
                output_text = event.output_text
                if log_transcriptions:
                    logger.debug("Output transcription: %s", event.output_text)
            if input_text or output_text:
                window.set_bilingual(input_text=input_text, output_text=output_text)

    async def connect_until_ready() -> bool:
        while not stop_event.is_set():
            try:
                window.set_text("Connecting to Gemini...")
                await translator.start()
                window.set_text("Connected. Waiting for audio...")
                return True
            except Exception as exc:
                logger.warning(
                    "Gemini initial connection failed: %s: %s",
                    type(exc).__name__,
                    exc,
                )
                window.set_text(f"Gemini connection failed: {type(exc).__name__}. Retrying...")
                if not reconnect:
                    stop_event.set()
                    return False
                await asyncio.sleep(reconnect_delay)
        return False

    tasks: list[asyncio.Task] = []
    capture_thread: threading.Thread | None = None
    try:
        if not await connect_until_ready():
            return
        capture_thread = threading.Thread(
            target=audio_capture_worker,
            name="audio-capture",
            daemon=True,
        )
        capture_thread.start()
        tasks = [
            asyncio.create_task(send_audio_loop()),
            asyncio.create_task(receive_text_loop()),
        ]
        while not stop_event.is_set():
            await asyncio.sleep(0.1)
    finally:
        stop_event.set()
        put_audio_item(None)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if capture_thread is not None:
            capture_thread.join(timeout=2)
        try:
            await asyncio.wait_for(translator.close(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Timed out while closing Gemini session.")


def main() -> None:
    config = load_config()
    app_cfg = config.get("app", {})
    debug = bool(app_cfg.get("debug", False))
    logger = setup_logger(debug, "main", app_cfg.get("log_file"))
    stop_event = threading.Event()

    subtitle_cfg = config.get("subtitle", {})
    window = SubtitleWindow(
        SubtitleWindowConfig(
            title=app_cfg.get("title", "Gemini Live Translator"),
            display_mode=str(subtitle_cfg.get("display_mode", "sentence")),
            subtitle_language=str(subtitle_cfg.get("subtitle_language", "bilingual")),
            layout_style=str(subtitle_cfg.get("layout_style", "compact")),
            compact_height=int(subtitle_cfg.get("compact_height", 68)),
            compact_window_width=int(subtitle_cfg.get("compact_window_width", 900)),
            compact_min_window_width=int(subtitle_cfg.get("compact_min_window_width", 380)),
            compact_font_size=int(subtitle_cfg.get("compact_font_size", 15)),
            compact_opacity=float(subtitle_cfg.get("compact_opacity", 0.90)),
            compact_background_color=str(subtitle_cfg.get("compact_background_color", "#161a20")),
            font_size=int(subtitle_cfg.get("font_size", 14)),
            font_family=str(subtitle_cfg.get("font_family", "Microsoft YaHei UI")),
            font_weight=str(subtitle_cfg.get("font_weight", "normal")),
            render_interval_ms=int(subtitle_cfg.get("render_interval_ms", 100)),
            opacity=float(subtitle_cfg.get("opacity", 0.85)),
            always_on_top=bool(subtitle_cfg.get("always_on_top", True)),
            window_width=int(subtitle_cfg.get("window_width", 760)),
            window_height=int(subtitle_cfg.get("window_height", 260)),
            min_window_width=int(subtitle_cfg.get("min_window_width", 420)),
            max_history_sentences=int(subtitle_cfg.get("max_history_sentences", 10)),
            min_pending_display_chars=int(subtitle_cfg.get("min_pending_display_chars", 18)),
            min_block_chars=int(subtitle_cfg.get("min_block_chars", 12)),
            target_line_chars=int(subtitle_cfg.get("target_line_chars", 32)),
            max_pending_chars=int(subtitle_cfg.get("max_pending_chars", 120)),
            log_rendered_subtitles=bool(subtitle_cfg.get("log_rendered_subtitles", False)),
            duplicate_recent_window=int(subtitle_cfg.get("duplicate_recent_window", 6)),
            duplicate_min_chars=int(subtitle_cfg.get("duplicate_min_chars", 6)),
            ja_pair_window_seconds=float(subtitle_cfg.get("ja_pair_window_seconds", 2.0)),
            ja_pair_max_chars=int(subtitle_cfg.get("ja_pair_max_chars", 60)),
            ja_pair_preroll_seconds=float(subtitle_cfg.get("ja_pair_preroll_seconds", 0.5)),
            ja_pair_postroll_seconds=float(subtitle_cfg.get("ja_pair_postroll_seconds", 0.25)),
            pair_commit_delay_ms=int(subtitle_cfg.get("pair_commit_delay_ms", 900)),
            require_bilingual_for_display=bool(subtitle_cfg.get("require_bilingual_for_display", True)),
            max_wait_for_ja_ms=int(subtitle_cfg.get("max_wait_for_ja_ms", 1800)),
            skip_filler_subtitles=bool(subtitle_cfg.get("skip_filler_subtitles", True)),
            filler_max_chars=int(subtitle_cfg.get("filler_max_chars", 6)),
            max_short_carry_ms=int(subtitle_cfg.get("max_short_carry_ms", 3000)),
            clear_ja_after_pair=bool(subtitle_cfg.get("clear_ja_after_pair", True)),
            background_color=str(subtitle_cfg.get("background_color", "#111111")),
            text_color=str(subtitle_cfg.get("text_color", "#f2f2f2")),
            accent_color=str(subtitle_cfg.get("accent_color", "#b8d7ff")),
        ),
        on_close=stop_event.set,
        logger=logger,
    )

    def worker() -> None:
        try:
            asyncio.run(run_translation(config, window, stop_event))
        except Exception as exc:
            logger.exception("Translator worker crashed.")
            stop_event.set()
            window.set_text(f"Translator stopped: {type(exc).__name__}: {exc}")

    thread = threading.Thread(target=worker, name="translator-worker", daemon=True)
    thread.start()
    window.run()
    stop_event.set()
    thread.join(timeout=5)


if __name__ == "__main__":
    main()
