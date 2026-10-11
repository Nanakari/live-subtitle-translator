"""Automated tests for Gemini Live Translator."""
"""Allow unittest discovery directly from an uninstalled source checkout."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
