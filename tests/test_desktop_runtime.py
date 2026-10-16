from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from live_subtitle_translator.desktop_runtime import (
    DesktopControlState,
    GlobalPauseHotkey,
    SettingsStore,
)


class DesktopRuntimeTests(unittest.TestCase):
    def test_settings_are_saved_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "desktop_state.json"
            store = SettingsStore(path)
            store.update(
                {
                    "layout_style": "compact",
                    "audio_device": "Headphones",
                    "compact_geometry": [900, 68, 200, 100],
                }
            )

            reloaded = SettingsStore(path)
            self.assertEqual(reloaded.get("layout_style"), "compact")
            self.assertEqual(reloaded.get("audio_device"), "Headphones")
            self.assertEqual(reloaded.get("compact_geometry"), [900, 68, 200, 100])

    def test_pause_and_device_change_state(self) -> None:
        controls = DesktopControlState("Speakers")
        self.assertFalse(controls.pause_event.is_set())
        self.assertTrue(controls.toggle_paused())
        self.assertTrue(controls.pause_event.is_set())
        self.assertFalse(controls.toggle_paused())

        controls.select_audio_device("Headphones")
        self.assertEqual(controls.audio_device(), "Headphones")
        self.assertTrue(controls.device_change_event.is_set())


class GlobalPauseHotkeyTests(unittest.TestCase):
    def test_registers_without_auto_repeat_and_dispatches_matching_hotkeys(self) -> None:
        controls = DesktopControlState()
        pause_states = []
        callback = Mock(side_effect=lambda: pause_states.append(controls.toggle_paused()))
        hotkey = GlobalPauseHotkey(callback)
        messages = iter(
            [
                (hotkey.WM_HOTKEY, hotkey.HOTKEY_ID),
                (0x000F, hotkey.HOTKEY_ID),  # An unrelated window message.
                (hotkey.WM_HOTKEY, hotkey.HOTKEY_ID + 1),
                (hotkey.WM_HOTKEY, hotkey.HOTKEY_ID),
            ]
        )

        def get_message(pointer, _window, _minimum, _maximum):
            message = next(messages, None)
            if message is None:
                return 0
            pointer._obj.message, pointer._obj.wParam = message
            return 1

        user32 = SimpleNamespace(
            RegisterHotKey=Mock(return_value=1),
            GetMessageW=Mock(side_effect=get_message),
            UnregisterHotKey=Mock(return_value=1),
        )
        windll = SimpleNamespace(
            user32=user32,
            kernel32=SimpleNamespace(GetCurrentThreadId=Mock(return_value=123)),
        )
        with patch("live_subtitle_translator.desktop_runtime.ctypes.windll", windll, create=True):
            hotkey._run()

        # Verify the actual WinAPI call, including the flag that suppresses repeats.
        user32.RegisterHotKey.assert_called_once_with(None, hotkey.HOTKEY_ID, 0x4003, 0x20)
        self.assertTrue(hotkey.registered)
        self.assertTrue(hotkey._ready.is_set())
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(pause_states, [True, False])
        self.assertEqual(controls.audio_generation(), 1)
        user32.UnregisterHotKey.assert_called_once_with(None, hotkey.HOTKEY_ID)


if __name__ == "__main__":
    unittest.main()
