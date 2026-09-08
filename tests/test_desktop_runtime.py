from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.desktop_runtime import DesktopControlState, SettingsStore


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


if __name__ == "__main__":
    unittest.main()
