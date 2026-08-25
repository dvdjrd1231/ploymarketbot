"""Locating the validated V2 package.

V3 is a **rebuild of the V2 installation**, not a second product beside it.
`pqv2/` and `pqv3/` are siblings inside one project, share one `var/`, one
config surface and one dashboard, and are installed and shipped together.

V3 reuses V2's causal substrate (`pqv2.substrate`), its gate-ownership model
and its validation ladder rather than reimplementing them. Those are ~11.8k
lines that already pass 158 tests and already enforce the no-look-ahead rule
with a heap rather than a promise. Reimplementing them would double the surface
area that can drift and halve the number of eyes on the causal code.

Because both packages sit in the same directory, importing V2 normally needs no
path manipulation at all. The fallback below exists only for the case where
`pqv3` has been copied somewhere else and `PQV3_V2_ROOT` points back at the
original installation.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent      # the project root

# Same directory by default: pqv2/ and pqv3/ are siblings.
V2_ROOT = Path(os.environ.get("PQV3_V2_ROOT", str(_HERE)))

_ADDED = False


def ensure_v2_importable() -> bool:
    """Make `pqv2` importable. Returns whether it now is.

    Never raises. V3's engine must start even if V2 is absent — the subsystems
    that need it degrade to DATA_UNAVAILABLE rather than killing the process,
    because a dashboard that will not open is worse at telling you what is
    broken than one that opens and says so.
    """
    global _ADDED
    if not _ADDED and V2_ROOT.is_dir() and str(V2_ROOT) not in sys.path:
        sys.path.insert(0, str(V2_ROOT))
        _ADDED = True
    try:
        import pqv2  # noqa: F401
        return True
    except Exception:                                        # noqa: BLE001
        return False


def v2_status() -> dict:
    ok = ensure_v2_importable()
    detail = ""
    if not ok:
        detail = ("not found at " + str(V2_ROOT)) if not V2_ROOT.is_dir() \
            else "present but not importable"
    return {"available": ok, "root": str(V2_ROOT), "detail": detail,
            "relationship": "pqv2 and pqv3 are siblings in one installation"}
