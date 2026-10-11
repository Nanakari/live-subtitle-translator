from __future__ import annotations

import asyncio
import logging
import os
import queue
import threading
from collections import deque

from live_subtitle_translator.audio_capture import SystemAudioCapture, SystemAudioCaptureError
from live_subtitle_translator.config import apply_network_config, load_project_config, resolve_config
from live_subtitle_translator.connection_errors import is_retryable_connection_error
from live_subtitle_translator.desktop_runtime import (
    DesktopControlState,
    GlobalPauseHotkey,
    SettingsStore,
    SingleInstanceLock,
    TrayController,
)
from live_subtitle_translator.gemini_live_translate import GeminiLiveTranslator, translator_options
from live_subtitle_translator.logger import setup_logger
from live_subtitle_translator.paths import application_root
from live_subtitle_translator.process_control import DesktopProcessRegistration
from live_subtitle_translator.silence_detector import SilenceDetector
from live_subtitle_translator.subtitle_window import SubtitleWindow, SubtitleWindowConfig


PROJECT_ROOT = application_root()


def load_config() -> dict:
    return load_project_config(PROJECT_ROOT)


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
    # Partial programmatic configurations can opt out of file logging.
    log_file = config.get("app", {}).get("log_file")
    config = resolve_config(config)
    apply_network_config(config)
    app_cfg = config.get("app", {})
    logger = setup_logger(
        app_cfg['debug'],
        "app",
        log_file,
        app_root=PROJECT_ROOT,
        max_bytes=int(app_cfg["log_max_bytes"]),
        backup_count=int(app_cfg["log_backup_count"]),
    )
    audio_cfg = config.get("audio", {})
    gemini_cfg = config.get("gemini", {})
    reconnect = bool(gemini_cfg['reconnect'])
    reconnect_delay = float(gemini_cfg['reconnect_delay_seconds'])
    chunk_seconds = max(0.001, float(audio_cfg['chunk_ms']) / 1000)
    silence_detector = SilenceDetector(
        enabled=bool(audio_cfg['silence_sleep_enabled']),
        sleep_after_seconds=float(audio_cfg['silence_sleep_after_seconds']),
        rms_threshold=float(audio_cfg['silence_rms_threshold']),
        wake_chunks=int(audio_cfg['silence_wake_chunks']),
    )

    translator = GeminiLiveTranslator(
        **translator_options(config),
        logger=setup_logger(
            app_cfg['debug'],
            "gemini",
            log_file,
            app_root=PROJECT_ROOT,
            max_bytes=int(app_cfg["log_max_bytes"]),
            backup_count=int(app_cfg["log_backup_count"]),
        ),
    )
    audio_queue: queue.Queue = queue.Queue(maxsize=20)

    def put_audio_item(item: object, generation: int | None = None) -> None:
        if item is None:
            enqueue_latest(audio_queue, None)
        else:
            generation = controls.audio_generation() if generation is None else generation
            enqueue_latest(audio_queue, (generation, item))

    def clear_audio_queue() -> None:
        try:
            while True:
                audio_queue.get_nowait()
        except queue.Empty:
            pass

    def audio_capture_worker() -> None:
        generation = controls.audio_generation()
        preroll = deque(maxlen=max(
            silence_detector.wake_chunks,
            int(float(audio_cfg['silence_preroll_ms']) / (chunk_seconds * 1000)),
            1,
        ))
        while not stop_event.is_set():
            device_was_changed = controls.device_change_event.is_set()
            if device_was_changed:
                clear_audio_queue()
                preroll.clear()
            controls.device_change_event.clear()
            capture = SystemAudioCapture(
                sample_rate=int(audio_cfg['sample_rate']),
                channels=audio_cfg['channels'],
                chunk_ms=int(audio_cfg['chunk_ms']),
                speaker_name=controls.audio_device(),
            )
            try:
                for chunk in capture.stream_audio_chunks(
                    stop_event=stop_event,
                    interrupt_event=controls.device_change_event,
                    pause_event=controls.pause_event,
                    reset_event=controls.capture_reset_event,
                ):
                    if stop_event.is_set() or controls.device_change_event.is_set():
                        break
                    if device_was_changed:
                        window.set_text("")
                        device_was_changed = False
                    if controls.pause_event.is_set():
                        preroll.clear()
                        continue
                    current_generation = controls.audio_generation()
                    if current_generation != generation:
                        preroll.clear()
                        generation = current_generation
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
                            put_audio_item(buffered_chunk, generation)
                        preroll.clear()
                        continue
                    if controls.auto_sleep_event.is_set():
                        continue
                    put_audio_item(chunk, generation)
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
                    packet = audio_queue.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(0.01)
                    continue
                if packet is None:
                    break
                generation, chunk = packet
                if generation != controls.audio_generation():
                    continue
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
        show_input = bool(gemini_cfg['show_input_transcription'])
        log_transcriptions = bool(gemini_cfg['log_transcriptions'])

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
                user_paused = controls.pause_event.is_set()
                if user_paused:
                    clear_audio_queue()
                # Cancel both users before closing: a sender awaiting network
                # backpressure must never hold up an intentional disconnect.
                await cancel_session_tasks()
                # Silence wake preroll is retained. Manually paused packets
                # carry an older generation and can never be sent on resume.
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
        instance_lock.close()
        return
    try:
        _run_desktop()
    finally:
        instance_lock.close()


def _run_desktop() -> None:
    config = load_config()
    app_cfg = config.get("app", {})
    debug = bool(app_cfg['debug'])
    logger = setup_logger(debug, "main", app_cfg['log_file'], app_root=PROJECT_ROOT,
                          max_bytes=int(app_cfg['log_max_bytes']),
                          backup_count=int(app_cfg['log_backup_count']))
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

    window = SubtitleWindow(
        SubtitleWindowConfig.from_config(config, settings),
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
    registration = DesktopProcessRegistration(PROJECT_ROOT)

    def worker() -> None:
        try:
            asyncio.run(run_translation(config, window, stop_event, controls))
        except Exception as exc:
            logger.exception("Translator worker crashed.")
            stop_event.set()
            window.set_text(f"Translator stopped: {type(exc).__name__}: {exc}")

    thread = threading.Thread(target=worker, name="translator-worker", daemon=True)
    try:
        try:
            registration.start(window.request_close)
        except OSError:
            logger.exception("Could not register the desktop stop signal.")
        try:
            tray.start()
        except Exception:
            logger.exception("Could not start the system tray icon.")
        if not hotkey.start():
            logger.warning("Could not register Ctrl+Alt+Space as a global hotkey.")
        thread.start()
        window.run()
    finally:
        stop_event.set()
        if thread.ident is not None:
            thread.join(timeout=5)
        hotkey.stop()
        tray.stop()
        registration.close()


if __name__ == "__main__":
    main()
