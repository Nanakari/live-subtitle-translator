from __future__ import annotations

import queue
import re
import time
import tkinter as tk
import tkinter.font as tkfont
import ctypes
from dataclasses import dataclass
from logging import Logger
from tkinter import Menu, ttk
from typing import Any, Callable


HARD_PUNCTUATION = "\u3002\uff01\uff1f!?;\uff1b"
SOFT_PUNCTUATION = "\uff0c,\u3001"
COMPACT_CORNER_RADIUS = 14
COMPACT_BORDER_COLOR = "#3b4654"
COMPACT_BOTTOM_PADDING = 9


@dataclass
class SubtitleWindowConfig:
    title: str = "Gemini Live Translator"
    display_mode: str = "sentence"
    subtitle_language: str = "bilingual"
    layout_style: str = "compact"
    compact_height: int = 68
    compact_window_width: int = 900
    compact_min_window_width: int = 380
    compact_font_size: int = 15
    compact_opacity: float = 0.90
    compact_background_color: str = "#161a20"
    font_size: int = 14
    font_family: str = "Microsoft YaHei UI"
    font_weight: str = "normal"
    render_interval_ms: int = 100
    opacity: float = 0.85
    always_on_top: bool = True
    window_width: int = 760
    window_height: int = 260
    min_window_width: int = 420
    max_history_sentences: int = 8
    min_pending_display_chars: int = 12
    min_block_chars: int = 8
    target_line_chars: int = 32
    max_pending_chars: int = 180
    stable_max_wait_ms: int = 6500
    stable_hard_max_wait_ms: int = 12000
    stable_pause_ms: int = 1800
    log_latency_metrics: bool = False
    log_rendered_subtitles: bool = False
    duplicate_recent_window: int = 6
    duplicate_min_chars: int = 6
    ja_pair_window_seconds: float = 2.0
    ja_pair_max_chars: int = 320
    ja_pair_preroll_seconds: float = 0.5
    ja_pair_postroll_seconds: float = 0.25
    pair_commit_delay_ms: int = 600
    require_bilingual_for_display: bool = True
    max_wait_for_ja_ms: int = 1800
    skip_filler_subtitles: bool = True
    filler_max_chars: int = 6
    max_short_carry_ms: int = 1800
    clear_ja_after_pair: bool = True
    background_color: str = "#111111"
    text_color: str = "#ffffff"
    accent_color: str = "#ffffff"


class SubtitleWindow:
    def __init__(
        self,
        config: SubtitleWindowConfig,
        on_close: Callable[[], None] | None = None,
        logger: Logger | None = None,
        on_pause_toggle: Callable[[], None] | None = None,
        audio_devices: list[str] | None = None,
        selected_audio_device: str = "",
        on_audio_device_selected: Callable[[str], None] | None = None,
        on_settings_changed: Callable[[dict], None] | None = None,
        desktop_state: dict | None = None,
    ) -> None:
        self.config = config
        self.on_close = on_close
        self.logger = logger
        self.on_pause_toggle = on_pause_toggle
        self.audio_devices = audio_devices or []
        self.on_audio_device_selected = on_audio_device_selected
        self.on_settings_changed = on_settings_changed
        self._closing = False
        desktop_state = desktop_state or {}
        # Gemini can briefly burst events after a reconnect.  A bounded queue
        # and batched draining keep the Tk main thread responsive to redraws
        # and the close button instead of processing an endless backlog.
        self._queue: queue.Queue[tuple[str, str | None, str | None]] = queue.Queue(maxsize=200)
        self._drag_x = 0
        self._drag_y = 0
        self._resize_start: tuple[int, int, int, int] | None = None
        self._resize_edges: tuple[bool, bool, bool, bool] | None = None
        self._compact_region_handle: int | None = None
        self._style_generation = 0
        self._classic_geometry = self._valid_geometry(
            desktop_state.get("classic_geometry"),
            (config.window_width, config.window_height, 180, 80),
        )
        self._compact_geometry = self._valid_geometry(
            desktop_state.get("compact_geometry"),
            (
                config.compact_window_width,
                config.compact_height,
                180,
                80,
            ),
        )
        self._history: list[dict[str, str]] = []
        self._ja_fragments: list[tuple[float, str]] = []
        self._source_tail: tuple[float, str] | None = None
        self._source_continuation: tuple[float, str] | None = None
        self._current_zh = ""
        self._current_zh_started_at: float | None = None
        self._current_zh_changed_at: float | None = None
        self._carry_ja = ""
        self._carry_zh = ""
        self._carry_started_at: float | None = None
        self._pending_zh_segments: list[tuple[float, float, float, str]] = []
        self._status = ""
        self._recent_blocks: list[tuple[str, str]] = []
        self._last_render_text = ""

        self.root = tk.Tk()
        self.display_mode_var = tk.StringVar(
            self.root,
            value=self._normalized_display_mode(config.display_mode),
        )
        self.subtitle_language_var = tk.StringVar(
            self.root,
            value=self._normalized_subtitle_language(config.subtitle_language),
        )
        self.layout_style_var = tk.StringVar(
            self.root,
            value=self._normalized_layout_style(config.layout_style),
        )
        self.audio_device_var = tk.StringVar(self.root, value=selected_audio_device)
        self.root.title(config.title)
        self.root.geometry(f"{config.window_width}x{config.window_height}+180+80")
        self.root.minsize(config.min_window_width, 120)
        self.root.resizable(True, True)
        self.root.attributes("-topmost", bool(config.always_on_top))
        self.root.attributes("-alpha", max(0.2, min(1.0, config.opacity)))
        self.root.configure(bg=config.background_color)
        self._configure_dark_scrollbar_style()
        self.text_font = tkfont.Font(
            family=config.font_family,
            size=config.font_size,
            weight=config.font_weight,
        )
        self.compact_source_font = tkfont.Font(
            family="Segoe UI",
            # Tk positive sizes are points; CSS uses pixels.  Negative Tk
            # sizes preserve the extension's 16px / 17px visual scale.
            size=-max(1, config.compact_font_size - 1),
            weight="normal",
        )
        self.compact_translation_font = tkfont.Font(
            family="Segoe UI",
            size=-config.compact_font_size,
            weight="normal",
        )

        self.compact_canvas = tk.Canvas(
            self.root,
            bg="#00ff00",
            highlightthickness=0,
            bd=0,
        )
        self.compact_canvas.bind("<Configure>", self._draw_compact_background)

        self.frame = tk.Frame(self.root, bg=config.background_color, padx=8, pady=6)
        self.frame.pack(fill="both", expand=True)

        self.header = tk.Frame(self.frame, bg=config.background_color, height=18)
        self.header.pack(fill="x")

        self.close_button = tk.Button(
            self.header,
            text="x",
            command=self.close,
            fg="#aaaaaa",
            bg=config.background_color,
            activebackground="#2a2a2a",
            activeforeground="white",
            bd=0,
            highlightthickness=0,
            width=2,
        )
        self.close_button.pack(anchor="ne")

        self.compact_close_button = tk.Button(
            self.frame,
            text="\u00d7",
            command=self.close,
            fg="#dddddd",
            bg=config.background_color,
            activebackground="#2a2a2a",
            activeforeground="white",
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            font=("Segoe UI", -16),
        )
        self.compact_resize_handle = tk.Canvas(
            self.frame,
            width=14,
            height=14,
            bg=config.background_color,
            highlightthickness=0,
            cursor="size_nw_se",
        )
        self.compact_resize_handle.create_line(4, 13, 13, 13, fill="#a0a0a0", width=1)
        self.compact_resize_handle.create_line(13, 4, 13, 13, fill="#a0a0a0", width=1)

        self.compact_source_label = tk.Label(
            self.frame,
            bg=config.compact_background_color,
            fg="#dbeafe",
            anchor="w",
            justify="left",
            font=self.compact_source_font,
        )
        self.compact_translation_label = tk.Label(
            self.frame,
            bg=config.compact_background_color,
            fg="#ffffff",
            anchor="w",
            justify="left",
            font=self.compact_translation_font,
        )
        self.frame.bind("<Configure>", self._layout_compact_labels)

        self.text_frame = tk.Frame(self.frame, bg=config.background_color)
        self.text_frame.pack(fill="both", expand=True)
        self.text_frame.grid_rowconfigure(0, weight=1)
        self.text_frame.grid_columnconfigure(0, weight=1)

        self.text = tk.Text(
            self.text_frame,
            bg=config.background_color,
            fg=config.text_color,
            insertbackground=config.text_color,
            selectbackground="#3a5f8f",
            relief="flat",
            bd=0,
            highlightthickness=0,
            font=self.text_font,
            wrap="char",
            state="disabled",
            cursor="arrow",
            spacing1=0,
            spacing2=0,
            spacing3=1,
            padx=12,
            pady=8,
        )
        self.text.grid(row=0, column=0, sticky="nsew")

        self.y_scroll = ttk.Scrollbar(
            self.text_frame,
            orient="vertical",
            command=self.text.yview,
            style="Dark.Vertical.TScrollbar",
        )
        self.y_scroll.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=self.y_scroll.set)

        self.menu = self._build_menu()
        self._apply_layout_style(resize=True)

        self.root.bind("<Escape>", self.close)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        for widget in (
            self.header,
            self.frame,
            self.text,
            self.compact_source_label,
            self.compact_translation_label,
        ):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._drag)
            widget.bind("<ButtonRelease-1>", self._end_resize)
            widget.bind("<Motion>", self._update_compact_cursor)
        self.compact_resize_handle.bind("<ButtonPress-1>", self._start_resize)
        self.compact_resize_handle.bind("<B1-Motion>", self._resize)
        self.compact_resize_handle.bind("<ButtonRelease-1>", self._end_resize)
        for widget in (self.root, self.frame, self.header, self.text, self.text_frame):
            widget.bind("<Button-3>", self._show_menu)
            widget.bind("<MouseWheel>", self._on_mousewheel)
        self.root.after(self.config.render_interval_ms, self._poll_text)

    def set_text(self, text: str) -> None:
        self._enqueue(("status" if self._is_status_text(text) else "zh", None, text))

    def set_bilingual(
        self,
        input_text: str | None = None,
        output_text: str | None = None,
    ) -> None:
        self._log_latency("ui_enqueued")
        self._enqueue(("bilingual", input_text, output_text))

    def set_paused(self, paused: bool) -> None:
        self._enqueue(("paused", None, "1" if paused else "0"))

    def request_visibility(self, visible: bool) -> None:
        self._enqueue(("visibility", None, "1" if visible else "0"))

    def request_close(self) -> None:
        self._enqueue(("close", None, None))

    def _enqueue(self, item: tuple[str, str | None, str | None]) -> None:
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                pass

    def run(self) -> None:
        self.root.mainloop()

    def close(self, _event: object | None = None) -> None:
        if self._closing:
            return
        self._closing = True
        self._remember_current_geometry(self._normalized_layout_style(self.config.layout_style))
        self._notify_settings()
        if self.on_close:
            self.on_close()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def _build_menu(self) -> Menu:
        menu = Menu(self.root, tearoff=0)

        output_menu = Menu(menu, tearoff=0)
        output_menu.add_radiobutton(
            label="Stable output",
            variable=self.display_mode_var,
            value="sentence",
            command=lambda: self._set_display_mode("sentence"),
        )
        output_menu.add_radiobutton(
            label="Streaming output",
            variable=self.display_mode_var,
            value="streaming",
            command=lambda: self._set_display_mode("streaming"),
        )
        menu.add_cascade(label="Output Mode", menu=output_menu)

        language_menu = Menu(menu, tearoff=0)
        language_menu.add_radiobutton(
            label="Bilingual",
            variable=self.subtitle_language_var,
            value="bilingual",
            command=lambda: self._set_subtitle_language("bilingual"),
        )
        language_menu.add_radiobutton(
            label="Chinese only",
            variable=self.subtitle_language_var,
            value="translation",
            command=lambda: self._set_subtitle_language("translation"),
        )
        language_menu.add_radiobutton(
            label="Original only",
            variable=self.subtitle_language_var,
            value="source",
            command=lambda: self._set_subtitle_language("source"),
        )
        menu.add_cascade(label="Subtitle Text", menu=language_menu)

        style_menu = Menu(menu, tearoff=0)
        style_menu.add_radiobutton(
            label="Classic history",
            variable=self.layout_style_var,
            value="classic",
            command=lambda: self._set_layout_style("classic"),
        )
        style_menu.add_radiobutton(
            label="Compact two-line",
            variable=self.layout_style_var,
            value="compact",
            command=lambda: self._set_layout_style("compact"),
        )
        menu.add_cascade(label="Subtitle Style", menu=style_menu)
        menu.add_separator()
        if self.on_pause_toggle is not None:
            menu.add_command(
                label="Pause / Resume (Ctrl+Alt+Space)",
                command=self.on_pause_toggle,
            )
        if self.audio_devices:
            device_menu = Menu(menu, tearoff=0)
            device_menu.add_radiobutton(
                label="Windows default",
                variable=self.audio_device_var,
                value="",
                command=lambda: self._set_audio_device(""),
            )
            for device_name in self.audio_devices:
                device_menu.add_radiobutton(
                    label=device_name,
                    variable=self.audio_device_var,
                    value=device_name,
                    command=lambda name=device_name: self._set_audio_device(name),
                )
            menu.add_cascade(label="Audio Device", menu=device_menu)
        menu.add_separator()
        menu.add_command(label="Close", command=self.close)
        return menu

    def _set_display_mode(self, mode: str) -> None:
        self.config.display_mode = self._normalized_display_mode(mode)
        self.display_mode_var.set(self.config.display_mode)
        self._force_render()
        self._notify_settings()

    def _set_subtitle_language(self, language: str) -> None:
        self.config.subtitle_language = self._normalized_subtitle_language(language)
        self.subtitle_language_var.set(self.config.subtitle_language)
        self._force_render()
        self._notify_settings()

    def _set_layout_style(self, style: str) -> None:
        next_style = self._normalized_layout_style(style)
        current_style = self._normalized_layout_style(self.config.layout_style)
        if next_style == current_style:
            return
        self._remember_current_geometry(current_style)
        if current_style == "compact":
            # Remove the old region before Tk recreates a decorated window.
            self._clear_compact_window_regions()
        self.config.layout_style = next_style
        self.layout_style_var.set(self.config.layout_style)
        self._apply_layout_style(resize=True)
        self._force_render()
        self._notify_settings()

    def _set_audio_device(self, device_name: str) -> None:
        self.audio_device_var.set(device_name)
        if self.on_audio_device_selected is not None:
            self.on_audio_device_selected(device_name)
        self._status = "Switching audio device..."
        self._force_render()
        self._notify_settings()

    def _apply_layout_style(self, resize: bool = False) -> None:
        compact = self._is_compact_style
        self._style_generation += 1
        generation = self._style_generation
        minimum_width = self.config.compact_min_window_width if compact else self.config.min_window_width
        minimum_height = self._minimum_compact_height() if compact else 120
        background = self.config.compact_background_color if compact else self.config.background_color

        # Switching a mapped Tk window between decorated and borderless modes
        # can leave its native size and region out of sync on Windows. Hide it
        # first, change the native style, then restore the saved geometry.
        self.root.withdraw()
        if not compact:
            self._clear_compact_window_regions()
        self.root.minsize(minimum_width, minimum_height)
        self.root.overrideredirect(compact)
        self.root.attributes(
            "-alpha",
            max(0.2, min(1.0, self.config.compact_opacity if compact else self.config.opacity)),
        )
        self.root.configure(bg=background)
        self.frame.configure(
            bg=background,
            padx=0 if compact else 8,
            pady=0 if compact else 6,
            bd=0,
            relief="flat",
            highlightbackground=COMPACT_BORDER_COLOR if compact else background,
            highlightthickness=1 if compact else 0,
        )
        self.header.configure(bg=background)
        self.close_button.configure(bg=background)
        self.text_frame.configure(bg=background)
        self.text.configure(bg=background)
        self.compact_close_button.configure(bg=background)
        self.compact_resize_handle.configure(bg=background)
        self.text.configure(
            padx=13 if compact else 12,
            pady=8 if compact else 8,
            spacing3=0 if compact else 1,
        )
        if compact:
            self.compact_canvas.place_forget()
            self.frame.pack_forget()
            self.frame.pack(fill="both", expand=True)
            self.header.pack_forget()
            self.y_scroll.grid_remove()
            self.text_frame.pack_forget()
            self._layout_compact_labels()
            self.compact_close_button.place(relx=1.0, x=-3, y=1, anchor="ne", width=22, height=20)
            self.compact_resize_handle.place(relx=1.0, rely=1.0, x=-2, y=-2, anchor="se")
            self.compact_close_button.lift()
            # Canvas.lift() raises a canvas item, not the widget itself.
            # Raise this child widget above the subtitle labels instead.
            self.root.tk.call("raise", self.compact_resize_handle._w)
        else:
            self.frame.pack_forget()
            self.compact_canvas.place_forget()
            self.frame.pack(fill="both", expand=True)
            self.compact_close_button.place_forget()
            self.compact_resize_handle.place_forget()
            self.compact_source_label.place_forget()
            self.compact_translation_label.place_forget()
            self.header.pack_forget()
            self.text_frame.pack_forget()
            self.header.pack(fill="x")
            self.text_frame.pack(fill="both", expand=True)
            self.y_scroll.grid()
        if resize:
            width, height, x, y = self._compact_geometry if compact else self._classic_geometry
            if compact:
                height = max(height, self._minimum_compact_height())
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.after_idle(lambda: self._apply_compact_window_region(generation))

    def _remember_current_geometry(self, style: str) -> None:
        geometry = (
            self.root.winfo_width(),
            self.root.winfo_height(),
            self.root.winfo_x(),
            self.root.winfo_y(),
        )
        if style == "compact":
            self._compact_geometry = geometry
        else:
            self._classic_geometry = geometry

    def _force_render(self) -> None:
        self._last_render_text = "\0"
        self._render_history()

    def _poll_text(self) -> None:
        try:
            changed = False
            # Yield to Tkinter after a finite amount of work so window events,
            # especially the close button, are never starved by a reconnect.
            for _ in range(50):
                try:
                    kind, ja_text, zh_text = self._queue.get_nowait()
                except queue.Empty:
                    break
                changed = self._consume_update(kind, ja_text, zh_text) or changed
            changed = self._flush_stale_stable_segment() or changed
            changed = self._flush_ready_segments() or changed
            changed = self._flush_stale_carry() or changed
            if changed:
                self._render_history()
                self._log_latency("ui_updated")
        except Exception:
            if self.logger is not None:
                self.logger.exception("Subtitle UI update failed; continuing on the next tick.")
        finally:
            try:
                self.root.after(self.config.render_interval_ms, self._poll_text)
            except tk.TclError:
                pass

    def _consume_update(
        self,
        kind: str,
        ja_text: str | None,
        zh_text: str | None,
    ) -> bool:
        if kind == "paused":
            paused = zh_text == "1"
            self._status = "Paused. Press Ctrl+Alt+Space to resume." if paused else ""
            return True
        if kind == "visibility":
            if zh_text == "1":
                self.root.deiconify()
                self.root.lift()
            else:
                self.root.withdraw()
            return False
        if kind == "close":
            self.close()
            return False
        if kind == "status":
            self._status = self._normalize(zh_text)
            self._log_subtitle("status", status=self._status)
            return True

        self._status = ""
        changed = False

        if ja_text:
            changed = self._add_ja_fragment(ja_text) or changed

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
        return changed

    def _consume_stream(self, previous: str, incoming: str) -> tuple[list[str], str]:
        text = self._merge_overlap(previous, self._normalize(incoming))
        complete, pending = self._split_complete_segments(text)
        return complete, pending

    def _add_ja_fragment(self, text: str) -> bool:
        incoming = self._normalize(text)
        if not incoming:
            return False
        now = time.monotonic()
        self._prune_ja_fragments(now)
        recent = self._recent_ja_text(now=now, include_window_only=False)
        # Complete source packets can be legitimate repeated replies (e.g.
        # "Yes. Yes."). Preserve their multiplicity for later pairing.
        if self._ends_sentence(incoming):
            self._ja_fragments.append((now, incoming))
            self._log_subtitle("buffer_ja", ja=incoming)
            return True
        merged = self._merge_overlap(recent, incoming)
        addition = ""
        if not recent:
            addition = incoming
        elif merged == recent:
            self._log_subtitle("skip_duplicate_ja_fragment", ja=incoming)
            return False
        elif merged.startswith(recent):
            addition = merged[len(recent) :].strip()
        elif incoming not in recent:
            addition = incoming
        if not addition:
            return False
        self._ja_fragments.append((now, addition))
        self._prune_ja_fragments(now)
        self._log_subtitle("buffer_ja", ja=addition)
        return True

    def _queue_zh_segment(self, text: str, start_at: float, end_at: float) -> None:
        zh = self._normalize(text)
        if not zh:
            return
        self._pending_zh_segments.append((time.monotonic(), start_at, end_at, zh))
        self._log_subtitle("queue_zh", zh=zh)
        self._log_latency("subtitle_queued")

    def _flush_stale_stable_segment(self, now: float | None = None) -> bool:
        """Commit an immutable chunk when a punctuation-free segment runs long."""
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
        self._queue_zh_segment(segment, segment_start, now)
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
        delay = max(0, self.config.pair_commit_delay_ms) / 1000
        while self._pending_zh_segments and now - self._pending_zh_segments[0][0] >= delay:
            queued_at, start_at, end_at, zh_segment = self._pending_zh_segments[0]
            # A timeout prefix and its continuation often arrive within the
            # same UI tick. Pair them together before consuming any source.
            while not self._ends_sentence(zh_segment) and len(self._pending_zh_segments) > 1:
                next_queued, next_start, next_end, next_text = self._pending_zh_segments[1]
                if next_start > end_at + self.config.ja_pair_postroll_seconds:
                    break
                merged = self._merge_overlap(zh_segment, next_text)
                if len(merged) > self.config.max_pending_chars:
                    break
                zh_segment = merged
                end_at = max(end_at, next_end)
                self._pending_zh_segments.pop(1)
                self._pending_zh_segments[0] = (queued_at, start_at, end_at, zh_segment)
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
            if self.config.require_bilingual_for_display and not ja_text and now - queued_at < max_wait:
                break
            self._pending_zh_segments.pop(0)
            self._log_subtitle("pair_candidate", ja=paired_source, zh=zh_segment)
            rendered = self._append_block(paired_source, zh_segment)
            changed = rendered or changed
            if self.config.clear_ja_after_pair:
                if self._ends_sentence(zh_segment):
                    self._consume_paired_ja(paired_source, end_at)
                    self._source_continuation = None
                elif paired_source:
                    # Without an alignment boundary it is safer to share the
                    # source context than to leave the continuation orphaned.
                    self._source_continuation = (end_at, paired_source)
        return changed

    def _flush_stale_carry(self) -> bool:
        if not (self._carry_ja or self._carry_zh) or self._carry_started_at is None:
            return False
        max_wait = max(0, self.config.max_short_carry_ms) / 1000
        if time.monotonic() - self._carry_started_at < max_wait:
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
            self.config.ja_pair_max_chars,
        )
        zh = self._merge_overlap(self._carry_zh, self._normalize(zh_text))
        if not ja and not zh:
            return False
        block = {"ja": ja, "zh": zh}
        signature = (ja, zh)

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

        if signature in self._recent_blocks:
            self._carry_ja = ""
            self._carry_zh = ""
            self._carry_started_at = None
            self._log_subtitle("skip_exact_duplicate", ja=ja, zh=zh)
            return False
        if self._history and self._blocks_similar(self._history[-1], block):
            self._carry_ja = ""
            self._carry_zh = ""
            self._carry_started_at = None
            self._log_subtitle("skip_similar_last", ja=ja, zh=zh)
            return False
        recent_window = max(1, self.config.duplicate_recent_window)
        if any(self._blocks_similar(entry, block) for entry in self._history[-recent_window:]):
            self._carry_ja = ""
            self._carry_zh = ""
            self._carry_started_at = None
            self._log_subtitle("skip_similar_recent", ja=ja, zh=zh)
            return False
        self._history.append(block)
        self._carry_ja = ""
        self._carry_zh = ""
        self._carry_started_at = None
        self._recent_blocks.append(signature)
        self._recent_blocks = self._recent_blocks[-12:]
        if not self._is_compact_style and len(self._history) > self.config.max_history_sentences:
            self._history = self._history[-self.config.max_history_sentences :]
        self._log_subtitle("render_block", ja=ja, zh=zh)
        return True

    def _render_history(self) -> None:
        if self._is_compact_style:
            self._render_compact_labels()
            return
        first, last = self.text.yview()
        at_bottom = last >= 0.98
        render_items = self._build_render_items()
        render_text = "\n".join(text for text, _ in render_items)
        if render_text == self._last_render_text:
            return
        self._last_render_text = render_text
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        # Classic uses the same visual language as Compact: pale-blue source
        # text and white translations, with the same pixel-based Segoe UI fonts.
        self.text.tag_configure("ja", foreground="#dbeafe", font=self.compact_source_font)
        self.text.tag_configure("zh", foreground="#ffffff", font=self.compact_translation_font)
        self.text.tag_configure("pending", foreground="#dbeafe", font=self.compact_source_font)
        self.text.tag_configure("status", foreground="#ffffff", font=self.compact_translation_font)

        for text, tag in render_items:
            self.text.insert("end", text + "\n", tag)

        self.text.configure(state="disabled")
        if at_bottom:
            self.text.see("end")
        else:
            self.text.yview_moveto(first)


    def _render_compact_labels(self) -> None:
        source_text, translation_text = self._compact_track_text()
        available_width = max(1, self.root.winfo_width() - 26)
        overflows = (
            self.compact_source_font.measure(source_text) > available_width
            or self.compact_translation_font.measure(translation_text) > available_width
        )
        if overflows and len(self._history) > 1:
            self._history = self._history[-1:]
            source_text, translation_text = self._compact_track_text()
        # Compact labels are one line high. Keep the newest suffix visible when
        # a continuous utterance is wider than the overlay; otherwise Tk's
        # left-aligned label keeps showing the old prefix and appears frozen.
        source_text = self._fit_compact_tail(
            source_text,
            self.compact_source_font,
            available_width,
        )
        translation_text = self._fit_compact_tail(
            translation_text,
            self.compact_translation_font,
            available_width,
        )
        self.compact_source_label.configure(text=source_text)
        self.compact_translation_label.configure(text=translation_text)
        self._place_compact_labels()

    @staticmethod
    def _fit_compact_tail(text: str, font: Any, available_width: int) -> str:
        if not text or font.measure(text) <= available_width:
            return text
        ellipsis = "…"
        if font.measure(ellipsis) >= available_width:
            return ellipsis
        low, high = 0, len(text)
        while low < high:
            size = (low + high + 1) // 2
            candidate = ellipsis + text[-size:]
            if font.measure(candidate) <= available_width:
                low = size
            else:
                high = size - 1
        return ellipsis + text[-low:] if low else ellipsis

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
        return " ".join(source_parts), " ".join(translation_parts)

    def _build_compact_render_items(self) -> list[tuple[str, str]]:
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
        items: list[tuple[str, str]] = []
        if source_parts:
            items.append((" ".join(source_parts), "ja"))
        if translation_parts:
            items.append((" ".join(translation_parts), "zh"))
        return items

    def _build_render_items(self) -> list[tuple[str, str]]:
        pending_ja = self._pending_ja_text()
        if self._status:
            return [(self._status, "status")]

        items: list[tuple[str, str]] = []
        for block in self._history:
            if self._show_source_text and block.get("ja"):
                items.append((block["ja"], "ja"))
            if self._show_translation_text and block.get("zh"):
                items.append((block["zh"], "zh"))

        if not self._show_pending:
            return items

        for block in self._pending_pair_blocks():
            if self._show_source_text and block.get("ja") and not self._is_recent_text(block["ja"], "ja"):
                items.append((block["ja"], "pending"))
            if self._show_translation_text and block.get("zh") and not self._is_recent_text(block["zh"], "zh"):
                items.append((block["zh"], "pending"))

        if self._carry_ja or self._carry_zh:
            carry_block = {"ja": self._carry_ja, "zh": self._carry_zh}
            if self._block_len(carry_block) >= self.config.min_pending_display_chars:
                if self._show_source_text and self._carry_ja and not self._is_recent_text(self._carry_ja, "ja"):
                    items.append((self._carry_ja, "pending"))
                if self._show_translation_text and self._carry_zh and not self._is_recent_text(self._carry_zh, "zh"):
                    items.append((self._carry_zh, "pending"))

        if pending_ja or self._current_zh:
            if self._show_source_text and pending_ja and not self._is_recent_text(pending_ja, "ja"):
                items.append((pending_ja, "pending"))
            if self._show_translation_text and self._current_zh and not self._is_recent_text(self._current_zh, "zh"):
                items.append((self._current_zh, "pending"))
            self._log_subtitle("render_pending", ja=pending_ja, zh=self._current_zh)
        return items

    @staticmethod
    def _normalize(text: str | None) -> str:
        return " ".join((text or "").split())

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

    def _blocks_similar(self, left: dict[str, str], right: dict[str, str]) -> bool:
        left_ja = self._compact(left.get("ja", ""))
        left_zh = self._compact(left.get("zh", ""))
        right_ja = self._compact(right.get("ja", ""))
        right_zh = self._compact(right.get("zh", ""))
        ja_same = self._text_similar(left_ja, right_ja)
        zh_same = self._text_similar(left_zh, right_zh)
        if left_ja and right_ja and left_zh and right_zh:
            return ja_same and zh_same
        return ja_same or zh_same

    def _text_similar(self, left: str, right: str) -> bool:
        if not left or not right:
            return False
        min_chars = max(1, self.config.duplicate_min_chars)
        if len(left) < min_chars or len(right) < min_chars:
            return False
        return left == right or left in right or right in left

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

    def _trim_recent_ja_prefix(self, text: str) -> str:
        incoming = self._normalize(text)
        if not incoming:
            return ""
        min_chars = max(1, self.config.duplicate_min_chars)
        for block in reversed(self._history[-max(1, self.config.duplicate_recent_window) :]):
            recent = self._normalize(block.get("ja", ""))
            if len(self._compact(recent)) < min_chars:
                continue
            if incoming.startswith(recent):
                incoming = incoming[len(recent) :].strip()
                break
            compact_incoming = self._compact(incoming)
            compact_recent = self._compact(recent)
            if compact_incoming.startswith(compact_recent):
                incoming = compact_incoming[len(compact_recent) :].strip()
                break
        return incoming

    def _pending_ja_text(self) -> str:
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
        for _, start_at, end_at, zh_text in self._pending_zh_segments:
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
            previous_end, _ = continuation
            ttl = max(1.0, self.config.stable_hard_max_wait_ms / 1000)
            if 0 <= end_at - previous_end <= ttl:
                fragments = [(ts, text) for ts, text in self._ja_fragments
                             if previous_end - ttl <= ts <= end]
            else:
                self._source_continuation = None
        if not fragments:
            window_start = end_at - max(0.1, self.config.ja_pair_window_seconds)
            fragments = [
                (timestamp, text)
                for timestamp, text in self._ja_fragments
                if window_start <= timestamp <= end_at
            ]
        text = self._join_source_fragments([fragment for _, fragment in fragments])
        return self._limit_text(text, self.config.ja_pair_max_chars)

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
        text = self._join_source_fragments([fragment for _, fragment in fragments])
        return self._limit_text(text, self.config.ja_pair_max_chars)

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
            start_at for _, start_at, _, _ in self._pending_zh_segments
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

    def _clear_paired_ja(self, now: float | None = None) -> None:
        if not self._ja_fragments:
            return
        now = time.monotonic() if now is None else now
        self._ja_fragments = [
            (timestamp, text)
            for timestamp, text in self._ja_fragments
            if timestamp > now
        ]

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

    def _limit_text(self, text: str, max_chars: int) -> str:
        normalized = self._normalize(text)
        if max_chars <= 0 or len(self._compact(normalized)) <= max_chars:
            return normalized
        trimmed = normalized[-max_chars:].strip()
        trim_chars = HARD_PUNCTUATION + SOFT_PUNCTUATION + " "
        while trimmed and trimmed[0] in trim_chars:
            trimmed = trimmed[1:].strip()
        return trimmed

    @staticmethod
    def _block_len(block: dict[str, str]) -> int:
        return max(len(SubtitleWindow._compact(block.get("ja", ""))), len(SubtitleWindow._compact(block.get("zh", ""))))

    def _log_subtitle(self, event: str, **fields: str) -> None:
        if not self.config.log_rendered_subtitles or self.logger is None:
            return
        detail = " | ".join(f"{key}={value}" for key, value in fields.items() if value)
        self.logger.info("subtitle %s%s%s", event, " | " if detail else "", detail)

    @staticmethod
    def _is_status_text(text: str | None) -> bool:
        normalized = SubtitleWindow._normalize(text)
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

    @staticmethod
    def _valid_geometry(value: object, fallback: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        if not isinstance(value, (list, tuple)) or len(value) != 4:
            return fallback
        try:
            width, height, x, y = (int(part) for part in value)
        except (TypeError, ValueError):
            return fallback
        if width < 100 or height < 40:
            return fallback
        return width, height, x, y

    def _notify_settings(self) -> None:
        if self.on_settings_changed is None:
            return
        self.on_settings_changed(
            {
                "display_mode": self.config.display_mode,
                "subtitle_language": self.config.subtitle_language,
                "layout_style": self.config.layout_style,
                "audio_device": self.audio_device_var.get(),
                "classic_geometry": list(self._classic_geometry),
                "compact_geometry": list(self._compact_geometry),
            }
        )

    @staticmethod
    def _merge_overlap(previous: str, current: str) -> str:
        if not previous:
            return current
        if current.startswith(previous):
            return current
        if previous.endswith(current):
            return previous
        max_overlap = min(len(previous), len(current), 40)
        for size in range(max_overlap, 0, -1):
            if previous[-size:] == current[:size]:
                return previous + current[size:]
        return previous + current

    def _split_complete_segments(self, text: str) -> tuple[list[str], str]:
        complete: list[str] = []
        start = 0
        for index, char in enumerate(text):
            if self._is_sentence_boundary(text, index):
                complete.append(text[start : index + 1].strip())
                start = index + 1

        pending = text[start:].strip()
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
        return bool(text) and SubtitleWindow._is_sentence_boundary(text, len(text) - 1)

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
            fragment = SubtitleWindow._normalize(fragment)
            if not fragment:
                continue
            # Latin words need separation; CJK fragments usually do not.
            if result and re.search(r"[A-Za-z0-9.!?]$", result) and re.match(r"[A-Za-z]", fragment):
                result += " "
            result += fragment
        return result

    @staticmethod
    def _looks_unfinished(text: str) -> bool:
        text = text.rstrip()
        if SubtitleWindow._is_hesitation_only(text):
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

    def _configure_dark_scrollbar_style(self) -> None:
        self.style = ttk.Style(self.root)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.style.configure(
            "Dark.Vertical.TScrollbar",
            background="#343434",
            darkcolor="#343434",
            lightcolor="#343434",
            troughcolor="#181818",
            bordercolor="#181818",
            arrowcolor="#666666",
            relief="flat",
            borderwidth=0,
            width=8,
        )
        self.style.map(
            "Dark.Vertical.TScrollbar",
            background=[("active", "#444444"), ("pressed", "#505050")],
            arrowcolor=[("active", "#888888"), ("pressed", "#999999")],
        )

    def _draw_compact_background(self, event: tk.Event) -> None:
        canvas = self.compact_canvas
        canvas.delete("compact-background")
        width = max(1, event.width)
        height = max(1, event.height)
        radius = min(COMPACT_CORNER_RADIUS, width // 2, height // 2)
        points = [
            radius, 1,
            width - radius, 1,
            width - 1, radius,
            width - 1, height - radius,
            width - radius, height - 1,
            radius, height - 1,
            1, height - radius,
            1, radius,
        ]
        canvas.create_polygon(
            points,
            fill=self.config.compact_background_color,
            outline=COMPACT_BORDER_COLOR,
            width=1,
            smooth=True,
            splinesteps=16,
            tags="compact-background",
        )

    def _start_drag(self, event: tk.Event) -> None:
        if self._is_compact_style:
            edge = self._compact_resize_edge(event)
            if any(edge):
                self._resize_edges = edge
                self._resize_start = (
                    event.x_root,
                    event.y_root,
                    self.root.winfo_width(),
                    self.root.winfo_height(),
                )
                self._drag_x = self.root.winfo_x()
                self._drag_y = self.root.winfo_y()
                return
        self._drag_x = event.x
        self._drag_y = event.y

    def _drag(self, event: tk.Event) -> None:
        if self._is_compact_style and self._resize_edges and self._resize_start:
            self._resize_from_pointer(event)
            return
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    def _start_resize(self, event: tk.Event) -> str:
        self._resize_edges = (False, True, False, True)
        self._drag_x = self.root.winfo_x()
        self._drag_y = self.root.winfo_y()
        self._resize_start = (
            event.x_root,
            event.y_root,
            self.root.winfo_width(),
            self.root.winfo_height(),
        )
        return "break"

    def _resize(self, event: tk.Event) -> str:
        self._resize_from_pointer(event)
        return "break"

    def _end_resize(self, _event: tk.Event) -> str:
        self._resize_start = None
        self._resize_edges = None
        return "break"

    def _compact_resize_edge(self, event: tk.Event) -> tuple[bool, bool, bool, bool]:
        margin = 10
        x = event.x_root - self.root.winfo_rootx()
        y = event.y_root - self.root.winfo_rooty()
        return (
            x <= margin,
            x >= self.root.winfo_width() - margin,
            y <= margin,
            y >= self.root.winfo_height() - margin,
        )

    def _update_compact_cursor(self, event: tk.Event) -> None:
        if not self._is_compact_style or self._resize_edges:
            return
        left, right, top, bottom = self._compact_resize_edge(event)
        if (left or right) and (top or bottom):
            cursor = "size_nw_se" if left == top else "size_ne_sw"
        elif left or right:
            cursor = "size_we"
        elif top or bottom:
            cursor = "size_ns"
        else:
            cursor = "arrow"
        for widget in (self.frame, self.text, self.compact_source_label, self.compact_translation_label):
            widget.configure(cursor=cursor)

    def _layout_compact_labels(self, _event: tk.Event | None = None) -> None:
        if not self._is_compact_style:
            return
        self._place_compact_labels()

    def _compact_label_dimensions(self) -> tuple[int, int, int]:
        """Return top padding and safe per-line heights from Tk's real font metrics."""
        # Leave a deliberately small amount of air above the first subtitle
        # line, without making the compact two-line layout feel taller.
        top = 4
        source_height = max(18, self.compact_source_font.metrics("linespace") + 3)
        translation_height = max(19, self.compact_translation_font.metrics("linespace") + 3)
        return top, source_height, translation_height

    def _minimum_compact_height(self) -> int:
        top, source_height, translation_height = self._compact_label_dimensions()
        return max(
            self.config.compact_height,
            top + source_height + translation_height + COMPACT_BOTTOM_PADDING,
        )

    def _place_compact_labels(self) -> None:
        top, source_height, translation_height = self._compact_label_dimensions()
        self.compact_source_label.place_configure(
            x=13, y=top, relwidth=1, width=-26, height=source_height
        )
        self.compact_translation_label.place_configure(
            x=13,
            y=top + source_height,
            relwidth=1,
            width=-26,
            height=translation_height,
        )

    def _resize_from_pointer(self, event: tk.Event) -> None:
        if self._resize_start is None or self._resize_edges is None:
            return
        start_x, start_y, start_width, start_height = self._resize_start
        left, right, top, bottom = self._resize_edges
        dx = event.x_root - start_x
        dy = event.y_root - start_y
        width, height = start_width, start_height
        window_x, window_y = self._drag_x, self._drag_y
        if right:
            width += dx
        if bottom:
            height += dy
        if left:
            width -= dx
            window_x += dx
        if top:
            height -= dy
            window_y += dy
        if width < self.config.compact_min_window_width:
            if left:
                window_x -= self.config.compact_min_window_width - width
            width = self.config.compact_min_window_width
        minimum_height = self._minimum_compact_height()
        if height < minimum_height:
            if top:
                window_y -= minimum_height - height
            height = minimum_height
        self.root.geometry(f"{width}x{height}+{window_x}+{window_y}")
        self._style_generation += 1
        generation = self._style_generation
        self.root.after_idle(lambda: self._apply_compact_window_region(generation))

    def _apply_compact_window_region(self, generation: int | None = None) -> None:
        """Apply a rounded region only to Compact's actual top-level window."""
        if generation is not None and generation != self._style_generation:
            return
        try:
            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            user32.GetAncestor.restype = ctypes.c_void_p
            user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
            user32.SetWindowRgn.restype = ctypes.c_int
            gdi32.CreateRoundRectRgn.argtypes = [
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int,
            ]
            gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
            if not self._is_compact_style:
                self._clear_compact_window_regions()
                return
            # GA_ROOT (2) resolves Tk's child hwnd to the one true window that
            # owns the borderless surface. Never alter arbitrary parent hwnds.
            widget_handle = self.root.winfo_id()
            handle = int(user32.GetAncestor(ctypes.c_void_p(widget_handle), 2) or widget_handle)
            width = self.root.winfo_width()
            height = self.root.winfo_height()
            radius = COMPACT_CORNER_RADIUS
            region = gdi32.CreateRoundRectRgn(
                0, 0, width + 1, height + 1, radius * 2, radius * 2
            )
            if user32.SetWindowRgn(ctypes.c_void_p(handle), region, True):
                self._compact_region_handle = handle
        except (AttributeError, OSError, tk.TclError):
            # Non-Windows Tk builds keep the Canvas-based rounded background.
            pass

    def _clear_compact_window_regions(self) -> None:
        """Remove the single Compact rounded region before a mode change."""
        try:
            user32 = ctypes.windll.user32
            user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_bool]
            user32.SetWindowRgn.restype = ctypes.c_int
            if self._compact_region_handle:
                user32.SetWindowRgn(ctypes.c_void_p(self._compact_region_handle), None, True)
            self._compact_region_handle = None
        except (AttributeError, OSError):
            pass

    def _show_menu(self, event: tk.Event) -> None:
        self.menu.tk_popup(event.x_root, event.y_root)

    def _on_mousewheel(self, event: tk.Event) -> str:
        self.text.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"
