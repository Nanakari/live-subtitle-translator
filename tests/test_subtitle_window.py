from __future__ import annotations

import unittest
from unittest.mock import patch

from src.subtitle_window import SubtitleWindow, SubtitleWindowConfig


class SubtitleWindowTests(unittest.TestCase):
    def test_timeout_prefix_and_ready_continuation_are_paired_together(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = [(10.0, "It is a happy thing, so I am happy.")]
        window._pending_zh_segments = [(10.0, 10.0, 10.1, "这是开心的事，"),
                                       (10.2, 10.1, 10.2, "所以我很开心。")]
        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=11.0):
                window._flush_ready_segments()
        append.assert_called_once_with("It is a happy thing, so I am happy.", "这是开心的事，所以我很开心。")
        self.assertEqual(window._ja_fragments, [])

    def test_timeout_fragment_waits_for_grace_and_merges_late_continuation(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = []
        window.config.require_bilingual_for_display = False
        window.config.pair_commit_delay_ms = 300
        window.config.timeout_commit_grace_ms = 700
        window._pending_zh_segments = [
            (10.0, 10.0, 10.1, "这个能行", True),
        ]
        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=10.9):
                self.assertFalse(window._flush_ready_segments())
            window._pending_zh_segments.append((10.8, 10.8, 10.9, "吗？"))
            with patch("src.subtitle_window.time.monotonic", return_value=11.0):
                self.assertTrue(window._flush_ready_segments())
        append.assert_called_once_with("", "这个能行吗？")

    def test_turn_complete_bypasses_timeout_grace(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = []
        window.config.require_bilingual_for_display = False
        window.config.pair_commit_delay_ms = 3000
        window.config.timeout_commit_grace_ms = 700
        window._turn_complete_pending = True
        window._pending_zh_segments = [
            (10.0, 10.0, 10.1, "这个能行", True),
        ]
        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=10.1):
                self.assertTrue(window._flush_ready_segments())
        append.assert_called_once_with("", "这个能行")

    def test_low_confidence_source_is_hidden_for_short_translation(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = [(10.0, "これは前の長い原文コンテキストが残っています。")]
        window.config.require_bilingual_for_display = False
        window._pending_zh_segments = [(10.0, 10.0, 10.1, "吧")]

        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=11.0):
                self.assertTrue(window._flush_ready_segments())

        append.assert_called_once_with("", "吧", force=True)
        self.assertEqual(window._ja_fragments, [])

    def test_turn_boundary_resets_source_before_next_turn(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = []

        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=10.0):
                window._consume_update(
                    "bilingual",
                    "第一原文。",
                    "第一句。",
                    turn_complete=True,
                )
                self.assertTrue(window._finish_turn_boundary())

            self.assertEqual(window._ja_fragments, [])
            self.assertIsNone(window._source_tail)
            self.assertIsNone(window._source_continuation)

            with patch("src.subtitle_window.time.monotonic", return_value=20.0):
                window._consume_update(
                    "bilingual",
                    None,
                    "第二句。",
                    turn_complete=True,
                )
                self.assertTrue(window._finish_turn_boundary())

        self.assertEqual(
            [call.args for call in append.call_args_list],
            [("第一原文。", "第一句。"), ("", "第二句。")],
        )

    def test_late_continuation_retains_source_context(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = [(10.0, "It is a happy thing, so I am happy.")]
        window._pending_zh_segments = [(10.0, 10.0, 10.1, "这是开心的事，")]
        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=11.0):
                window._flush_ready_segments()
            window._history = [{"ja": "It is a happy thing, so I am happy.", "zh": "这是开心的事，"}]
            window._pending_zh_segments = [(12.0, 10.1, 12.0, "所以我很开心。")]
            with patch("src.subtitle_window.time.monotonic", return_value=13.0):
                window._flush_ready_segments()
        self.assertEqual(len(append.call_args_list), 2)
        # The full source context is retained internally for pairing, but the
        # already-rendered prefix is not shown a second time.
        self.assertEqual(append.call_args_list[1].args[0], "")
        self.assertEqual(window._ja_fragments, [])
        self.assertIsNone(window._source_continuation)

    def test_continuation_displays_only_new_source_suffix(self) -> None:
        window = self.make_window()
        window._history = [{"ja": "これは前半です。", "zh": "这是前半句。"}]
        window._source_continuation = (10.1, "これは前半です。")

        self.assertEqual(
            window._source_text_for_display("これは前半です。後半です。"),
            "後半です。",
        )

    def test_late_continuation_does_not_duplicate_previous_source_prefix(self) -> None:
        window = self.make_window()
        window._source_continuation = (10.1, "何では岩じゃな")
        window._ja_fragments = [(10.0, "何では岩じゃな"), (11.0, "い 。")]

        paired = window._ja_text_for_range(10.1, 11.1, 11.1)

        self.assertEqual(paired, "何では岩じゃない 。")

    def test_turn_complete_commits_without_waiting_for_pair_delay(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = []
        window.config.pair_commit_delay_ms = 6000

        with patch("src.subtitle_window.time.monotonic", return_value=20.0):
            with patch.object(window, "_append_block", return_value=True) as append:
                window._consume_update(
                    "bilingual",
                    "これは文です。",
                    "这是句子。",
                    turn_complete=True,
                )
                self.assertTrue(window._turn_complete_pending)
                self.assertTrue(window._flush_ready_segments())

        append.assert_called_once_with("これは文です。", "这是句子。")

    def test_first_translation_does_not_consume_next_complete_source_sentence(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = [(10.0, "I am happy. Yes.")]
        window._pending_zh_segments = [(10.0, 10.0, 10.1, "我很开心。")]
        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=11.0):
                window._flush_ready_segments()
        append.assert_called_once_with("I am happy.", "我很开心。")
        self.assertEqual(window._ja_fragments, [(10.1, "Yes.")])

    def test_repeated_complete_replies_retain_two_source_packets(self) -> None:
        window = self.make_window()
        window._ja_fragments = []
        with patch("src.subtitle_window.time.monotonic", return_value=11.0):
            window._add_ja_fragment("はい。")
            window._add_ja_fragment("はい。")
        self.assertEqual(len(window._ja_fragments), 2)
        window._consume_paired_ja("はい。", 11.0)
        self.assertEqual(window._ja_fragments, [(11.0, "はい。")])

    def test_continuation_context_expires_instead_of_matching_unrelated_speech(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._current_zh_started_at = None
        window._source_continuation = (10.0, "old source")
        window._ja_fragments = [(10.0, "old source"), (30.0, "New topic.")]
        self.assertEqual(window._ja_text_for_range(30.0, 30.1, 30.1), "New topic.")
        self.assertIsNone(window._source_continuation)

    def test_observed_hesitations_wait_then_expire_without_becoming_subtitles(self) -> None:
        for text in ("真的是", "好像，好像", "那个"):
            with self.subTest(text=text):
                window = self.make_window()
                window._current_zh = text
                window._current_zh_changed_at = 11.0
                self.assertFalse(window._flush_stale_stable_segment(now=14.0))
                self.assertFalse(window._flush_stale_stable_segment(now=22.0))
                self.assertEqual(window._pending_zh_segments, [])
                self.assertEqual(window._current_zh, "")

    def test_hard_deadline_keeps_unfinished_tail_after_a_complete_clause(self) -> None:
        window = self.make_window()
        window._current_zh = "我们已经准备好了，只是"
        window._current_zh_changed_at = 21.9
        self.assertTrue(window._flush_stale_stable_segment(now=22.0))
        self.assertEqual(window._pending_zh_segments[0][3], "我们已经准备好了，")
        self.assertEqual(window._current_zh, "只是")

    def test_unfinished_predicate_waits_for_its_continuation(self) -> None:
        window = self.make_window()
        window._current_zh = "嗯，我觉得它会"
        window._current_zh_changed_at = 13.4
        self.assertFalse(window._flush_stale_stable_segment(now=13.5))
        self.assertFalse(window._flush_stale_stable_segment(now=17.0))
        complete, pending = window._consume_stream(window._current_zh, "吓到很多人。")
        self.assertEqual(complete, ["嗯，我觉得它会吓到很多人。"])
        self.assertEqual(pending, "")

    def test_hard_deadline_still_releases_text_without_punctuation(self) -> None:
        window = self.make_window()
        window._current_zh = "我觉得它会"
        window._current_zh_changed_at = 21.9
        self.assertFalse(window._flush_stale_stable_segment(now=21.9))
        self.assertTrue(window._flush_stale_stable_segment(now=22.0))
        self.assertEqual(window._pending_zh_segments[0][3], "我觉得它会")
        self.assertFalse(window._pending_zh_segments[0][4])

    def test_pause_can_release_complete_phrase_before_soft_deadline(self) -> None:
        window = self.make_window()
        window._current_zh = "它需要一点时间让我习惯"
        window._current_zh_changed_at = 11.0
        self.assertFalse(window._flush_stale_stable_segment(now=12.0))
        self.assertTrue(window._flush_stale_stable_segment(now=13.0))
        self.assertTrue(window._pending_zh_segments[0][4])

    def test_sentence_mode_does_not_split_on_comma_just_for_length(self) -> None:
        window = self.make_window()
        first = "就是当你习惯了一件事很长时间，然后它突然改变了，"
        complete, pending = window._consume_stream("", first)
        self.assertEqual(complete, [])
        complete, pending = window._consume_stream(pending, "人们会感到害怕。")
        self.assertEqual(complete, [first + "人们会感到害怕。"])

    def test_english_source_keeps_word_spaces_and_next_sentence(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = [(10.0, "Yes. Where"), (10.8, "are people's thoughts"), (11.0, "on this?")]
        window._pending_zh_segments = [(10.0, 10.0, 10.1, "是的。")]
        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=10.7):
                window._flush_ready_segments()
        append.assert_called_once_with("Yes.", "是的。")
        self.assertEqual(window._ja_text_for_range(10.8, 11.1, 11.1), "Where are people's thoughts on this?")

    def test_decimal_and_abbreviation_do_not_end_a_sentence(self) -> None:
        window = self.make_window()
        complete, pending = window._split_complete_segments("Dr. Smith paid 3.14 dollars. Next")
        self.assertEqual(complete, ["Dr. Smith paid 3.14 dollars."])
        self.assertEqual(pending, "Next")

    def test_display_replaces_sentence_periods_but_keeps_decimal_points(self) -> None:
        self.assertEqual(
            SubtitleWindow._display_text("原文。Next. 3.14｡"),
            "原文 Next 3.14",
        )

    def test_render_items_replace_periods_without_mutating_pairing_history(self) -> None:
        window = self.make_window()
        window._history = [{"ja": "Original. Next.", "zh": "原文。下一句。"}]

        self.assertEqual(
            window._build_render_items(),
            [("Original Next", "ja"), ("原文 下一句", "zh")],
        )
        self.assertEqual(window._history[0]["ja"], "Original. Next.")
        self.assertEqual(window._history[0]["zh"], "原文。下一句。")

    def test_repeated_topic_is_not_removed_from_a_new_source_sentence(self) -> None:
        window = self.make_window()
        window._history = [{"ja": "Horror live action.", "zh": "恐怖直播动作。"}]
        window._ja_fragments = []
        with patch("src.subtitle_window.time.monotonic", return_value=11.0):
            window._add_ja_fragment("Horror live")
            window._add_ja_fragment("people.")
        self.assertEqual(window._recent_ja_text(now=11.0), "Horror live people.")

    def test_sentence_pair_keeps_next_sentence_tail_in_same_packet(self) -> None:
        window = self.make_window()
        window._current_zh = ""
        window._ja_fragments = [(10.0, "そうなんだ。じゃあ")]
        window._pending_zh_segments = [(10.0, 10.0, 10.1, "原来是这样。")]
        window.config.pair_commit_delay_ms = 300
        with patch.object(window, "_append_block", return_value=True) as append:
            with patch("src.subtitle_window.time.monotonic", return_value=10.5):
                window._flush_ready_segments()
        append.assert_called_once_with("そうなんだ。", "原来是这样。")
        self.assertEqual(window._ja_fragments, [(10.1, "じゃあ")])
        window._ja_fragments.append((11.0, "四人かな。"))
        self.assertEqual(window._ja_text_for_range(11.0, 11.5, 11.5), "じゃあ四人かな。")

    def test_timeout_prefers_a_clause_boundary(self) -> None:
        window = self.make_window()
        window.config.target_line_chars = 12
        window.config.min_block_chars = 4
        window.config.stable_max_wait_ms = 3500
        window._current_zh_changed_at = 13.9
        window._current_zh = "这是第一分句，还有第二分句正在继续"
        self.assertTrue(window._flush_stale_stable_segment(now=14.0))
        self.assertEqual(window._pending_zh_segments[0][3], "这是第一分句，")
        self.assertEqual(window._current_zh, "还有第二分句正在继续")

    def test_short_carry_deadline_does_not_restart_with_each_fragment(self) -> None:
        window = self.make_window()
        window._carry_ja = window._carry_zh = ""
        window._carry_started_at = None
        window._recent_blocks = []
        window._history = []
        window.config.skip_filler_subtitles = False
        window.config.max_short_carry_ms = 1000
        with patch.object(window, "_should_carry_block", return_value=True):
            with patch("src.subtitle_window.time.monotonic", return_value=10.0):
                window._append_block("一", "甲")
            with patch("src.subtitle_window.time.monotonic", return_value=10.8):
                window._append_block("二", "乙")
        self.assertEqual(window._carry_started_at, 10.0)
        with patch("src.subtitle_window.time.monotonic", return_value=11.1):
            self.assertTrue(window._flush_stale_carry())

    def make_window(self) -> SubtitleWindow:
        window = SubtitleWindow.__new__(SubtitleWindow)
        window.config = SubtitleWindowConfig(display_mode="sentence")
        window.logger = None
        window._current_zh = "这是持续增长的中文字幕片段"
        window._current_zh_started_at = 10.0
        window._current_zh_changed_at = 12.0
        window._ja_fragments = [(10.0, "これは連続した音声です")]
        window._pending_zh_segments = []
        window._carry_ja = ""
        window._carry_zh = ""
        window._carry_started_at = None
        window._status = ""
        return window

    def test_removed_balanced_mode_migrates_to_stable_output(self) -> None:
        self.assertEqual(
            SubtitleWindow._normalized_display_mode("balanced"),
            "sentence",
        )
        self.assertEqual(
            SubtitleWindow._normalized_display_mode("streaming"),
            "streaming",
        )

    def test_compact_overflow_keeps_latest_text_visible(self) -> None:
        class FixedWidthFont:
            @staticmethod
            def measure(text: str) -> int:
                return len(text) * 10

        visible = SubtitleWindow._fit_compact_tail(
            "旧内容一二三四最新内容",
            FixedWidthFont(),
            70,
        )

        self.assertTrue(visible.startswith("…"))
        self.assertTrue(visible.endswith("最新内容"))
        self.assertLessEqual(FixedWidthFont.measure(visible), 70)

    def test_active_translation_preserves_older_japanese_for_pairing(self) -> None:
        window = self.make_window()
        window.config.ja_pair_window_seconds = 2.0
        window.config.ja_pair_preroll_seconds = 0.5
        window.config.ja_pair_max_chars = 120
        window.config.max_pending_chars = 72
        window._current_zh = "continuous translation"
        window._current_zh_started_at = 10.0
        window._pending_zh_segments = []
        window._ja_fragments = [
            (10.0, "utterance beginning"),
            (29.0, "utterance ending"),
        ]

        paired = window._ja_text_for_range(10.0, 30.0, 30.0)

        self.assertIn("utterance beginning", paired)
        self.assertIn("utterance ending", paired)

    def test_unprotected_old_japanese_is_still_pruned(self) -> None:
        window = self.make_window()
        window.config.ja_pair_window_seconds = 2.0
        window._current_zh = ""
        window._current_zh_started_at = None
        window._pending_zh_segments = []
        window._ja_fragments = [(10.0, "old"), (29.0, "recent")]

        window._prune_ja_fragments(now=30.0)

        self.assertEqual(window._ja_fragments, [(29.0, "recent")])

    def test_stable_output_splits_a_long_running_unpunctuated_segment(self) -> None:
        window = self.make_window()
        window.config.stable_max_wait_ms = 8000
        window.config.stable_hard_max_wait_ms = 8000
        window.config.target_line_chars = 32
        window.config.min_block_chars = 8
        window._current_zh = "这是没有结束标点但已经稳定收到的连续中文字幕"
        window._current_zh_started_at = 10.0
        window._current_zh_changed_at = 17.5

        self.assertFalse(window._flush_stale_stable_segment(now=17.9))
        self.assertTrue(window._flush_stale_stable_segment(now=18.0))
        self.assertEqual(
            window._pending_zh_segments[0][3],
            "这是没有结束标点但已经稳定收到的连续中文字幕",
        )
        self.assertEqual(window._current_zh, "")

    def test_streaming_output_does_not_use_stable_timeout_split(self) -> None:
        window = self.make_window()
        window.config.display_mode = "streaming"
        window.config.stable_max_wait_ms = 8000

        self.assertFalse(window._flush_stale_stable_segment(now=20.0))
        self.assertEqual(window._pending_zh_segments, [])


if __name__ == "__main__":
    unittest.main()
