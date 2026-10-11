"""Typed subtitle options, with defaults supplied by the bundled YAML."""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import get_type_hints

from .config import default_config

_DEFAULTS = default_config()


@dataclass
class SubtitleWindowConfig:
    title: str = _DEFAULTS["app"]["title"]
    display_mode: str = _DEFAULTS["subtitle"]["display_mode"]
    subtitle_language: str = _DEFAULTS["subtitle"]["subtitle_language"]
    layout_style: str = _DEFAULTS["subtitle"]["layout_style"]
    compact_height: int = _DEFAULTS["subtitle"]["compact_height"]
    compact_window_width: int = _DEFAULTS["subtitle"]["compact_window_width"]
    compact_min_window_width: int = _DEFAULTS["subtitle"]["compact_min_window_width"]
    compact_font_size: int = _DEFAULTS["subtitle"]["compact_font_size"]
    compact_opacity: float = _DEFAULTS["subtitle"]["compact_opacity"]
    compact_background_color: str = _DEFAULTS["subtitle"]["compact_background_color"]
    font_size: int = _DEFAULTS["subtitle"]["font_size"]
    font_family: str = _DEFAULTS["subtitle"]["font_family"]
    font_weight: str = _DEFAULTS["subtitle"]["font_weight"]
    render_interval_ms: int = _DEFAULTS["subtitle"]["render_interval_ms"]
    opacity: float = _DEFAULTS["subtitle"]["opacity"]
    always_on_top: bool = _DEFAULTS["subtitle"]["always_on_top"]
    window_width: int = _DEFAULTS["subtitle"]["window_width"]
    window_height: int = _DEFAULTS["subtitle"]["window_height"]
    min_window_width: int = _DEFAULTS["subtitle"]["min_window_width"]
    max_history_sentences: int = _DEFAULTS["subtitle"]["max_history_sentences"]
    min_pending_display_chars: int = _DEFAULTS["subtitle"]["min_pending_display_chars"]
    min_block_chars: int = _DEFAULTS["subtitle"]["min_block_chars"]
    target_line_chars: int = _DEFAULTS["subtitle"]["target_line_chars"]
    max_pending_chars: int = _DEFAULTS["subtitle"]["max_pending_chars"]
    stable_max_wait_ms: int = _DEFAULTS["subtitle"]["stable_max_wait_ms"]
    stable_hard_max_wait_ms: int = _DEFAULTS["subtitle"]["stable_hard_max_wait_ms"]
    stable_pause_ms: int = _DEFAULTS["subtitle"]["stable_pause_ms"]
    timeout_commit_grace_ms: int = _DEFAULTS["subtitle"]["timeout_commit_grace_ms"]
    log_latency_metrics: bool = _DEFAULTS["app"]["log_latency_metrics"]
    log_rendered_subtitles: bool = _DEFAULTS["subtitle"]["log_rendered_subtitles"]
    duplicate_recent_window: int = _DEFAULTS["subtitle"]["duplicate_recent_window"]
    duplicate_min_chars: int = _DEFAULTS["subtitle"]["duplicate_min_chars"]
    ja_pair_window_seconds: float = _DEFAULTS["subtitle"]["ja_pair_window_seconds"]
    ja_pair_max_chars: int = _DEFAULTS["subtitle"]["ja_pair_max_chars"]
    ja_pair_preroll_seconds: float = _DEFAULTS["subtitle"]["ja_pair_preroll_seconds"]
    ja_pair_postroll_seconds: float = _DEFAULTS["subtitle"]["ja_pair_postroll_seconds"]
    max_source_to_translation_ratio: float = _DEFAULTS["subtitle"]["max_source_to_translation_ratio"]
    max_source_overhang_chars: int = _DEFAULTS["subtitle"]["max_source_overhang_chars"]
    pair_commit_delay_ms: int = _DEFAULTS["subtitle"]["pair_commit_delay_ms"]
    require_bilingual_for_display: bool = _DEFAULTS["subtitle"]["require_bilingual_for_display"]
    max_wait_for_ja_ms: int = _DEFAULTS["subtitle"]["max_wait_for_ja_ms"]
    skip_filler_subtitles: bool = _DEFAULTS["subtitle"]["skip_filler_subtitles"]
    filler_max_chars: int = _DEFAULTS["subtitle"]["filler_max_chars"]
    max_short_carry_ms: int = _DEFAULTS["subtitle"]["max_short_carry_ms"]
    clear_ja_after_pair: bool = _DEFAULTS["subtitle"]["clear_ja_after_pair"]
    background_color: str = _DEFAULTS["subtitle"]["background_color"]
    text_color: str = _DEFAULTS["subtitle"]["text_color"]
    accent_color: str = _DEFAULTS["subtitle"]["accent_color"]

    @classmethod
    def from_config(cls, config: dict, settings=None):
        values = {field.name: config["subtitle"].get(field.name, field.default) for field in fields(cls)}
        values["title"] = config["app"]["title"]
        values["log_latency_metrics"] = config["app"]["log_latency_metrics"]
        if settings is not None:
            for key in ("display_mode", "subtitle_language", "layout_style"):
                values[key] = settings.get(key, values[key])
        for key, kind in get_type_hints(cls).items():
            value = values[key]
            if kind is bool and not isinstance(value, bool):
                raise ValueError(f"subtitle.{key} must be a boolean")
            values[key] = kind(value)
        return cls(**values)
