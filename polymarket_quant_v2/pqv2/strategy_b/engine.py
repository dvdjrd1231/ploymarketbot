"""The Strategy B route: wallet signal -> behaviour match -> Strategy B
conditions -> Strategy B validation -> risk -> portfolio -> execution.

The pipeline the brief specifies, in that order, with NO Strategy A gate
anywhere in it. That is enforced, not promised: every rejection goes through
`SignalRecord.reject`, which calls `gates.assert_may_block(key, "B")` and
raises on any gate owned by STRATEGY_A. `tests/test_isolation.py` asserts the
raise actually fires.

Strategy A keeps its own filters and its own route. Neither engine can see the
other's gates. They meet for the first time at the portfolio layer, which is
owned by PORTFOLIO_RISK and is allowed to bind both -- that is what a portfolio
layer is for, and a portfolio rejection never erases the signal.

Every signal that enters `evaluate` leaves in exactly one terminal state, and
`Funnel.assert_balanced()` refuses to let the numbers not add up.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings
from ..ledger import Funnel, LedgerStore, Mode, SignalRecord, Stage, new_signal_id
from ..risk.compounding import Account, Position
from ..risk.execution import ExecutionModel
from ..risk.portfolio import Portfolio
from ..risk.sizing import ExpansionEvidence, size
from .behavior import BehaviorMatcher, CompositeMatcher
from .strategy import CopyStrategy


@dataclass
class StrategyBinding:
    """A validated strategy, its matcher, and the evidence sizing may use."""

    strategy: CopyStrategy
    matcher: object                       # BehaviorMatcher | CompositeMatcher
    status: str
    evidence: ExpansionEvidence = field(default_factory=ExpansionEvidence)
    mode: str = Mode.SHADOW.value

    @property
    def strategy_id(self) -> str:
        return self.strategy.strategy_id


class StrategyBEngine:
    """The independent Strategy B route."""

    ROUTE = "B"

    def __init__(self, st: Settings, account: Account, *,
                 bindings: list | None = None, tape=None,
                 mode: str = Mode.SHADOW.value,
                 ledger: LedgerStore | None = None,
                 funnel: Funnel | None = None) -> None:
        self.st = st
        self.account = account
        self.portfolio = Portfolio(st, account)
        self.execution = ExecutionModel(st, tape=tape)
        self.bindings = bindings or []
        self.mode = mode
        self.ledger = ledger
        self.funnel = funnel or Funnel()
        self.records: list = []

    def add(self, binding: StrategyBinding) -> None:
        self.bindings.append(binding)

    # -- the route -----------------------------------------------------------
    def evaluate(self, o) -> list:
        """Evaluate one wallet action against every bound strategy.

        Returns the SignalRecords produced. One observation can produce several
        signals if several strategies follow the same wallet -- each is
        recorded separately so per-strategy statistics stay separate.
        """
        out = []
        self.funnel.observe_opportunity(self.ROUTE)

        for binding in self.bindings:
            if binding.strategy.wallet and \
                    o.trade.wallet != binding.strategy.wallet:
                # Not this strategy's wallet. Not a rejection -- no signal was
                # ever generated, and counting it as one would make the funnel
                # meaningless.
                continue
            rec = self._route(o, binding)
            self.funnel.record(rec)
            self.records.append(rec)
            out.append(rec)
        return out

    def _route(self, o, binding: StrategyBinding) -> SignalRecord:
        st = self.st
        cfg = st.strategy_b
        strategy = binding.strategy

        rec = SignalRecord(
            signal_id=new_signal_id("B"), ts=o.trade.ts, route=self.ROUTE,
            market_id=o.trade.market_id, token_id=o.trade.token_id,
            wallet=o.trade.wallet, strategy_id=binding.strategy_id,
            mode=binding.mode, entry_price=o.price,
            signal_strength=min(1.0, o.rel_notional / 3.0),
            secs_to_settle=o.secs_to_settle if o.secs_to_settle >= 0 else None,
            recent_price_move=o.market_price_move,
            wallet_settled_n=o.w_settled_n, wallet_win_rate=o.w_win_rate,
            # Depth, spread and market state are NOT available on this
            # substrate. None, never 0 -- a zero would read as measured-empty
            # and silently justify a depth rejection.
            depth=None, spread=None, liquidity=None, market_state=None,
            strategy_a_result="NOT_CONSULTED",
        )

        # 1. BEHAVIOUR MATCH
        matched, mr = binding.matcher.matches(o, cfg.min_behavior_match)
        rec.behavior_match = mr.score
        if not matched:
            return rec.reject(
                Stage.STRATEGY_REJECTED, "b.behavior_match",
                f"behaviour match {mr.score:.2f} < {cfg.min_behavior_match:.2f} "
                f"({mr.reason})",
                strategy_b_result="REJECTED_BEHAVIOR")
        rec.advance(Stage.BEHAVIOR_MATCHED)

        # 2. STRATEGY B CONDITIONS -- its own, never Strategy A's
        ok, why = strategy.admits(o)
        if not ok:
            return rec.reject(Stage.STRATEGY_REJECTED, "b.conditions", why,
                              strategy_b_result="REJECTED_CONDITIONS")

        # 3. STRATEGY B VALIDATION -- may this status trade in this mode?
        allowed, why = self._mode_allows(binding)
        if not allowed:
            return rec.reject(Stage.STRATEGY_REJECTED,
                              "b.strategy_not_validated", why,
                              strategy_b_result="REJECTED_STATUS")
        rec.advance(Stage.STRATEGY_ACCEPTED, strategy_b_result="ACCEPTED")

        # 4. RISK / SIZING
        decision = size(st, self.account.equity, binding.evidence,
                        win_prob=o.w_win_rate, price=o.price,
                        drawdown=self.account.drawdown)
        rec.stake = decision.stake
        if decision.stake <= 0:
            return rec.reject(
                Stage.RISK_REJECTED, "g.per_trade_cap",
                "sizing returned zero: " + "; ".join(
                    decision.reasons + decision.caps_applied),
                risk_result="REJECTED_SIZE")
        if not (st.costs.min_price < o.price < st.costs.max_price):
            return rec.reject(
                Stage.RISK_REJECTED, "g.price_bounds",
                f"price {o.price:.3f} outside global bounds "
                f"[{st.costs.min_price}, {st.costs.max_price}]",
                risk_result="REJECTED_PRICE")
        rec.advance(Stage.RISK_PASSED,
                    risk_result=f"SIZED x{decision.multiplier:.2f}")

        # 5. PORTFOLIO -- may bind, never erases
        candidate = Position(
            token_id=o.trade.token_id, market_id=o.trade.market_id,
            wallet=o.trade.wallet, strategy_id=binding.strategy_id,
            route=self.ROUTE, stake=decision.stake, entry=o.price,
            ts=o.trade.ts)
        pv = self.portfolio.evaluate(candidate, route=self.ROUTE,
                                     correlation_group=o.trade.market_id)
        if not pv.approved:
            return rec.reject(Stage.PORTFOLIO_REJECTED, pv.gate_key, pv.reason,
                              portfolio_result="REJECTED")
        rec.stake = pv.stake
        candidate.stake = pv.stake
        rec.advance(Stage.PORTFOLIO_APPROVED,
                    portfolio_result="APPROVED"
                    + (f" ({'; '.join(pv.adjustments)})" if pv.adjustments else ""))

        # 6. EXECUTION
        rec.advance(Stage.EXECUTION_ATTEMPTED)
        band = ((strategy.min_price or st.costs.min_price),
                (strategy.max_price or st.costs.max_price))
        xr = self.execution.execute(
            token_id=o.trade.token_id, signal_ts=o.trade.ts,
            delay_secs=strategy.delay_secs, stake=pv.stake,
            reference_price=o.price, band=band, depth=None, spread=None)
        if not xr.filled:
            return rec.advance(Stage.EXECUTION_FAILED, gate_key=xr.gate_key,
                               reason=xr.reason, execution_result="FAILED")

        candidate.stake = xr.stake
        candidate.entry = xr.price
        self.account.open(f"{o.trade.token_id}:{rec.signal_id}", candidate)
        return rec.advance(Stage.EXECUTION_SUCCESSFUL,
                           execution_result="FILLED", fill_price=xr.price,
                           stake=xr.stake)

    def _mode_allows(self, binding: StrategyBinding) -> tuple:
        """The evidence bar, which rises with what is at stake.

        This is where the V1 deadlock is broken WITHOUT loosening anything.
        V1 required closed live trades before a setup could be trusted, and
        could not obtain closed live trades without trusting it, so nothing
        ever traded. Here SHADOW mode -- simulated fills, no capital -- runs on
        any status, so evidence accumulates; PAPER requires VALIDATED; LIVE
        requires VALIDATED plus an explicit human decision that this module
        does not make.
        """
        mode = binding.mode
        if mode in (Mode.RESEARCH.value, Mode.SHADOW.value):
            return True, ""
        if binding.status != "VALIDATED":
            return False, (
                f"status {binding.status} does not authorise {mode}; "
                "VALIDATED is required to move capital")
        if mode == Mode.LIVE.value:
            return False, (
                "LIVE requires an explicit human promotion recorded in the "
                "registry; the engine will not promote itself")
        return True, ""

    # -- settlement ----------------------------------------------------------
    def settle(self, key: str, ret: float, ts: int = 0) -> float:
        pnl = self.account.close(key, ret, ts)
        self.funnel.record_outcome(self.ROUTE, pnl, ret)
        self.account.enforce_halt(self.st)
        return pnl

    def flush(self) -> int:
        if self.ledger is None:
            return 0
        n = self.ledger.write(self.records)
        self.records.clear()
        return n

    def report(self) -> dict:
        self.funnel.assert_balanced()
        return {"funnel": self.funnel.summary(),
                "account": self.account.summary(),
                "bindings": len(self.bindings), "mode": self.mode}


def bind_from_verdicts(st: Settings, verdicts, profiles: dict,
                       strategies: dict, *, mode: str = Mode.SHADOW.value,
                       families: dict | None = None) -> list:
    """Turn validated verdicts into live bindings.

    Where a family exists, the matcher is built from EVERY member profile, not
    just the wallet that suggested the idea: a behaviour profile built from
    four independent wallets is much harder to overfit than one built from the
    wallet whose returns pointed at it in the first place.
    """
    out = []
    families = families or {}
    for v in verdicts:
        strategy = strategies.get((v.strategy_id, v.wallet))
        if strategy is None:
            continue
        fam_wallets = families.get(v.family) or []
        member_profiles = [profiles[w] for w in fam_wallets if w in profiles]
        if len(member_profiles) >= 2:
            matcher = CompositeMatcher(member_profiles)
        elif v.wallet in profiles:
            matcher = BehaviorMatcher(profiles[v.wallet])
        else:
            continue
        oos = v.oos or {}
        ev = ExpansionEvidence(
            sample_size=oos.get("n_filled", 0),
            expectancy=(v.is_sample or {}).get("expectancy", 0.0),
            oos_expectancy=oos.get("expectancy", 0.0),
            max_drawdown_pct=oos.get("max_drawdown_pct", 0.0),
            strategy_score=v.score,
            behavior_match=st.strategy_b.min_behavior_match,
            size_predicts_win=getattr(
                profiles.get(v.wallet), "sizing", None).size_predicts_win
            if v.wallet in profiles else 0.0)
        out.append(StrategyBinding(strategy=strategy, matcher=matcher,
                                   status=v.status, evidence=ev, mode=mode))
    return out
