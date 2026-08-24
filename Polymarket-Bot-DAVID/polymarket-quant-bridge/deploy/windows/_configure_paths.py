"""Point a freshly created config.yaml at the data folder, wherever it is.

Why this exists
---------------
`config.example.yaml` defaults to a self-contained layout, with the databases
inside the bot folder:

    db: "state/intel.sqlite3"

That is the right default for someone starting from nothing. But this project
ships its collected research in a SEPARATE sibling folder:

    <parent>/
        Polymarket-Bot-DAVID/
            polymarket-quant-bridge/     <- CWD when setup runs
        Polymarket-Bot-DATA/
            state/                       <- 2.4 GB of history lives here

If the config is left at the default, the bot starts against an empty database
and silently ignores that history. Nothing errors; it just looks like a brand
new install, which is a genuinely hard failure to spot.

So setup calls this once, immediately after creating config.yaml, and it points
the four data paths at the sibling folder IF that folder actually exists. If it
does not, the file is left exactly as the example shipped it and a fresh local
`state/` is used instead.

Only ever touches a file setup has just created from the template, and only
rewrites values that still hold the template's own defaults -- so a config a
human has edited is never modified. Idempotent.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# key -> (template default, replacement relative to the bridge folder)
PATHS = {
    "kill_switch_file": ("state/KILL", "{data}/KILL"),
    "stop_file": ("state/STOP", "{data}/STOP"),
    "db": ("state/intel.sqlite3", "{data}/intel.sqlite3"),
    "data_dir": ("state", "{data}"),
}


def find_data_dir(bridge: Path) -> Path | None:
    """Locate Polymarket-Bot-DATA/state relative to the bridge folder.

    Checked in order of how the project is actually laid out. Returns None when
    there is no such folder, which is a normal fresh install.
    """
    candidates = [
        bridge.parent.parent / "Polymarket-Bot-DATA" / "state",   # sibling of the bundle
        bridge.parent / "Polymarket-Bot-DATA" / "state",          # inside the bundle
    ]
    for c in candidates:
        if c.is_dir():
            return c
    return None


def relative_path(data: Path, bridge: Path) -> str:
    """POSIX-style relative path, since the config is read with forward slashes."""
    try:
        return Path(os.path.relpath(data, bridge)).as_posix()
    except ValueError:
        # Different drive letters on Windows -- relpath cannot express it, so
        # fall back to the absolute path, which the loader also accepts.
        return data.as_posix()


def rewrite(config: Path, rel: str) -> list[str]:
    text = config.read_text(encoding="utf-8")
    changed: list[str] = []

    for key, (default, template) in PATHS.items():
        want = template.format(data=rel)
        # Match `  key: "value"` with the template's own default only.
        pattern = re.compile(
            rf'^(?P<indent>\s*){re.escape(key)}:\s*"{re.escape(default)}"',
            re.MULTILINE,
        )

        def _sub(m: re.Match) -> str:
            return f'{m.group("indent")}{key}: "{want}"'

        text, n = pattern.subn(_sub, text)
        if n:
            changed.append(f"{key} -> {want}")

    if changed:
        config.write_text(text, encoding="utf-8")
    return changed


def main() -> int:
    bridge = Path(__file__).resolve().parents[2]
    config = bridge / "config" / "config.yaml"

    if not config.is_file():
        print("  [i] No config.yaml to configure.")
        return 0

    data = find_data_dir(bridge)
    if data is None:
        print("  [i] No Polymarket-Bot-DATA folder found - using a local state\\ folder.")
        return 0

    rel = relative_path(data, bridge)
    changed = rewrite(config, rel)
    if changed:
        print(f"  [OK] Data folder found - config.yaml now points at {rel}")
        for c in changed:
            print(f"       {c}")
    else:
        print("  [OK] Data paths already set - left untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
