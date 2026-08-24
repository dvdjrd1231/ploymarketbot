"""
Reference into the Prop Firm Quant Bridge (``qc_lean_bridge``) — the brain.

That project is the existing QuantConnect LEAN research platform: feature
engineering, automated strategy discovery, walk-forward and Monte Carlo
validation, ranking, and generated LEAN algorithms. This project does not
reimplement any of it. It supplies Polymarket data in the shape that platform
already consumes, and reads the strategies it produces back out.

The reference is a **path shim**, matching :mod:`pqb.upstream`: the bridge is
not pip-installable, so its root goes on ``sys.path`` and its ``core`` package
is imported normally. One file to change if the layout moves.

Location resolution, in order:
  1. ``PQB_QUANT_BRIDGE_PATH`` environment variable.
  2. ``../Advanced Strategy Development for QuantConnect LEAN/qc_lean_bridge``
  3. ``../qc_lean_bridge``

**Importing this module is optional and never required to run.** The bridge
brings pandas and numpy with it, and a deployment that only executes an
already-discovered strategy should not be forced to install them. So nothing
here is imported at module load: call :func:`available` to test, and
:func:`load` to import. Every consumer degrades to a clear message rather than
an ImportError traceback.

Note the bridge's package is imported under the generic top-level name
``core``. If this process ever also imports a different package called
``core``, resolve it by importing this module first.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CANDIDATES = (
    _PROJECT_ROOT.parent / "Advanced Strategy Development for QuantConnect LEAN"
    / "qc_lean_bridge",
    _PROJECT_ROOT.parent / "qc_lean_bridge",
)

# The file whose presence proves a directory really is the bridge, rather than
# an empty folder with the right name.
_MARKER = Path("core") / "pipeline.py"


class QuantBridgeNotFound(RuntimeError):
    """The qc_lean_bridge checkout could not be located or imported."""


def bridge_root() -> Path:
    override = (os.environ.get("PQB_QUANT_BRIDGE_PATH") or "").strip()
    candidates = ([Path(override).expanduser()] if override
                  else list(_CANDIDATES))
    for candidate in candidates:
        if (candidate / _MARKER).exists():
            return candidate.resolve()
    searched = "\n  ".join(str(c) for c in candidates)
    raise QuantBridgeNotFound(
        "Could not find the Prop Firm Quant Bridge (qc_lean_bridge). Looked "
        f"in:\n  {searched}\nSet PQB_QUANT_BRIDGE_PATH to its root.")


def ensure_on_path() -> Path:
    root = bridge_root()
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return root


@dataclass
class QuantBridge:
    """The imported surface of the Quant Bridge, in one object."""

    root: Path
    Config: Any
    ResearchEngine: Any
    Strategy: Any
    PropConstraints: Any

    def config(self, overrides: Optional[dict[str, Any]] = None):
        """The bridge's own default config, with dotted-key overrides applied.

        Its defaults are the researched ones — search method, validation gates,
        ranking weights — and reusing them is the point of the integration.
        Only the paths and the instrument description need to differ for a
        prediction market, and those are passed in.
        """
        cfg = self.Config.load()
        for key, value in (overrides or {}).items():
            cfg.set(key, value)
        return cfg


_cached: Optional[QuantBridge] = None


def load() -> QuantBridge:
    """Import the bridge. Raises :class:`QuantBridgeNotFound` if unavailable."""
    global _cached
    if _cached is not None:
        return _cached
    root = ensure_on_path()
    try:
        from core.backtester import Strategy            # noqa: PLC0415
        from core.config import Config                  # noqa: PLC0415
        from core.pipeline import ResearchEngine        # noqa: PLC0415
        from core.risk_management import PropConstraints  # noqa: PLC0415
    except ImportError as exc:
        raise QuantBridgeNotFound(
            f"Found the Quant Bridge at '{root}' but could not import it: "
            f"{exc}. It needs pandas, numpy and PyYAML — "
            f"`pip install -r {root / 'requirements.txt'}`.") from exc
    _cached = QuantBridge(root=root, Config=Config,
                          ResearchEngine=ResearchEngine, Strategy=Strategy,
                          PropConstraints=PropConstraints)
    return _cached


def available() -> tuple[bool, str]:
    """``(ok, reason)`` — never raises, so callers can report and carry on."""
    try:
        load()
        return True, ""
    except QuantBridgeNotFound as exc:
        return False, str(exc)
    except Exception as exc:                            # pragma: no cover
        return False, f"{type(exc).__name__}: {exc}"
