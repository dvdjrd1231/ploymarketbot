"""Application data locations."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "PolymarketBotWeb"


def app_data_dir() -> Path:
    """Per-user application data directory (created if missing).

    Kept separate from the desktop app's ``PolymarketCopyBot`` folder so the two
    can coexist. The OS keyring entry is deliberately *shared* (see
    ``settings_store``) so a key already stored by the desktop app is picked up
    here without re-entering it.
    """
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = Path(base) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path
