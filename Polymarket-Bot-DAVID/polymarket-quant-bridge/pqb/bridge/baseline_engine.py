"""
Reference decision engine (prompt section 4).

**This is a placeholder for the Prop Firm Quant Bridge's LEAN analysis layer**,
which is the intended brain and is not present in this checkout. It exists so
the pipeline is demonstrable end-to-end from day one, and so the shape of the
contract in ``ports.py`` is proven by something real. It implements the
behaviour section 4 specifies — every branch driven by config, no hard-coded
thresholds — but it is not a researched strategy, and its scoring should be
replaced, not tuned.

Two structural commitments here *are* meant to survive into the real engine:

  * **Exits are adjudicated before entries, every cycle, for every position.**
    Section 4.1 makes this the highest-priority capability, so it is the first
    thing the engine does and it cannot be skipped by an early return.
  * **A target wallet is an input, never an instruction.** Wallet activity
    contributes a weighted term to a score. A wallet exit is followed only when
    our own conviction is below ``wallet_exit_override_score``; above it, the
    exit is explicitly overridden and the override is journalled with its
    reasoning, so the decision to ignore a wallet is as reviewable as the
    decision to follow it.
"""

from __future__ import annotations

import time
from typing import Optional

from ..config import EngineConfig
from ..models import (
    Action, BridgeContext, Decision, MarketFeatures, MarketStatus,
    OutcomeQuote, PositionView, WalletSignal,
)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class BaselineDecisionEngine:
    """Scores markets and positions from the context it is handed."""

    def __init__(self, engine_config: EngineConfig):
        self.cfg = engine_config

    # ==================================================================
    # entry point
    # ==================================================================

    def evaluate(self, context: BridgeContext) -> list[Decision]:
        decisions: list[Decision] = []

        # 1. Exit management first — always, for every position.
        held_tokens: set[str] = set()
        for position in context.positions:
            held_tokens.add(position.token_id)
            decisions.append(self._evaluate_position(position, context))

        # 2. Entries, only if the portfolio is in a state to take them.
        block = self._entry_block_reason(context, len(context.positions))
        if block:
            decisions.append(Decision(
                action=Action.NOTHING, reason=block,
                rationale={"gate": "entries_blocked", "detail": block},
            ))
            return decisions

        candidates, diagnostics = self._entry_candidates(context, held_tokens)
        if not candidates:
            # An explicit "nothing qualified", with the numbers behind it. A
            # silent empty result and a scan that found nothing look identical
            # in a journal, and only one of them is a bug.
            decisions.append(Decision(
                action=Action.NOTHING,
                reason=(f"Scanned {diagnostics['scanned']} outcome(s); none "
                        f"reached the {self.cfg.entry.min_score:.2f} entry "
                        f"score (best {diagnostics['bestScore']:.2f})."),
                rationale={"gate": "no_candidates", **diagnostics},
            ))
            return decisions

        room = max(0, self.cfg.portfolio.position_cap
                   - len(context.positions))
        for decision in candidates[:room]:
            decisions.append(decision)
        for decision in candidates[room:]:
            decision.action = Action.NOTHING
            decision.reason = (
                f"Qualified (score {decision.score:.2f}) but the "
                f"{self.cfg.portfolio.max_open_positions}-position limit is full."
            )
            decisions.append(decision)
        return decisions

    # ==================================================================
    # exit management (section 4.1)
    # ==================================================================

    def _evaluate_position(self, position: PositionView,
                           context: BridgeContext) -> Decision:
        market = context.market_for(position)
        quote = market.quote(position.token_id) if market else None
        exits = self.cfg.exits
        ret = position.return_pct
        conviction = self._hold_conviction(position, market, quote, context)

        def verdict(action: Action, style: str, reason: str,
                    extra: Optional[dict] = None) -> Decision:
            rationale = {
                "returnPct": round(ret, 4),
                "drawdownFromPeak": round(position.drawdown_from_peak, 4),
                "peakPrice": position.peak_price,
                "markPrice": position.cur_price,
                "entryPrice": position.avg_price,
                "secondsToResolution": position.seconds_to_resolution,
                "conviction": round(conviction, 4),
                "quoteSource": quote.source if quote else "none",
                "spread": quote.spread if quote else None,
                "thresholds": {
                    "takeProfit": exits.take_profit_pct,
                    "reduceAt": exits.reduce_at_pct,
                    "stopLoss": exits.stop_loss_pct,
                    "trailing": exits.trailing_drawdown_pct,
                },
            }
            rationale.update(extra or {})
            return Decision(
                action=action, token_id=position.token_id,
                market_id=position.market_id, outcome=position.outcome,
                question=position.question,
                size_shares=(position.size if action is Action.EXIT else 0.0),
                limit_price=quote.bid if quote else position.cur_price,
                confidence=round(conviction, 4),
                score=round(conviction, 4), reason=reason, exit_style=style,
                rationale=rationale, lifecycle_id=position.lifecycle_id,
            )

        # -- unconditional exits ---------------------------------------------
        if context.flattening:
            style = context.flatten_reason or "doubling"
            return verdict(Action.EXIT, style, (
                "Kill switch is flattening the book."
                if style == "kill_switch"
                else "Portfolio-doubling rule is flattening the book."))

        if market is not None and market.status is not MarketStatus.ACTIVE:
            return verdict(Action.EXIT, "resolution",
                           f"Market is {market.status.value}; realising now.")

        # -- risk exits, worst case first ------------------------------------
        if exits.stop_loss_pct > 0 and ret <= -exits.stop_loss_pct:
            return verdict(Action.EXIT, "stop",
                           f"Stop hit: {ret * 100:.1f}% vs "
                           f"-{exits.stop_loss_pct * 100:.0f}% limit.")

        armed = (position.peak_price > 0 and position.avg_price > 0
                 and (position.peak_price / position.avg_price - 1.0)
                 >= exits.trailing_arm_pct)
        if (armed and exits.trailing_drawdown_pct > 0
                and position.drawdown_from_peak >= exits.trailing_drawdown_pct):
            return verdict(
                Action.EXIT, "trailing",
                f"Gave back {position.drawdown_from_peak * 100:.1f}% from the "
                f"{position.peak_price:.3f} peak.")

        if (exits.max_hold_seconds and position.opened_ts
                and time.time() - position.opened_ts > exits.max_hold_seconds):
            return verdict(Action.EXIT, "time",
                           "Maximum hold time reached.")

        # -- stagnation: prune dead capital (toggle, off by default) ----------
        # A position that has gone nowhere for its whole window is not a small
        # loss waiting to recover; it is balance locked out of every live setup
        # the engine could otherwise take. The window scales with the market's
        # horizon: a 15-minute market gets minutes, an event market gets hours.
        if exits.stagnation_enabled and position.opened_ts:
            window = self._stagnation_window(position, exits)
            held = time.time() - position.opened_ts
            if window > 0 and held > window \
                    and ret < exits.stagnation_min_move_pct:
                return verdict(
                    Action.EXIT, "stagnant",
                    f"No momentum: {ret * 100:+.1f}% after "
                    f"{held / 60:.0f} min (window {window / 60:.0f} min); "
                    "freeing the capital.")

        # -- the edge exit: the PRIMARY exit ----------------------------------
        # Ahead of take-profit on purpose (master spec): a position is held
        # because the measured edge says hold, and when that conviction dies
        # the trade is over regardless of where the price sits. The stop and
        # take-profit above/below are backstops, not the mechanism.
        held_s = (time.time() - position.opened_ts) if position.opened_ts else 0.0
        if (exits.edge_exit_floor > 0
                and held_s > exits.edge_exit_grace_seconds
                and conviction < exits.edge_exit_floor):
            return verdict(
                Action.EXIT, "edge_gone",
                f"Edge is gone: conviction {conviction:.2f} fell below the "
                f"{exits.edge_exit_floor:.2f} floor; the reason for holding "
                "no longer exists.")

        # -- profit taking (a backstop behind the edge exit) ------------------
        if exits.take_profit_pct > 0 and ret >= exits.take_profit_pct:
            return verdict(Action.EXIT, "take_profit",
                           f"Target reached: +{ret * 100:.1f}%.")

        # -- wallet exit: a signal to weigh, not obey -------------------------
        wallet_exit = self._wallet_exit_signal(position, context)
        if wallet_exit is not None and exits.follow_wallet_exit:
            threshold = self._wallet_override_threshold(context)
            if conviction >= threshold:
                return verdict(
                    Action.HOLD, "wallet_override",
                    f"{wallet_exit.label} exited; holding anyway "
                    f"(conviction {conviction:.2f} >= {threshold:.2f}).",
                    {"walletExit": wallet_exit.to_dict(), "override": True,
                     "overrideThreshold": round(threshold, 4)},
                )
            decision = verdict(
                Action.EXIT, "wallet",
                f"Following {wallet_exit.label}'s exit "
                f"(conviction {conviction:.2f} below override threshold).",
                {"walletExit": wallet_exit.to_dict(), "override": False},
            )
            decision.wallet_influence = wallet_exit.label
            return decision

        # -- partial de-risking ----------------------------------------------
        if (exits.reduce_at_pct > 0 and ret >= exits.reduce_at_pct
                and position.lifecycle_id is not None
                and not self._already_reduced(position, context)):
            return verdict(
                Action.REDUCE, "reduce",
                f"Banking {exits.reduce_fraction * 100:.0f}% at "
                f"+{ret * 100:.1f}%; letting the rest run.")

        # -- time decay near resolution --------------------------------------
        remaining = position.seconds_to_resolution
        if (remaining is not None and exits.near_resolution_seconds
                and remaining <= exits.near_resolution_seconds):
            if exits.near_resolution_action.lower() == "exit":
                return verdict(
                    Action.EXIT, "time_decay",
                    f"{remaining / 60:.0f} min to resolution; exiting per policy.")
            return verdict(
                Action.HOLD, "time_decay",
                f"{remaining / 60:.0f} min to resolution; holding to settlement.")

        return verdict(Action.HOLD, "", "No exit condition met.")

    @staticmethod
    def _stagnation_window(position: PositionView, exits) -> float:
        """The stagnation window for this position, from its market's horizon.

        The operator's matrix, keyed by time-to-resolution: a market resolving
        within the hour is his "ultra-short", within the day his intraday, and
        everything longer (or with no known end) his multi-day/event bucket. A
        position with an unknown end is treated as long-horizon deliberately —
        the aggressive short windows exist for fast markets we can prove are
        fast, not for whatever failed to report an end date.
        """
        remaining = position.seconds_to_resolution
        if remaining is not None and remaining <= 3_600:
            return float(exits.stagnation_fast_seconds)
        if remaining is not None and remaining <= 86_400:
            return float(exits.stagnation_intraday_seconds)
        return float(exits.stagnation_long_seconds)

    def _already_reduced(self, position: PositionView,
                         context: BridgeContext) -> bool:
        """Reduce fires once per position.

        Without this a position drifting around the reduce threshold would be
        trimmed every cycle, paying the spread each time for no change in view.
        The runner stamps the count onto the position from the journal.
        """
        return bool(getattr(position, "reduced_count", 0))

    def _wallet_override_threshold(self, context: BridgeContext) -> float:
        """The conviction needed to ignore a tracked wallet's exit.

        A hook rather than a direct config read, so an engine with evidence
        about which of the two courses has actually paid can move the bar. Here
        it is simply the configured value.
        """
        return self.cfg.exits.wallet_exit_override_score

    def _wallet_exit_signal(self, position: PositionView,
                            context: BridgeContext) -> Optional[WalletSignal]:
        ttl = self.cfg.entry.signal_ttl_seconds
        now = time.time()
        best: Optional[WalletSignal] = None
        for signal in context.signals_for(position.token_id):
            if signal.action != "EXIT":
                continue
            if ttl and signal.timestamp and now - signal.timestamp > ttl:
                continue
            if best is None or signal.weight > best.weight:
                best = signal
        return best

    def _hold_conviction(self, position: PositionView,
                         market: Optional[MarketFeatures],
                         quote: Optional[OutcomeQuote],
                         context: BridgeContext) -> float:
        """How strongly the quantitative view supports continuing to hold.

        Kept intentionally simple and legible — it is the term the real engine
        replaces. What matters structurally is that a number exists, that it is
        journalled, and that it is what decides whether a wallet's exit is
        followed or overridden.
        """
        score = 0.5

        # Being right so far is evidence, with diminishing weight.
        ret = position.return_pct
        score += _clamp(ret, -0.5, 0.5) * 0.4

        # Price level: a token near 1.0 has little left to gain and a long way
        # to fall, so conviction in *holding* fades at the extremes.
        price = position.cur_price or position.avg_price
        if price > 0:
            score += (0.10 if 0.15 <= price <= 0.85 else -0.15)

        # A widening spread means an exit will cost more than the mark suggests.
        if quote is not None and quote.spread is not None and price > 0:
            relative = quote.spread / max(price, 0.01)
            score -= _clamp(relative, 0.0, 0.5) * 0.2

        # Momentum: still above the entry and near the peak reads as intact.
        if position.peak_price > 0:
            score -= _clamp(position.drawdown_from_peak, 0.0, 1.0) * 0.3

        # Approaching resolution with a winning position favours holding to
        # settlement; approaching it while losing does not.
        remaining = position.seconds_to_resolution
        if remaining is not None and remaining <= 6 * 3600:
            score += 0.12 if ret > 0 else -0.12

        # Illiquidity is a reason to be out earlier rather than later.
        if market is not None and market.liquidity < 5_000:
            score -= 0.08

        # Other tracked wallets still holding the same token is mild support.
        holders = sum(1 for p in context.wallet_positions
                      if p.token_id == position.token_id and p.size > 0)
        if holders:
            score += min(0.10, 0.05 * holders)

        return _clamp(score)

    # ==================================================================
    # entries
    # ==================================================================

    def _entry_block_reason(self, context: BridgeContext,
                            open_count: int) -> str:
        if context.flattening:
            return "Doubling rule is flattening; no new entries."
        if not self.cfg.entry.enabled:
            return "Entries are disabled in the config."
        portfolio = self.cfg.portfolio
        if open_count >= portfolio.position_cap:
            return (f"At the {portfolio.max_open_positions}-position limit.")
        if (portfolio.max_drawdown_pct > 0
                and context.portfolio_drawdown >= portfolio.max_drawdown_pct):
            return (f"Portfolio drawdown {context.portfolio_drawdown * 100:.1f}% "
                    f"is at the {portfolio.max_drawdown_pct * 100:.0f}% limit.")
        spendable = context.account.balance * (
            1.0 - portfolio.reserve_cash_fraction)
        floor = max(portfolio.min_order_usdc, context.min_trade_size)
        if spendable < floor:
            return (f"Spendable cash ${spendable:.2f} is below the "
                    f"${floor:.2f} minimum trade size.")
        return ""

    def _entry_candidates(self, context: BridgeContext,
                          held: set[str]) -> tuple[list[Decision], dict]:
        entry = self.cfg.entry
        now = time.time()
        candidates: list[Decision] = []
        # Counted so a cycle that buys nothing can still say why — "every quote
        # was stale" and "everything scored 0.4" call for different fixes.
        diagnostics = {"scanned": 0, "bestScore": 0.0, "gated": {},
                       "belowScore": 0, "noWalletSignal": 0, "unfunded": 0}

        def gate_hit(reason: str) -> None:
            diagnostics["gated"][reason] = diagnostics["gated"].get(reason, 0) + 1

        for market in context.markets.values():
            if not market.tradable:
                continue
            for token_id, quote in market.quotes.items():
                if token_id in held:
                    continue
                diagnostics["scanned"] += 1
                gate = self._entry_gate(market, quote, entry)
                if gate:
                    gate_hit(gate)
                    continue
                score, parts, influence = self._entry_score(
                    market, quote, token_id, context, now)
                diagnostics["bestScore"] = max(diagnostics["bestScore"], score)
                if entry.require_wallet_signal and not influence:
                    diagnostics["noWalletSignal"] += 1
                    continue
                if score < entry.min_score:
                    diagnostics["belowScore"] += 1
                    continue
                candidates.append(Decision(
                    action=Action.BUY, token_id=token_id,
                    market_id=market.market_id, outcome=quote.outcome,
                    question=market.question,
                    limit_price=quote.ask, confidence=round(score, 4),
                    score=round(score, 4),
                    reason=(f"Score {score:.2f} >= {entry.min_score:.2f} "
                            f"at ask {quote.ask}"),
                    rationale={
                        "components": parts,
                        "quote": quote.to_dict(),
                        "liquidity": market.liquidity,
                        "volume24h": market.volume_24h,
                        "secondsToResolution": market.seconds_to_resolution,
                        "minScore": entry.min_score,
                    },
                    wallet_influence=influence,
                ))

        # The high-confidence filter: the last gate before a candidate may be
        # funded. Rejections stay visible — a NOTHING decision carrying every
        # reason — because a filter that silently eats trades is one nobody
        # can tune. The base engine has no filter; engines with a journal
        # (the LEAN brain) arm it via _entry_filter.
        gated: list[Decision] = []
        rejected: list[Decision] = []
        for decision in candidates:
            market = context.markets.get(decision.market_id)
            quote = market.quote(decision.token_id) if market else None
            verdict = self._entry_filter(decision, market, quote, context)
            if verdict is None or verdict.allow:
                if verdict is not None:
                    decision.rationale["highConfidence"] = verdict.to_dict()
                gated.append(decision)
                continue
            decision.action = Action.NOTHING
            decision.reason = ("High-confidence filter: "
                               + "; ".join(verdict.reasons))
            decision.rationale["highConfidence"] = verdict.to_dict()
            rejected.append(decision)
        candidates = gated

        # Rank first, then fund. Sizing during the scan would hand the bankroll
        # to whichever market the iteration reached first rather than to the
        # best-scoring one, and — because each candidate would be sized against
        # the same opening balance — the batch as a whole could commit several
        # times the cash actually available.
        candidates.sort(key=lambda d: d.score, reverse=True)
        funded = self._allocate(candidates, context, diagnostics)
        diagnostics["bestScore"] = round(diagnostics["bestScore"], 4)
        # Filter rejections travel with the funded set so they are journalled
        # with their reasons; they carry NOTHING and cost no capital.
        return funded + rejected, diagnostics

    def _allocate(self, candidates: list[Decision], context: BridgeContext,
                  diagnostics: dict) -> list[Decision]:
        """Spend one bankroll across the ranked candidates, best first.

        At most one outcome per market. The outcomes of a binary market are
        complements: holding both is a locked-in loss of the spread, since the
        two prices sum to roughly 1.00 and only one pays out. Scoring them
        independently makes both look attractive at once, so the constraint has
        to be applied here, where the whole ranked set is visible.
        """
        portfolio = self.cfg.portfolio
        budget = context.account.balance * (1.0 - portfolio.reserve_cash_fraction)
        floor = max(portfolio.min_order_usdc, context.min_trade_size)
        claimed = {p.market_id for p in context.positions if p.market_id}
        funded: list[Decision] = []
        for decision in candidates:
            if decision.market_id and decision.market_id in claimed:
                diagnostics["sameMarket"] = diagnostics.get("sameMarket", 0) + 1
                continue
            if budget < floor:
                diagnostics["unfunded"] += 1
                continue
            stake = min(self._entry_size(decision.score, context), budget)
            if stake < floor:
                diagnostics["unfunded"] += 1
                continue
            # A stake too small to outrun its own round-trip fee is refused.
            # Every such trade looks individually reasonable and collectively
            # bleeds the account, which is the hardest kind of loss to notice.
            from ..adapters.sizing import fee_drag
            drag = fee_drag(stake, portfolio.fee_per_trade_usdc)
            if portfolio.max_fee_fraction > 0 and drag > portfolio.max_fee_fraction:
                diagnostics["feeTooHigh"] = diagnostics.get("feeTooHigh", 0) + 1
                decision.rationale["feeDrag"] = round(drag, 4)
                continue
            decision.size_usdc = stake
            decision.rationale["allocation"] = {
                "stake": round(stake, 4),
                "budgetBefore": round(budget, 4),
                "floor": round(floor, 4),
            }
            budget -= stake
            if decision.market_id:
                claimed.add(decision.market_id)
            funded.append(decision)
        return funded

    def _entry_gate(self, market: MarketFeatures, quote: OutcomeQuote,
                    entry) -> str:
        """Hard disqualifiers. Returned as a string for logging, "" = passes."""
        if quote.ask is None or quote.ask <= 0:
            return "no ask"
        if not (entry.min_price <= quote.ask <= entry.max_price):
            return "price outside band"
        if quote.spread is not None and quote.spread > entry.max_spread:
            return "spread too wide"
        if quote.source == "none":
            return "no live quote"
        remaining = market.seconds_to_resolution
        if remaining is not None and remaining <= 0:
            return "already resolving"
        return ""

    def _entry_score(self, market: MarketFeatures, quote: OutcomeQuote,
                     token_id: str, context: BridgeContext,
                     now: float) -> tuple[float, dict, str]:
        """Blend a market-quality view with weighted wallet evidence."""
        entry = self.cfg.entry
        parts: dict[str, float] = {}

        # -- market quality (0-1) --------------------------------------------
        liquidity_score = _clamp(market.liquidity / 50_000.0)
        volume_score = _clamp(market.volume_24h / 25_000.0)
        spread_score = 1.0 - _clamp((quote.spread or 0.05) / 0.10)
        price = quote.ask or 0.5
        # Mid-range probabilities carry the most information per dollar risked;
        # the tails are where a small adverse move is a large percentage loss.
        band_score = 1.0 - abs(price - 0.5) * 2.0
        remaining = market.seconds_to_resolution or 0.0
        hours = remaining / 3600.0
        # Too soon leaves no room to be right; too far ties capital up.
        time_score = _clamp(1.0 - abs(hours - 48.0) / 240.0)

        market_score = (
            liquidity_score * 0.25 + volume_score * 0.20
            + spread_score * 0.25 + band_score * 0.15 + time_score * 0.15
        )
        parts.update({
            "liquidity": round(liquidity_score, 4),
            "volume": round(volume_score, 4),
            "spread": round(spread_score, 4),
            "priceBand": round(band_score, 4),
            "timeToResolution": round(time_score, 4),
            "marketScore": round(market_score, 4),
        })

        # -- wallet evidence (0-1) -------------------------------------------
        signals = [
            s for s in context.signals_for(token_id)
            if s.action == "ENTRY"
            and (not entry.signal_ttl_seconds
                 or not s.timestamp
                 or now - s.timestamp <= entry.signal_ttl_seconds)
        ]
        holders = [p for p in context.wallet_positions
                   if p.token_id == token_id and p.size > 0]
        wallet_score = 0.0
        labels: list[str] = []
        if signals or holders:
            # Influence is the wallet's *earned* weight where the analytical
            # layer has ranked it — measured performance shrunk by sample size —
            # and only falls back to the configured weight for a wallet with no
            # profile yet. A wallet the config trusts but the record does not
            # therefore carries less than the config asked for, which is the
            # correct direction for a system meant to rank wallets itself.
            weight_sum = sum(self._influence(s.wallet, s.weight, context)
                             for s in signals)
            weight_sum += sum(self._influence(p.wallet, p.weight, context) * 0.4
                              for p in holders)
            wallet_score = _clamp(weight_sum / 2.0)
            labels = sorted({self._label(s.wallet, s.label, context)
                             for s in signals}
                            | {self._label(p.wallet, p.label, context)
                               for p in holders})
            # A wallet that paid *more* than the current ask is weak evidence
            # for buying here; one that paid less makes this entry better than
            # theirs, which is the stronger case.
            better = [s for s in signals if quote.ask and quote.ask <= s.price]
            if better:
                wallet_score = _clamp(wallet_score + 0.15)
        parts["walletScore"] = round(wallet_score, 4)

        # With no wallets configured the wallet term is undefined, not zero.
        # Blending a structural zero into every score would cap the maximum
        # achievable score at (1 - weight) and silently disable all entries —
        # which is a config that looks armed but can never fire.
        weight = _clamp(entry.wallet_signal_weight) if context.tracked_wallets \
            else 0.0
        score = market_score * (1.0 - weight) + wallet_score * weight
        parts["blendWeight"] = round(weight, 4)
        parts["final"] = round(score, 4)
        return score, parts, ", ".join(labels)

    @staticmethod
    def _influence(wallet: str, configured: float,
                   context: BridgeContext) -> float:
        intel = context.intel_for(wallet)
        return intel.weight if intel is not None else _clamp(configured)

    @staticmethod
    def _label(wallet: str, fallback: str, context: BridgeContext) -> str:
        intel = context.intel_for(wallet)
        return (intel.label if intel is not None and intel.label
                else (fallback or str(wallet)[:10]))

    def _entry_filter(self, decision: Decision, market, quote,
                      context: BridgeContext):
        """The high-confidence gate. ``None`` = no filter armed (base engine).

        Engines with a journal (the LEAN brain) override this with the real
        filter; the reference engine keeps its historical behaviour untouched.
        """
        return None

    def _size_throttle(self, context: BridgeContext) -> float:
        """How much to shrink entries, 1.0 = none. Overridden by engines that
        track their own realised performance; the base engine reads the regime
        aggressiveness the runner computed, so a thin or chaotic market-wide
        environment shrinks every stake without any engine-specific state."""
        regime = getattr(context, "regime", None) or {}
        # The capital-preservation multiplier folds in here rather than at the
        # cap, because this is already the one place a stake is scaled by
        # something that is not conviction. It is clamped to <= 1.0 by the
        # policy, so the product can only ever be smaller than the regime term
        # alone — a preservation bug cannot make a position bigger.
        capital = float(getattr(context, "capital_scale", 1.0) or 1.0)
        return float(regime.get("regime_aggressiveness", 1.0)) \
            * max(0.0, min(1.0, capital))

    def _entry_size(self, score: float, context: BridgeContext) -> float:
        """Confidence-scaled stake, bounded by the portfolio and the progression."""
        portfolio = self.cfg.portfolio
        equity = context.account.portfolio_value
        cap = equity * portfolio.max_position_fraction
        spendable = context.account.balance * (
            1.0 - portfolio.reserve_cash_fraction)
        floor = max(portfolio.min_order_usdc, context.min_trade_size)
        # Half the cap at the minimum passing score, the full cap at 1.0.
        confidence_scale = 0.5 + 0.5 * _clamp(
            (score - self.cfg.entry.min_score)
            / max(1e-6, 1.0 - self.cfg.entry.min_score))
        confidence_scale *= _clamp(self._size_throttle(context), 0.1, 1.2)
        stake = min(cap * confidence_scale, spendable)
        if stake < floor:
            # Take the floor if it fits; the progression is a minimum stake, so
            # rounding down below it would defeat the doubling rule.
            return floor if spendable >= floor else 0.0
        return stake
