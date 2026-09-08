from __future__ import annotations

import unittest

import numpy as np

from src.silence_detector import SilenceDetector


class SilenceDetectorTests(unittest.TestCase):
    def test_sustained_silence_sleeps_and_stable_audio_wakes(self) -> None:
        detector = SilenceDetector(
            sleep_after_seconds=0.3,
            rms_threshold=0.01,
            wake_chunks=2,
        )
        silence = np.zeros(160, dtype=np.float32)
        audible = np.full(160, 0.1, dtype=np.float32)

        self.assertIsNone(detector.update(silence, 0.1))
        self.assertIsNone(detector.update(silence, 0.1))
        self.assertEqual(detector.update(silence, 0.1), "sleep")
        self.assertTrue(detector.sleeping)
        self.assertIsNone(detector.update(audible, 0.1))
        self.assertEqual(detector.update(audible, 0.1), "wake")
        self.assertFalse(detector.sleeping)

    def test_short_silence_does_not_sleep(self) -> None:
        detector = SilenceDetector(sleep_after_seconds=1.0, rms_threshold=0.01)
        silence = np.zeros(160, dtype=np.float32)
        audible = np.full(160, 0.1, dtype=np.float32)

        for _ in range(5):
            self.assertIsNone(detector.update(silence, 0.1))
        self.assertIsNone(detector.update(audible, 0.1))
        self.assertFalse(detector.sleeping)


if __name__ == "__main__":
    unittest.main()
