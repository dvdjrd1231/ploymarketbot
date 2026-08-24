"""
Reference into the sibling ``ploymarketbot`` project.

That project already contains proven, working Polymarket plumbing: a streaming
order-book service, the Gamma metadata client, the Data API client and an async
facade over ``py-clob-client``. Rewriting any of it here would be duplicated
risk for no gain, so this project **references** it rather than copying it.

The reference is a path shim, not a package dependency: ``ploymarketbot`` is not
pip-installable, so its repository root is put on ``sys.path`` and its
``backend`` package is imported normally. Everything the rest of this project
needs from upstream is re-exported here, which means there is exactly one file
to change if the upstream layout ever moves.

Location resolution, in order:
  1. ``PQB_POLYMARKETBOT_PATH`` environment variable.
  2. ``../ploymarketbot`` relative to this project (the normal checkout layout).

Note the upstream package is imported under the generic top-level name
``backend``. If this process ever also imports a *different* package called
``backend``, resolve it by importing this module first.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_UPSTREAM = _PROJECT_ROOT.parent / "ploymarketbot"


class UpstreamNotFound(RuntimeError):
    """The ploymarketbot checkout could not be located."""


def upstream_root() -> Path:
    override = (os.environ.get("PQB_POLYMARKETBOT_PATH") or "").strip()
    root = Path(override).expanduser().resolve() if override else _DEFAULT_UPSTREAM
    if not (root / "backend" / "services" / "trading.py").exists():
        raise UpstreamNotFound(
            f"Could not find the ploymarketbot project at '{root}'. Clone it "
            "next to this project, or set PQB_POLYMARKETBOT_PATH to its root."
        )
    return root


def ensure_on_path() -> Path:
    root = upstream_root()
    entry = str(root)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return root


ensure_on_path()

# --- re-exports: the entire upstream surface this project uses ---------------

from backend.models import (  # noqa: E402
    AccountSnapshot as UpstreamAccountSnapshot,
    Side,
    TargetTrade,
)
from backend.services.data_api import DataApiClient  # noqa: E402
from backend.services.gamma import (  # noqa: E402
    GammaClient,
    MarketInfo,
    iso_to_ts,
)
from backend.services.price_service import Book, PriceService  # noqa: E402
from backend.services.trading import (  # noqa: E402
    OrderResult,
    PolymarketError,
    TradingClient,
    clob_available,
    derive_signer_address,
)

__all__ = [
    "Book",
    "DataApiClient",
    "GammaClient",
    "MarketInfo",
    "OrderResult",
    "PolymarketError",
    "PriceService",
    "Side",
    "TargetTrade",
    "TradingClient",
    "UpstreamAccountSnapshot",
    "UpstreamNotFound",
    "clob_available",
    "derive_signer_address",
    "ensure_on_path",
    "iso_to_ts",
    "upstream_root",
]
