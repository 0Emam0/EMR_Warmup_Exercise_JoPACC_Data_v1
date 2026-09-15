"""Load and validate the pipeline configuration."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import yaml

REQUIRED = ["paths", "scope", "classification", "text_layer", "ocr",
            "layout", "validation", "structure", "load", "logging", "runtime"]


def load_config(path: str | Path) -> dict:
    path = Path(path)
    cfg = yaml.safe_load(path.read_text())
    missing = [key for key in REQUIRED if key not in cfg]
    if missing:
        raise ValueError(f"config {path} is missing section(s): {', '.join(missing)}")

    for key in ("window_start", "window_end"):
        value = cfg["scope"][key]
        if isinstance(value, str):
            cfg["scope"][key] = dt.date.fromisoformat(value)

    root = path.resolve().parent.parent
    for key, value in cfg["paths"].items():
        resolved = Path(value)
        cfg["paths"][key] = resolved if resolved.is_absolute() else root / resolved
    return cfg
