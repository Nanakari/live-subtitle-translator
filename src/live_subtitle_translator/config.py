"""Load the shared project configuration and optional local overrides."""

from __future__ import annotations

import os
import warnings
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


_DEFAULTS = yaml.safe_load(Path(__file__).with_name("defaults.yaml").read_text(encoding="utf-8"))
_REMOVED_OPTIONS = {"gemini": ("source_language",), "subtitle": ("max_chars", "dedup")}


def default_config() -> dict[str, Any]:
    """Return an independent copy of the single bundled defaults document."""
    return deepcopy(_DEFAULTS)


def apply_network_config(config: dict) -> None:
    network = config.get("network", {})
    proxy_url = (network.get("proxy_url") or "").strip()
    no_proxy = (network.get("no_proxy") or "").strip()
    if proxy_url:
        # WebSocket-specific variables take precedence over HTTPS_PROXY, and
        # lowercase variables take precedence on some platforms. Keep the
        # configured route authoritative while retaining NO_PROXY handling.
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "WS_PROXY", "WSS_PROXY"):
            os.environ[name] = proxy_url
            os.environ[name.lower()] = proxy_url
    for name in ("NO_PROXY", "no_proxy"):
        os.environ[name] = no_proxy


def resolve_config(override: dict[str, Any]) -> dict[str, Any]:
    clean = deepcopy(override)
    for section, keys in _REMOVED_OPTIONS.items():
        values = clean.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f"Configuration section {section!r} must be a mapping")
        for key in keys:
            if key in values:
                warnings.warn(f"Ignoring retired option {section}.{key}; remove it from your override file.", UserWarning, stacklevel=2)
                del values[key]
    for section in _DEFAULTS:
        if section in clean and not isinstance(clean[section], dict):
            raise ValueError(f"Configuration section {section!r} must be a mapping")
    return _merge(_DEFAULTS, clean)


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
    """Merge bundled defaults, a legacy config.yaml, then private local overrides."""
    override = {}
    for filename in ("config.yaml", "config.local.yaml"):
        path = project_root / filename
        if not path.exists():
            continue
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(value, dict):
            raise ValueError(f"{filename} must contain a YAML mapping")
        override = _merge(override, value)
    return resolve_config(override)
