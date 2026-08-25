"""The executable copy strategy, and the transformation space searched over it.

The load-bearing idea: a wallet's raw trades are OUTPUTS, not the strategy. So
a candidate here is never "follow wallet X". It is

    follow behaviour that looks like X, but only when <conditions>, sized
    <this way>, entered <at this delay>, exited <this way>

and the search runs over those conditions rather than over the wallet. That is
what makes a result TRANSFERABLE: `params_only_hash()` strips the wallet, so
"does this same idea work on four unrelated wallets?" is answerable.

A CopyStrategy is pure, frozen and hashable. It carries no results and no
state: `backtest.py` evaluates it, `registry.py` stores it, and `spec_hash()`
makes "have we already tried this?" answerable, which is what stops a search
from re-testing the same hypothesis and quietly inflating the denominator.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, asdict, replace
from itertools import product
from typing import Iterator

from ..substrate.state import Observation


# --- exit models -----------------------------------------------------------
# The brief is explicit that no generic exit may be imposed. Each of these is a
# separate hypothesis about how a position should end, and `research/exits.py`
# measures them against each other per strategy family rather than assuming.

EXIT_SETTLEMENT = "settlement"          # hold to resolution
EXIT_TARGET = "profit_target"           # exit at +X on the tape
EXIT_TRAIL = "trailing"                 # give back X from the peak
EXIT_TIME = "time_stop"                 # exit after N seconds regardless
EXIT_STOP = "stop_loss"                 # cut at -X
EXIT_PARTIAL = "partial_then_hold"      # bank half at target, ride the rest

EXIT_MODELS = (EXIT_SETTLEMENT, EXIT_TARGET, EXIT_TRAIL, EXIT_TIME,
               EXIT_STOP, EXIT_PARTIAL)


@dataclass(frozen=True)
class ExitRule:
    """How a position ends. `settlement` is the default because on this venue
    it is the only exit whose payoff is exactly known rather than modelled.
    """

    model: str = EXIT_SETTLEMENT
    target_return: float = 0.30      # for profit_target / partial
    trail_return: float = 0.20       # for trailing
    stop_return: float = -0.35       # for stop_loss; the loss-control lever
    max_hold_secs: int = 0           # 0 = to settlement
    partial_fraction: float = 0.50

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CopyStrategy:
    """One executable copy rule.

    Every field is a condition the COPIER applies. None means "no constraint",
    which keeps the naive-copy baseline representable as a strategy with every
    field None -- so baselines and candidates run through identical code and a
    candidate can never beat a baseline that was measured differently.
    """

    wallet: str

    # --- entry filters ------------------------------------------------------
    min_price: float | None = None
    max_price: float | None = None
    min_notional: float | None = None
    max_notional: float | None = None
    min_rel_notional: float | None = None      # conviction: big vs their norm

    # --- wallet-state filters (point-in-time, settled evidence only) --------
    min_settled_n: int | None = None
    min_win_rate: float | None = None
    min_roll_win_rate: float | None = None
    min_roll_roi: float | None = None
    min_edge_t: float | None = None
    max_consec_losses: int | None = None       # stop following a cold wallet

    # --- market-context filters --------------------------------------------
    min_market_prints: int | None = None       # liquidity proxy where no book
    max_market_move: float | None = None       # do not chase
    min_market_move: float | None = None

    # --- timing -------------------------------------------------------------
    delay_secs: int = 60
    max_secs_to_settle: int | None = None
    min_secs_to_settle: int | None = None
    skip_repeat_token: bool = False            # first entry only, not the adds

    # --- sizing -------------------------------------------------------------
    stake_mode: str = "flat"                   # flat | proportional | fractional
    stake_flat: float = 100.0
    stake_fraction: float = 0.05

    # --- exit ---------------------------------------------------------------
    exit: ExitRule = ExitRule()

    label: str = ""
    family: str = ""

    # ------------------------------------------------------------- identity
    def spec(self) -> dict:
        d = asdict(self)
        d.pop("label", None)
        d.pop("family", None)
        return d

    def spec_hash(self) -> str:
        blob = json.dumps(self.spec(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    def params_only_hash(self) -> str:
        """Hash of the rule WITHOUT the wallet -- identifies a transformation.

        Two wallets carrying the same params_only_hash are running the same
        idea. This is the key the cross-wallet generalisation test groups on,
        and it is the difference between "one wallet got lucky" and "this
        behaviour recurs independently".
        """
        d = self.spec()
        d.pop("wallet", None)
        blob = json.dumps(d, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def strategy_id(self) -> str:
        tag = (self.family or "WALLET").upper()
        return f"{tag}_{self.params_only_hash()}"

    def for_wallet(self, wallet: str) -> "CopyStrategy":
        return replace(self, wallet=wallet)

    def with_exit(self, exit_rule: ExitRule) -> "CopyStrategy":
        return replace(self, exit=exit_rule)

    # ------------------------------------------------------------- decision
    def admits(self, o: Observation) -> tuple[bool, str]:
        """Does this strategy copy this observation, and if not, exactly why?

        Returning the reason rather than a bare False is what makes the
        no-silent-block audit possible at the strategy layer -- the reason
        string reaches the ledger unchanged.
        """
        if self.min_price is not None and o.price < self.min_price:
            return False, f"price {o.price:.3f} below band min {self.min_price:.2f}"
        if self.max_price is not None and o.price > self.max_price:
            return False, f"price {o.price:.3f} above band max {self.max_price:.2f}"
        if self.min_notional is not None and o.notional < self.min_notional:
            return False, (f"wallet staked ${o.notional:,.0f}, under the "
                           f"${self.min_notional:,.0f} conviction floor")
        if self.max_notional is not None and o.notional > self.max_notional:
            return False, f"wallet staked ${o.notional:,.0f} above cap"
        if self.min_rel_notional is not None and o.rel_notional < self.min_rel_notional:
            return False, (f"stake is {o.rel_notional:.2f}x the wallet's norm, "
                           f"under {self.min_rel_notional:.2f}x")
        if self.min_settled_n is not None and o.w_settled_n < self.min_settled_n:
            return False, (f"wallet has {o.w_settled_n} settled trades "
                           f"point-in-time, under {self.min_settled_n}")
        if self.min_win_rate is not None and o.w_win_rate < self.min_win_rate:
            return False, f"wallet win rate {o.w_win_rate:.0%} under bar"
        if self.min_roll_win_rate is not None and o.w_roll_win_rate < self.min_roll_win_rate:
            return False, f"rolling win rate {o.w_roll_win_rate:.0%} under bar"
        if self.min_roll_roi is not None and o.w_roll_roi < self.min_roll_roi:
            return False, f"rolling ROI {o.w_roll_roi:+.3f} under bar"
        if self.min_edge_t is not None and o.w_edge_t < self.min_edge_t:
            return False, f"wallet edge t={o.w_edge_t:.2f} under bar"
        if self.max_consec_losses is not None and o.w_consec_losses > self.max_consec_losses:
            return False, (f"wallet on {o.w_consec_losses} consecutive losses, "
                           f"over {self.max_consec_losses}")
        if self.skip_repeat_token and o.w_token_repeat:
            return False, "not the opening position in this token"
        if self.min_market_prints is not None and o.market_recent_prints < self.min_market_prints:
            return False, (f"{o.market_recent_prints} prints in the last hour, "
                           f"under {self.min_market_prints} - too thin to copy")
        if self.max_market_move is not None and abs(o.market_price_move) > self.max_market_move:
            return False, (f"market moved {o.market_price_move:+.3f} in the "
                           "hour before - this is chasing")
        if self.min_market_move is not None and abs(o.market_price_move) < self.min_market_move:
            return False, "market is not moving"
        if self.max_secs_to_settle is not None:
            if o.secs_to_settle < 0 or o.secs_to_settle > self.max_secs_to_settle:
                return False, "horizon longer than the strategy trades"
        if self.min_secs_to_settle is not None:
            if o.secs_to_settle < 0 or o.secs_to_settle < self.min_secs_to_settle:
                return False, "resolves too soon to enter"
        return True, ""

    def admits_fast(self, o: Observation) -> bool:
        """`admits` without building the reason string.

        Measured, not guessed: profiling a 400-candidate sweep showed 1.3M
        `str.split` and 654k `str.join` calls -- 18% of total runtime -- spent
        formatting rejection reasons that a sweep never reads. The reasons are
        essential in the live route, where every rejection must be explainable
        (rule 6), and pure waste in the search. So both exist, and
        `tests/test_strategy.py` asserts they agree on every observation.
        """
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
        if self.min_market_prints is not None and o.market_recent_prints < self.min_market_prints:
            return False
        if self.max_market_move is not None and abs(o.market_price_move) > self.max_market_move:
            return False
        if self.min_market_move is not None and abs(o.market_price_move) < self.min_market_move:
            return False
        if self.max_secs_to_settle is not None:
            if o.secs_to_settle < 0 or o.secs_to_settle > self.max_secs_to_settle:
                return False
        if self.min_secs_to_settle is not None:
            if o.secs_to_settle < 0 or o.secs_to_settle < self.min_secs_to_settle:
                return False
        return True

    def stake_for(self, o: Observation, equity: float = 0.0) -> float:
        if self.stake_mode == "proportional":
            return max(1.0, o.notional * self.stake_fraction)
        if self.stake_mode == "fractional" and equity > 0:
            return max(1.0, equity * self.stake_fraction)
        return self.stake_flat

    def describe(self) -> str:
        bits = []
        if self.min_price is not None or self.max_price is not None:
            bits.append(f"price {self.min_price or 0:.2f}-{self.max_price or 1:.2f}")
        if self.min_rel_notional:
            bits.append(f"stake >= {self.min_rel_notional:.1f}x their norm")
        if self.min_settled_n:
            bits.append(f"wallet proven ({self.min_settled_n}+ settled)")
        if self.min_roll_win_rate:
            bits.append(f"running warm (>={self.min_roll_win_rate:.0%})")
        if self.max_consec_losses is not None:
            bits.append(f"<= {self.max_consec_losses} losses in a row")
        if self.skip_repeat_token:
            bits.append("opening entry only")
        if self.min_market_prints:
            bits.append(f">= {self.min_market_prints} recent prints")
        bits.append(f"enter +{self.delay_secs}s")
        bits.append(f"exit {self.exit.model}")
        return "copy when " + ", ".join(bits) if bits else "naive copy"


def naive_copy(wallet: str, delay_secs: int = 60) -> CopyStrategy:
    """The baseline every candidate must beat: copy everything, flat stake.

    A candidate that does not beat this is not a strategy, it is a wallet.
    """
    return CopyStrategy(wallet=wallet, delay_secs=delay_secs, label="naive_copy")


# ---------------------------------------------------------------------------
# The transformation space.
#
# Deliberately small and INTERPRETABLE. A ten-million-point grid over this
# dataset would be a false-discovery generator, not research -- see
# validation/stats.py and the multiple-testing budget it enforces. Each axis is
# a distinct economic hypothesis about why a wallet's edge might be
# conditional, not a free parameter to be tuned.
#
# Current size: 6 * 3 * 3 * 2 * 2 * 4 * 2 * 3 = 5,184 per wallet.
# ---------------------------------------------------------------------------

AXES: dict[str, list] = {
    # "the wallet is only sharp in a particular probability band"
    "price_band": [None, (0.02, 0.30), (0.30, 0.70), (0.70, 0.98),
                   (0.02, 0.50), (0.50, 0.98)],
    # "size expresses conviction"
    "min_rel_notional": [None, 1.5, 3.0],
    # "ignore the wallet until it has proven itself, point-in-time"
    "min_settled_n": [None, 20, 50],
    # "follow only while it is running warm"
    "min_roll_win_rate": [None, 0.55],
    "max_consec_losses": [None, 3],
    # "the edge decays with how late you are"
    "delay_secs": [0, 60, 300, 1800],
    # "only the opening position carries information, not the adds"
    "skip_repeat_token": [False, True],
    # "the copy is only executable where the tape is alive"
    "min_market_prints": [None, 3, 10],
}


def transformation_grid() -> Iterator[dict]:
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


def candidates_for(wallet: str, family: str = "") -> Iterator[CopyStrategy]:
    """All candidate strategies for one wallet, baseline first."""
    yield naive_copy(wallet)
    for i, kw in enumerate(transformation_grid()):
        yield CopyStrategy(wallet=wallet, label=f"t{i:05d}", family=family, **kw)
