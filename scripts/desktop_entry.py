"""Frozen desktop entry point with an offline packaging smoke test."""
from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path


def self_test(destination: Path) -> int:
    try:
        import numpy as np
        import soundcard
        import pystray
        import tkinter as tk
        from google import genai
        from live_subtitle_translator import app
        from live_subtitle_translator.gemini_live_translate import GeminiLiveTranslator

        assert hasattr(soundcard, "get_microphone")
        assert hasattr(pystray, "Icon")
        config = app.load_config()
        assert config["gemini"]["api_key"] == ""
        translator = GeminiLiveTranslator(api_key="offline-self-test")
        assert len(translator.audio_to_pcm16_bytes(np.zeros(160, dtype=np.float32))) == 320
        translator._build_config()
        translator._build_http_options()
        from live_subtitle_translator.paths import resolve_app_path
        from live_subtitle_translator.subtitle_config import SubtitleWindowConfig
        from live_subtitle_translator.subtitle_window import SubtitleWindow
        from unittest.mock import patch

        root = tk.Tk()
        root.withdraw()
        try:
            with patch.object(tk, "Tk", return_value=root), patch.object(root, "deiconify"):
                window = SubtitleWindow(SubtitleWindowConfig.from_config(config))
                window.set_bilingual("Hello world.", "你好世界。", turn_complete=True, session_id=1)
                window._poll_text()
                assert window._history[-1] == {"ja": "Hello world.", "zh": "你好世界。"}
                window._set_layout_style("classic")
                window._set_layout_style("compact")
                root.update_idletasks()
        finally:
            root.destroy()
        assert resolve_app_path(config["app"]["log_file"]) == app.PROJECT_ROOT / "logs" / "app.log"
        result = {"ok": True, "frozen": bool(getattr(sys, "frozen", False)),
                  "config_root": str(app.PROJECT_ROOT)}
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test(Path(sys.argv[2])))
    from live_subtitle_translator import app
    app.main()
