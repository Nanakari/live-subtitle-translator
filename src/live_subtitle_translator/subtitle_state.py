"""Subtitle accumulation and pairing, independent of Tk and Windows."""
from __future__ import annotations

import re
import time
from logging import Logger

from .subtitle_config import SubtitleWindowConfig

HARD_PUNCTUATION = "。！？!?;；"
SOFT_PUNCTUATION = "，,、"


class SubtitleState:
    def __init__(self, config: SubtitleWindowConfig, logger: Logger | None = None):
        self.config = config
        self.logger = logger
        self._session_id = None
        self._reset_session_state()

    def _consume_update(
        self,
        kind: str,
        ja_text: str | None,
        zh_text: str | None,
        turn_complete: bool = False,
        session_id: int | None = None,
    ) -> bool:
        session_changed = False
        if session_id is not None:
            current_id = getattr(self, "_session_id", None)
            if current_id is not None and session_id < current_id:
                return False
            if session_id != current_id:
                self._reset_session_state()
                self._session_id = session_id
                session_changed = True
        if kind == "paused":
            paused = zh_text == "1"
            self._status = "Paused. Press Ctrl+Alt+Space to resume." if paused else ""
            return True
        if kind == "reset_pairing":
            self._reset_session_state()
            return True
        if kind == "status":
            self._status = self._normalize(zh_text)
            self._log_subtitle("status", status=self._status)
            return True

        self._status = ""
        changed = session_changed

        if ja_text:
            changed = self._add_ja_fragment(ja_text) or changed

        if self._normalized_subtitle_language(self.config.subtitle_language) == "source":
            changed = self._flush_source_segments(force=turn_complete) or changed
            if turn_complete:
                self._turn_complete_pending = True
            return changed

        if zh_text:
            now = time.monotonic()
            if not self._current_zh:
                self._current_zh_started_at = now
            segment_start = self._current_zh_started_at or now
            previous_zh = self._current_zh
            zh_complete, self._current_zh = self._consume_stream(self._current_zh, zh_text)
            if self._current_zh != previous_zh:
                self._current_zh_changed_at = now if self._current_zh else None
            for zh_segment in zh_complete:
                self._queue_zh_segment(zh_segment, segment_start, now)
                changed = True
                segment_start = now
            self._current_zh_started_at = segment_start if self._current_zh else None

        pending_ja = self._pending_ja_text()
        if self._show_pending and len(pending_ja) >= self.config.min_pending_display_chars:
            changed = True
        if self._show_pending and len(self._current_zh) >= self.config.min_pending_display_chars:
            changed = True
        if len(self._current_zh) >= self.config.max_pending_chars:
            now = time.monotonic()
            self._queue_zh_segment(self._current_zh, self._current_zh_started_at or now, now)
            self._current_zh = ""
            self._current_zh_started_at = None
            self._current_zh_changed_at = None
            changed = True
        if turn_complete:
            changed = self._finalize_current_turn() or changed
        return changed

    def _finalize_current_turn(self) -> bool:
        """Queue the current turn for immediate stable pairing.

        A Live turn-complete marker is stronger than punctuation heuristics:
        the model has stopped revising this turn, so sentence mode can commit
        it without waiting for the long-segment timeout.
        """
        self._turn_complete_pending = True
        if not self._current_zh:
            return bool(self._pending_zh_segments)
        now = time.monotonic()
        self._queue_zh_segment(
            self._current_zh,
            self._current_zh_started_at or now,
            now,
        )
        self._current_zh = ""
        self._current_zh_started_at = None
        self._current_zh_changed_at = None
        return True

    def _finish_turn_boundary(self) -> bool:
        """Commit this turn, then prevent its source from leaking into the next."""
        changed = self._flush_ready_segments()
        if self._pending_zh_segments:
            return changed
        changed = self._flush_stale_carry(force=True) or changed
        self._reset_pairing_state()
        self._turn_complete_pending = False
        return changed

    def _consume_stream(self, previous: str, incoming: str) -> tuple[list[str], str]:
        # Live output transcription packets are deltas. Text overlap is not a
        # segment identity: even "Hel" + "lo" must retain both copies of "l".
        text = previous + re.sub(r"\s+", " ", incoming)
        complete, pending = self._split_complete_segments(text)
        return complete, pending

    def _flush_source_segments(self, force: bool = False, now: float | None = None) -> bool:
        if not self._source_buffer:
            return False
        now = time.monotonic() if now is None else now
        started = self._source_started_at
        last_change = self._source_changed_at
        complete, pending = self._split_complete_segments(self._source_buffer)
        max_wait = max(0, self.config.stable_max_wait_ms) / 1000
        hard_wait = max(max_wait, self.config.stable_hard_max_wait_ms / 1000)
        stable = max_wait > 0 and (
            now - started >= hard_wait or
            (now - started >= max_wait and now - last_change >= self.config.stable_pause_ms / 1000)
        )
        if pending and (force or stable):
            complete.append(pending)
            pending = ""
        if not complete:
            return False
        changed = False
        for source in complete:
            changed = self._append_block(source, "", force=True) or changed
        self._source_buffer = pending
        self._source_started_at = now if pending else None
        self._source_changed_at = now if pending else None
        return changed

    def _add_ja_fragment(self, text: str) -> bool:
        # Input transcription packets are deltas as well. Preserve whitespace,
        # overlapping letters, and real repetitions until the display boundary.
        incoming = re.sub(r"\s+", " ", text)
        if not incoming:
            return False
        now = time.monotonic()
        if self._normalized_subtitle_language(self.config.subtitle_language) == "source":
            if not self._source_buffer:
                self._source_started_at = now
            self._source_buffer += incoming
            self._source_changed_at = now
            return True
        self._prune_ja_fragments(now)
        self._ja_fragments.append((now, incoming))
        self._prune_ja_fragments(now)
        self._log_subtitle("buffer_ja", ja=incoming)
        return True

    @staticmethod
    def _pending_zh_values(item: tuple) -> tuple[float, float, float, str, bool]:
        """Read both current and legacy four-field pending segment tuples."""
        queued_at, start_at, end_at, zh_text = item[:4]
        provisional = bool(item[4]) if len(item) > 4 else False
        return queued_at, start_at, end_at, zh_text, provisional

    def _queue_zh_segment(
        self,
        text: str,
        start_at: float,
        end_at: float,
        provisional: bool = False,
    ) -> None:
        zh = self._normalize(text)
        if not zh:
            return
        self._pending_zh_segments.append(
            (time.monotonic(), start_at, end_at, zh, bool(provisional))
        )
        self._log_subtitle(
            "queue_zh",
            zh=zh,
            provisional="1" if provisional else "",
        )
        self._log_latency("subtitle_queued")

    def _pending_commit_delay(self, provisional: bool) -> float:
        if getattr(self, "_turn_complete_pending", False):
            return 0.0
        delay = max(0, self.config.pair_commit_delay_ms) / 1000
        if provisional:
            delay += max(0, self.config.timeout_commit_grace_ms) / 1000
        return delay

    def _reset_session_state(self) -> None:
        """A new session cannot inherit pending text or history-based dedup."""
        self._reset_pairing_state()
        self._source_buffer = ""
        self._source_started_at = None
        self._source_changed_at = None
        self._current_zh = ""
        self._current_zh_started_at = None
        self._current_zh_changed_at = None
        self._pending_zh_segments = []
        self._carry_ja = ""
        self._carry_zh = ""
        self._carry_started_at = None
        self._turn_complete_pending = False
        self._history = []
        self._status = ""
        self._last_render_text = "\0"

    def _reset_pairing_state(self) -> None:
        """Clear source context without touching already queued translations."""
        self._ja_fragments = []
        self._source_tail = None
        self._source_continuation = None

    def _flush_stale_stable_segment(self, now: float | None = None) -> bool:
        """Commit an immutable chunk when a punctuation-free segment runs long."""
        if self._normalized_subtitle_language(self.config.subtitle_language) == "source":
            return self._flush_source_segments(now=now)
        if self._show_pending or not self._current_zh or self._current_zh_started_at is None:
            return False
        max_wait = max(0, self.config.stable_max_wait_ms) / 1000
        if max_wait <= 0:
            return False
        now = time.monotonic() if now is None else now
        age = now - self._current_zh_started_at
        hard_wait = max(max_wait, self.config.stable_hard_max_wait_ms / 1000)
        last_change = self._current_zh_changed_at or self._current_zh_started_at
        paused = now - last_change >= max(0.1, self.config.stable_pause_ms / 1000)
        # Do not publish a moving, unfinished predicate just because the
        # soft deadline elapsed. Prefer a clause boundary or a real pause.
        text = self._current_zh
        boundaries = [i + 1 for i, char in enumerate(text)
                      if char in SOFT_PUNCTUATION and i + 1 >= self.config.min_block_chars]
        if age >= hard_wait and self._is_hesitation_only(text):
            self._current_zh = ""
            self._current_zh_started_at = None
            self._current_zh_changed_at = None
            self._log_subtitle("drop_incomplete_hesitation", zh=text)
            return False
        if age >= hard_wait and self._looks_unfinished(text) and boundaries:
            split_at = boundaries[-1]
        elif age >= hard_wait or (paused and not self._looks_unfinished(text)):
            split_at = len(text)
        elif age >= max_wait and boundaries:
            split_at = boundaries[-1]
        else:
            return False
        segment = text[:split_at].strip()
        remaining = text[split_at:].strip()
        if not segment:
            return False

        segment_start = self._current_zh_started_at
        # A soft/pause split is provisional: give a continuation a short
        # chance to arrive and merge before rendering it.  A hard deadline
        # remains hard so the configured upper bound is not extended.
        self._queue_zh_segment(
            segment,
            segment_start,
            now,
            provisional=age < hard_wait,
        )
        self._current_zh = remaining
        self._current_zh_started_at = now if remaining else None
        self._current_zh_changed_at = now if remaining else None
        self._log_subtitle("stable_timeout_split", zh=segment)
        if age >= hard_wait:
            self._log_subtitle("hard_deadline_split", zh=segment)
        return True

    def _flush_ready_segments(self) -> bool:
        if not self._pending_zh_segments:
            return False
        changed = False
        now = time.monotonic()
        while self._pending_zh_segments:
            queued_at, start_at, end_at, zh_segment, provisional = self._pending_zh_values(
                self._pending_zh_segments[0]
            )
            delay = self._pending_commit_delay(provisional)
            if now - queued_at < delay:
                break
            # A timeout prefix and its continuation often arrive within the
            # same UI tick. Provisional timeout prefixes also get a short
            # grace window for a continuation that arrives after the source
            # timing window has moved on.
            while not self._ends_sentence(zh_segment) and len(self._pending_zh_segments) > 1:
                next_queued, next_start, next_end, next_text, _ = self._pending_zh_values(
                    self._pending_zh_segments[1]
                )
                within_timeout_grace = provisional and next_queued <= queued_at + delay
                if not within_timeout_grace and next_start > end_at + self.config.ja_pair_postroll_seconds:
                    break
                merged = self._join_source_fragments([zh_segment, next_text])
                if len(merged) > self.config.max_pending_chars:
                    break
                zh_segment = merged
                end_at = max(end_at, next_end)
                self._pending_zh_segments.pop(1)
                self._pending_zh_segments[0] = (
                    queued_at,
                    start_at,
                    end_at,
                    zh_segment,
                    provisional,
                )
            ja_text = self._ja_text_for_range(start_at, end_at, now)
            # Keep the unfinished beginning of the next source sentence for
            # the next translation, including tails within the same packet.
            paired_source = ja_text
            if self._ends_sentence(zh_segment):
                boundaries = [i for i in range(len(ja_text)) if self._is_sentence_boundary(ja_text, i)]
                sentences = sum(self._is_sentence_boundary(zh_segment, i) for i in range(len(zh_segment)))
                boundary = boundaries[min(sentences, len(boundaries)) - 1] if boundaries else -1
                if boundary >= 0 and ja_text[boundary + 1:].strip():
                    paired_source = ja_text[:boundary + 1].strip()
            max_wait = max(0, self.config.max_wait_for_ja_ms) / 1000
            if (
                self.config.require_bilingual_for_display
                and not ja_text
                and not getattr(self, "_turn_complete_pending", False)
                and now - queued_at < max_wait
            ):
                break
            self._pending_zh_segments.pop(0)
            display_source = self._source_text_for_display(paired_source)
            source_confident, source_reason = self._source_pair_confidence(
                display_source,
                zh_segment,
            )
            if display_source and not source_confident:
                self._log_subtitle(
                    "reject_low_confidence_source",
                    ja=display_source,
                    zh=zh_segment,
                    reason=source_reason,
                )
                display_source = ""
                self._carry_ja = ""
                self._carry_zh = ""
                self._carry_started_at = None
            self._log_subtitle("pair_candidate", ja=display_source, zh=zh_segment)
            if source_confident:
                rendered = self._append_block(display_source, zh_segment)
            else:
                # Do not let a rejected short block become carry context for
                # a later, unrelated source fragment.
                rendered = self._append_block(display_source, zh_segment, force=True)
            changed = rendered or changed
            if not source_confident:
                self._reset_pairing_state()
            elif self.config.clear_ja_after_pair:
                if self._ends_sentence(zh_segment):
                    self._consume_paired_ja(paired_source, end_at)
                    self._source_continuation = None
                elif paired_source:
                    # Without an alignment boundary it is safer to share the
                    # source context than to leave the continuation orphaned.
                    self._source_continuation = (end_at, paired_source)
        return changed

    def _source_pair_confidence(
        self,
        source: str,
        translation: str,
    ) -> tuple[bool, str]:
        """Reject source text that is likely stale context for this translation."""
        source_compact = self._compact(source)
        translation_compact = self._compact(translation)
        if not source_compact:
            return True, ""

        translation_length = max(1, len(translation_compact))
        max_source_length = max(
            self.config.max_source_overhang_chars,
            int(
                translation_length * max(1.0, self.config.max_source_to_translation_ratio)
                + self.config.max_source_overhang_chars
            ),
        )
        if len(source_compact) > max_source_length:
            return False, "source_overlong"

        continuation = getattr(self, "_source_continuation", None)
        if continuation is not None:
            _, previous_source = continuation
            if source_compact == self._compact(previous_source):
                return False, "stale_continuation"
        return True, ""

    def _source_text_for_display(self, paired_source: str) -> str:
        """Avoid displaying a continuation's already-rendered source prefix.

        The full source context is still used for alignment and consumption,
        but repeating it in every continuation block makes stable bilingual
        subtitles look duplicated. Only the newly arrived suffix is rendered
        once the previous context is already present in history.
        """
        continuation = getattr(self, "_source_continuation", None)
        if not continuation or not paired_source:
            return paired_source
        _, previous_source = continuation
        if not previous_source or not self._is_recent_text(previous_source, "ja"):
            return paired_source
        normalized_source = self._normalize(paired_source)
        normalized_previous = self._normalize(previous_source)
        if normalized_source.startswith(normalized_previous):
            return normalized_source[len(normalized_previous) :].strip()
        return paired_source

    def _flush_stale_carry(self, force: bool = False) -> bool:
        if not (self._carry_ja or self._carry_zh) or self._carry_started_at is None:
            return False
        max_wait = max(0, self.config.max_short_carry_ms) / 1000
        if not force and time.monotonic() - self._carry_started_at < max_wait:
            return False
        ja = self._carry_ja
        zh = self._carry_zh
        self._carry_ja = ""
        self._carry_zh = ""
        self._carry_started_at = None
        self._log_subtitle("flush_short_block", ja=ja, zh=zh)
        rendered = self._append_block(ja, zh, force=True)
        return rendered

    def _append_block(self, ja_text: str, zh_text: str, force: bool = False) -> bool:
        incoming_block = {
            "ja": self._normalize(ja_text),
            "zh": self._normalize(zh_text),
        }
        if self.config.skip_filler_subtitles and self._is_filler_block(incoming_block):
            self._log_subtitle(
                "skip_filler_block",
                ja=incoming_block["ja"],
                zh=incoming_block["zh"],
            )
            return False

        ja = self._limit_text(
            self._join_source_fragments([self._carry_ja, self._normalize(ja_text)]),
            0 if self._normalized_subtitle_language(self.config.subtitle_language) == "source"
            else self.config.ja_pair_max_chars,
        )
        zh = self._join_source_fragments([self._carry_zh, zh_text])
        if not ja and not zh:
            return False
        block = {"ja": ja, "zh": zh}

        if self.config.skip_filler_subtitles and self._is_filler_block(block):
            self._carry_ja = ""
            self._carry_zh = ""
            self._carry_started_at = None
            self._log_subtitle("skip_filler_block", ja=ja, zh=zh)
            return False

        if not force and self._should_carry_block(block):
            self._carry_ja = ja
            self._carry_zh = zh
            if self._carry_started_at is None:
                self._carry_started_at = time.monotonic()
            self._log_subtitle("carry_short_block", ja=ja, zh=zh)
            return self._show_pending and self._block_len(block) >= self.config.min_pending_display_chars

        # A committed segment is a new utterance. Without a server segment ID,
        # text equality or containment cannot establish that it is a replay.
        self._history.append(block)
        self._carry_ja = ""
        self._carry_zh = ""
        self._carry_started_at = None
        if not self._is_compact_style and len(self._history) > self.config.max_history_sentences:
            self._history = self._history[-self.config.max_history_sentences :]
        self._log_subtitle("render_block", ja=ja, zh=zh)
        return True

    def _compact_track_text(self) -> tuple[str, str]:
        if self._status:
            return "", self._status
        source_parts: list[str] = []
        translation_parts: list[str] = []
        for block in self._history:
            if self._show_source_text and block.get("ja"):
                source_parts.append(block["ja"])
            if self._show_translation_text and block.get("zh"):
                translation_parts.append(block["zh"])
        if self._show_pending:
            pending_ja = self._pending_ja_text()
            if self._show_source_text and pending_ja:
                source_parts.append(pending_ja)
            if self._show_translation_text and self._current_zh:
                translation_parts.append(self._current_zh)
        return (
            self._display_text(" ".join(source_parts)),
            self._display_text(" ".join(translation_parts)),
        )

    def _build_render_items(self) -> list[tuple[str, str]]:
        pending_ja = self._pending_ja_text()
        if self._status:
            return [(self._status, "status")]

        items: list[tuple[str, str]] = []
        for block in self._history:
            if self._show_source_text and block.get("ja"):
                items.append((self._display_text(block["ja"]), "ja"))
            if self._show_translation_text and block.get("zh"):
                items.append((self._display_text(block["zh"]), "zh"))

        if not self._show_pending:
            return items

        for block in self._pending_pair_blocks():
            if self._show_source_text and block.get("ja") and not self._is_recent_text(block["ja"], "ja"):
                items.append((self._display_text(block["ja"]), "pending"))
            if self._show_translation_text and block.get("zh") and not self._is_recent_text(block["zh"], "zh"):
                items.append((self._display_text(block["zh"]), "pending"))

        if self._carry_ja or self._carry_zh:
            carry_block = {"ja": self._carry_ja, "zh": self._carry_zh}
            if self._block_len(carry_block) >= self.config.min_pending_display_chars:
                if self._show_source_text and self._carry_ja and not self._is_recent_text(self._carry_ja, "ja"):
                    items.append((self._display_text(self._carry_ja), "pending"))
                if self._show_translation_text and self._carry_zh and not self._is_recent_text(self._carry_zh, "zh"):
                    items.append((self._display_text(self._carry_zh), "pending"))

        if pending_ja or self._current_zh:
            if self._show_source_text and pending_ja and not self._is_recent_text(pending_ja, "ja"):
                items.append((self._display_text(pending_ja), "pending"))
            if self._show_translation_text and self._current_zh and not self._is_recent_text(self._current_zh, "zh"):
                items.append((self._display_text(self._current_zh), "pending"))
            self._log_subtitle("render_pending", ja=pending_ja, zh=self._current_zh)
        return items

    @staticmethod
    def _normalize(text: str | None) -> str:
        return " ".join((text or "").split())

    @staticmethod
    def _display_text(text: str | None) -> str:
        """Replace sentence full stops only at the final display boundary."""
        normalized = SubtitleState._normalize(text)
        if not normalized:
            return ""
        # Keep decimal points such as 3.14 intact, while replacing ordinary
        # ASCII sentence periods and CJK full stops with visual spaces.
        normalized = re.sub(r"(?<!\d)\.(?!\d)", " ", normalized)
        normalized = normalized.translate(str.maketrans({"。": " ", "｡": " ", "．": " "}))
        return SubtitleState._normalize(normalized)

    @property
    def _show_pending(self) -> bool:
        return self.config.display_mode.lower() in {"stream", "streaming", "pending"}

    @property
    def _show_source_text(self) -> bool:
        return self._normalized_subtitle_language(self.config.subtitle_language) in {"bilingual", "source"}

    @property
    def _show_translation_text(self) -> bool:
        return self._normalized_subtitle_language(self.config.subtitle_language) in {"bilingual", "translation"}

    @staticmethod
    def _normalized_display_mode(mode: str) -> str:
        value = (mode or "").strip().lower()
        if value in {"stream", "streaming", "pending"}:
            return "streaming"
        return "sentence"

    @staticmethod
    def _normalized_subtitle_language(language: str) -> str:
        value = (language or "").strip().lower()
        if value in {"source", "original", "ja", "japanese", "input"}:
            return "source"
        if value in {"translation", "translated", "zh", "chinese", "target"}:
            return "translation"
        return "bilingual"

    @staticmethod
    def _normalized_layout_style(style: str) -> str:
        value = (style or "").strip().lower()
        return "compact" if value in {"compact", "two-line", "twoline", "browser"} else "classic"

    @property
    def _is_compact_style(self) -> bool:
        return self._normalized_layout_style(self.config.layout_style) == "compact"

    @staticmethod
    def _compact(text: str) -> str:
        return "".join(text.split())

    def _is_recent_text(self, text: str, field: str) -> bool:
        compact = self._compact(self._normalize(text))
        if not compact:
            return False
        if len(compact) < self.config.duplicate_min_chars:
            return False
        recent_window = max(1, self.config.duplicate_recent_window)
        for block in self._history[-recent_window:]:
            recent = self._compact(block.get(field, ""))
            if compact == recent or (len(compact) >= 8 and compact in recent):
                return True
        return False

    def _should_carry_block(self, block: dict[str, str]) -> bool:
        if self._block_len(block) >= self.config.min_block_chars:
            return False
        return True

    def _is_filler_block(self, block: dict[str, str]) -> bool:
        ja = self._compact_for_filter(block.get("ja", ""))
        zh = self._compact_for_filter(block.get("zh", ""))
        max_chars = max(1, self.config.filler_max_chars)
        if ja and len(ja) > max_chars:
            return False
        if zh and len(zh) > max_chars:
            return False
        ja_is_filler = self._is_filler_text(ja, "ja")
        zh_is_filler = self._is_filler_text(zh, "zh")
        if ja and zh:
            return ja_is_filler and zh_is_filler
        return ja_is_filler or zh_is_filler

    @staticmethod
    def _compact_for_filter(text: str) -> str:
        trim_chars = HARD_PUNCTUATION + SOFT_PUNCTUATION + " \t\r\n.。！？!?、，,;；"
        return "".join(text.strip(trim_chars).split()).lower()

    @staticmethod
    def _is_filler_text(text: str, language: str) -> bool:
        if not text:
            return False
        ja_fillers = {
            "\u3046\u3093",
            "\u3046",
            "\u3046\u3046\u3093",
            "\u3046\u30fc\u3093",
            "\u3048",
            "\u3048\u3048",
            "\u3048\u30fc",
            "\u3042",
            "\u3042\u3042",
            "\u3042\u306e",
            "\u3048\u3063\u3068",
            "\u307e\u3042",
            "\u306f\u3044",
            "\u306d",
            "\u306a",
            "\u305d\u3046",
            "\u305d\u3046\u3060",
            "\u305d\u3046\u3060\u306d",
            "\u3046\u3093\u305d\u3046",
            "\u305d\u3046\u3046\u3093",
            "\u305d\u3046\u305d\u3046",
            "\u3046\u3093\u305d\u3046\u305d\u3046",
            "\u306f\u3044\u306f\u3044",
            "\u3075\u3075",
            "\u3075\u3075\u3075",
        }
        zh_fillers = {
            "\u55ef",
            "\u55ef\u55ef",
            "\u554a",
            "\u54e6",
            "\u989d",
            "\u5443",
            "\u8bf6",
            "\u5450",
            "\u5bf9",
            "\u55ef\u5bf9",
            "\u5bf9\u5bf9",
            "\u55ef\u5bf9\u5bf9",
            "\u5bf9\u5bf9\u5bf9",
            "\u5bf9\u554a",
            "\u662f",
            "\u662f\u7684",
            "\u662f\u554a",
            "\u662f\u5427",
            "\u90a3\u4e2a",
            "\u8fd9\u4e2a",
            "\u600e\u4e48\u8bf4",
            "\u600e\u4e48\u8bf4\u5462",
            "\u5475\u5475",
            "\u54c8\u54c8",
        }
        fillers = ja_fillers if language == "ja" else zh_fillers
        return text in fillers

    def _pending_ja_text(self) -> str:
        if self._normalized_subtitle_language(self.config.subtitle_language) == "source":
            return self._source_buffer
        if self._current_zh and self._current_zh_started_at is not None:
            return self._ja_text_for_range(
                self._current_zh_started_at,
                time.monotonic(),
                time.monotonic(),
            )
        if self._pending_zh_segments:
            return ""
        return self._recent_ja_text()

    def _pending_pair_blocks(self) -> list[dict[str, str]]:
        now = time.monotonic()
        blocks: list[dict[str, str]] = []
        for item in self._pending_zh_segments:
            _, start_at, end_at, zh_text, _ = self._pending_zh_values(item)
            blocks.append(
                {
                    "ja": self._ja_text_for_range(start_at, end_at, now),
                    "zh": zh_text,
                }
            )
        return blocks

    def _ja_text_for_range(self, start_at: float, end_at: float, now: float) -> str:
        self._prune_ja_fragments(now)
        start = start_at - max(0.0, self.config.ja_pair_preroll_seconds)
        end = end_at + max(0.0, self.config.ja_pair_postroll_seconds)
        fragments = [
            (timestamp, text)
            for timestamp, text in self._ja_fragments
            if start <= timestamp <= end
            or ((timestamp, text) == getattr(self, "_source_tail", None) and timestamp <= end)
        ]
        continuation = getattr(self, "_source_continuation", None)
        if continuation is not None:
            previous_end, continuation_text = continuation
            ttl = max(1.0, self.config.stable_hard_max_wait_ms / 1000)
            if 0 <= end_at - previous_end <= ttl:
                # The previous incomplete translation already consumed this
                # source context logically.  Re-selecting the old fragments
                # here duplicates prefixes such as "何では岩じゃな" when
                # the continuation arrives. Keep that context once, then add
                # only source fragments that arrived afterwards.
                continuation_fragments = [
                    (ts, text)
                    for ts, text in self._ja_fragments
                    if previous_end < ts <= end
                ]
                fragments = [(previous_end, continuation_text)] + continuation_fragments
            else:
                self._source_continuation = None
        if not fragments:
            window_start = end_at - max(0.1, self.config.ja_pair_window_seconds)
            fragments = [
                (timestamp, text)
                for timestamp, text in self._ja_fragments
                if window_start <= timestamp <= end_at
            ]
        text = "".join(fragment for _, fragment in fragments)
        return self._limit_text(text, self.config.ja_pair_max_chars, preserve_whitespace=True)

    def _recent_ja_text(
        self,
        now: float | None = None,
        include_window_only: bool = True,
    ) -> str:
        now = time.monotonic() if now is None else now
        self._prune_ja_fragments(now)
        fragments = self._ja_fragments
        if include_window_only:
            window = max(0.1, self.config.ja_pair_window_seconds)
            fragments = [(ts, text) for ts, text in fragments if now - ts <= window]
            if not fragments:
                fragments = self._ja_fragments[-3:]
        text = "".join(fragment for _, fragment in fragments)
        return self._limit_text(text, self.config.ja_pair_max_chars, preserve_whitespace=True)

    def _prune_ja_fragments(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        keep_seconds = max(3.0, self.config.ja_pair_window_seconds * 3)
        cutoff = now - keep_seconds
        # An output-transcription segment may remain open for tens of seconds
        # during continuous speech.  Keep the Japanese that belongs to that
        # segment until it is committed; otherwise the Chinese still starts at
        # the beginning of the utterance while the Japanese has already been
        # reduced to the last few seconds.
        protected_starts: list[float] = []
        if self._current_zh and self._current_zh_started_at is not None:
            protected_starts.append(self._current_zh_started_at)
        protected_starts.extend(
            self._pending_zh_values(item)[1]
            for item in self._pending_zh_segments
        )
        if protected_starts:
            cutoff = min(
                cutoff,
                min(protected_starts)
                - max(0.0, self.config.ja_pair_preroll_seconds),
            )
        continuation = getattr(self, "_source_continuation", None)
        if continuation is not None:
            previous_end, _ = continuation
            ttl = max(1.0, self.config.stable_hard_max_wait_ms / 1000)
            if now - previous_end <= ttl:
                cutoff = min(cutoff, previous_end - ttl)
        self._ja_fragments = [
            (timestamp, text)
            for timestamp, text in self._ja_fragments
            if timestamp >= cutoff
        ]
        max_chars = max(
            self.config.ja_pair_max_chars * 4,
            self.config.max_pending_chars * 4,
            self.config.ja_pair_max_chars,
        )
        total = 0
        kept: list[tuple[float, str]] = []
        for timestamp, text in reversed(self._ja_fragments):
            total += len(self._compact(text))
            kept.append((timestamp, text))
            if total >= max_chars:
                break
        self._ja_fragments = list(reversed(kept))

    def _consume_paired_ja(self, paired: str, end_at: float) -> None:
        """Consume only the selected source prefix; retain a packet's tail."""
        remaining = self._normalize(paired)
        kept = []
        source_tail = getattr(self, "_source_tail", None)
        for timestamp, text in self._ja_fragments:
            normalized = self._normalize(text)
            if remaining and remaining.startswith(normalized):
                remaining = remaining[len(normalized):].lstrip()
            elif remaining and normalized.startswith(remaining):
                tail = normalized[len(remaining):].lstrip()
                if tail:
                    # Keep this remainder eligible for the next output range.
                    source_tail = (max(timestamp, end_at), tail)
                    kept.append(source_tail)
                remaining = ""
            else:
                kept.append((timestamp, text))
        self._ja_fragments = kept
        self._source_tail = source_tail if source_tail in kept else None

    def _log_latency(self, stage: str) -> None:
        if self.config.log_latency_metrics and self.logger is not None:
            self.logger.info("latency stage=%s monotonic_s=%.6f", stage, time.monotonic())

    def _limit_text(self, text: str, max_chars: int, preserve_whitespace: bool = False) -> str:
        normalized = re.sub(r"\s+", " ", text) if preserve_whitespace else self._normalize(text)
        if max_chars <= 0 or len(self._compact(normalized)) <= max_chars:
            return normalized
        trimmed = normalized[-max_chars:]
        trimmed = trimmed.lstrip() if preserve_whitespace else trimmed.strip()
        trim_chars = HARD_PUNCTUATION + SOFT_PUNCTUATION + " "
        while trimmed and trimmed[0] in trim_chars:
            trimmed = trimmed[1:]
            trimmed = trimmed.lstrip() if preserve_whitespace else trimmed.strip()
        return trimmed

    @staticmethod
    def _block_len(block: dict[str, str]) -> int:
        return max(len(SubtitleState._compact(block.get("ja", ""))), len(SubtitleState._compact(block.get("zh", ""))))

    def _log_subtitle(self, event: str, **fields: str) -> None:
        if not self.config.log_rendered_subtitles or self.logger is None:
            return
        detail = " | ".join(f"{key}={value}" for key, value in fields.items() if value)
        self.logger.info("subtitle %s%s%s", event, " | " if detail else "", detail)

    @staticmethod
    def _is_status_text(text: str | None) -> bool:
        normalized = SubtitleState._normalize(text)
        prefixes = (
            "Connecting to Gemini",
            "Connected.",
            "Gemini connection failed",
            "Gemini reconnecting",
            "Audio/Gemini error",
            "Audio capture stopped",
            "Switching audio device",
            "Paused.",
            "No audio.",
            "Audio detected.",
            "Translator stopped",
            "Missing Gemini API key",
            "No usable loopback device",
            "No default speaker",
        )
        return normalized.startswith(prefixes)

    def _split_complete_segments(self, text: str) -> tuple[list[str], str]:
        complete: list[str] = []
        start = 0
        for index, char in enumerate(text):
            # A Live delta may end halfway through a decimal ("3." + "14").
            # Keep that ambiguous suffix until another character disambiguates
            # it. Turn completion and the existing stable deadlines still flush
            # pending text when the period really does end an integer sentence.
            if char == "." and index == len(text) - 1 and index > 0 and text[index - 1].isdigit():
                continue
            if self._is_sentence_boundary(text, index):
                complete.append(text[start : index + 1].strip())
                start = index + 1

        pending = text[start:].lstrip()
        target_chars = max(self.config.min_block_chars, self.config.target_line_chars)
        if self._show_pending and not complete and len(pending) >= target_chars:
            for mark in SOFT_PUNCTUATION:
                split_at = pending.rfind(mark)
                if split_at >= self.config.min_block_chars:
                    complete.append(pending[: split_at + 1].strip())
                    pending = pending[split_at + 1 :].strip()
                    break
        if not complete and len(pending) >= self.config.max_pending_chars:
            limit = self.config.max_pending_chars
            boundaries = [i + 1 for i, char in enumerate(pending[:limit]) if char in SOFT_PUNCTUATION]
            split_at = boundaries[-1] if boundaries else limit
            complete.append(pending[:split_at].strip())
            pending = pending[split_at:].strip()
        return [item for item in complete if item], pending

    @staticmethod
    def _ends_sentence(text: str) -> bool:
        text = text.strip()
        return bool(text) and SubtitleState._is_sentence_boundary(text, len(text) - 1)

    @staticmethod
    def _is_sentence_boundary(text: str, index: int) -> bool:
        char = text[index]
        if char in HARD_PUNCTUATION:
            return True
        if char != ".":
            return False
        if 0 < index < len(text) - 1 and text[index - 1].isdigit() and text[index + 1].isdigit():
            return False
        prefix = text[:index + 1]
        if re.search(r"\b(?:Mr|Mrs|Ms|Dr|Prof|Sr|Jr|vs|etc)\.$", prefix, re.I):
            return False
        if re.search(r"(?:\b[A-Za-z]\.){2,}$", prefix):
            return False
        return index == len(text) - 1 or text[index + 1].isspace()

    @staticmethod
    def _join_source_fragments(fragments: list[str]) -> str:
        result = ""
        for fragment in fragments:
            fragment = SubtitleState._normalize(fragment)
            if not fragment:
                continue
            # Latin words need separation; CJK fragments usually do not.
            if result and re.search(r"[A-Za-z0-9.,;:!?]$", result) and re.match(r"[A-Za-z]", fragment):
                result += " "
            result += fragment
        return result

    @staticmethod
    def _looks_unfinished(text: str) -> bool:
        text = text.rstrip()
        if SubtitleState._is_hesitation_only(text):
            return True
        if text.endswith(tuple(SOFT_PUNCTUATION)):
            return True
        return bool(re.search(
            r"(?:因为|所以|但是|虽然|如果|然后|比如|例如|觉得|认为|它会|他会|我会|将会|会|能够|可以|需要|正在|很|的|在|把|被|只是|真的是|再也|好像|那个|我嗯)$"
            r"|\b(?:because|although|if|but|and|will|would|can|could|to|the|a|an|of|for|with)$",
            text, re.I,
        ))

    @staticmethod
    def _is_hesitation_only(text: str) -> bool:
        compact = re.sub(r"[\s，,、。.!！？?]", "", text)
        return bool(re.fullmatch(r"(?:嗯|啊|呃|那个|好像|真的是|只是|再也)+", compact))
