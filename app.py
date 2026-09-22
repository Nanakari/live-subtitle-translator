from __future__ import annotations

import asyncio
import logging
import os
import queue
import sys
import threading
from collections import deque
from pathlib import Path

from src.audio_capture import SystemAudioCapture, SystemAudioCaptureError
from src.config import load_project_config
from src.connection_errors import is_retryable_connection_error
from src.desktop_runtime import (
    DesktopControlState,
    GlobalPauseHotkey,
    SettingsStore,
    SingleInstanceLock,
    TrayController,
)
from src.gemini_live_translate import GeminiLiveTranslator
from src.logger import setup_logger
from src.silence_detector import SilenceDetector
from src.subtitle_window import SubtitleWindow, SubtitleWindowConfig


PROJECT_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent


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


def enqueue_latest(item_queue: queue.Queue, item: object) -> None:
    """Enqueue an item while discarding the oldest buffered item when full."""
    try:
        item_queue.put_nowait(item)
    except queue.Full:
        try:
            item_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            item_queue.put_nowait(item)
        except queue.Full:
            pass


async def run_translation(
    config: dict,
    window: SubtitleWindow,
    stop_event: threading.Event,
    controls: DesktopControlState | None = None,
) -> None:
    controls = controls or DesktopControlState()
    apply_network_config(config)
    app_cfg = config.get("app", {})
    logger = setup_logger(
        app_cfg.get("debug", False),
        "app",
        app_cfg.get("log_file"),
    )
    audio_cfg = config.get("audio", {})
    gemini_cfg = config.get("gemini", {})
    reconnect = bool(gemini_cfg.get("reconnect", True))
    reconnect_delay = float(gemini_cfg.get("reconnect_delay_seconds", 2))
    chunk_seconds = max(0.001, float(audio_cfg.get("chunk_ms", 100)) / 1000)
    silence_detector = SilenceDetector(
        enabled=bool(audio_cfg.get("silence_sleep_enabled", True)),
        sleep_after_seconds=float(audio_cfg.get("silence_sleep_after_seconds", 20)),
        rms_threshold=float(audio_cfg.get("silence_rms_threshold", 0.0015)),
        wake_chunks=int(audio_cfg.get("silence_wake_chunks", 3)),
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
        connect_timeout_seconds=float(gemini_cfg.get("connect_timeout_seconds", 15)),
        send_timeout_seconds=float(gemini_cfg.get("send_timeout_seconds", 5)),
        cleanup_timeout_seconds=float(gemini_cfg.get("cleanup_timeout_seconds", 2)),
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
        log_latency_metrics=bool(app_cfg.get("log_latency_metrics", False)),
        logger=setup_logger(
            app_cfg.get("debug", False),
            "gemini",
            app_cfg.get("log_file"),
        ),
    )
    audio_queue: queue.Queue = queue.Queue(maxsize=20)

    def put_audio_item(item: object) -> None:
        enqueue_latest(audio_queue, item)

    def clear_audio_queue() -> None:
        try:
            while True:
                audio_queue.get_nowait()
        except queue.Empty:
            pass

    def audio_capture_worker() -> None:
        preroll = deque(maxlen=max(
            silence_detector.wake_chunks,
            int(float(audio_cfg.get("silence_preroll_ms", 500)) / (chunk_seconds * 1000)),
            1,
        ))
        while not stop_event.is_set():
            device_was_changed = controls.device_change_event.is_set()
            if device_was_changed:
                clear_audio_queue()
                preroll.clear()
            controls.device_change_event.clear()
            capture = SystemAudioCapture(
                sample_rate=int(audio_cfg.get("sample_rate", 16000)),
                channels=audio_cfg.get("channels"),
                chunk_ms=int(audio_cfg.get("chunk_ms", 100)),
                speaker_name=controls.audio_device(),
            )
            try:
                for chunk in capture.stream_audio_chunks(
                    stop_event=stop_event,
                    interrupt_event=controls.device_change_event,
                    pause_event=controls.pause_event,
                ):
                    if stop_event.is_set() or controls.device_change_event.is_set():
                        break
                    if device_was_changed:
                        window.set_text("")
                        device_was_changed = False
                    if controls.pause_event.is_set():
                        preroll.clear()
                        continue
                    if silence_detector.sleeping:
                        preroll.append(chunk)
                    silence_action = silence_detector.update(chunk, chunk_seconds)
                    if silence_action == "sleep":
                        clear_audio_queue()
                        controls.set_auto_sleep(True)
                        window.set_text("No audio. Gemini is sleeping.")
                        continue
                    if silence_action == "wake":
                        controls.set_auto_sleep(False)
                        window.set_text("Audio detected. Reconnecting to Gemini...")
                        for buffered_chunk in preroll:
                            put_audio_item(buffered_chunk)
                        preroll.clear()
                        continue
                    if controls.auto_sleep_event.is_set():
                        continue
                    put_audio_item(chunk)
            except SystemAudioCaptureError as exc:
                if controls.device_change_event.is_set():
                    continue
                logger.error(str(exc))
                window.set_text(str(exc))
                stop_event.set()
            except Exception as exc:
                if controls.device_change_event.is_set():
                    continue
                logger.exception("System audio capture stopped unexpectedly.")
                window.set_text(f"Audio capture stopped: {type(exc).__name__}: {exc}")
                stop_event.set()

    async def send_audio_loop() -> None:
        while not stop_event.is_set():
            try:
                # Poll without an executor thread: cancelling a blocking
                # queue.get leaves a reader behind that can consume wake preroll.
                try:
                    chunk = audio_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                if chunk is None:
                    break
                if isinstance(chunk, Exception):
                    raise chunk
                if controls.session_should_sleep():
                    continue
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
                if not reconnect or not is_retryable_connection_error(exc):
                    window.set_text(f"Gemini connection failed: {type(exc).__name__}: {exc}")
                    stop_event.set()
                    break
                window.set_text(f"Audio/Gemini error: {type(exc).__name__}. Retrying...")
                await asyncio.sleep(reconnect_delay)

    last_session_id: int | None = None

    async def receive_text_loop() -> None:
        nonlocal last_session_id
        show_input = bool(gemini_cfg.get("show_input_transcription", False))
        log_transcriptions = bool(gemini_cfg.get("log_transcriptions", False))

        async for event in translator.receive_translations():
            if stop_event.is_set():
                break
            if (event.session_id is not None and last_session_id is not None
                    and event.session_id < last_session_id):
                continue
            if event.session_id is not None and event.session_id != last_session_id:
                last_session_id = event.session_id
                reset_pairing = getattr(window, "reset_pairing", None)
                if callable(reset_pairing):
                    reset_pairing(session_id=event.session_id)
            if event.error:
                log_level = logging.INFO if is_expected_reconnect_message(event.error) else logging.WARNING
                logger.log(log_level, "Gemini event error: %s", event.error)
                if event.fatal:
                    window.set_text(f"Gemini connection failed: {event.error}")
                    stop_event.set()
                    return
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
            if input_text or output_text or event.turn_complete:
                window.set_bilingual(
                    input_text=input_text,
                    output_text=output_text,
                    turn_complete=event.turn_complete,
                    session_id=event.session_id,
                )

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
                if not reconnect or not is_retryable_connection_error(exc):
                    window.set_text(f"Gemini connection failed: {type(exc).__name__}: {exc}")
                    stop_event.set()
                    return False
                window.set_text(f"Gemini connection failed: {type(exc).__name__}. Retrying...")
                await asyncio.sleep(reconnect_delay)
        return False

    tasks: dict[str, asyncio.Task] = {}
    capture_thread: threading.Thread | None = None

    async def cancel_session_tasks() -> None:
        cancelled = [tasks.pop(name) for name in ("connect", "send", "receive") if name in tasks]
        for task in cancelled:
            task.cancel()
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)

    try:
        session_sleeping = False
        while not stop_event.is_set():
            # Keep all network operations in child tasks so neither a pending
            # setup response nor a slow close blocks pause/stop observation.
            disconnect_task = tasks.get("disconnect")
            if disconnect_task is not None:
                if not disconnect_task.done():
                    await asyncio.sleep(0.05)
                    continue
                disconnect_task.result()
                del tasks["disconnect"]
                if controls.pause_event.is_set():
                    window.set_text("Paused. Gemini session disconnected.")
                elif controls.auto_sleep_event.is_set():
                    window.set_text("No audio. Gemini is sleeping.")

            should_sleep = controls.session_should_sleep()
            if should_sleep and not session_sleeping:
                # Cancel both users before closing: a sender awaiting network
                # backpressure must never hold up an intentional disconnect.
                await cancel_session_tasks()
                # Capture already discards silence. Do not clear here: audio
                # may have returned while receiver cancellation was pending.
                logger.info("Desktop session sleeping | reason=%s", "user_pause" if controls.pause_event.is_set() else "silence")
                tasks["disconnect"] = asyncio.create_task(
                    translator.disconnect(), name="Gemini disconnect"
                )
                session_sleeping = True
                if controls.pause_event.is_set():
                    window.set_text("Paused. Disconnecting from Gemini...")
                else:
                    window.set_text("No audio. Disconnecting from Gemini...")
            elif not should_sleep:
                session_sleeping = False
                if "connect" not in tasks and "send" not in tasks:
                    tasks["connect"] = asyncio.create_task(
                        connect_until_ready(), name="Gemini connect"
                    )
                connect_task = tasks.get("connect")
                if connect_task is not None and connect_task.done():
                    if not connect_task.result():
                        break
                    del tasks["connect"]
                    if capture_thread is None:
                        capture_thread = threading.Thread(
                            target=audio_capture_worker, name="audio-capture", daemon=True
                        )
                        capture_thread.start()
                    tasks["send"] = asyncio.create_task(send_audio_loop(), name="audio sender")
                    tasks["receive"] = asyncio.create_task(
                        receive_text_loop(), name="Gemini receiver"
                    )

            for name in ("send", "receive"):
                task = tasks.get(name)
                if task is None:
                    continue
                if not task.done() or task.cancelled():
                    continue
                error = task.exception()
                if error is not None:
                    raise error
                window.set_text(f"Translator stopped: {task.get_name()} ended unexpectedly.")
                stop_event.set()
                break
            await asyncio.sleep(0.05)
    finally:
        stop_event.set()
        put_audio_item(None)
        for task in tasks.values():
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks.values(), return_exceptions=True)
        if capture_thread is not None:
            capture_thread.join(timeout=2)
        try:
            await asyncio.wait_for(translator.close(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Timed out while closing Gemini session.")


def main() -> None:
    instance_lock = SingleInstanceLock(r"Local\GeminiLiveTranslatorDesktop")
    if not instance_lock.acquire():
        if os.name == "nt":
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                None,
                "Gemini Live Translator 已经在运行。",
                "Gemini Live Translator",
                0x40,
            )
        return

    config = load_config()
    app_cfg = config.get("app", {})
    debug = bool(app_cfg.get("debug", False))
    logger = setup_logger(debug, "main", app_cfg.get("log_file"))
    stop_event = threading.Event()
    settings = SettingsStore(PROJECT_ROOT / "desktop_state.json")
    try:
        audio_devices = SystemAudioCapture.list_speakers()
    except SystemAudioCaptureError as exc:
        logger.warning("Could not list playback devices: %s", exc)
        audio_devices = []
    selected_audio_device = str(settings.get("audio_device", "") or "")
    if selected_audio_device and selected_audio_device not in audio_devices:
        selected_audio_device = ""
    controls = DesktopControlState(selected_audio_device)
    window_holder: dict[str, SubtitleWindow] = {}

    def toggle_pause() -> None:
        paused = controls.toggle_paused()
        window_ref = window_holder.get("window")
        if window_ref is not None:
            window_ref.set_paused(paused)

    subtitle_cfg = config.get("subtitle", {})
    window = SubtitleWindow(
        SubtitleWindowConfig(
            title=app_cfg.get("title", "Gemini Live Translator"),
            display_mode=str(settings.get("display_mode", subtitle_cfg.get("display_mode", "sentence"))),
            subtitle_language=str(
                settings.get("subtitle_language", subtitle_cfg.get("subtitle_language", "bilingual"))
            ),
            layout_style=str(settings.get("layout_style", subtitle_cfg.get("layout_style", "compact"))),
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
            max_pending_chars=int(subtitle_cfg.get("max_pending_chars", 180)),
            stable_max_wait_ms=int(subtitle_cfg.get("stable_max_wait_ms", 6500)),
            stable_hard_max_wait_ms=int(subtitle_cfg.get("stable_hard_max_wait_ms", 12000)),
            stable_pause_ms=int(subtitle_cfg.get("stable_pause_ms", 1800)),
            timeout_commit_grace_ms=int(subtitle_cfg.get("timeout_commit_grace_ms", 700)),
            log_latency_metrics=bool(app_cfg.get("log_latency_metrics", False)),
            log_rendered_subtitles=bool(subtitle_cfg.get("log_rendered_subtitles", False)),
            duplicate_recent_window=int(subtitle_cfg.get("duplicate_recent_window", 6)),
            duplicate_min_chars=int(subtitle_cfg.get("duplicate_min_chars", 6)),
            ja_pair_window_seconds=float(subtitle_cfg.get("ja_pair_window_seconds", 2.0)),
            ja_pair_max_chars=int(subtitle_cfg.get("ja_pair_max_chars", 320)),
            ja_pair_preroll_seconds=float(subtitle_cfg.get("ja_pair_preroll_seconds", 0.5)),
            ja_pair_postroll_seconds=float(subtitle_cfg.get("ja_pair_postroll_seconds", 0.25)),
            max_source_to_translation_ratio=float(
                subtitle_cfg.get("max_source_to_translation_ratio", 3.0)
            ),
            max_source_overhang_chars=int(
                subtitle_cfg.get("max_source_overhang_chars", 12)
            ),
            pair_commit_delay_ms=int(subtitle_cfg.get("pair_commit_delay_ms", 600)),
            require_bilingual_for_display=bool(subtitle_cfg.get("require_bilingual_for_display", True)),
            max_wait_for_ja_ms=int(subtitle_cfg.get("max_wait_for_ja_ms", 1800)),
            skip_filler_subtitles=bool(subtitle_cfg.get("skip_filler_subtitles", True)),
            filler_max_chars=int(subtitle_cfg.get("filler_max_chars", 6)),
            max_short_carry_ms=int(subtitle_cfg.get("max_short_carry_ms", 1800)),
            clear_ja_after_pair=bool(subtitle_cfg.get("clear_ja_after_pair", True)),
            background_color=str(subtitle_cfg.get("background_color", "#111111")),
            text_color=str(subtitle_cfg.get("text_color", "#f2f2f2")),
            accent_color=str(subtitle_cfg.get("accent_color", "#b8d7ff")),
        ),
        on_close=stop_event.set,
        logger=logger,
        on_pause_toggle=toggle_pause,
        audio_devices=audio_devices,
        selected_audio_device=selected_audio_device,
        on_audio_device_selected=controls.select_audio_device,
        on_settings_changed=settings.update,
        desktop_state={
            "classic_geometry": settings.get("classic_geometry"),
            "compact_geometry": settings.get("compact_geometry"),
        },
    )
    window_holder["window"] = window
    tray = TrayController(
        on_toggle_pause=toggle_pause,
        on_toggle_visibility=lambda: window.request_visibility(controls.toggle_visible()),
        on_exit=window.request_close,
    )
    hotkey = GlobalPauseHotkey(toggle_pause)
    try:
        tray.start()
    except Exception:
        logger.exception("Could not start the system tray icon.")
    if not hotkey.start():
        logger.warning("Could not register Ctrl+Alt+Space as a global hotkey.")

    def worker() -> None:
        try:
            asyncio.run(run_translation(config, window, stop_event, controls))
        except Exception as exc:
            logger.exception("Translator worker crashed.")
            stop_event.set()
            window.set_text(f"Translator stopped: {type(exc).__name__}: {exc}")

    thread = threading.Thread(target=worker, name="translator-worker", daemon=True)
    thread.start()
    window.run()
    stop_event.set()
    thread.join(timeout=5)
    hotkey.stop()
    tray.stop()
    instance_lock.close()


if __name__ == "__main__":
    main()
