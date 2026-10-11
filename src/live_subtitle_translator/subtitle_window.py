"""Tk desktop overlay; text state is maintained by SubtitleState."""
from __future__ import annotations

import ctypes
import queue
import tkinter as tk
import tkinter.font as tkfont
from logging import Logger
from tkinter import Menu, ttk
from typing import Any, Callable

from .subtitle_config import SubtitleWindowConfig
from .subtitle_state import SubtitleState

COMPACT_CORNER_RADIUS = 14
COMPACT_BORDER_COLOR = "#3b4654"
COMPACT_BOTTOM_PADDING = 9


class SubtitleWindow(SubtitleState):
    def _consume_update(self, kind, ja_text, zh_text, turn_complete=False, session_id=None):
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
        return super()._consume_update(kind, ja_text, zh_text, turn_complete, session_id)

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
        super().__init__(config, logger)
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
        self._queue: queue.Queue[tuple] = queue.Queue(maxsize=200)
        self._session_id: int | None = None
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
            # sizes preserve the compact overlay's 16px / 17px visual scale.
            size=-max(1, config.compact_font_size - 1),
            weight="normal",
        )
        self.compact_translation_font = tkfont.Font(
            family="Segoe UI",
            size=-config.compact_font_size,
            weight="normal",
        )

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
        self._enqueue(("status" if self._is_status_text(text) else "zh", None, text, False))

    def set_bilingual(
        self,
        input_text: str | None = None,
        output_text: str | None = None,
        turn_complete: bool = False,
        session_id: int | None = None,
    ) -> None:
        self._log_latency("ui_enqueued")
        self._enqueue(("bilingual", input_text, output_text, bool(turn_complete), session_id))

    def set_paused(self, paused: bool) -> None:
        self._enqueue(("paused", None, "1" if paused else "0", False))

    def request_visibility(self, visible: bool) -> None:
        self._enqueue(("visibility", None, "1" if visible else "0", False))

    def request_close(self) -> None:
        self._enqueue(("close", None, None, False))

    def reset_pairing(self, session_id: int | None = None) -> None:
        """Discard all subtitle state belonging to a retired logical session."""
        self._enqueue(("reset_pairing", None, None, False, session_id))

    def _enqueue(self, item: tuple) -> None:
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
        try:
            self._remember_current_geometry(self._normalized_layout_style(self.config.layout_style))
            self._notify_settings()
        except Exception:
            if self.logger is not None:
                self.logger.warning("Could not save subtitle settings during exit", exc_info=True)
        finally:
            try:
                if self.on_close:
                    self.on_close()
            finally:
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
        previous_language = self.config.subtitle_language
        self.config.subtitle_language = self._normalized_subtitle_language(language)
        if previous_language != self.config.subtitle_language and "source" in (
            previous_language, self.config.subtitle_language
        ):
            # Original-only consumes source independently; pending translated
            # pairs cannot be carried into or out of that mode.
            history = self._history
            self._reset_session_state()
            self._history = history
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
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                session_id = item[4] if len(item) >= 5 else None
                if len(item) == 3:
                    # Keep tests and old in-process callers tolerant of the
                    # pre-turn-boundary queue shape.
                    kind, ja_text, zh_text = item  # type: ignore[misc]
                    turn_complete = False
                else:
                    kind, ja_text, zh_text, turn_complete = item[:4]
                changed = self._consume_update(
                    kind,
                    ja_text,
                    zh_text,
                    turn_complete=turn_complete,
                    session_id=session_id,
                ) or changed
                if turn_complete and (session_id is None or session_id == self._session_id):
                    changed = self._finish_turn_boundary() or changed
            changed = self._flush_stale_stable_segment() or changed
            changed = self._flush_ready_segments() or changed
            if getattr(self, "_turn_complete_pending", False) and not self._pending_zh_segments:
                changed = self._flush_stale_carry(force=True) or changed
                self._reset_pairing_state()
                self._turn_complete_pending = False
            else:
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
        self.text.tag_configure("ja", foreground=self.config.accent_color, font=self.text_font)
        self.text.tag_configure("zh", foreground=self.config.text_color, font=self.text_font)
        self.text.tag_configure("pending", foreground=self.config.accent_color, font=self.text_font)
        self.text.tag_configure("status", foreground=self.config.text_color, font=self.text_font)

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
