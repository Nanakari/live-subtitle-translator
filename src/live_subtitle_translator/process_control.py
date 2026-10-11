"""Register this desktop process and receive a Windows graceful-stop signal."""
from __future__ import annotations

import ctypes
import json
import os
import secrets
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Callable


def _kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    kernel.SetEvent.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.GetModuleFileNameW.argtypes = [wintypes.HMODULE, wintypes.LPWSTR, wintypes.DWORD]
    kernel.GetModuleFileNameW.restype = wintypes.DWORD
    return kernel


class DesktopProcessRegistration:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.path = self.root / ".runtime" / "desktop-process.json"
        self._token = secrets.token_hex(16)
        self._handle = None
        self._thread = None
        self._closed = threading.Event()

    def start(self, on_stop: Callable[[], None]) -> None:
        if os.name != "nt":
            return
        kernel = _kernel32()
        event_name = f"Local\\LiveSubtitleTranslator-{self._token}"
        self._handle = kernel.CreateEventW(None, True, False, event_name)
        if not self._handle:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if not kernel.GetProcessTimes(kernel.GetCurrentProcess(), *(ctypes.byref(value) for value in times)):
                raise ctypes.WinError(ctypes.get_last_error())
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            executable = ctypes.create_unicode_buffer(32768)
            if not kernel.GetModuleFileNameW(None, executable, len(executable)):
                raise ctypes.WinError(ctypes.get_last_error())
            record = {
                "kind": "live-subtitle-translator", "root": str(self.root),
                "pid": os.getpid(), "executable": executable.value,
                "created_filetime": str(created), "event_name": event_name,
                "token": self._token,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record, indent=2), encoding="utf-8")
            temporary.replace(self.path)

            def wait_for_stop():
                # close() also signals the event to release this thread.
                result = kernel.WaitForSingleObject(self._handle, 0xFFFFFFFF)
                if result == 0 and not self._closed.is_set():
                    on_stop()

            self._thread = threading.Thread(target=wait_for_stop, name="desktop-stop", daemon=True)
            self._thread.start()
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        self._closed.set()
        if self._handle is not None:
            kernel = _kernel32()
            kernel.SetEvent(self._handle)
            if self._thread is not None and self._thread is not threading.current_thread():
                self._thread.join(timeout=2)
            kernel.CloseHandle(self._handle)
            self._handle = None
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            if record.get("token") == self._token:
                self.path.unlink()
        except (OSError, ValueError):
            pass
