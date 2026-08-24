"""The formal copy strategy, and the transformation space searched over it.

§4 is the load-bearing idea: the wallet's raw trades are *outputs*, not the
strategy. So a candidate here is never "follow wallet X". It is

    follow wallet X, but only when <conditions>, sized <this way>, entered
    <at this delay>

and the search runs over those conditions rather than over the wallet.

A CopyStrategy is a pure, hashable, executable specification (§36). It carries
no results and no state; `backtest.py` evaluates it, `registry.py` stores it,
and `spec_hash()` is what makes "have we already tried this?" answerable (§13).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, replace
from itertools import product
from typing import Iterator

from .state import Observation


@dataclass(frozen=True)
class CopyStrategy:
    """One executable copy rule (§16).

    Every field is a condition the *copier* applies. None means "no constraint",
    which keeps the naive-copy baseline representable as a strategy with every
    field None — so baselines and candidates run through identical code (§46).
    """

    wallet: str

    # --- entry filters -------------------------------------------------------
    min_price: float | None = None
    max_price: float | None = None
    min_notional: float | None = None
    max_notional: float | None = None
    min_rel_notional: float | None = None       # conviction: big vs their norm

    # --- wallet-state filters ------------------------------------------------
    min_settled_n: int | None = None            # ignore until track record exists
    min_win_rate: float | None = None
    min_roll_win_rate: float | None = None
    min_roll_roi: float | None = None
    min_edge_t: float | None = None
    max_consec_losses: int | None = None        # stop following a cold wallet

    # --- timing --------------------------------------------------------------
    delay_secs: int = 0                         # §30
    max_secs_to_settle: int | None = None
    min_secs_to_settle: int | None = None
    skip_repeat_token: bool = False             # first entry only, not the adds

    # --- sizing --------------------------------------------------------------
    stake_mode: str = "flat"                    # flat | proportional
    stake_flat: float = 100.0
    stake_fraction: float = 0.05                # of the wallet's own notional

    label: str = ""

    # ---------------------------------------------------------------- identity
    def spec(self) -> dict:
        d = asdict(self)
        d.pop("label", None)
        return d

    def spec_hash(self) -> str:
        blob = json.dumps(self.spec(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def params_only_hash(self) -> str:
        """Hash of the rule without the wallet — identifies a *transformation*.

        Two wallets carrying the same params_only_hash are running the same
        idea, which is what cross-wallet generalisation tests (§9).
        """
        d = self.spec()
        d.pop("wallet", None)
        blob = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def for_wallet(self, wallet: str) -> "CopyStrategy":
        return replace(self, wallet=wallet)

    # ---------------------------------------------------------------- decision
    def admits(self, o: Observation) -> bool:
        """Does this strategy copy this observation? Causal fields only."""
        if self.min_price is not None and o.price < self.min_price:
            return False
        if self.max_price is not None and o.price > self.max_price:
            return False
        if self.min_notional is not None and o.notional < self.min_notional:
            return False
        if self.max_notional is not None and o.notional > self.max_notional:
            return False
        if self.min_rel_notional is not None and o.rel_notional < self.min_rel_notional:
            return False
        if self.min_settled_n is not None and o.w_settled_n < self.min_settled_n:
            return False
        if self.min_win_rate is not None and o.w_win_rate < self.min_win_rate:
            return False
        if self.min_roll_win_rate is not None and o.w_roll_win_rate < self.min_roll_win_rate:
            return False
        if self.min_roll_roi is not None and o.w_roll_roi < self.min_roll_roi:
            return False
        if self.min_edge_t is not None and o.w_edge_t < self.min_edge_t:
            return False
        if self.max_consec_losses is not None and o.w_consec_losses > self.max_consec_losses:
            return False
        if self.skip_repeat_token and o.w_token_repeat:
            return False
        if self.max_secs_to_settle is not None:
            if o.secs_to_settle < 0 or o.secs_to_settle > self.max_secs_to_settle:
                return False
        if self.min_secs_to_settle is not None:
            if o.secs_to_settle < 0 or o.secs_to_settle < self.min_secs_to_settle:
                return False
        return True

    def stake_for(self, o: Observation) -> float:
        if self.stake_mode == "proportional":
            return max(1.0, o.notional * self.stake_fraction)
        return self.stake_flat


def naive_copy(wallet: str) -> CopyStrategy:
    """The §46 baseline: copy everything, flat stake, no delay."""
    return CopyStrategy(wallet=wallet, label="naive_copy")


# ---------------------------------------------------------------------------
# The transformation space (§6).
#
# Deliberately small and *interpretable*. A 10-million-point grid over this
# dataset would be a false-discovery generator, not research — see stats.py and
# the multiple-testing budget it enforces. Each axis below is a distinct
# economic hypothesis about why a wallet's edge might be conditional, not a
# free parameter to be tuned.
# ---------------------------------------------------------------------------

AXES: dict[str, list] = {
    # "the wallet is only sharp in a particular probability band"
    "price_band": [None, (0.02, 0.30), (0.30, 0.70), (0.70, 0.98), (0.02, 0.50), (0.50, 0.98)],
    # "size expresses conviction"
    "min_rel_notional": [None, 1.5, 3.0],
    # "ignore the wallet until it has proven itself, point-in-time"
    "min_settled_n": [None, 20, 50],
    # "follow only while it is running warm"
    "min_roll_win_rate": [None, 0.55],
    "max_consec_losses": [None, 3],
    # "the edge decays with how late you are" (§30)
    "delay_secs": [0, 60, 300, 1800],
    # "only the opening position carries information, not the adds"
    "skip_repeat_token": [False, True],
}


def transformation_grid() -> Iterator[dict]:
    """Every combination of the axes above, as kwargs for CopyStrategy."""
    keys = list(AXES)
    for combo in product(*(AXES[k] for k in keys)):
        kw = dict(zip(keys, combo))
        band = kw.pop("price_band")
        if band is not None:
            kw["min_price"], kw["max_price"] = band
        yield kw


def grid_size() -> int:
    n = 1
    for v in AXES.values():
        n *= len(v)
    return n


def candidates_for(wallet: str) -> Iterator[CopyStrategy]:
    """All candidate strategies for one wallet, baseline first."""
    yield naive_copy(wallet)
    for i, kw in enumerate(transformation_grid()):
        yield CopyStrategy(wallet=wallet, label=f"t{i:05d}", **kw)
