"""Stream boundaries must not change numbers in either subtitle path."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from live_subtitle_translator.subtitle_config import SubtitleWindowConfig
from live_subtitle_translator.subtitle_state import SubtitleState


class DecimalStreamingTests(unittest.TestCase):
    def make_state(self, language="translation", layout="compact"):
        return SubtitleState(SubtitleWindowConfig(
            subtitle_language=language,
            layout_style=layout,
            skip_filler_subtitles=False,
            require_bilingual_for_display=False,
        ))

    def update(self, state, text, *, turn_complete=False):
        source = text if state.config.subtitle_language == "source" else None
        translation = None if source is not None else text
        state._consume_update("bilingual", source, translation,
                              turn_complete=turn_complete)
        if turn_complete:
            state._finish_turn_boundary()

    def test_split_decimal_matches_whole_text_in_all_languages_and_layouts(self):
        for language in ("source", "translation", "bilingual"):
            for layout in ("classic", "compact"):
                with self.subTest(language=language, layout=layout):
                    state = self.make_state(language, layout)
                    with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10):
                        state._consume_update("bilingual", "The value is 3.", "圆周率的近似值是3.")
                        self.assertEqual(state._history, [])
                        self.assertEqual(state._pending_zh_segments, [])
                        state._consume_update("bilingual", "14.", "14。", turn_complete=True)
                        state._finish_turn_boundary()

                        whole = self.make_state(language, layout)
                        whole._consume_update("bilingual", "The value is 3.14.",
                                              "圆周率的近似值是3.14。", turn_complete=True)
                        whole._finish_turn_boundary()
                    self.assertEqual(state._history, whole._history)
                    self.assertEqual(len(state._history), 1)
                    self.assertEqual(state._compact_track_text(), whole._compact_track_text())
                    self.assertEqual(state._build_render_items(), whole._build_render_items())

    def test_following_whitespace_confirms_a_sentence_ending_in_an_integer(self):
        for language in ("source", "translation"):
            with self.subTest(language=language):
                state = self.make_state(language)
                with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10):
                    self.update(state, "The count is 3.")
                    self.assertEqual(state._history, [])
                    self.assertEqual(state._pending_zh_segments, [])
                    self.update(state, " Next sentence")
                with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10.5):
                    state._flush_ready_segments()
                field = "ja" if language == "source" else "zh"
                self.assertEqual([block[field] for block in state._history], ["The count is 3."])

    def test_turn_complete_commits_a_sentence_ending_in_an_integer(self):
        for language in ("source", "translation"):
            with self.subTest(language=language):
                state = self.make_state(language)
                self.update(state, "The count is 3.", turn_complete=True)
                field = "ja" if language == "source" else "zh"
                self.assertEqual([block[field] for block in state._history], ["The count is 3."])

    def test_existing_deadline_commits_when_no_more_packets_arrive(self):
        for language in ("source", "translation"):
            with self.subTest(language=language):
                state = self.make_state(language)
                with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=10):
                    self.update(state, "The count is 3.")
                with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=20):
                    self.assertTrue(state._flush_stale_stable_segment())
                with patch("live_subtitle_translator.subtitle_state.time.monotonic", return_value=20.5):
                    state._flush_ready_segments()
                field = "ja" if language == "source" else "zh"
                self.assertEqual([block[field] for block in state._history], ["The count is 3."])

    def test_unambiguous_sentence_period_and_abbreviations_keep_existing_behavior(self):
        state = self.make_state()
        self.assertEqual(state._consume_stream("", "An ordinary sentence."),
                         (["An ordinary sentence."], ""))
        self.assertEqual(state._consume_stream("", "Dr."), ([], "Dr."))
        self.assertEqual(state._consume_stream("Dr.", " Smith reports 3."),
                         ([], "Dr. Smith reports 3."))
        self.assertEqual(state._consume_stream("Dr. Smith reports 3.", "14!"),
                         (["Dr. Smith reports 3.14!"], ""))


if __name__ == "__main__":
    unittest.main()
