"""Config loading for the rubric and the caps.

YAML is optional. If PyYAML is not installed you can hand the same structure
in as JSON, or run on the built-in defaults - the package itself has zero
required third-party dependencies.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional

from .caps import DEFAULT_CAPS
from .score import DEFAULT_RUBRIC

__all__ = ["load_config_file", "load_rubric", "load_caps"]


def load_config_file(path: str) -> Dict[str, Any]:
    """Load a .yaml/.yml/.json config file into a dict."""
    if not os.path.exists(path):
        raise FileNotFoundError("config file not found: " + path)
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    if path.lower().endswith(".json"):
        return json.loads(text)
    try:
        import yaml  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on environment
        raise ImportError(
            "PyYAML is needed to read " + path +
            " - run `pip install pyyaml`, or pass the same structure as .json"
        ) from exc
    loaded = yaml.safe_load(text)
    return loaded or {}


def _merge(base: Dict[str, Any], override: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base.items()}
    if not override:
        return out
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key].update(value)
        else:
            out[key] = value
    return out


def load_rubric(path: Optional[str] = None) -> Dict[str, Any]:
    """Defaults, overlaid with the file at ``path`` if one is given."""
    return _merge(DEFAULT_RUBRIC, load_config_file(path) if path else None)


def load_caps(path: Optional[str] = None) -> Dict[str, Any]:
    """Defaults, overlaid with the file at ``path`` if one is given."""
    return _merge(DEFAULT_CAPS, load_config_file(path) if path else None)
