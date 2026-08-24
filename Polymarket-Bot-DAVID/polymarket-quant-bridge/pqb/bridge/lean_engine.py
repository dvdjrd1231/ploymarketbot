"""
The Quant Bridge as the brain (steps 4 and 6).

This is the engine the brief asks for: it makes no strategic assertions of its
own. What it decides comes from three sources, none of them written here.

  1. **Discovered rules.** Strategies found, validated and ranked by the Prop
     Firm Quant Bridge over captured Polymarket features (:mod:`pqb.research`).
     Each rule votes on every candidate; the votes are weighted by how well the
     rule scored and how many independent token series it survived on.
  2. **The analytical layer.** Dynamically ranked wallets and detected
     anomalies, arriving as ordinary feature columns so a rule can reference
     them exactly like a spread or a liquidity figure — which is what lets
     discovery decide whether they predict anything at all.
  3. **Its own history.** Bounded tilts from what the journal says has actually
     worked (:mod:`pqb.analytics.feedback`).

What it inherits from :class:`BaselineDecisionEngine` is deliberately only the
*structure* — the exit precedence ladder, one outcome per market, rank-then-fund
allocation, wallet evidence never being an instruction. Those are properties of
the problem, not strategy, and re-deriving them here would be duplicating the
part that is already right. Everything that constitutes a *view* is overridden.

**Before any research has run** there are no discovered rules, and the engine
says so in every decision's rationale and falls back to the baseline's market
quality score. That is a legitimate day-one state: the research series is
captured live and cannot be backfilled, so there is necessarily a period where
the bridge is gathering the history it will later be researched on.
"""

from __future__ import annotations

import time
from typing import Any, Optional

from ..analytics import feedback
from ..config import Config, EngineConfig
from ..features import token_features
from ..logs import Log
from ..models import (
    BridgeContext, MarketFeatures, OutcomeQuote, PositionView,
)
from ..research import DiscoveredStrategy, load_strategies
from .baseline_engine import BaselineDecisionEngine, _clamp
from .live_features import LiveFeatureEngineer

# How often the journal is re-read for performance feedback. The journal only
# changes when a position closes, so re-reading it every cycle would be a query
# per cycle to learn nothing on almost all of them.
FEEDBACK_REFRESH_SECONDS = 300

# How often discovered strategies are re-read from disk. Non-zero so that
# `pqb.cli research` can hand new rules to a *running* bridge without a restart.
STRATEGY_REFRESH_SECONDS = 600


def _compare(value: float, op: str, threshold: float) -> bool:
    if op == ">":
        return value > threshold
    if op == "<":
        return value < threshold
    if op == ">=":
        return value >= threshold
    if op == "<=":
        return value <= threshold
    return False


class LeanDecisionEngine(BaselineDecisionEngine):
    """Evaluates Quant-Bridge-discovered rules against live Polymarket features."""

    def __init__(self, engine_config: EngineConfig,
                 config: Optional[Config] = None,
                 journal: Any = None, log: Optional[Log] = None,
                 intel_store: Any = None):
        super().__init__(engine_config)
        self.config = config
        self.journal = journal
        self.log = log
        self.live_features = (LiveFeatureEngineer(intel_store, log)
                              if intel_store is not None else None)
        self._strategy_path = (config.data_dir / "strategies.json"
                               if config is not None else None)
        self.strategies: list[DiscoveredStrategy] = []
        self.memory = feedback.PerformanceMemory()
        self._hc_filter = None
        self._last_strategy_load = 0.0
        self._last_feedback = 0.0
        self._reload_strategies(force=True)
        self._refresh_feedback(force=True)

    # ==================================================================
    # state the engine keeps between cycles
    # ==================================================================

    def _reload_strategies(self, force: bool = False) -> None:
        if self._strategy_path is None:
            return
        now = time.time()
        if not force and now - self._last_strategy_load < STRATEGY_REFRESH_SECONDS:
            return
        self._last_strategy_load = now
        previous = len(self.strategies)
        self.strategies = load_strategies(self._strategy_path)
        if self.live_features is not None:
            # Only the columns these rules need get engineered each cycle; the
            # full set costs ~260ms per token, which a real universe cannot
            # afford. See live_features.set_referenced_features.
            self.live_features.set_referenced_features(self.referenced_features)
        if self.log is not None and (force or len(self.strategies) != previous):
            if self.strategies:
                self.log.event("engine.strategies", count=len(self.strategies),
                               source=str(self._strategy_path))
            else:
                self.log.warning(
                    "No discovered strategies; scoring falls back to baseline "
                    "market quality. Run `python -m pqb.cli research` once "
                    "enough feature history has been captured.",
                    path=str(self._strategy_path))

    def _refresh_feedback(self, force: bool = False) -> None:
        if self.journal is None:
            return
        now = time.time()
        if not force and now - self._last_feedback < FEEDBACK_REFRESH_SECONDS:
            return
        self._last_feedback = now
        try:
            self.memory = feedback.build(self.journal)
        except Exception as exc:                        # noqa: BLE001
            # Feedback is an enhancement, not a dependency. A malformed journal
            # must not stop the engine from deciding — it should cost the tilt.
            if self.log is not None:
                self.log.warning("Performance feedback unavailable",
                                 error=repr(exc))
            self.memory = feedback.PerformanceMemory()
            return
        if self.log is not None and self.memory.active:
            self.log.event("engine.feedback", closed=self.memory.book_sample,
                           meanReturn=round(self.memory.book_mean_return, 4),
                           groups=sum(len(g) for g in self.memory.groups.values()))

    @property
    def total_rule_weight(self) -> float:
        return sum(self._weight(s) for s in self.trading_strategies)

    @property
    def referenced_features(self) -> set[str]:
        """Every feature name the loaded rules read, entry and filter alike."""
        names: set[str] = set()
        for strategy in self.trading_strategies:
            for key in ("entry_feature", "filter_feature"):
                value = strategy.rule.get(key)
                if value:
                    names.add(str(value))
        return names

    @property
    def trading_strategies(self) -> list:
        """Only strategies PROVEN on unseen markets may drive money (§7).

        Candidates and still-testing rules stay visible on the Discovery tab
        but carry zero weight here — trading on a rule graded by its own
        discovery data is the one thing the validation spec forbids.

        Sequence/event-chain and sharp-move rules are additionally EXCLUDED
        from voting even once validated (the operator's specs, §17 and the
        sharp-move module's zero-authority constraint): they accumulate
        evidence in the library, but acting on one requires a live
        event-stream execution path that must be built deliberately after
        forward validation — never inherited implicitly by a feature-
        threshold voting loop that cannot evaluate them.
        """
        return [s for s in self.strategies
                if getattr(s, "tradable", False)
                and (s.rule or {}).get("type") not in ("sequence",
                                                       "sharp_move",
                                                       "longshot",
                                                       "wallet_state",
                                                       "wallet_behavior")]

    @staticmethod
    def _weight(strategy: DiscoveredStrategy) -> float:
        """How much one rule's vote counts: OOS evidence, never raw win rate.

        Wilson-bounded confidence carries the sample size (98% over 8 trades
        weighs far less than 90% over 300), scaled by OOS breadth — how many
        independent unseen markets it actually traded in.
        """
        confidence = getattr(strategy, "confidence", 0.0)
        breadth = min(1.0, getattr(strategy, "oos_markets", 0) / 5.0)
        if confidence > 0:
            return max(0.05, confidence * max(0.2, breadth))
        # Back-compat for pre-validation strategy files.
        confirmation = min(1.0, strategy.accepted_on / 3.0)
        return max(0.05, strategy.score) * confirmation

    # ==================================================================
    # evaluation
    # ==================================================================

    def evaluate(self, context: BridgeContext) -> list:
        self._reload_strategies()
        self._refresh_feedback()
        if self.live_features is not None:
            self.live_features.begin_cycle(context.cycle_id)
        # Attached so the scoring hooks below, which the parent calls without
        # passing it, can reach the performance memory's evidence for the
        # journal. Cleared implicitly next cycle by being reassigned.
        self._context = context
        return super().evaluate(context)

    def _entry_block_reason(self, context, open_count: int) -> str:
        """Learning mode sits above every other entry gate.

        No validated strategies -> no entries, full stop. Exits, capture,
        ranking and hourly discovery all keep running; the moment a strategy
        survives validation, trading resumes on its own. This is the operator's
        capital-preservation rule made structural: the simulated account cannot
        be spent by the fallback scorer while discovery is still empty-handed.
        """
        cfg = getattr(self.config, "engine", None)
        if cfg is not None and cfg.entry.require_strategies \
                and not self.trading_strategies:
            return ("Learning mode: no validated strategies yet - capital is "
                    "parked until discovery produces one (exits still run).")
        return super()._entry_block_reason(context, open_count)

    def _entry_filter(self, decision, market, quote, context):
        """Arm the high-confidence filter over every LEAN entry candidate."""
        cfg = getattr(self.config, "engine", None)
        if cfg is None or not cfg.filter.enabled:
            return None
        if self._hc_filter is None:
            from ..decision.high_confidence import HighConfidenceFilter
            self._hc_filter = HighConfidenceFilter(
                cfg.filter, journal=self.journal,
                live=bool(self.config and self.config.mode.live))
        if market is None or quote is None:
            return None
        row = self._features_for(market, quote, context)
        stake = self._entry_size(decision.score, context)
        return self._hc_filter.evaluate(
            score=decision.score, category=market.category, stake=stake,
            features=row, spread=float(quote.spread or 0.0),
            depth=float(quote.ask_depth or 0.0)
            * float(quote.ask or 0.0),
            ev_per_dollar=None,
            wallet_net=float(row.get("wallet_net_usdc", 0.0)),
            context=context, token_id=decision.token_id)

    def _size_throttle(self, context: BridgeContext) -> float:
        """Regime scaling plus the performance-deviation throttle.

        When the book's rolling realised return has gone materially negative
        over a real sample, live behaviour has deviated from what validation
        promised — so entries shrink (and shrink harder at twice the
        deviation) until results recover. Never scales up on a hot streak.
        """
        scale = super()._size_throttle(context)
        portfolio = self.cfg.portfolio
        memory = self.memory
        if (memory.active
                and memory.book_sample >= portfolio.throttle_min_sample
                and portfolio.throttle_at_return < 0):
            realised = memory.book_mean_return
            if realised <= 2 * portfolio.throttle_at_return:
                scale *= portfolio.throttle_factor ** 2
            elif realised <= portfolio.throttle_at_return:
                scale *= portfolio.throttle_factor
        return scale

    def _features_for(self, market: MarketFeatures, quote: OutcomeQuote,
                      context: BridgeContext) -> dict[str, float]:
        """The row a discovered rule is evaluated against.

        Raw columns always; the bridge's engineered columns as well, whenever
        this token has enough captured history for them to be defined. Rules
        are discovered over the engineered set, so without the second half most
        of them could never fire — see :mod:`pqb.bridge.live_features`.
        """
        raw = token_features(
            market, quote, context,
            market_intel=context.market_intel.get(market.market_id))
        if self.live_features is None or not self.trading_strategies:
            return raw
        engineered = self.live_features.row_for(quote.token_id, raw)
        return engineered if engineered is not None else raw

    def _rule_votes(self, row: dict[str, float]) -> tuple[float, list[dict]]:
        """Net directional conviction from the discovered rules, in -1..1.

        Normalised by the *total* weight on the board rather than by the weight
        that fired, so "one weak rule fired" and "every rule fired" do not both
        read as total conviction.
        """
        total = self.total_rule_weight
        if total <= 0:
            return 0.0, []
        net = 0.0
        fired: list[dict] = []
        for strategy in self.trading_strategies:
            rule = strategy.rule
            feature = str(rule.get("entry_feature") or "")
            if feature not in row:
                # A rule referencing a column this build no longer produces is
                # stale, not false. Skipping it keeps it out of the numerator
                # AND leaves it in the denominator, so a stale strategy file
                # dilutes conviction instead of silently voting no.
                continue
            if not _compare(row[feature], str(rule.get("entry_op") or ">"),
                            float(rule.get("entry_threshold") or 0.0)):
                continue
            filter_feature = rule.get("filter_feature")
            if filter_feature:
                if str(filter_feature) not in row:
                    continue
                if not _compare(row[str(filter_feature)],
                                str(rule.get("filter_op") or ">"),
                                float(rule.get("filter_threshold") or 0.0)):
                    continue
            weight = self._weight(strategy)
            # A short signal on a prediction-market outcome is a statement that
            # the outcome is overpriced. There is no borrow, so it cannot be
            # traded directly — it is evidence against buying, which is what
            # the negative sign expresses.
            direction = 1.0 if str(rule.get("direction")) == "long" else -1.0
            net += weight * direction
            fired.append({
                "signature": strategy.signature,
                "describe": strategy.describe,
                "direction": rule.get("direction"),
                "weight": round(weight, 4),
                "acceptedOn": strategy.accepted_on,
            })
        return _clamp(net / total, -1.0, 1.0), fired

    # -- entries -------------------------------------------------------------

    def _entry_score(self, market: MarketFeatures, quote: OutcomeQuote,
                     token_id: str, context: BridgeContext,
                     now: float) -> tuple[float, dict, str]:
        base_score, parts, influence = super()._entry_score(
            market, quote, token_id, context, now)

        row = self._features_for(market, quote, context)
        net, fired = self._rule_votes(row)

        if self.strategies:
            # Discovered rules are the view; the baseline's market-quality score
            # is retained only as the floor they move away from, so a market
            # nothing can trade in cannot be talked into a high score by rules.
            score = _clamp(base_score * 0.4 + (0.5 + 0.5 * net) * 0.6)
            parts["ruleNet"] = round(net, 4)
            parts["rulesFired"] = len(fired)
            parts["rulesTotal"] = len(self.strategies)
        else:
            score = base_score
            parts["ruleNet"] = 0.0
            parts["rulesFired"] = 0
            parts["rulesTotal"] = 0
            parts["fallback"] = "no discovered strategies; baseline scoring"

        tilt, evidence = self.memory.tilt(
            category=market.category,
            liquidity_bucket=_liquidity_bucket(market.liquidity),
            ttr_bucket=_ttr_bucket(market.seconds_to_resolution))
        score = _clamp(score + tilt)

        parts["feedbackTilt"] = round(tilt, 5)
        parts["final"] = round(score, 4)
        # Carried into the journal so a decision can be re-derived from the
        # record alone — which is the whole point of journalling the rationale.
        parts["_evidence"] = {
            "rules": fired[:6],
            "feedback": evidence,
            "features": {k: round(v, 6) for k, v in row.items() if v},
        }
        return score, parts, influence

    # -- exits ---------------------------------------------------------------

    def _hold_conviction(self, position: PositionView,
                         market: Optional[MarketFeatures],
                         quote: Optional[OutcomeQuote],
                         context: BridgeContext) -> float:
        conviction = super()._hold_conviction(position, market, quote, context)
        if market is None or quote is None or not self.trading_strategies:
            return conviction

        row = self._features_for(market, quote, context)
        net, _fired = self._rule_votes(row)
        # The rules' current view of this outcome is evidence about whether to
        # keep holding it. Bounded to a third of the scale: an exit ladder that
        # a rule vote could dominate would let a single freshly-fired rule
        # unwind a position the risk thresholds are managing.
        conviction = _clamp(conviction + net * 0.33)

        tilt, _evidence = self.memory.tilt(
            category=market.category,
            liquidity_bucket=_liquidity_bucket(market.liquidity),
            ttr_bucket=_ttr_bucket(market.seconds_to_resolution))
        return _clamp(conviction + tilt)

    def _wallet_override_threshold(self, context: BridgeContext) -> float:
        """Adapt the follow-or-override bar to which one has actually paid.

        The brief asks the bridge to decide whether following a wallet's exit or
        ignoring it produces the better result. The journal can answer that once
        both arms have closed positions, so the threshold moves toward whichever
        has done better rather than staying at the number in the config.

        Movement is capped at +/-0.15 and requires both arms to have a sample:
        one arm alone says nothing about the other, and an unbounded adjustment
        would eventually pin the threshold at 0 or 1 and stop it being a
        decision at all.
        """
        base = super()._wallet_override_threshold(context)
        bias, evidence = self.memory.wallet_exit_bias()
        if not evidence.get("applied"):
            return base
        # bias > 0 means overriding has done better, so overriding should be
        # easier — which means a *lower* bar for conviction to clear.
        return _clamp(base - bias * 0.15, 0.05, 0.95)


def _liquidity_bucket(liquidity: float) -> str:
    from ..models import liquidity_bucket
    return liquidity_bucket(liquidity)


def _ttr_bucket(seconds: Optional[float]) -> str:
    from ..models import ttr_bucket
    return ttr_bucket(seconds)
