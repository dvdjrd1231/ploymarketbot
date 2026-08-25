"""The twelve validity gates.

Every candidate trade passes through all twelve. Two properties are worth more
than the individual rules:

  1. **Every gate runs, even after one fails.** The naive design short-circuits
     on the first failure, which produces a system that reports "blocked by
     DATA_VALIDITY" for a year and hides the fact that eleven other things were
     also wrong. V1's whole pathology was exactly this: 40,820 of 40,820
     decisions blocked by one gate sitting above all the others, so nobody
     could see that the gates below it had never been reached. Here the first
     *critical* failure sets `action=DO_NOT_TRADE`, but the full verdict of all
     twelve is recorded on the decision row.

  2. **Every gate names its owner.** Reusing V2's ownership model: a gate may
     only block what it owns, and a gate that blocks everything must carry
     written evidence for why. Without this, a research-layer filter quietly
     becomes a global veto and nobody notices for 40,000 decisions.

A gate returning "cannot judge" is a FAILURE, not a pass. This inverts the
usual default and is the single most consequential choice in the file: on
missing data the system declines to trade rather than trades blind.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from ..config import Mode, Settings
from ..core.canon import Availability, EvidenceState, GateResult
from ..portfolio.capital import Account, Feasibility, SizingResult


class Owner(str, Enum):
    DATA = "DATA"
    RESEARCH = "RESEARCH"
    STRATEGY = "STRATEGY"
    GLOBAL_SAFETY = "GLOBAL_SAFETY"
    PORTFOLIO_RISK = "PORTFOLIO_RISK"
    EXECUTION = "EXECUTION"


@dataclass(frozen=True)
class GateSpec:
    name: str
    owner: Owner
    critical: bool
    rationale: str


GATES: tuple[GateSpec, ...] = (
    GateSpec("DATA_VALIDITY", Owner.DATA, True,
             "Stale or absent inputs cannot support a trade. Fail-safe rule 1."),
    GateSpec("MARKET_VALIDITY", Owner.DATA, True,
             "The market must be open, priced inside the tradeable band, and "
             "not about to resolve inside our own latency."),
    GateSpec("INFORMATION_VALIDITY", Owner.DATA, True,
             "The evidence state must be reconstructable at the decision "
             "timestamp with no layer sourced from after it."),
    GateSpec("SIGNAL_VALIDITY", Owner.STRATEGY, True,
             "A signal must exist, point somewhere, and clear the noise floor."),
    GateSpec("STATISTICAL_VALIDITY", Owner.RESEARCH, True,
             "Effect must survive the pass's multiple-testing threshold with a "
             "reported denominator."),
    GateSpec("OUT_OF_SAMPLE_VALIDITY", Owner.RESEARCH, True,
             "The strategy must have been profitable on data it never saw."),
    GateSpec("EXECUTION_VALIDITY", Owner.EXECUTION, True,
             "A modelled fill must exist at a knowable price."),
    GateSpec("LIQUIDITY_VALIDITY", Owner.EXECUTION, True,
             "Unmeasured liquidity is not infinite liquidity."),
    GateSpec("CAPITAL_VALIDITY", Owner.GLOBAL_SAFETY, True,
             "The trade must be affordable at THIS bankroll, including venue "
             "minimums that do not scale."),
    GateSpec("RISK_VALIDITY", Owner.GLOBAL_SAFETY, True,
             "Drawdown, max loss and adverse-move exposure within limits."),
    GateSpec("PORTFOLIO_VALIDITY", Owner.PORTFOLIO_RISK, True,
             "Correlated and per-wallet exposure within limits after this fill."),
    GateSpec("ADVERSARIAL_VALIDITY", Owner.RESEARCH, True,
             "The red team must have failed to kill it."),
)

BY_NAME = {g.name: g for g in GATES}


@dataclass
class GateReport:
    results: list = field(default_factory=list)
    checked_ts: int = 0

    @property
    def passed(self) -> bool:
        return all(r.passed for r in self.results if r.critical)

    @property
    def blocking(self) -> list:
        return [r for r in self.results if r.critical and not r.passed]

    @property
    def blocking_gate(self) -> str:
        b = self.blocking
        return b[0].gate if b else ""

    def to_dict(self) -> dict:
        return {"passed": self.passed,
                "blocking_gate": self.blocking_gate,
                "n_failed": len(self.blocking),
                "checked_ts": self.checked_ts,
                "results": [r.to_dict() for r in self.results]}


class GateRunner:
    """Runs all twelve. Pure: same inputs, same verdicts, no I/O."""

    def __init__(self, st: Settings) -> None:
        self.st = st

    def run(self, *, ev: EvidenceState, account: Account,
            sizing: SizingResult, signal_strength: float,
            fair_probability: float, market_probability: float,
            confidence: float,
            strategy: dict | None = None,
            red_team: dict | None = None) -> GateReport:
        rep = GateReport(checked_ts=int(time.time()))
        strategy = strategy or {}
        red_team = red_team or {}
        add = rep.results.append

        add(self._data_validity(ev))
        add(self._market_validity(ev))
        add(self._information_validity(ev))
        add(self._signal_validity(signal_strength, fair_probability,
                                  market_probability, confidence))
        add(self._statistical_validity(strategy))
        add(self._oos_validity(strategy))
        add(self._execution_validity(ev, sizing))
        add(self._liquidity_validity(ev, sizing))
        add(self._capital_validity(sizing, account))
        add(self._risk_validity(account, sizing))
        add(self._portfolio_validity(account, sizing))
        add(self._adversarial_validity(red_team))
        return rep

    # -- individual gates ---------------------------------------------------
    def _res(self, name: str, passed: bool, reason: str = "",
             **detail) -> GateResult:
        spec = BY_NAME[name]
        return GateResult(gate=name, passed=passed, critical=spec.critical,
                          reason=reason,
                          detail={"owner": spec.owner.value, **detail})

    def _data_validity(self, ev: EvidenceState) -> GateResult:
        stale = [l.name for l in ev.layers()
                 if l.availability is Availability.STALE]
        if stale:
            return self._res("DATA_VALIDITY", False,
                             f"stale layers: {', '.join(stale)}",
                             stale=stale)
        # The four layers without which nothing can be judged. Others may be
        # absent — that costs confidence, not validity.
        essential = {"price": ev.price, "market": ev.market,
                     "liquidity": ev.liquidity, "risk": ev.risk}
        missing = [k for k, l in essential.items() if not l.ok]
        if missing:
            return self._res("DATA_VALIDITY", False,
                             f"essential layers unavailable: {', '.join(missing)}",
                             missing=missing)
        return self._res("DATA_VALIDITY", True,
                         f"{len(ev.available_layers())}/{len(ev.layers())} layers "
                         f"usable", completeness=round(ev.completeness, 3))

    def _market_validity(self, ev: EvidenceState) -> GateResult:
        px = float(ev.price.get("last") or 0.0)
        if not (self.st.costs.min_price <= px <= self.st.costs.max_price):
            return self._res("MARKET_VALIDITY", False,
                             f"price {px:.4f} outside tradeable band", price=px)
        close_ts = int(ev.market.get("close_ts") or 0)
        if close_ts:
            secs = close_ts - ev.as_of
            if secs <= 0:
                return self._res("MARKET_VALIDITY", False,
                                 "market already closed", secs_to_close=secs)
            # A market resolving inside our own round-trip latency cannot be
            # entered — the fill would land after the outcome is known.
            if secs * 1000 < self.st.costs.latency_ms * 4:
                return self._res("MARKET_VALIDITY", False,
                                 f"resolves in {secs}s, inside 4x our "
                                 f"{self.st.costs.latency_ms}ms latency budget",
                                 secs_to_close=secs)
        status = str(ev.market.get("status") or "").upper()
        if status in ("CLOSED", "RESOLVED", "ARCHIVED"):
            return self._res("MARKET_VALIDITY", False, f"market status {status}")
        return self._res("MARKET_VALIDITY", True, "open and priced in band")

    def _information_validity(self, ev: EvidenceState) -> GateResult:
        future = [l.name for l in ev.layers() if l.as_of and l.as_of > ev.as_of]
        if future:
            # Should be impossible by construction. If it fires, the state
            # builder has a leak and no trade may be made until it is found.
            return self._res("INFORMATION_VALIDITY", False,
                             f"LEAK: layers dated after as_of: {future}",
                             leaked=future)
        if ev.completeness < 0.30:
            return self._res("INFORMATION_VALIDITY", False,
                             f"only {ev.completeness:.0%} of the information "
                             f"environment is available; too little to "
                             f"reconstruct a decision",
                             completeness=round(ev.completeness, 3),
                             missing=ev.missing_layers())
        return self._res("INFORMATION_VALIDITY", True,
                         f"point-in-time state complete to {ev.completeness:.0%}",
                         completeness=round(ev.completeness, 3))

    def _signal_validity(self, strength: float, fair: float, mkt: float,
                         confidence: float) -> GateResult:
        if strength <= 0:
            return self._res("SIGNAL_VALIDITY", False, "no signal")
        edge = fair - mkt
        # The noise floor: the edge must exceed round-trip cost, or we are
        # paying the spread for the privilege of being right.
        floor = self.st.costs.slippage_bps / 10_000.0 * 2
        if abs(edge) <= floor:
            return self._res("SIGNAL_VALIDITY", False,
                             f"edge {edge:+.4f} inside the {floor:.4f} cost floor",
                             edge=round(edge, 5), floor=round(floor, 5))
        if confidence < 0.30:
            return self._res("SIGNAL_VALIDITY", False,
                             f"confidence {confidence:.2f} below 0.30",
                             confidence=round(confidence, 3))
        return self._res("SIGNAL_VALIDITY", True,
                         f"edge {edge:+.4f} at confidence {confidence:.2f}",
                         edge=round(edge, 5))

    def _statistical_validity(self, s: dict) -> GateResult:
        if not s:
            return self._res("STATISTICAL_VALIDITY", False,
                             "no strategy record; an ad-hoc signal has no "
                             "measured false-discovery rate")
        n = int(s.get("trade_count") or 0)
        p = s.get("p_value")
        thr = s.get("bh_threshold")
        denom = int(s.get("hypotheses_tested") or 0)
        if n < self.st.research.min_oos_fills:
            return self._res("STATISTICAL_VALIDITY", False,
                             f"{n} fills is below the {self.st.research.min_oos_fills} "
                             f"minimum sample", n=n)
        if not denom:
            return self._res("STATISTICAL_VALIDITY", False,
                             "hypothesis denominator not recorded; a p-value "
                             "without its search is uninterpretable")
        if p is None or thr is None:
            return self._res("STATISTICAL_VALIDITY", False,
                             "no p-value or BH threshold recorded")
        if float(p) > float(thr):
            return self._res("STATISTICAL_VALIDITY", False,
                             f"p={float(p):.4g} above BH threshold "
                             f"{float(thr):.4g} over {denom} tests",
                             p_value=p, bh_threshold=thr, tested=denom)
        return self._res("STATISTICAL_VALIDITY", True,
                         f"p={float(p):.4g} clears BH {float(thr):.4g} over "
                         f"{denom} tests", tested=denom)

    def _oos_validity(self, s: dict) -> GateResult:
        if not s:
            return self._res("OUT_OF_SAMPLE_VALIDITY", False, "no strategy record")
        status = str(s.get("status") or "DISCOVERED")
        if status in ("DEGRADED", "SUSPENDED", "RETIRED"):
            return self._res("OUT_OF_SAMPLE_VALIDITY", False,
                             f"strategy status {status}", status=status)
        exp = float(s.get("oos_expectancy") or s.get("expectancy") or 0.0)
        if exp <= 0:
            return self._res("OUT_OF_SAMPLE_VALIDITY", False,
                             f"out-of-sample expectancy {exp:+.4f}",
                             expectancy=exp)
        wf = float(s.get("walkforward_positive") or 0.0)
        if wf < self.st.research.min_walkforward_positive:
            return self._res("OUT_OF_SAMPLE_VALIDITY", False,
                             f"positive in only {wf:.0%} of walk-forward folds",
                             walkforward_positive=wf)
        # An apparently perfect strategy triggers MORE validation, not less.
        wr = float(s.get("win_rate") or 0.0)
        if wr >= self.st.research.perfect_winrate_threshold:
            need = int(self.st.research.min_oos_fills
                       * self.st.research.perfect_extra_oos_multiple)
            if int(s.get("trade_count") or 0) < need:
                return self._res(
                    "OUT_OF_SAMPLE_VALIDITY", False,
                    f"win rate {wr:.1%} is near-perfect on only "
                    f"{s.get('trade_count')} fills. Perfection is evidence of "
                    f"insufficient sampling until it survives {need} fills.",
                    win_rate=wr, required_fills=need)
        return self._res("OUT_OF_SAMPLE_VALIDITY", True,
                         f"OOS expectancy {exp:+.4f}, {wf:.0%} of folds positive")

    def _execution_validity(self, ev: EvidenceState, sz: SizingResult) -> GateResult:
        if not ev.execution.ok:
            return self._res("EXECUTION_VALIDITY", False,
                             ev.execution.note or "execution cannot be modelled")
        if sz.feasibility is Feasibility.PRICE_OUT_OF_RANGE:
            return self._res("EXECUTION_VALIDITY", False, sz.reason)
        if sz.fill_probability < 0.5 and sz.size_usdc > 0:
            return self._res("EXECUTION_VALIDITY", False,
                             f"modelled fill probability {sz.fill_probability:.0%}",
                             fill_probability=sz.fill_probability)
        unc = ev.execution.get("uncertainty") or []
        if unc and self.st.mode is Mode.LIVE:
            return self._res("EXECUTION_VALIDITY", False,
                             f"LIVE requires a measured book; unmodelled: "
                             f"{', '.join(unc)}", uncertainty=unc)
        return self._res("EXECUTION_VALIDITY", True,
                         f"fill modelled at {sz.entry_price:.4f}",
                         uncertainty=unc)

    def _liquidity_validity(self, ev: EvidenceState, sz: SizingResult) -> GateResult:
        if sz.feasibility is Feasibility.LIQUIDITY_INFEASIBLE:
            return self._res("LIQUIDITY_VALIDITY", False, sz.reason)
        if sz.available_liquidity <= 0:
            return self._res("LIQUIDITY_VALIDITY", False,
                             "liquidity unmeasured; refusing to assume a fill")
        return self._res("LIQUIDITY_VALIDITY", True,
                         f"${sz.available_liquidity:.2f} visible, "
                         f"${sz.size_usdc:.2f} requested",
                         available=sz.available_liquidity)

    def _capital_validity(self, sz: SizingResult, acct: Account) -> GateResult:
        if sz.feasibility in (Feasibility.CAPITAL_INFEASIBLE, Feasibility.NO_CASH):
            return self._res("CAPITAL_VALIDITY", False, sz.reason,
                             feasibility=sz.feasibility.value,
                             equity=round(acct.equity, 2))
        if sz.size_usdc <= 0:
            return self._res("CAPITAL_VALIDITY", False,
                             "sizing produced no order")
        if sz.size_usdc > acct.available_cash:
            return self._res("CAPITAL_VALIDITY", False,
                             f"order ${sz.size_usdc:.2f} exceeds available cash "
                             f"${acct.available_cash:.2f}")
        return self._res("CAPITAL_VALIDITY", True,
                         f"${sz.size_usdc:.2f} of ${acct.equity:.2f} equity "
                         f"({sz.size_usdc / acct.equity:.1%})"
                         if acct.equity > 0 else "sized")

    def _risk_validity(self, acct: Account, sz: SizingResult) -> GateResult:
        dd = acct.drawdown
        if dd >= self.st.capital.hard_stop_drawdown:
            return self._res("RISK_VALIDITY", False,
                             f"drawdown {dd:.1%} at or past the "
                             f"{self.st.capital.hard_stop_drawdown:.0%} hard stop; "
                             f"a human must resume trading",
                             drawdown=round(dd, 4))
        if sz.max_loss > acct.equity * self.st.capital.max_fraction_per_trade + 1e-9:
            return self._res("RISK_VALIDITY", False,
                             f"max loss ${sz.max_loss:.2f} exceeds the per-trade "
                             f"cap of ${acct.equity * self.st.capital.max_fraction_per_trade:.2f}")
        if sz.expected_value <= 0:
            return self._res("RISK_VALIDITY", False,
                             f"expected value ${sz.expected_value:+.4f} is not "
                             f"positive after costs",
                             expected_value=sz.expected_value)
        return self._res("RISK_VALIDITY", True,
                         f"EV ${sz.expected_value:+.3f}, max loss "
                         f"${sz.max_loss:.2f}, drawdown {dd:.1%}")

    def _portfolio_validity(self, acct: Account, sz: SizingResult) -> GateResult:
        if sz.feasibility is Feasibility.EXPOSURE_LIMIT:
            return self._res("PORTFOLIO_VALIDITY", False, sz.reason,
                             binding=sz.detail.get("binding_cap"))
        if sz.feasibility is Feasibility.POSITION_LIMIT:
            return self._res("PORTFOLIO_VALIDITY", False, sz.reason)
        eq = acct.equity or 1e-9
        post = acct.exposure.gross + sz.size_usdc
        # The whole book against deployable capital, not against equity: the
        # reserve is not available to be exposed.
        cap = self.st.capital.deployable(eq)
        if post > cap:
            return self._res("PORTFOLIO_VALIDITY", False,
                             f"gross exposure would be ${post:.2f}, above the "
                             f"${cap:.2f} deployable limit "
                             f"({1 - self.st.capital.reserve_fraction:.0%} of equity)",
                             post_exposure=round(post, 2))
        return self._res("PORTFOLIO_VALIDITY", True,
                         f"gross exposure ${post:.2f} of ${cap:.2f} deployable "
                         f"({post / cap:.0%})" if cap > 0 else "within limits")

    def _adversarial_validity(self, rt: dict) -> GateResult:
        if not rt:
            return self._res("ADVERSARIAL_VALIDITY", False,
                             "red team did not run; an unreviewed thesis is "
                             "not a validated one")
        if rt.get("killed"):
            objs = rt.get("objections") or []
            return self._res("ADVERSARIAL_VALIDITY", False,
                             "red team killed it: " + "; ".join(objs[:3]),
                             objections=objs)
        dis = float(rt.get("model_disagreement") or 0.0)
        if dis > self.st.agents.max_model_disagreement:
            return self._res("ADVERSARIAL_VALIDITY", False,
                             f"model disagreement {dis:.2f} above "
                             f"{self.st.agents.max_model_disagreement:.2f}",
                             model_disagreement=round(dis, 3))
        cons = float(rt.get("consensus") or 0.0)
        if cons < self.st.agents.min_consensus:
            return self._res("ADVERSARIAL_VALIDITY", False,
                             f"agent consensus {cons:.2f} below "
                             f"{self.st.agents.min_consensus:.2f}",
                             consensus=round(cons, 3))
        return self._res("ADVERSARIAL_VALIDITY", True,
                         f"survived {rt.get('n_agents', 0)} agents, consensus "
                         f"{cons:.2f}, disagreement {dis:.2f}")


def gate_catalogue() -> list[dict]:
    """For the dashboard's VALIDATION tab: who owns what, and why."""
    return [{"gate": g.name, "owner": g.owner.value, "critical": g.critical,
             "rationale": g.rationale} for g in GATES]
