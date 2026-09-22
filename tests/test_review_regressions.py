from __future__ import annotations

import io
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.logger import register_secret, setup_logger
from tests import test_subtitle_window as subtitle_tests


def window():
    result = subtitle_tests.SubtitleWindowTests().make_window()
    result._reset_session_state()
    result.config.skip_filler_subtitles = False
    return result


class SubtitleRegressionTests(unittest.TestCase):
    def test_source_only_commits_without_translation_and_renders(self):
        w = window()
        w.config.subtitle_language = "source"
        w._consume_update("bilingual", "An original sentence.", None)
        self.assertEqual(w._history, [{"ja": "An original sentence.", "zh": ""}])
        self.assertTrue(w._build_render_items())

    def test_source_only_turn_commits_unpunctuated_source(self):
        w = window()
        w.config.subtitle_language = "source"
        w._consume_update("bilingual", "unpunctuated original", None, turn_complete=True)
        w._finish_turn_boundary()
        self.assertEqual(w._history[-1]["ja"], "unpunctuated original")
        self.assertFalse(w._ja_fragments)

    def test_source_only_stable_timeout_commits_without_turn_event(self):
        w = window()
        w.config.subtitle_language = "source"
        with patch("src.subtitle_window.time.monotonic", return_value=10):
            w._consume_update("bilingual", "unpunctuated original", None)
        self.assertTrue(w._flush_stale_stable_segment(now=100))
        self.assertEqual(w._history[-1]["ja"], "unpunctuated original")

    def test_source_only_does_not_duplicate_when_translation_arrives(self):
        w = window()
        w.config.subtitle_language = "source"
        w._consume_update("bilingual", "Original sentence.", None)
        w._consume_update("bilingual", None, "译文。", turn_complete=True)
        w._finish_turn_boundary()
        self.assertEqual(len(w._history), 1)

    def test_new_segments_preserve_extensions_and_repeated_utterances(self):
        for source in (True, False):
            with self.subTest(source=source):
                w = window()
                for text in ("I like apples.", "Actually, I like apples.", "I like apples."):
                    self.assertTrue(w._append_block(text if source else "", text, force=True))
                self.assertEqual(len(w._history), 3)

    def test_translation_chunk_whitespace_and_overlapping_letters(self):
        for chunks in (("Hello", " world!"), ("Hello ", "world!"),
                       ("Hello", " ", "world!"),
                       ("Hel", "lo", " world!")):
            with self.subTest(chunks=chunks):
                w = window()
                for text in chunks:
                    w._consume_update("bilingual", None, text)
                w._consume_update("bilingual", None, None, turn_complete=True)
                w._finish_turn_boundary()
                self.assertEqual(w._history[-1]["zh"], "Hello world!")

    def test_cjk_chunks_do_not_gain_spaces(self):
        w = window()
        self.assertEqual(w._consume_stream("你好", "世界！"), (["你好世界！"], ""))

    def test_short_repeated_sentences_survive_carry(self):
        w = window()
        w.config.min_block_chars = 20
        w._append_block("", "Hello!")
        w._append_block("", "Hello!", force=True)
        self.assertEqual(w._history[-1]["zh"], "Hello! Hello!")

    def test_switching_source_mode_clears_pending_but_keeps_history(self):
        w = window()
        w._append_block("Original.", "译文。", force=True)
        w._current_zh = "unfinished"
        w._ja_fragments = [(10, "pending")]
        w.subtitle_language_var = Mock()
        w._force_render = Mock()
        w._notify_settings = Mock()
        w._set_subtitle_language("source")
        self.assertEqual(len(w._history), 1)
        self.assertFalse(w._current_zh)
        self.assertFalse(w._ja_fragments)

    def test_failed_settings_save_still_stops_and_destroys_once(self):
        for method in ("_remember_current_geometry", "_notify_settings"):
            with self.subTest(method=method):
                w = window()
                w._closing = False
                w.root = Mock()
                w.on_close = Mock()
                w._remember_current_geometry = Mock()
                w._notify_settings = Mock()
                getattr(w, method).side_effect = PermissionError("read only")
                w.close()
                w.close()
                w.on_close.assert_called_once()
                w.root.destroy.assert_called_once()

    def test_failed_stop_callback_still_destroys_window(self):
        w = window()
        w._closing = False
        w.root = Mock()
        w.on_close = Mock(side_effect=RuntimeError("stop failed"))
        w._remember_current_geometry = Mock()
        w._notify_settings = Mock()
        with self.assertRaises(RuntimeError):
            w.close()
        w.root.destroy.assert_called_once()


class LoggingRegressionTests(unittest.TestCase):
    def test_both_handlers_redact_message_exception_chain_and_stack(self):
        secrets = ("AQ.synthetic_test_token_123456", "AIza" + "x" * 35,
                   "custom-secret-value-with-no-standard-prefix")
        register_secret(secrets[-1])
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "test.log"
            with patch("sys.stderr", stream):
                logger = setup_logger(name="review.redaction", log_file=str(path))
            try:
                try:
                    try:
                        raise ValueError(secrets[0])
                    except ValueError as exc:
                        raise RuntimeError(secrets[1]) from exc
                except RuntimeError:
                    logger.exception("Failure %s", secrets[-1],
                                     extra={"detail": secrets[0]})
                record = logging.LogRecord(logger.name, logging.ERROR, __file__, 1,
                                           secrets[0], (), None)
                record.stack_info = "stack " + secrets[-1]
                logger.handle(record)
                for handler in logger.handlers:
                    handler.flush()
                for output in (stream.getvalue(), path.read_text(encoding="utf-8")):
                    for secret in secrets:
                        self.assertNotIn(secret, output)
                    self.assertIn("<redacted-api-key>", output)
                    self.assertIn("RuntimeError", output)
                    self.assertIn("ValueError", output)
            finally:
                for handler in logger.handlers[:]:
                    handler.close()
                    logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()
