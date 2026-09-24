"""Local paths and environment configuration."""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    override = os.getenv("FOODFINDER_DATA_DIR")
    if override:
        path = Path(override).expanduser()
    elif os.getenv("LOCALAPPDATA"):
        path = Path(os.environ["LOCALAPPDATA"]) / "UofTFreeFoodFinder"
    else:
        path = Path.home() / ".local" / "share" / "UofTFreeFoodFinder"
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Sandboxed/portable launches can lack a writable Windows profile directory.
        path = Path.cwd() / ".foodfinder-data"
        path.mkdir(parents=True, exist_ok=True)
    return path


def database_path() -> Path:
    return Path(os.getenv("FOODFINDER_DB", data_dir() / "foodfinder.sqlite3"))


HOST = os.getenv("FOODFINDER_HOST", "127.0.0.1")
PORT = int(os.getenv("FOODFINDER_PORT", "8000"))
SCAN_INTERVAL_HOURS = int(os.getenv("FOODFINDER_SCAN_INTERVAL_HOURS", "6"))
DISCOVERY_INTERVAL_DAYS = int(os.getenv("FOODFINDER_DISCOVERY_INTERVAL_DAYS", "7"))
POST_LIMIT = int(os.getenv("FOODFINDER_POST_LIMIT", "100"))
POST_AGE_DAYS = int(os.getenv("FOODFINDER_POST_AGE_DAYS", "60"))
