"""Stable application paths for a checkout, an installed package, or a ZIP."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def application_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "pyproject.toml").is_file() and (checkout / "app.py").is_file():
        return checkout
    user_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local" / "share"))
    return user_data / "LiveSubtitleTranslator"


def resolve_app_path(value: str | Path, root: Path | None = None) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else (root or application_root()) / path).resolve()
