"""Configuration loading / saving.

The whole app is config-driven so that a new research dataset can be processed
with zero code changes - you point `dataset.data_dir` at the new exports and
re-run. This module resolves relative paths against the project root and gives
convenient dotted access.
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import yaml


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


# When frozen by PyInstaller, read-only bundled resources live in _MEIPASS while
# user-writable files (config copy, outputs, sample data) belong next to the exe.
if _is_frozen():
    RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    PROJECT_ROOT = Path(sys.executable).resolve().parent
else:
    RESOURCE_ROOT = Path(__file__).resolve().parent.parent
    PROJECT_ROOT = RESOURCE_ROOT

DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default_config.yaml"
BUNDLED_CONFIG_PATH = RESOURCE_ROOT / "config" / "default_config.yaml"


class Config:
    """Thin wrapper over the parsed YAML dict with dotted-key access."""

    def __init__(self, data: dict[str, Any], source_path: Path | None = None):
        self._data = data
        self.source_path = source_path

    # ------------------------------------------------------------------ load
    @classmethod
    def load(cls, path: str | os.PathLike | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        # In a frozen build the user-writable config may not exist yet; seed it
        # from the bundled default so it can be edited next to the executable.
        if not p.exists() and BUNDLED_CONFIG_PATH.exists() and p == DEFAULT_CONFIG_PATH:
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(BUNDLED_CONFIG_PATH, p)
        with open(p, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls(data, source_path=p)

    def save(self, path: str | os.PathLike | None = None) -> Path:
        p = Path(path) if path else (self.source_path or DEFAULT_CONFIG_PATH)
        with open(p, "w", encoding="utf-8") as fh:
            yaml.safe_dump(self._data, fh, sort_keys=False, default_flow_style=False)
        self.source_path = p
        return p

    # --------------------------------------------------------------- access
    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in dotted.split("."):
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                return default
        return node

    def set(self, dotted: str, value: Any) -> None:
        keys = dotted.split(".")
        node = self._data
        for key in keys[:-1]:
            node = node.setdefault(key, {})
        node[keys[-1]] = value

    def section(self, name: str) -> dict[str, Any]:
        return copy.deepcopy(self._data.get(name, {}))

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    # ------------------------------------------------------------ path help
    def resolve_path(self, dotted: str, default: str = "") -> Path:
        raw = self.get(dotted, default)
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p)
