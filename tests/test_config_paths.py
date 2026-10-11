from __future__ import annotations

import logging
import os
import tempfile
import unittest
from dataclasses import asdict
from logging.handlers import RotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from live_subtitle_translator.config import default_config, load_project_config
from live_subtitle_translator.logger import setup_logger
from live_subtitle_translator.subtitle_config import SubtitleWindowConfig


def close_loggers(*loggers):
    handlers = set()
    for logger in loggers:
        handlers.update(logger.handlers)
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
    for handler in handlers:
        handler.close()


class ConfigurationTests(unittest.TestCase):
    def test_defaults_work_without_files_in_the_launch_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_project_config(Path(directory)), default_config())

    def test_typed_subtitle_defaults_match_the_bundled_document(self):
        config = default_config()
        self.assertEqual(asdict(SubtitleWindowConfig()), asdict(SubtitleWindowConfig.from_config(config)))

    def test_legacy_config_and_private_overrides_remain_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("gemini:\n  target_language_code: en\n  source_language: ja\n", encoding="utf-8")
            private = "gemini:\n  target_language_code: fr\nsubtitle:\n  dedup: true\n  max_chars: 12\n"
            (root / "config.local.yaml").write_text(private, encoding="utf-8")
            with self.assertWarns(UserWarning):
                config = load_project_config(root)
            self.assertEqual(config["gemini"]["target_language_code"], "fr")
            self.assertEqual(config["audio"], default_config()["audio"])
            self.assertNotIn("source_language", config["gemini"])
            self.assertNotIn("dedup", config["subtitle"])
            self.assertNotIn("max_chars", config["subtitle"])
            self.assertEqual((root / "config.local.yaml").read_text(encoding="utf-8"), private)

    def test_invalid_yaml_sections_report_the_option_not_private_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.local.yaml").write_text("gemini: private-value", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "gemini.*mapping") as error:
                load_project_config(root)
            self.assertNotIn("private-value", str(error.exception))


class LogPathTests(unittest.TestCase):
    def test_changing_working_directory_does_not_move_the_log(self):
        original = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            elsewhere = root / "elsewhere"
            elsewhere.mkdir()
            try:
                os.chdir(elsewhere)
                logger = setup_logger(name="review.paths", log_file="logs/app.log", app_root=root)
                logger.info("path check")
                self.assertTrue((root / "logs/app.log").is_file())
                self.assertFalse((elsewhere / "logs").exists())
            finally:
                os.chdir(original)
                close_loggers(logging.getLogger("review.paths"))

    def test_app_and_translator_can_rotate_the_same_log_on_windows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = setup_logger(name="review.rotate.app", log_file="app.log", app_root=root, max_bytes=240, backup_count=2)
            second = setup_logger(name="review.rotate.gemini", log_file="app.log", app_root=root, max_bytes=240, backup_count=2)
            try:
                for index in range(12):
                    (first if index % 2 else second).info("rotation record %s %s", index, "x" * 40)
                self.assertTrue((root / "app.log.1").is_file())
                self.assertTrue((root / "app.log.2").is_file())
                self.assertFalse((root / "app.log.3").exists())
                self.assertIn("record 11", (root / "app.log").read_text(encoding="utf-8"))
                files = [h for log in (first, second) for h in log.handlers if isinstance(h, RotatingFileHandler)]
                self.assertEqual(len(set(files)), 1)
            finally:
                close_loggers(first, second)

    def test_unwritable_log_does_not_abort_desktop_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("live_subtitle_translator.logger.RotatingFileHandler", side_effect=PermissionError("read only")):
                logger = setup_logger(name="review.readonly", log_file="app.log", app_root=Path(directory))
            try:
                logger.warning("The desktop can still display the problem")
            finally:
                close_loggers(logger)
