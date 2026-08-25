"""THE TWO-ROUTE ROUTER — Strategy B reaches execution on its own path.

    WALLET SIGNAL
      -> BEHAVIOUR MATCH
      -> STRATEGY B CONDITIONS
      -> STRATEGY B VALIDATION
      -> STRATEGY B RISK CHECK
      -> PORTFOLIO CHECK
      -> EXECUTION

Never:

    WALLET SIGNAL -> Strategy A filters -> Strategy A veto -> DO_NOTHING

That second shape is what the measured build did — with the added detail that
route B was not connected at all, so its signals were not even vetoed, they
were never constructed. This module builds them, and routes them past the
gates that have no authority over them.

**The one rule everything else follows from.** :func:`Router.submit` consults
:func:`pqv2.gatemap.blocks_route` before letting any gate terminate a signal.
A Strategy A gate presented against a route-B signal does not reject it — it
is recorded as an *inherited* gate and the signal continues. Not because
Strategy A's opinion is wrong, but because it is an opinion about Strategy A's
edge, and route B has its own out-of-sample evidence about its own edge.

**What still blocks route B, and always will.** Everything owned by
GLOBAL_SAFETY, PORTFOLIO or EXECUTION: no quote, market resolving, cash below
reserve, halt or kill switch, drawdown halt, position cap, correlated-exposure
cap, total exposure, same-market, liquidity, minimum order, fee drag. Route B
is independent of Strategy A. It is not independent of the account.

**Shadow first.** The router defaults to ``mode="shadow"``: it constructs
signals, runs every gate, writes the full ledger, and emits nothing to any
execution adapter. That is deliberate — the whole point of the audit is that
the system had no evidence about where its opportunities went, and running a
newly connected route live before it has produced that evidence would repeat
the mistake in the other direction.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from . import gatemap
from .funnel import (BEHAVIOR_MATCHED, EXECUTION_ATTEMPTED, EXECUTION_FAILED,
                     EXECUTION_SUCCESSFUL, Opportunity, OpportunityLedger,
                     PORTFOLIO_APPROVED, PORTFOLIO_REJECTED, RISK_PASSED,
                     RISK_REJECTED, ROUTE_A, ROUTE_B, SIGNAL_RECEIVED,
                     STRATEGY_ACCEPTED, STRATEGY_REJECTED)


# ---------------------------------------------------------------------------
# Strategy B's own conditions, read from walletlab's validated specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CopySpec:
    """One VALIDATED walletlab strategy, as route B needs to see it.

    A deliberately thin projection of walletlab's `CopyStrategy`. V2 does not
    re-implement the search, the FDR control or the walk-forward — that engine
    exists, it is tested, and duplicating its logic here would create two
    definitions of the same strategy that can disagree. This carries only what
    is needed to recognise a live opportunity and size it.
    """

    wallet: str
    strategy_id: str
    score: float = 0.0
    oos_p: float = 1.0
    min_price: Optional[float] = None
    max_price: Optional[float] = None
    delay_secs: float = 0.0
    stake_mode: str = "flat"
    stake_flat: float = 0.0
    stake_fraction: float = 0.0
    max_consec_losses: Optional[int] = None
    skip_repeat_token: bool = False
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_registry_row(cls, row: dict) -> "CopySpec":
        try:
            spec = json.loads(row.get("spec_json") or "{}") or {}
        except (TypeError, ValueError):
            spec = {}
        wallet = str(row.get("wallet") or spec.get("wallet") or "")
        return cls(
            wallet=wallet,
            strategy_id=f"WALLET_{wallet[:10]}_{row.get('spec_hash', '')[:8]}",
            score=float(row.get("score") or 0.0),
            oos_p=float(row.get("oos_p") or 1.0),
            min_price=spec.get("min_price"),
            max_price=spec.get("max_price"),
            delay_secs=float(spec.get("delay_secs") or 0.0),
            stake_mode=str(spec.get("stake_mode") or "flat"),
            stake_flat=float(spec.get("stake_flat") or 0.0),
            stake_fraction=float(spec.get("stake_fraction") or 0.0),
            max_consec_losses=spec.get("max_consec_losses"),
            skip_repeat_token=bool(spec.get("skip_repeat_token")),
            raw=spec)

    def matches(self, price: float, seconds_since_wallet_trade: float
                ) -> tuple[bool, float, str]:
        """Does a live opportunity match this spec's entry conditions?

        Returns ``(matched, confidence, reason)``. `confidence` is the
        behaviour-match score the ledger records — here the spec's own
        validation score, because that is the only calibrated number available
        and inventing a second one would be inventing evidence.
        """
        if self.min_price is not None and price < float(self.min_price):
            return False, 0.0, (
                f"price {price:.3f} below this strategy's own "
                f"{float(self.min_price):.3f} floor")
        if self.max_price is not None and price > float(self.max_price):
            return False, 0.0, (
                f"price {price:.3f} above this strategy's own "
                f"{float(self.max_price):.3f} ceiling")
        # The delay is part of the validated spec: the backtest entered this
        # many seconds after the wallet did, and copying sooner is a different
        # strategy from the one that was validated.
        if seconds_since_wallet_trade < self.delay_secs:
            return False, 0.0, (
                f"only {seconds_since_wallet_trade:.0f}s since the wallet "
                f"traded; this spec enters at +{self.delay_secs:.0f}s")
        return True, self.score, ""

    def stake_for(self, equity: float) -> float:
        """The stake this spec was validated with, expressed in today's money.

        `flat` is kept proportional rather than literal: walletlab validates
        on a notional $100 flat stake, and copying that number onto a $40
        account would be a 250% position. The fraction it represents is what
        transfers; the dollar figure is an artefact of the backtest's units.
        """
        if self.stake_mode == "fraction" and self.stake_fraction > 0:
            return max(0.0, equity * self.stake_fraction)
        fraction = self.stake_fraction if self.stake_fraction > 0 else 0.05
        return max(0.0, equity * fraction)


def load_validated(registry_path: str | Path) -> list:
    """Every VALIDATED walletlab strategy. Read-only; nothing else qualifies.

    Only ``VALIDATED``. INSUFFICIENT_EVIDENCE, NOT_SIGNIFICANT, OVERFIT and
    FAILED are all excluded, which is the entire point of walletlab having
    those statuses — on the measured registry that is 2 specs out of 54, and
    the 52 stay out.
    """
    path = Path(registry_path)
    if not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT wallet, spec_hash, score, oos_p, spec_json FROM "
            "experiments WHERE status='VALIDATED' ORDER BY score DESC")]
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    return [CopySpec.from_registry_row(r) for r in rows]


# ---------------------------------------------------------------------------
# the router
# ---------------------------------------------------------------------------


@dataclass
class GateVerdict:
    """One layer's answer about one signal."""

    gate_key: str
    passed: bool
    reason: str = ""


class Router:
    """Routes signals to execution, or names what stopped them.

    `execute` is injected rather than imported so V2 has no dependency on the
    original installation's execution adapter — the router can be exercised
    end-to-end in a test with a stub, and in shadow mode it is never called at
    all.
    """

    def __init__(self, ledger: OpportunityLedger, *, mode: str = "shadow",
                 execute: Optional[Callable[[Opportunity], bool]] = None):
        self.ledger = ledger
        self.mode = str(mode).lower()
        self._execute = execute
        self.inherited_gates: list = []   # A-gates that tried to block B

    # -- the pipeline ------------------------------------------------------

    def submit(self, opp: Opportunity, *,
               strategy_gates: Iterable[GateVerdict] = (),
               risk_gates: Iterable[GateVerdict] = (),
               portfolio_gates: Iterable[GateVerdict] = ()) -> Opportunity:
        """Run one opportunity through its own route.

        Each stage is a list of verdicts from the layers that looked at the
        signal. A failing verdict only terminates the signal if the gate that
        produced it is entitled to block this route; otherwise it is recorded
        as inherited and the signal continues. That single check is the
        architectural rule the master prompt calls non-negotiable.
        """
        opp.advance(SIGNAL_RECEIVED)
        if opp.behavior_match is not None:
            opp.advance(BEHAVIOR_MATCHED)

        for stage, verdicts, ok_state, reject_state, field_name in (
                ("strategy", strategy_gates, STRATEGY_ACCEPTED,
                 STRATEGY_REJECTED, "strategy_b_result"),
                ("risk", risk_gates, RISK_PASSED, RISK_REJECTED,
                 "risk_result"),
                ("portfolio", portfolio_gates, PORTFOLIO_APPROVED,
                 PORTFOLIO_REJECTED, "portfolio_result")):
            blocked = self._first_binding(opp, verdicts)
            if blocked is not None:
                setattr(opp, field_name, f"rejected: {blocked.gate_key}")
                opp.reject(reject_state, blocked.gate_key, blocked.reason)
                self.ledger.record(opp)
                return opp
            setattr(opp, field_name, "accepted")
            opp.advance(ok_state)

        return self._execute_stage(opp)

    def _first_binding(self, opp: Opportunity,
                       verdicts: Iterable[GateVerdict]
                       ) -> Optional[GateVerdict]:
        """The first FAILING verdict entitled to block this route.

        A failing verdict from a gate that does not own this route is recorded
        on the opportunity and on `self.inherited_gates` — so the thing that
        would previously have silently killed the signal now shows up in the
        audit as a named, counted, attributed near-miss.
        """
        for verdict in verdicts:
            if verdict.passed:
                continue
            if gatemap.blocks_route(verdict.gate_key, opp.route):
                return verdict
            gate = gatemap.GATES_BY_KEY.get(verdict.gate_key)
            note = {
                "gate": verdict.gate_key,
                "owner": gate.owner if gate else "UNCLASSIFIED",
                "route": opp.route,
                "reason": verdict.reason,
                "action": "NOT APPLIED — this gate belongs to the other route",
            }
            self.inherited_gates.append(note)
            opp.trail.append({"ts": time.time(), "state": "GATE_NOT_APPLIED",
                              **note})
            if opp.route == ROUTE_B:
                opp.strategy_a_result = (
                    f"would have rejected ({verdict.gate_key}) — not applied")
        return None

    def _execute_stage(self, opp: Opportunity) -> Opportunity:
        if self.mode != "live":
            opp.execution_result = f"{self.mode}: not sent"
            self.ledger.record(opp)
            return opp
        opp.advance(EXECUTION_ATTEMPTED)
        ok = False
        try:
            ok = bool(self._execute(opp)) if self._execute else False
        except Exception as exc:                            # noqa: BLE001
            opp.execution_result = f"error: {exc!r}"
            opp.reject(EXECUTION_FAILED, "exec_liquidity", repr(exc))
            self.ledger.record(opp)
            return opp
        if ok:
            opp.execution_result = "filled"
            opp.advance(EXECUTION_SUCCESSFUL)
        else:
            opp.execution_result = "not filled"
            opp.reject(EXECUTION_FAILED, "exec_liquidity",
                       "the venue did not fill this order")
        self.ledger.record(opp)
        return opp


# ---------------------------------------------------------------------------
# building route-B signals
# ---------------------------------------------------------------------------


def signal_from(spec: CopySpec, *, market: str, token: str, price: float,
                seconds_since_wallet_trade: float, equity: float,
                spread: Optional[float] = None,
                liquidity: Optional[float] = None,
                depth: Optional[float] = None,
                seconds_to_resolution: Optional[float] = None,
                market_state: Optional[float] = None,
                wallet_history_n: Optional[int] = None
                ) -> tuple[Optional[Opportunity], str]:
    """Construct a route-B opportunity, or say why the spec does not match.

    A non-match is NOT a rejection and is not routed: the strategy simply has
    no opinion about this market. Recording every non-match as a rejected
    signal would bury the real rejections under the entire market universe.
    """
    matched, confidence, why = spec.matches(price, seconds_since_wallet_trade)
    if not matched:
        return None, why
    opp = Opportunity(
        ts=time.time(), route=ROUTE_B, market=market, token=token,
        wallet=spec.wallet, strategy_id=spec.strategy_id,
        signal_strength=spec.score, behavior_match=confidence,
        market_state=market_state, entry_price=price, spread=spread,
        liquidity=liquidity, depth=depth,
        seconds_to_resolution=seconds_to_resolution,
        wallet_history_n=wallet_history_n,
        stake=spec.stake_for(equity))
    return opp, ""
