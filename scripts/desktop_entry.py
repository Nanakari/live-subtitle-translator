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
        import soundfile
        import pystray
        import tkinter as tk
        from google import genai
        from scipy.signal import resample_poly
        import app
        from src.gemini_live_translate import GeminiLiveTranslator

        assert resample_poly(np.zeros(480), 1, 3).shape == (160,)
        assert soundfile.available_formats()
        assert hasattr(soundcard, "get_microphone")
        assert hasattr(pystray, "Icon")
        config = app.load_config()
        assert config["gemini"]["api_key"] == ""
        translator = GeminiLiveTranslator(api_key="offline-self-test")
        translator._build_config()
        translator._build_http_options()
        root = tk.Tk()
        root.withdraw()
        root.update_idletasks()
        root.destroy()
        result = {"ok": True, "frozen": bool(getattr(sys, "frozen", False)),
                  "config_root": str(app.PROJECT_ROOT)}
    except Exception:
        result = {"ok": False, "error": traceback.format_exc()}
    destination.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test(Path(sys.argv[2])))
    import app
    app.main()
