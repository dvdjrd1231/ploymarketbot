"""QUESTION B — does acting on the prediction make money?

Kept in its own file, with its own vocabulary, because the failure this whole
subsystem exists to avoid is answering Question A and reporting it as B. A
classification report and a P&L report never appear in the same table here.

The trade, exactly as Part 8 specifies it: when the classifier predicts
AGGRESSIVE_OPPOSITE, BUY THE OPPOSITE OUTCOME at the +H-minute snapshot, hold,
and settle. Nothing else generates a trade. PROTECT predictions do nothing —
which is itself a result worth stating, because a strategy that trades on
every case is a different strategy.

Three separations are enforced rather than described:

* **Settled and marked results never merge.** A market that resolved is a
  realised fact; a market still open is an opinion about an open position.
  Both are reported, each with its own count, and the headline is the settled
  one because it is the only one that has actually happened.
* **Baselines run over the SAME cases.** Part 15's list is only meaningful if
  the alternatives are offered the same opportunities — a baseline evaluated
  over a different population is a comparison of populations.
* **Concentration is checked before the result is believed.** Part 22: if one
  wallet, one market or three trades carry the number, the number is about
  them and it is flagged as such.
"""

from __future__ import annotations

import math
import random
import statistics
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .classifier import AGGRESSIVE
from .pricing import BASE, ExecutionAssumption, PriceOracle, SETTLEMENT


@dataclass
class SimulatedTrade:
    """One simulated position, entry to exit, with full provenance."""

    wallet: str = ""
    market_id: str = ""
    token_id: str = ""
    question: str = ""
    category: str = ""
    signal_ts: float = 0.0
    entry_price: float = 0.0
    shares: float = 0.0
    stake: float = 0.0
    fee: float = 0.0
    slippage_cost: float = 0.0
    price_source: str = ""
    exit_basis: str = ""            # settlement | mark | unavailable
    exit_price: float = 0.0
    exit_value: float = 0.0
    hold_seconds: float = 0.0
    predicted: str = ""
    actual_label: str = ""
    model_version: str = ""

    @property
    def net_pnl(self) -> float:
        return self.exit_value - self.stake

    @property
    def gross_pnl(self) -> float:
        """Before OUR costs. Fees and slippage are modelled, not observed, so
        the gross figure is what survives if those assumptions are wrong."""
        return self.net_pnl + self.fee + self.slippage_cost

    @property
    def roi(self) -> float:
        return (self.net_pnl / self.stake) if self.stake > 0 else 0.0

    @property
    def settled(self) -> bool:
        return self.exit_basis == SETTLEMENT

    def to_dict(self) -> dict:
        return {"wallet": self.wallet, "market": self.market_id,
                "token": self.token_id, "category": self.category,
                "signalTs": self.signal_ts,
                "entryPrice": round(self.entry_price, 6),
                "shares": round(self.shares, 4),
                "stake": round(self.stake, 4),
                "fee": round(self.fee, 6),
                "slippageCost": round(self.slippage_cost, 6),
                "priceSource": self.price_source,
                "exitBasis": self.exit_basis,
                "exitPrice": round(self.exit_price, 6),
                "exitValue": round(self.exit_value, 6),
                "netPnl": round(self.net_pnl, 6),
                "roi": round(self.roi, 6),
                "holdSeconds": round(self.hold_seconds, 1),
                "predicted": self.predicted, "actualLabel": self.actual_label,
                "modelVersion": self.model_version}


@dataclass
class TradingReport:
    """Question B's answer. Never mixed with Question A's."""

    model_version: str = ""
    assumption: str = ""
    stake: float = 0.0
    signals: int = 0
    attempted: int = 0
    filled: int = 0
    unfilled: int = 0
    unfilled_reasons: dict = field(default_factory=dict)
    skipped_no_exit: int = 0
    trades: list = field(default_factory=list)
    price_source_mix: dict = field(default_factory=dict)

    # -- settled subset: the only realised numbers ---------------------------
    def settled_trades(self) -> list:
        return [t for t in self.trades if t.settled]

    def marked_trades(self) -> list:
        return [t for t in self.trades if not t.settled]

    def to_dict(self) -> dict:
        settled = self.settled_trades()
        marked = self.marked_trades()
        return {
            "modelVersion": self.model_version,
            "assumption": self.assumption,
            "stakeUsdc": self.stake,
            "signals": self.signals,
            "attempted": self.attempted,
            "filled": self.filled,
            "unfilled": self.unfilled,
            "unfilledReasons": dict(sorted(self.unfilled_reasons.items(),
                                           key=lambda kv: -kv[1])[:8]),
            "skippedNoExit": self.skipped_no_exit,
            "priceSourceMix": dict(self.price_source_mix),
            "settled": _pnl_block(settled),
            "markedToMarket": _pnl_block(marked),
            "concentration": concentration(settled or marked),
            "note": ("SETTLED is the realised result and is the number to "
                     "read. MARKED-TO-MARKET covers positions whose market "
                     "had not resolved inside this tape; it is an opinion "
                     "about an open position and is reported separately so it "
                     "can never be booked as profit."),
        }


def _pnl_block(trades: list) -> dict:
    """Every number Part 8 asks for, over one homogeneous set of trades."""
    n = len(trades)
    if not n:
        return {"trades": 0}
    pnls = [t.net_pnl for t in trades]
    rois = [t.roi for t in trades]
    stakes = sum(t.stake for t in trades)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)

    # Bankroll curve in signal order — drawdown is meaningless out of order.
    ordered = sorted(trades, key=lambda t: t.signal_ts)
    equity, peak, max_dd = 0.0, 0.0, 0.0
    curve = []
    for trade in ordered:
        equity += trade.net_pnl
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
        curve.append(round(equity, 4))

    mean_roi = sum(rois) / n
    stdev = statistics.pstdev(rois) if n > 1 else 0.0
    downside = [r for r in rois if r < 0]
    downside_dev = statistics.pstdev(downside) if len(downside) > 1 else 0.0
    return {
        "trades": n,
        "winners": len(wins), "losers": len(losses),
        "winRate": round(len(wins) / n, 4),
        "lossRate": round(len(losses) / n, 4),
        "avgEntryPrice": round(sum(t.entry_price for t in trades) / n, 6),
        "avgExitPrice": round(sum(t.exit_price for t in trades) / n, 6),
        "grossPnl": round(sum(t.gross_pnl for t in trades), 4),
        "fees": round(sum(t.fee for t in trades), 4),
        "slippage": round(sum(t.slippage_cost for t in trades), 4),
        "netPnl": round(sum(pnls), 4),
        "capitalDeployed": round(stakes, 4),
        "roi": round(sum(pnls) / stakes, 4) if stakes else 0.0,
        "meanTradeRoi": round(mean_roi, 4),
        "medianTradeRoi": round(statistics.median(rois), 4),
        "maxDrawdown": round(max_dd, 4),
        "largestWinner": round(max(pnls), 4),
        "largestLoser": round(min(pnls), 4),
        "profitFactor": (round(gross_win / gross_loss, 4)
                         if gross_win > 0 and gross_loss > 0 else None),
        "avgHoldSeconds": round(sum(t.hold_seconds for t in trades) / n, 1),
        # Sharpe/Sortino are per-trade, not annualised: annualising 40 trades
        # over a 90-day tape would produce a confident-looking number about a
        # year nobody observed.
        "perTradeSharpe": (round(mean_roi / stdev, 4)
                           if n >= 20 and stdev > 0 else None),
        "perTradeSortino": (round(mean_roi / downside_dev, 4)
                            if n >= 20 and downside_dev > 0 else None),
        "statisticallyMeaningful": n >= 20,
        "bankrollCurve": curve[:500],
        "bootstrap": bootstrap_roi(rois),
    }


def bootstrap_roi(rois: list, iterations: int = 2000,
                  seed: int = 20260823) -> dict:
    """Percentile bootstrap on the mean per-trade ROI (Part 22).

    A fixed seed so a report is reproducible. Refuses below 20 samples rather
    than returning an interval nobody should read: a bootstrap over 6 trades
    resamples the same 6 trades and reports its own sample back with a
    confident-looking interval around it.
    """
    n = len(rois)
    if n < 20:
        return {"available": False,
                "reason": f"{n} trades — below the 20-trade floor for a "
                          "bootstrap interval"}
    rng = random.Random(seed)
    means = []
    for _ in range(iterations):
        sample = [rois[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo = means[int(0.025 * iterations)]
    hi = means[int(0.975 * iterations) - 1]
    point = sum(rois) / n
    return {
        "available": True, "iterations": iterations,
        "meanRoi": round(point, 4),
        "ci95Low": round(lo, 4), "ci95High": round(hi, 4),
        "standardError": round(statistics.pstdev(means), 5),
        "excludesZero": bool(lo > 0 or hi < 0),
        "reading": ("the 95% interval excludes zero"
                    if lo > 0 or hi < 0 else
                    "the 95% interval CONTAINS zero — this result is not "
                    "distinguishable from no edge"),
    }


def concentration(trades: list) -> dict:
    """Part 22: is this result one wallet, one market, or three trades?"""
    if not trades:
        return {"available": False}
    total_profit = sum(max(0.0, t.net_pnl) for t in trades)
    by_wallet: dict[str, float] = {}
    by_market: dict[str, float] = {}
    for trade in trades:
        by_wallet[trade.wallet] = by_wallet.get(trade.wallet, 0.0) + trade.net_pnl
        by_market[trade.market_id] = \
            by_market.get(trade.market_id, 0.0) + trade.net_pnl
    ordered = sorted((t.net_pnl for t in trades), reverse=True)
    top3 = sum(ordered[:3])
    net = sum(ordered)
    flags = []
    top_wallet = max(by_wallet.items(), key=lambda kv: kv[1]) if by_wallet \
        else ("", 0.0)
    # Only meaningful across wallets. In a single-wallet study "one wallet
    # carries all the profit" is arithmetic, not a finding, and flagging it
    # there trains the reader to ignore the flag where it matters.
    if len(by_wallet) > 1 and total_profit > 0 \
            and top_wallet[1] / total_profit > 0.5:
        flags.append(f"one wallet ({top_wallet[0][:12]}...) carries "
                     f"{top_wallet[1] / total_profit:.0%} of the profit")
    if total_profit > 0 and top3 / total_profit > 0.6 and len(trades) > 6:
        flags.append(f"three trades carry {top3 / total_profit:.0%} of the "
                     "profit")
    if len(by_market) < max(3, len(trades) // 4):
        flags.append(f"{len(trades)} trades across only {len(by_market)} "
                     "market(s)")
    return {
        "available": True, "trades": len(trades),
        "wallets": len(by_wallet), "markets": len(by_market),
        "singleWallet": len(by_wallet) == 1,
        "topWalletShareOfProfit": (round(top_wallet[1] / total_profit, 4)
                                   if total_profit > 0 else 0.0),
        "topThreeTradeShareOfNet": (round(top3 / net, 4)
                                    if net > 0 else 0.0),
        "flags": flags,
        "dominated": bool(flags),
    }


# ---------------------------------------------------------------------------
# The simulation
# ---------------------------------------------------------------------------


def simulate(cases: Iterable[dict], oracle: PriceOracle,
             stake: float = 10.0,
             assumption: ExecutionAssumption = BASE,
             model_version: str = "",
             trade_on: str = AGGRESSIVE,
             category_of=None,
             max_simultaneous: int = 0) -> TradingReport:
    """Part 8's trade, over the cases a classifier produced.

    `cases` are the rows `classifier.evaluate` returned, so the population is
    exactly the one Question A was scored on — a profitability number computed
    over a different set of episodes would not be an answer to "is this
    prediction tradable".

    `max_simultaneous` caps concurrent open positions (Part 21). Zero means no
    cap; when set, a signal arriving while the book is full is recorded as
    skipped rather than silently taken, because pretending to have capital we
    did not have is the same error as pretending to have liquidity.
    """
    report = TradingReport(model_version=model_version,
                           assumption=assumption.name, stake=stake)
    open_until: list = []
    for case in sorted(cases, key=lambda c: c["snapshot"].ts):
        prediction = case["prediction"]
        episode = case["episode"]
        snapshot = case["snapshot"]
        if not prediction.valid or prediction.label != trade_on:
            continue
        report.signals += 1

        if max_simultaneous:
            open_until = [t for t in open_until if t > snapshot.ts]
            if len(open_until) >= max_simultaneous:
                report.skipped_no_exit += 0     # counted below, distinctly
                report.unfilled += 1
                report.unfilled_reasons["portfolio full"] = \
                    report.unfilled_reasons.get("portfolio full", 0) + 1
                continue

        token = episode.opposite_token
        report.attempted += 1
        fill = oracle.buy(token, snapshot.ts, stake, assumption)
        report.price_source_mix[fill.price_source] = \
            report.price_source_mix.get(fill.price_source, 0) + 1
        if not fill.filled:
            report.unfilled += 1
            report.unfilled_reasons[fill.reason] = \
                report.unfilled_reasons.get(fill.reason, 0) + 1
            continue

        value, basis, exit_price = oracle.exit_value(
            token, snapshot.ts, fill.shares, assumption)
        if basis == "unavailable":
            # We could buy it and cannot say what happened to it. Counted, and
            # excluded — booking it at cost would quietly add a zero-P&L trade
            # to the win rate's denominator and to nothing else.
            report.skipped_no_exit += 1
            continue

        report.filled += 1
        trade = SimulatedTrade(
            wallet=episode.wallet, market_id=episode.market_id,
            token_id=token, question=episode.question,
            category=(category_of(episode.question) if category_of else ""),
            signal_ts=snapshot.ts, entry_price=fill.price,
            shares=fill.shares, stake=fill.usdc, fee=fill.fee,
            slippage_cost=fill.slippage_cost, price_source=fill.price_source,
            exit_basis=basis, exit_price=exit_price, exit_value=value,
            hold_seconds=max(0.0, episode.last_activity_ts - snapshot.ts),
            predicted=prediction.label, actual_label=episode.label,
            model_version=model_version or prediction.model_version)
        report.trades.append(trade)
        if max_simultaneous:
            open_until.append(snapshot.ts + max(3_600.0, trade.hold_seconds))
    return report


# ---------------------------------------------------------------------------
# Part 15 — the baselines, over the same cases
# ---------------------------------------------------------------------------


def baselines(cases: list, oracle: PriceOracle, stake: float,
              assumption: ExecutionAssumption, category_of=None,
              seed: int = 20260823) -> dict:
    """Every alternative the brief asks to be beaten, on the same cases.

    Without this block a positive ROI means nothing: buying the opposite
    outcome of every switched episode might do just as well, in which case the
    classifier has contributed exactly nothing and the edge — if there is one
    — belongs to the SETUP, not to the prediction.
    """
    out: dict = {}

    # 1. No trade. The bar that a losing strategy fails to clear.
    out["no_trade"] = {"trades": 0, "netPnl": 0.0, "roi": 0.0,
                       "note": "the alternative of doing nothing"}

    # 2. Enter EVERY switched episode, regardless of prediction.
    every = simulate(
        [dict(c, prediction=_forced(c["prediction"])) for c in cases],
        oracle, stake, assumption, model_version="BASELINE_ALWAYS_ENTER_V1",
        category_of=category_of)
    out["always_enter"] = every.to_dict()

    # 3. Random half. Same setup, prediction replaced by a coin flip — the
    #    control that separates "the signal works" from "the setup works".
    rng = random.Random(seed)
    randomised = []
    for case in cases:
        flip = _forced(case["prediction"]) if rng.random() < 0.5 \
            else _suppressed(case["prediction"])
        randomised.append(dict(case, prediction=flip))
    out["random_half"] = simulate(
        randomised, oracle, stake, assumption,
        model_version="BASELINE_RANDOM_V1", category_of=category_of).to_dict()

    # 4. Immediate entry: buy at the opposite BUY itself rather than waiting
    #    for the horizon. Isolates whether the WAIT is doing the work.
    immediate = []
    for case in cases:
        episode = case["episode"]
        zero = episode.snapshot(0.0)
        if zero.valid:
            immediate.append({"episode": episode, "snapshot": zero,
                              "prediction": _forced(case["prediction"])})
    out["immediate_entry"] = simulate(
        immediate, oracle, stake, assumption,
        model_version="BASELINE_IMMEDIATE_V1",
        category_of=category_of).to_dict()
    return out


def _forced(prediction):
    clone = _clone(prediction)
    clone.label = AGGRESSIVE
    clone.valid = True
    return clone


def _suppressed(prediction):
    clone = _clone(prediction)
    clone.label = "SUPPRESSED"
    return clone


def _clone(prediction):
    from copy import copy
    return copy(prediction)


def compare(strategy: dict, baseline_block: dict) -> dict:
    """The only comparison that matters: did the signal ADD anything?

    Deliberately compares against `always_enter` first. If the classifier's
    ROI does not exceed entering every switched episode, then whatever edge
    exists lives in the setup — 'a wallet just bought the other side' — and the
    prediction is decoration.
    """
    settled = (strategy.get("settled") or {})
    baseline = ((baseline_block.get("always_enter") or {}).get("settled")
                or {})
    strategy_roi = float(settled.get("roi") or 0.0)
    baseline_roi = float(baseline.get("roi") or 0.0)
    strategy_n = int(settled.get("trades") or 0)
    baseline_n = int(baseline.get("trades") or 0)
    if strategy_n < 20 or baseline_n < 20:
        verdict = ("INCONCLUSIVE — fewer than 20 settled trades on one side "
                   "of the comparison")
    elif strategy_roi > baseline_roi:
        verdict = (f"the classifier beat always-enter by "
                   f"{strategy_roi - baseline_roi:+.2%} ROI; check the "
                   "bootstrap interval before believing it")
    else:
        verdict = ("the classifier did NOT beat always-enter — any edge here "
                   "belongs to the setup, not to the prediction")
    return {
        "strategyRoi": round(strategy_roi, 4),
        "alwaysEnterRoi": round(baseline_roi, 4),
        "delta": round(strategy_roi - baseline_roi, 4),
        "strategyTrades": strategy_n, "baselineTrades": baseline_n,
        "verdict": verdict,
    }
