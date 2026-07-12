"""Load the shared project configuration and optional local overrides."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge an optional local configuration into shared defaults."""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_project_config(project_root: Path) -> dict[str, Any]:
    """Read config.yaml plus an untracked config.local.yaml when present."""
    config_path = project_root / "config.yaml"
    local_config_path = project_root / "config.local.yaml"
    base = yaml.safe_load(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    local = yaml.safe_load(local_config_path.read_text(encoding="utf-8")) if local_config_path.exists() else {}
    return _merge(base or {}, local or {})
