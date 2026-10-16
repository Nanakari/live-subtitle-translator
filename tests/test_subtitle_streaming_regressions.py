"""Keep subtitle content intact as live fragments move between display stages."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from live_subtitle_translator.subtitle_config import SubtitleWindowConfig
from live_subtitle_translator.subtitle_state import SubtitleState


class SubtitleStreamingRegressionTests(unittest.TestCase):
    def make_state(self, language="bilingual", mode="streaming"):
        return SubtitleState(SubtitleWindowConfig(
            subtitle_language=language,
            display_mode=mode,
            layout_style="compact",
            skip_filler_subtitles=False,
        ))

    def update(self, state, source, translation, now, *, complete=False):
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=now):
            changed = state._consume_update("bilingual", source, translation, turn_complete=complete)
            if complete:
                changed = state._finish_turn_boundary() or changed
            return changed

    def visible(self, state, now):
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=now):
            return state._compact_track_text()

    def test_short_number_sentences_do_not_turn_into_a_decimal(self):
        for language in ("bilingual", "source", "translation"):
            with self.subTest(language=language):
                state = SubtitleState(SubtitleWindowConfig(subtitle_language=language))
                self.update(state, "Count 1. 2.", "Count 1. 2.", 10, complete=True)
                for field in (("ja",) if language == "source" else ("ja", "zh")):
                    text = " ".join(block[field] for block in state._history)
                    self.assertEqual(text, "Count 1. 2.")
                    self.assertNotIn("1.2", text)

    def test_decimal_deltas_still_survive_short_carry_in_both_tracks(self):
        state = self.make_state()
        self.update(state, "3.", "3.", 10)
        self.assertEqual(state._pending_zh_segments, [])
        self.update(state, "14!", "14!", 10.1, complete=True)
        self.assertEqual(state._history, [{"ja": "3.14!", "zh": "3.14!"}])

    def test_cjk_short_sentences_keep_their_original_word_spacing(self):
        state = self.make_state()
        self.update(state, "你好。再见。", "你好。再见。", 10, complete=True)
        self.assertEqual(state._history, [{"ja": "你好。再见。", "zh": "你好。再见。"}])
        self.assertEqual(self.visible(state, 10), ("你好  再见", "你好  再见"))

    def test_short_streaming_text_stays_visible_through_pending_carry_and_history(self):
        for language in ("bilingual", "translation", "source"):
            with self.subTest(language=language):
                state = self.make_state(language)
                expected = ("See you" if language != "translation" else "",
                            "再见" if language != "source" else "")
                self.update(state, "See you", "再见", 10)
                self.assertEqual(self.visible(state, 10), expected)
                self.update(state, ".", "。", 10.1)
                self.assertEqual(self.visible(state, 10.1), expected)
                if language != "source":
                    self.assertTrue(state._pending_zh_segments)
                    with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10.5):
                        self.assertTrue(state._flush_ready_segments())
                    self.assertFalse(state._history)
                    self.assertTrue(state._carry_zh)
                    self.assertEqual(self.visible(state, 10.5), expected)
                    with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=11.6):
                        self.assertTrue(state._flush_stale_carry())
                self.assertEqual(len(state._history), 1)
                self.assertEqual(self.visible(state, 11.6), expected)

    def test_full_sentence_stays_visible_while_waiting_to_pair(self):
        state = self.make_state()
        source = "This is the complete subtitle"
        translation = "这是完整的一句字幕"
        self.update(state, source, translation, 10)
        expected = (source, translation)
        self.assertEqual(self.visible(state, 10), expected)
        self.update(state, ".", "。", 10.1)
        self.assertEqual(self.visible(state, 10.1), expected)
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10.5):
            self.assertTrue(state._flush_ready_segments())
        self.assertEqual(len(state._history), 1)
        self.assertEqual(self.visible(state, 10.5), expected)

    def test_queued_sentences_and_current_suffix_do_not_repeat_source(self):
        state = self.make_state()
        self.update(state, "First. Second", "第一句。第二句", 10)
        expected = ("First  Second", "第一句  第二句")
        self.assertEqual(self.visible(state, 10), expected)
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10.4):
            self.assertTrue(state._flush_ready_segments())
        self.assertEqual(self.visible(state, 10.4), expected)
        self.update(state, ".", "。", 10.5, complete=True)
        self.assertEqual(self.visible(state, 10.5), expected)

    def test_real_repeated_utterance_is_visible_once_per_utterance(self):
        state = self.make_state()
        self.update(state, "See you.", "再见。", 10, complete=True)
        self.update(state, "See you", "再见", 10.1)
        expected = ("See you  See you", "再见  再见")
        self.assertEqual(self.visible(state, 10.1), expected)
        self.update(state, ".", "。", 10.2)
        self.assertEqual(self.visible(state, 10.2), expected)
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10.6):
            state._flush_ready_segments()
        self.assertEqual(self.visible(state, 10.6), expected)
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=11.7):
            state._flush_stale_carry()
        self.assertEqual(self.visible(state, 11.7), expected)
        self.assertEqual(len(state._history), 2)

    def test_unfinished_carry_source_is_not_repeated_with_its_continuation(self):
        state = self.make_state()
        state.config.max_pending_chars = 4
        self.update(state, "We", "同意事情", 10)
        self.assertEqual(self.visible(state, 10), ("We", "同意事情"))
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10.4):
            self.assertTrue(state._flush_ready_segments())
        self.assertEqual(self.visible(state, 10.4), ("We", "同意事情"))
        self.update(state, " agree.", "了。", 10.5, complete=True)
        self.assertEqual(state._history, [{"ja": "We agree.", "zh": "同意事情了。"}])
        self.assertEqual(self.visible(state, 10.5), ("We agree", "同意事情了"))

    def test_sentence_mode_still_waits_for_history_commit(self):
        state = self.make_state(mode="sentence")
        self.update(state, "See you", "再见", 10)
        self.assertEqual(self.visible(state, 10), ("", ""))
        self.update(state, ".", "。", 10.1)
        self.assertEqual(self.visible(state, 10.1), ("", ""))
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10.5):
            state._flush_ready_segments()
        self.assertEqual(self.visible(state, 10.5), ("", ""))
        with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=11.6):
            state._flush_stale_carry()
        self.assertEqual(self.visible(state, 11.6), ("See you", "再见"))


if __name__ == "__main__":
    unittest.main()
