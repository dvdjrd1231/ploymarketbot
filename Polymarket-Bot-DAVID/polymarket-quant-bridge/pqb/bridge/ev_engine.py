"""
The expected-value decision engine.

The architecture the brief asks for, in the order it asks for it:

    raw data -> wallet behaviour -> market/position reconstruction
             -> probability estimation      decision/probability.py
             -> expected value              decision/expected_value.py
             -> opportunity ranking         decision/portfolio.py
             -> portfolio comparison        decision/portfolio.py
             -> execution
             -> outcome feedback            decision/calibration.py

Rather than:

    wallet score > 0.55 -> BUY

Three things follow from that ordering, and they are the point of this engine.

**A wallet score is not a probability.** It says how good a wallet has been,
not how likely this outcome is. Here it is one input to an estimate that starts
from the market's own price and moves it only as far as the evidence — and the
*amount* of evidence — justifies.

**No trade happens without a positive expected edge.** EV per dollar is
``(our probability - the ask) / the ask``, minus slippage and fees. Most
candidates fail this, which is correct: if we agree with the market there is
nothing to win.

**A full book is not a reason to stop thinking.** Held positions are re-priced
every cycle on the same scale as new candidates, so a better opportunity can
displace a worse holding instead of being refused for lack of room.

The structural exit ladder is inherited from :class:`BaselineDecisionEngine` —
flatten, resolution, stop loss, trailing drawdown — because those are risk
controls that must fire regardless of what any model believes.
"""

from __future__ import annotations

from typing import Any, Optional

from ..config import Config, EngineConfig
from ..decision import calibration as calib
from ..decision.expected_value import EVConfig, evaluate, position_ev
from ..decision.portfolio import HeldPosition, build_plan
from ..analytics.correlation import build_clusters, independent_subset
from ..decision.probability import estimate as estimate_probability
from ..logs import Log
from ..models import (
    Action, BridgeContext, Decision, MarketFeatures, OutcomeQuote, PositionView,
    ttr_bucket,
)
from .baseline_engine import BaselineDecisionEngine


class EVDecisionEngine(BaselineDecisionEngine):
    """Trades only where the data shows a measurable, positive edge."""

    def __init__(self, engine_config: EngineConfig,
                 config: Optional[Config] = None,
                 journal: Any = None, log: Optional[Log] = None,
                 intel_store: Any = None):
        super().__init__(engine_config)
        self.config = config
        self.journal = journal
        self.log = log
        self.ev = EVConfig(
            min_ev=engine_config.entry.min_expected_value,
            min_confidence=engine_config.entry.min_confidence,
            slippage=engine_config.entry.slippage,
            fee_per_trade_usdc=engine_config.portfolio.fee_per_trade_usdc,
            exit_ev=engine_config.exits.exit_expected_value,
        )
        self.replace_margin = engine_config.portfolio.replace_margin
        self.calibration = calib.Calibration(journal) if journal else None
        self.intel_store = intel_store
        # Which wallets behave as one voice. Rebuilt on the same slow cadence
        # as the ranking, because it reads the same history.
        self._clusters: dict = {}
        self._clusters_at = 0.0
        self._last_plan: dict = {}

    def _refresh_clusters(self) -> None:
        """Find wallets that copy each other, so consensus is not double-counted.

        Five wallets buying the same outcome is five times the evidence only if
        they decided separately. Measured on the estimator, three correlated
        wallets move the implied probability 23 points on what may be a single
        opinion.
        """
        import time as _t
        if self.intel_store is None or _t.time() - self._clusters_at < 900:
            return
        self._clusters_at = _t.time()
        try:
            rows = self.intel_store.trades_since(int(_t.time() - 14 * 86400))
            self._clusters = build_clusters(rows)
        except Exception as exc:                       # noqa: BLE001
            if self.log is not None:
                self.log.warning("Correlation pass failed", error=repr(exc))
            return
        if self.log is not None and self._clusters:
            groups = {id(c) for c in self._clusters.values()}
            self.log.event("ev.correlation", wallets=len(self._clusters),
                           clusters=len(groups))

    # ==================================================================
    # the pass
    # ==================================================================

    def evaluate(self, context: BridgeContext) -> list[Decision]:
        self._refresh_clusters()
        decisions: list[Decision] = []

        # Risk controls first and unconditionally. A stop loss is not an
        # opinion about value and must not be negotiable by a model.
        held: list[HeldPosition] = []
        for position in context.positions:
            verdict = self._evaluate_position(position, context)
            if verdict.action in (Action.EXIT, Action.REDUCE):
                decisions.append(verdict)          # ladder fired; done here
                continue
            priced = self._price_position(position, context)
            if priced is None:
                decisions.append(verdict)
                continue
            held.append(priced)

        candidates = self._candidates(context, {h.position.token_id for h in held})

        block = self._entry_block_reason(context, len(context.positions))
        if block:
            plan = build_plan([], held, self.ev,
                              self.cfg.portfolio.position_cap,
                              self.replace_margin)
            plan.notes.append(block)
        else:
            plan = build_plan(candidates, held, self.ev,
                              self.cfg.portfolio.position_cap,
                              self.replace_margin)

        decisions.extend(self._decisions_from(plan, context, held))
        self._last_plan = plan.to_dict()
        if self.log is not None and (plan.enter or plan.replace or plan.exit):
            self.log.event("ev.plan", enter=len(plan.enter),
                           replace=len(plan.replace), exit=len(plan.exit),
                           scanned=len(candidates))
        return decisions

    # ==================================================================
    # pricing
    # ==================================================================

    def _wallet_evidence(self, token_id: str, context: BridgeContext):
        """Which INDEPENDENT wallets acted on this outcome, and how much.

        Wallets that copy one another are collapsed to a single voice before
        anything is counted, so consensus reflects how many separate opinions
        there were rather than how many transactions.
        """
        by_wallet: dict[str, float] = {}
        for signal in context.signals_for(token_id):
            if context.intel_for(signal.wallet) is None:
                continue
            signed = signal.usdc if signal.action == "ENTRY" else -signal.usdc
            key = str(signal.wallet).lower()
            by_wallet[key] = by_wallet.get(key, 0.0) + signed

        keep = (independent_subset(list(by_wallet), self._clusters)
                if self._clusters else list(by_wallet))
        out = []
        for wallet in keep:
            intel = context.intel_for(wallet)
            if intel is not None:
                out.append((intel, by_wallet.get(wallet, 0.0)))
        return out

    def _candidates(self, context: BridgeContext, held: set[str]):
        candidates = []
        for market in context.markets.values():
            if not market.tradable:
                continue
            for token_id, quote in market.quotes.items():
                if token_id in held:
                    continue
                if self._entry_gate(market, quote, self.cfg.entry):
                    continue
                evidence = self._wallet_evidence(token_id, context)
                estimate = estimate_probability(quote, market, context, evidence)
                stake = self._entry_size(0.6, context)
                opportunity = evaluate(quote, market, estimate, self.ev, stake)
                candidates.append(opportunity)
                # Recorded whether or not we act — scoring only the trades we
                # took would measure the filter rather than the model. Tagged
                # with category / time-to-resolution / consensus so calibration
                # can be broken down by them.
                if self.calibration is not None:
                    self.calibration.record(
                        opportunity, acted=opportunity.acceptable, stake=stake,
                        category=market.category,
                        ttr=ttr_bucket(market.seconds_to_resolution),
                        consensus=len(evidence))
        return candidates

    def _price_position(self, position: PositionView,
                        context: BridgeContext) -> Optional[HeldPosition]:
        market = context.market_for(position)
        quote = market.quote(position.token_id) if market else None
        if market is None or quote is None:
            return None
        estimate = estimate_probability(
            quote, market, context,
            self._wallet_evidence(position.token_id, context))
        exit_price = quote.bid or quote.mid or position.cur_price or 0.0
        return HeldPosition(
            position=position,
            ev_per_dollar=position_ev(exit_price or 0.01, estimate, self.ev),
            probability=estimate.probability, exit_price=exit_price)

    # ==================================================================
    # turning the plan into decisions
    # ==================================================================

    def _decisions_from(self, plan, context: BridgeContext,
                        held: list[HeldPosition]) -> list[Decision]:
        out: list[Decision] = []
        replaced = {h.position.token_id for h, _ in plan.replace}

        for position, why in plan.exit:
            out.append(self._exit_decision(position, why, "ev_gone"))
        for position, incoming in plan.replace:
            out.append(self._exit_decision(
                position,
                f"replacing with {incoming.outcome[:20]} — "
                f"{incoming.ev_per_dollar:+.1%} vs {position.ev_per_dollar:+.1%} "
                "per dollar", "ev_replace"))

        for opportunity in plan.enter:
            out.append(self._entry_decision(opportunity, context))
        for _position, opportunity in plan.replace:
            out.append(self._entry_decision(opportunity, context))

        # Everything still held and not acted on is an explicit HOLD, with the
        # number behind it — a journal of only the trades taken cannot say
        # whether holding was right.
        for position in held:
            token = position.position.token_id
            if token in replaced or any(p.token_id == token for p, _ in plan.exit):
                continue
            out.append(Decision(
                action=Action.HOLD, token_id=token,
                market_id=position.position.market_id,
                outcome=position.position.outcome,
                question=position.position.question,
                score=round(position.probability, 4),
                confidence=round(position.probability, 4),
                reason=(f"holding: {position.ev_per_dollar:+.1%} per dollar "
                        f"at {position.exit_price:.2f} against our "
                        f"{position.probability:.2f}"),
                rationale={"evPerDollar": round(position.ev_per_dollar, 5),
                           "probability": round(position.probability, 4),
                           "exitPrice": position.exit_price},
                lifecycle_id=position.position.lifecycle_id))

        if not out or (not plan.enter and not plan.replace):
            reason = "; ".join(plan.notes) or "no opportunity cleared the EV bar"
            out.append(Decision(action=Action.NOTHING, reason=reason,
                                rationale={"plan": plan.to_dict()}))
        return out

    def _entry_decision(self, opportunity, context: BridgeContext) -> Decision:
        stake = opportunity.stake or self._entry_size(0.6, context)
        influence = ", ".join(
            e.name.split(":", 1)[1] for e in opportunity.estimate.evidence
            if e.name.startswith("wallet:"))[:80]
        return Decision(
            action=Action.BUY, token_id=opportunity.token_id,
            market_id=opportunity.market_id, outcome=opportunity.outcome,
            question=opportunity.question, size_usdc=stake,
            limit_price=opportunity.entry_price,
            confidence=round(opportunity.estimate.confidence, 4),
            score=round(opportunity.estimate.probability, 4),
            reason=(f"EV {opportunity.ev_per_dollar:+.1%} per dollar: we make "
                    f"this {opportunity.estimate.probability:.2f} against an "
                    f"ask of {opportunity.entry_price:.2f}"),
            rationale=opportunity.to_dict(), wallet_influence=influence)

    def _exit_decision(self, position: HeldPosition, why: str,
                       style: str) -> Decision:
        view = position.position
        return Decision(
            action=Action.EXIT, token_id=view.token_id,
            market_id=view.market_id, outcome=view.outcome,
            question=view.question, size_shares=view.size,
            limit_price=position.exit_price, exit_style=style,
            score=round(position.probability, 4), reason=why,
            rationale={"evPerDollar": round(position.ev_per_dollar, 5),
                       "probability": round(position.probability, 4)},
            lifecycle_id=view.lifecycle_id)
