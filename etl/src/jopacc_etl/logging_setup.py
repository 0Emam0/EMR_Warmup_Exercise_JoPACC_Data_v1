"""Configure console and file logging for a run."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path


def setup(cfg: dict) -> Path:
    log_dir = Path(cfg["paths"]["log_dir"])
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"etl_{stamp}.log"

    fmt = logging.Formatter(cfg["logging"]["format"])
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    console = logging.StreamHandler()
    console.setLevel(getattr(logging, cfg["logging"]["level"]))
    console.setFormatter(fmt)
    root.addHandler(console)

    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(getattr(logging, cfg["logging"]["file_level"]))
    handler.setFormatter(fmt)
    root.addHandler(handler)
    return log_file
