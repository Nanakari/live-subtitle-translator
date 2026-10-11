"""Compatibility launcher: python app.py still works from a source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from live_subtitle_translator import app as application

if __name__ == "__main__":
    application.main()
else:
    # Existing in-process callers keep patching the real application module.
    sys.modules[__name__] = application
