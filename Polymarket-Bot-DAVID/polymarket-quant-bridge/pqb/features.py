"""
The Polymarket feature vector — the bridge's actual input contract.

**One function builds it, and both paths call that function.** The research
export writes these columns to CSV so strategy discovery searches over them, and
the live engine builds the same dict every cycle so a discovered rule can be
evaluated against the market it is looking at right now. If the two were built
separately they would drift, and the failure mode is silent and total: discovery
finds a rule on ``wallet_cohort_net`` and the live engine, which computes that
column slightly differently or not at all, quietly never fires it.

The vector is deliberately **wider than any strategy will use**. Everything
reasonably available goes in — quote, book, liquidity, volume, timing, the trade
tape, wallet intel, market flow and every anomaly kind — and the discovery
layer's feature selection decides which columns carry measurable predictive
value. That is the brief's instruction exactly: keep the raw data available,
don't force all of it into the decision model.

Every value is a plain float. No NaN, no None: an absent measurement is a
documented sentinel (0.0 unless noted), because a rule comparing against NaN
silently evaluates false for every row and looks like a rule that never
triggers rather than one whose input is missing.
"""

from __future__ import annotations

import math
from typing import Any, Optional

from .analytics.anomalies import KINDS
from .models import (
    BridgeContext, MarketFeatures, MarketIntel, OutcomeQuote, PositionView,
)

# Column order is fixed so the exported CSV is stable across runs and a diff
# between two exports shows changed data rather than reordered columns.
FEATURE_NAMES: tuple[str, ...] = (
    # -- quote and book ----------------------------------------------------
    "price", "bid", "ask", "mid", "spread", "spread_rel",
    "bid_depth", "ask_depth", "depth_total", "depth_imbalance",
    "quote_is_live", "quote_age",
    # -- market shape ------------------------------------------------------
    "liquidity", "log_liquidity", "volume_24h", "volume_total",
    "volume_turnover", "price_band", "level_distance", "outcome_count",
    # -- timing ------------------------------------------------------------
    "hours_to_resolution", "log_hours_to_resolution", "market_age_hours",
    "lifecycle_pct",
    "is_active",
    # -- the trade tape ----------------------------------------------------
    "tape_trades", "tape_notional", "tape_buy_ratio", "tape_avg_size",
    "tape_price_drift", "tape_velocity", "tape_trade_rate",
    "tape_size_concentration", "tape_buy_volume", "tape_sell_volume",
    "tape_large_ratio",
    # -- wallet evidence ---------------------------------------------------
    "wallet_entries", "wallet_exits", "wallet_net_usdc", "wallet_weighted",
    "wallet_best_score", "wallet_best_rank", "wallet_cohort_entries",
    "wallet_holders", "wallet_holder_value", "wallet_concentration",
    "wallet_count_tape",
    # -- market flow -------------------------------------------------------
    "flow_z", "flow_net_usdc", "flow_wallets", "cohort_net_usdc",
    # -- anomalies (one strength column per kind, plus the max) -------------
    *(f"anomaly_{kind}" for kind in KINDS),
    "anomaly_max", "anomaly_count",
    # -- market-wide regime (same values for every token in a cycle) --------
    "regime_liquidity", "regime_volatility", "regime_breadth",
    "regime_aggressiveness",
    # -- liquidation-cascade capture (analytics.cascade): forced-flow
    # pressure from the derivatives market, same values for every token in a
    # cycle. Discovery decides whether they predict anything — the
    # hypothesis rides as columns, never as a hard-coded rule.
    "liq_long_usd_60s", "liq_short_usd_60s", "liq_events_300s",
    "liq_imbalance",
    # -- the Market-State layer (events -> state -> LEAN) -------------------
    "ms_chg_1s", "ms_chg_5s", "ms_chg_15s", "ms_chg_30s", "ms_chg_60s",
    "ms_chg_300s", "ms_vol_chg_1s", "ms_vol_chg_5s", "ms_vol_chg_15s",
    "ms_vol_chg_60s", "ms_trade_rate", "ms_trade_rate_short", "ms_volume",
    "ms_imbalance", "ms_liquidity_chg", "ms_data_quality",
    "ms_impulse", "ms_exhaustion", "ms_anomaly", "ms_state", "ms_sequence_id",
)

# What the research export writes in addition to the features: the timestamp
# the pipeline keys on, and the identifiers needed to join a row back to what
# it described. `price` doubles as the pipeline's P&L column.
EXPORT_META = ("timestamp", "token_id", "market_id", "outcome", "category")


def _f(value: Any, default: float = 0.0) -> float:
    """Coerce to a finite float. NaN and inf become the default, not sentinels."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _safe_log1p(value: float) -> float:
    return math.log1p(max(0.0, value))


def token_features(market: MarketFeatures, quote: OutcomeQuote,
                   context: Optional[BridgeContext] = None,
                   market_intel: Optional[MarketIntel] = None,
                   now: Optional[float] = None) -> dict[str, float]:
    """The full feature vector for one outcome token at one instant."""
    import time as _time
    now = _time.time() if now is None else now

    price = _f(quote.ask if quote.ask else quote.mid or quote.last)
    bid, ask = _f(quote.bid), _f(quote.ask)
    mid = _f(quote.mid)
    spread = _f(quote.spread)
    bid_depth, ask_depth = _f(quote.bid_depth), _f(quote.ask_depth)
    depth_total = bid_depth + ask_depth

    out: dict[str, float] = {
        "price": price,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "spread": spread,
        # Relative spread is what actually matters: 2c wide is nothing at 0.80
        # and prohibitive at 0.04.
        "spread_rel": spread / price if price > 0 else 0.0,
        "bid_depth": bid_depth,
        "ask_depth": ask_depth,
        "depth_total": depth_total,
        "depth_imbalance": ((bid_depth - ask_depth) / depth_total
                            if depth_total > 0 else 0.0),
        "quote_is_live": 1.0 if quote.source == "stream" else 0.0,
        "quote_age": max(0.0, now - quote.updated_ts) if quote.updated_ts else 0.0,

        "liquidity": _f(market.liquidity),
        "log_liquidity": _safe_log1p(_f(market.liquidity)),
        "volume_24h": _f(market.volume_24h),
        "volume_total": _f(market.volume_total),
        # How much of the market's lifetime volume traded in the last day — a
        # market waking up scores high here even if it is small in absolute
        # terms, which a raw volume column cannot express.
        "volume_turnover": (_f(market.volume_24h) / _f(market.volume_total)
                            if _f(market.volume_total) > 0 else 0.0),
        "price_band": 1.0 - abs(price - 0.5) * 2.0 if price > 0 else 0.0,
        # Distance to the nearest major probability level. Crowds anchor at
        # round probabilities, and an impulse breaking AWAY from one reads
        # differently from one drifting between levels.
        "level_distance": (min(abs(price - lvl) for lvl in
                               (0.10, 0.25, 0.50, 0.75, 0.90))
                           if price > 0 else 0.0),
        "outcome_count": float(len(market.quotes)),

        "is_active": 1.0 if market.tradable else 0.0,
    }

    remaining = market.seconds_to_resolution
    hours = (remaining / 3600.0) if remaining is not None else 0.0
    out["hours_to_resolution"] = hours
    out["log_hours_to_resolution"] = _safe_log1p(hours)
    # A brand-new market finding its price and an old one suddenly repricing
    # produce the same impulse with different meanings; age separates them.
    age_hours = (max(0.0, (now - market.created_ts) / 3600.0)
                 if market.created_ts else 0.0)
    out["market_age_hours"] = age_hours
    # Where in its life this market is, 0..1. The same minute of the same
    # impulse means something different at 5% of a market's life than at 95%,
    # and normalising by lifecycle is what lets patterns transfer across
    # markets whose absolute lifetimes differ by orders of magnitude.
    life_total = age_hours + hours
    out["lifecycle_pct"] = (age_hours / life_total) if life_total > 0 else 0.0

    # Wallet participation on this cycle's tape: distinct printing wallets and
    # the largest one's share of the notional. Same formula as the historical
    # dataset's WalletConcentration, so the column transfers.
    if context is not None and context.tape_wallets:
        tape_w = context.tape_wallets.get(str(quote.token_id))
        if tape_w:
            out["wallet_count_tape"] = _f(tape_w.get("count"))
            out["wallet_concentration"] = _f(tape_w.get("concentration"))

    if context is not None and context.regime:
        out.update(context.regime)
    # Neutral, not zero: a row with no regime reading means "nothing dampened",
    # and recording 0.0 would teach discovery that absence looks like crisis.
    out.setdefault("regime_aggressiveness", 1.0)

    # Liquidation-cascade pressure this instant (analytics.cascade). Zero IS
    # the honest neutral here: no liquidations means no forced flow.
    if context is not None and getattr(context, "cascade", None):
        out.update(context.cascade)
    for column in ("liq_long_usd_60s", "liq_short_usd_60s",
                   "liq_events_300s", "liq_imbalance"):
        out.setdefault(column, 0.0)

    # The Market-State layer's snapshot for this token: rolling changes, the
    # three scores, and the DORMANT..REVERSAL classification, as columns LEAN
    # (and discovery) consume instead of reconstructing.
    if context is not None and context.market_state:
        state = context.market_state.get(str(quote.token_id))
        if state:
            out.update(state)

    out.update(_tape_features(market, quote.token_id))
    out.update(_wallet_features(context, quote.token_id))
    out.update(_flow_features(market_intel))
    out.update(_anomaly_features(context, quote.token_id, market.market_id))

    # Guarantee the contract: every declared column present, every value finite.
    row = {name: _f(out.get(name, 0.0)) for name in FEATURE_NAMES}

    # The non-print engine's structural columns ride AFTER the fixed contract,
    # in sorted order so exports stay diffable. Dynamic on purpose: the engine
    # defines its own snapshot keys, and both consumers of this row — the
    # capture discovery researches over, and the live evaluation of whatever
    # it discovers — must see identical columns or a discovered rule quietly
    # changes meaning in production.
    if context is not None and context.nonprint:
        structural = context.nonprint.get(str(quote.token_id))
        if structural:
            for key in sorted(structural):
                row[key] = _f(structural[key])
    return row


def _tape_features(market: MarketFeatures, token_id: str) -> dict[str, float]:
    """Summarise the recent print tape for this token."""
    prints = [t for t in market.recent_trades
              if str(t.get("tokenId") or "") == str(token_id)]
    if not prints:
        return {"tape_trades": 0.0, "tape_notional": 0.0,
                "tape_buy_ratio": 0.0, "tape_avg_size": 0.0,
                "tape_price_drift": 0.0}

    sizes = [_f(t.get("size")) for t in prints]
    notional = sum(_f(t.get("price")) * _f(t.get("size")) for t in prints)
    buys = sum(1 for t in prints
               if str(t.get("side") or "").upper().startswith("B"))
    ordered = sorted(prints, key=lambda t: _f(t.get("ts")))
    first_price, last_price = _f(ordered[0].get("price")), _f(ordered[-1].get("price"))
    drift = ((last_price - first_price) / first_price if first_price > 0 else 0.0)

    # Impulse measurements (master spec): the tape carries its own clock, so
    # rate-of-change is computable statelessly — how FAST the probability is
    # moving and how HARD the tape is hitting, not merely where price sits.
    # Discovery decides which of these predict; nothing here is a signal.
    span_s = max(1.0, _f(ordered[-1].get("ts")) - _f(ordered[0].get("ts")))
    avg_size = sum(sizes) / len(sizes) if sizes else 0.0
    buy_volume = sum(_f(t.get("price")) * _f(t.get("size")) for t in prints
                     if str(t.get("side") or "").upper().startswith("B"))
    # Large prints: notional share carried by trades over 3x the tape's own
    # average — the same threshold the historical dataset uses, so a rule
    # discovered on LargeTradeRatio means one thing in both worlds.
    large_cut = 3.0 * (notional / len(prints)) if prints else 0.0
    large_notional = sum(_f(t.get("price")) * _f(t.get("size")) for t in prints
                         if _f(t.get("price")) * _f(t.get("size")) > large_cut)
    return {
        "tape_buy_volume": buy_volume,
        "tape_sell_volume": notional - buy_volume,
        "tape_large_ratio": (large_notional / notional) if notional > 0 else 0.0,
        "tape_trades": float(len(prints)),
        "tape_notional": notional,
        "tape_buy_ratio": buys / len(prints),
        "tape_avg_size": avg_size,
        # Direction of travel across the visible tape, as a fraction.
        "tape_price_drift": drift,
        # Probability movement per hour — the impulse's speed.
        "tape_velocity": drift * 3600.0 / span_s,
        # Prints per minute — activity acceleration shows up here first.
        "tape_trade_rate": len(prints) * 60.0 / span_s,
        # Largest print vs the average — size concentration: one aggressive
        # actor reads differently from many small ones at the same notional.
        "tape_size_concentration": (max(sizes) / avg_size
                                    if sizes and avg_size > 0 else 0.0),
    }


def _wallet_features(context: Optional[BridgeContext],
                     token_id: str) -> dict[str, float]:
    """Wallet evidence for this token, weighted by *earned* influence.

    ``wallet_weighted`` uses :attr:`WalletIntel.weight`, which is the wallet's
    measured score scaled by how much evidence stands behind it — so a wallet's
    influence here is something it earned by being right, not something a
    config file asserted.
    """
    empty = {
        "wallet_entries": 0.0, "wallet_exits": 0.0, "wallet_net_usdc": 0.0,
        "wallet_weighted": 0.0, "wallet_best_score": 0.0,
        "wallet_best_rank": 0.0, "wallet_cohort_entries": 0.0,
        "wallet_holders": 0.0, "wallet_holder_value": 0.0,
    }
    if context is None:
        return empty

    entries = exits = cohort_entries = 0
    net_usdc = weighted = 0.0
    best_score = 0.0
    best_rank = 0.0

    for signal in context.signals_for(token_id):
        intel = context.intel_for(signal.wallet)
        # Fall back to the signal's own configured weight only when the wallet
        # has no derived profile yet — a wallet seen for the first time this
        # cycle has nothing measured about it.
        weight = intel.weight if intel is not None else _f(signal.weight)
        if signal.action == "ENTRY":
            entries += 1
            net_usdc += _f(signal.usdc)
            weighted += weight
            if intel is not None and intel.in_cohort:
                cohort_entries += 1
        else:
            exits += 1
            net_usdc -= _f(signal.usdc)
            weighted -= weight
        if intel is not None:
            best_score = max(best_score, intel.score)
            # Rank 1 is best, so the *lowest* non-zero rank is the strongest
            # wallet involved. Reported inverted (1/rank) so that, like every
            # other column here, larger means stronger.
            if intel.rank and (best_rank == 0.0 or intel.rank < best_rank):
                best_rank = float(intel.rank)

    holders = [p for p in context.wallet_positions
               if p.token_id == str(token_id) and p.size > 0]

    return {
        "wallet_entries": float(entries),
        "wallet_exits": float(exits),
        "wallet_net_usdc": net_usdc,
        "wallet_weighted": weighted,
        "wallet_best_score": best_score,
        "wallet_best_rank": (1.0 / best_rank) if best_rank else 0.0,
        "wallet_cohort_entries": float(cohort_entries),
        "wallet_holders": float(len(holders)),
        "wallet_holder_value": sum(_f(p.value) for p in holders),
    }


def _flow_features(intel: Optional[MarketIntel]) -> dict[str, float]:
    if intel is None:
        return {"flow_z": 0.0, "flow_net_usdc": 0.0, "flow_wallets": 0.0,
                "cohort_net_usdc": 0.0}
    return {
        "flow_z": _f(intel.flow_z),
        "flow_net_usdc": _f(intel.net_usdc),
        "flow_wallets": float(intel.wallets_active),
        "cohort_net_usdc": _f(intel.cohort_net_usdc),
    }


def _anomaly_features(context: Optional[BridgeContext], token_id: str,
                      market_id: str) -> dict[str, float]:
    """One column per anomaly kind, carrying its strength.

    Each kind gets its own column rather than being collapsed into a single
    "anomaly" flag, because the whole question the brief poses is *which* kinds
    predict anything — and a collapsed column makes that unanswerable.
    """
    out = {f"anomaly_{kind}": 0.0 for kind in KINDS}
    out["anomaly_max"] = 0.0
    out["anomaly_count"] = 0.0
    if context is None:
        return out

    found = context.anomalies_for(token_id=token_id, market_id=market_id)
    for anomaly in found:
        key = f"anomaly_{anomaly.kind}"
        if key in out:
            out[key] = max(out[key], _f(anomaly.strength))
    out["anomaly_max"] = max(out[f"anomaly_{k}"] for k in KINDS)
    out["anomaly_count"] = float(len(found))
    return out


def position_features(position: PositionView, market: Optional[MarketFeatures],
                      quote: Optional[OutcomeQuote],
                      context: Optional[BridgeContext] = None,
                      market_intel: Optional[MarketIntel] = None
                      ) -> dict[str, float]:
    """The token's features, plus how *our* position in it is doing.

    Exit management reads this. The position-specific columns are prefixed so
    they cannot collide with the market-side ones, and they are appended rather
    than replacing anything: an exit decision needs the market's state as much
    as an entry does.
    """
    base: dict[str, float] = (
        token_features(market, quote, context, market_intel)
        if market is not None and quote is not None
        else {name: 0.0 for name in FEATURE_NAMES})
    base.update({
        "pos_return": _f(position.return_pct),
        "pos_unrealized": _f(position.unrealized_pnl),
        "pos_size": _f(position.size),
        "pos_cost": _f(position.cost),
        "pos_value": _f(position.market_value),
        "pos_entry_price": _f(position.avg_price),
        "pos_mark": _f(position.cur_price),
        "pos_drawdown_from_peak": _f(position.drawdown_from_peak),
        "pos_peak_gain": (_f(position.peak_price) / _f(position.avg_price) - 1.0
                          if _f(position.avg_price) > 0 else 0.0),
        "pos_hold_hours": ((_time_now() - position.opened_ts) / 3600.0
                           if position.opened_ts else 0.0),
        "pos_reduced": float(getattr(position, "reduced_count", 0) or 0),
    })
    return base


def _time_now() -> float:
    import time as _time
    return _time.time()
