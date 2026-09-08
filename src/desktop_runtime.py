from __future__ import annotations

import ctypes
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable


class SettingsStore:
    """Thread-safe JSON storage for desktop-only preferences."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._state = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._state.get(key, default)

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self._state.update(values)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary, self.path)


class DesktopControlState:
    def __init__(self, audio_device: str = "") -> None:
        self.pause_event = threading.Event()
        self.auto_sleep_event = threading.Event()
        self.device_change_event = threading.Event()
        self._lock = threading.Lock()
        self._audio_device = audio_device
        self._visible = True

    def toggle_paused(self) -> bool:
        if self.pause_event.is_set():
            self.pause_event.clear()
            return False
        self.pause_event.set()
        return True

    def audio_device(self) -> str:
        with self._lock:
            return self._audio_device

    def select_audio_device(self, name: str) -> None:
        with self._lock:
            if name == self._audio_device:
                return
            self._audio_device = name
        self.device_change_event.set()

    def set_auto_sleep(self, sleeping: bool) -> None:
        if sleeping:
            self.auto_sleep_event.set()
        else:
            self.auto_sleep_event.clear()

    def session_should_sleep(self) -> bool:
        return self.pause_event.is_set() or self.auto_sleep_event.is_set()

    def toggle_visible(self) -> bool:
        with self._lock:
            self._visible = not self._visible
            return self._visible


class SingleInstanceLock:
    """Windows named mutex used to keep only one desktop translator running."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self, name: str) -> None:
        self.name = name
        self.handle: int | None = None

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            return False
        self.handle = int(handle)
        return kernel32.GetLastError() != self.ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self.handle))
        self.handle = None


class GlobalPauseHotkey:
    """Register Ctrl+Alt+Space without installing a keyboard hook."""

    HOTKEY_ID = 0x4754
    WM_HOTKEY = 0x0312
    WM_QUIT = 0x0012
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    VK_SPACE = 0x20

    def __init__(self, callback: Callable[[], None]) -> None:
        self.callback = callback
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self.registered = False
        self._ready = threading.Event()

    def start(self) -> bool:
        if os.name != "nt":
            return False
        self._thread = threading.Thread(target=self._run, name="global-hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)
        return self.registered

    def _run(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        self._thread_id = int(kernel32.GetCurrentThreadId())
        self.registered = bool(
            user32.RegisterHotKey(
                None,
                self.HOTKEY_ID,
                self.MOD_CONTROL | self.MOD_ALT,
                self.VK_SPACE,
            )
        )
        self._ready.set()
        if not self.registered:
            return
        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            if message.message == self.WM_HOTKEY and message.wParam == self.HOTKEY_ID:
                self.callback()
        user32.UnregisterHotKey(None, self.HOTKEY_ID)

    def stop(self) -> None:
        if self._thread_id and os.name == "nt":
            ctypes.windll.user32.PostThreadMessageW(
                self._thread_id,
                self.WM_QUIT,
                0,
                0,
            )
        if self._thread:
            self._thread.join(timeout=2)
        self._thread = None
        self._thread_id = 0


class TrayController:
    def __init__(
        self,
        on_toggle_pause: Callable[[], None],
        on_toggle_visibility: Callable[[], None],
        on_exit: Callable[[], None],
    ) -> None:
        self.on_toggle_pause = on_toggle_pause
        self.on_toggle_visibility = on_toggle_visibility
        self.on_exit = on_exit
        self._icon = None

    def start(self) -> None:
        from PIL import Image, ImageDraw
        import pystray

        image = Image.new("RGBA", (64, 64), (22, 26, 32, 255))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((5, 8, 59, 56), radius=12, fill=(45, 92, 150, 255))
        draw.text((16, 17), "GT", fill=(255, 255, 255, 255))
        menu = pystray.Menu(
            pystray.MenuItem("暂停 / 继续翻译", lambda _icon, _item: self.on_toggle_pause()),
            pystray.MenuItem("显示 / 隐藏字幕", lambda _icon, _item: self.on_toggle_visibility()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda _icon, _item: self.on_exit()),
        )
        self._icon = pystray.Icon("gemini_live_translator", image, "Gemini Live Translator", menu)
        ready = threading.Event()

        def setup(icon) -> None:
            icon.visible = True
            ready.set()

        self._icon.run_detached(setup=setup)
        if not ready.wait(timeout=3):
            self._icon.stop()
            self._icon = None
            raise RuntimeError("System tray icon did not become ready.")

    def stop(self) -> None:
        if self._icon is not None:
            self._icon.stop()
            self._icon = None
