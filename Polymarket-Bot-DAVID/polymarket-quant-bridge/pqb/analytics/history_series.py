"""
Research series from HISTORY: the backfilled trade tapes, not just live capture.

David's correction, and it is the important one: *"I wanted ALL of the
historical closed trades in these wallets to be studied... Past history. Not
history created after the bot starts."* He is right. Discovery previously read
only the live-captured feature series — rows that begin existing when the bot
starts — while the store already held tens of thousands of trades from ALREADY
SETTLED markets, pulled by `backfill`, with their full price paths and known
outcomes. The best training data in the system was sitting unused.

This module turns those historical tapes into research series the Quant Bridge
can study immediately. Every trade anyone made is an observation of price and
flow at a moment in time, so a settled market's tape replays into a time series
of exactly the kind discovery runs over — except it is weeks deep and its
outcome is already known.

Honesty about what history can and cannot provide:

* **Derivable, and computed**: price (the pipeline's P&L column), the whole
  tape family (trades, notional, buy ratio, avg size, drift), wallet activity
  (entries/exits/net flow, weighted by the CURRENT ranking), rolling flow,
  volume, time-to-resolution (measured back from the tape's end).
* **Not derivable, and said so**: order-book columns (bid/ask/depth/spread).
  A book that existed for four seconds last year is gone. Those columns are
  pinned to neutral constants (`bid = ask = mid = price`, zero depth,
  ``quote_is_live = 0``) so they carry NO variance — a constant column cannot
  be fitted on, which is the safe failure mode. Rules discovered on history are
  therefore price/tape/wallet rules, and the manifest marks each series
  ``source: history`` so that is never hidden.
"""

from __future__ import annotations

import collections
import math
from dataclasses import dataclass
from typing import Optional

from ..features import FEATURE_NAMES
from .store import IntelStore

# Aim each series at roughly this many rows: enough for the bridge to engineer
# and walk-forward over, small enough that a thin market is not stretched into
# hundreds of empty buckets.
#
# 400 was too few once trading costs were charged honestly. A Polymarket round
# trip costs about 4% of price, so a target has to clear that, and a target
# that large is reached rarely: across 44 series the median strategy took SEVEN
# trades, against a validation floor of 30, and 99.8% of candidates were
# rejected for too few trades before any other gate was even reached. The tapes
# were not short - a median span of 69 hours, some of them 88 days - they were
# being compressed into 400 bars of ten minutes each, and everything that
# happened inside a bar was invisible to the search.
#
# Resolution is capped by trade density rather than fixed, so a thin tape is
# still not stretched into empty buckets: see `rows_for`.
TARGET_ROWS = 1600

# Bump this whenever construction changes in a way that could change WHICH
# tapes are admitted or what a row means. Cached pool verdicts stamped with an
# older recipe are re-tried once, which is what makes "rebuild the OOS pool
# after this change" automatic instead of a manual step somebody forgets. It
# is not a cache key for admitted series — those are immutable tapes and their
# CSVs stay valid; it only re-opens the *rejections*.
SERIES_BUILD_VERSION = 2

# THE SAFETY BACKSTOP, and nothing more.
#
# This was 60 seconds, and it was the single largest constraint on the research
# universe. A market that settled in eleven minutes with four thousand prints
# is not a thin market — it is one of the densest tapes in the store — but a
# 60-second floor gave it eleven rows and the builder discarded it for being
# short. The audit measured the cost: roughly twelve usable OOS series survived
# construction, so every validation gate downstream was being applied to a
# universe too small to clear it.
#
# What legitimately floors the bucket is the resolution of the clock. Tape
# timestamps are whole seconds, so two prints inside the same second cannot be
# ordered and a sub-second bucket would invent a distinction the data does not
# carry. One second is therefore the real limit, and wall-clock duration is no
# longer a requirement at all — trade density is (see `plan_series`).
MIN_BUCKET_SECONDS = 1

# Fewest trades a bucket may average before the series is given fewer rows. A
# bucket with no prints repeats the previous price, and a run of those is a
# flat stretch the search would read as real.
MIN_TRADES_PER_ROW = 3

# A tape below this is not a market whose bars are too coarse, it is a market
# with nothing in it. Rejected outright and counted, never stretched.
MIN_TOTAL_TRADES = 24

# Fewest rows worth building at all, independent of what the caller asks for.
# The caller's own `min_rows` is applied on top and is usually higher; this is
# the floor below which construction is not attempted.
MIN_USEFUL_ROWS = 24


def rows_for(trade_count: int, target: int = TARGET_ROWS) -> int:
    """How many buckets a tape of this size can honestly fill.

    Capped by density in BOTH directions. The old version floored the answer
    at 200 rows, which meant a 50-trade tape was handed 200 buckets: 150 of
    them empty, each repeating the previous price. That is a fabricated flat
    stretch, and a flat stretch is exactly what a threshold rule reads as a
    stable regime. Stretching is now impossible by construction — the answer
    can go to zero, and a tape that cannot fill `MIN_USEFUL_ROWS` is rejected
    with a reason rather than padded into one.
    """
    return max(0, min(target, trade_count // max(1, MIN_TRADES_PER_ROW)))


@dataclass(frozen=True)
class SeriesPlan:
    """How (and whether) one tape becomes a series, with the reason attached.

    Every rejection carries a machine-readable `reason` so the funnel can
    report *why* markets are not reaching validation instead of only how few
    do — the difference between "the pool is small" and "the pool is small
    because 4,000 markets have under 24 prints".
    """

    rows: int
    bucket_seconds: int
    admitted: bool
    reason: str


def plan_series(trade_count: int, span_seconds: float, min_rows: int = 0,
                target: int = TARGET_ROWS) -> SeriesPlan:
    """Trade-density-aware bucketing: resolution follows information, not time.

    The requirement a market must meet is *sufficient valid trade
    information*, expressed as a target of `MIN_TRADES_PER_ROW` prints per row
    and a minimum number of useful observations. Wall-clock duration is a
    backstop on bucket width, never an admission test — an eleven-minute
    market with four thousand prints carries more information than an
    eleven-day market with forty, and the old floor admitted the wrong one.

    Note what this does NOT do: no validation requirement moves. OOS trade
    counts, market breadth, expectancy, confidence, cost and drawdown gates
    are untouched. This widens the universe those gates are applied to.
    """
    trade_count = int(trade_count)
    if trade_count < MIN_TOTAL_TRADES:
        return SeriesPlan(0, MIN_BUCKET_SECONDS, False, "insufficient_trades")

    rows = rows_for(trade_count, target)
    floor = max(MIN_USEFUL_ROWS, int(min_rows))
    if rows < floor:
        return SeriesPlan(rows, MIN_BUCKET_SECONDS, False,
                          "insufficient_observations")

    span = max(1.0, float(span_seconds))
    bucket = max(MIN_BUCKET_SECONDS, int(math.ceil(span / rows)))
    return SeriesPlan(rows, bucket, True, "")


def _empty_row() -> dict:
    return {name: 0.0 for name in FEATURE_NAMES}


def _large_ratio(prints: list[dict], notional: float) -> float:
    """Share of the bucket's notional carried by prints over 3x its average —
    the same threshold and formula as the live tape's LargeTradeRatio."""
    if not prints or notional <= 0:
        return 0.0
    cut = 3.0 * (notional / len(prints))
    large = sum(float(p.get("usdc") or 0.0) for p in prints
                if float(p.get("usdc") or 0.0) > cut)
    return large / notional


def build_series(store: IntelStore, min_rows: int = 200,
                 max_tokens: int = 24,
                 only_tokens: Optional[set] = None,
                 stats: Optional[dict] = None) -> list[dict]:
    """Per-token feature series from settled markets' trade tapes.

    Returns entries of ``{"tokenId", "marketId", "outcome", "category",
    "series": [row, ...]}`` where each row carries ``ts`` plus every feature
    column. Only tokens whose market has a KNOWN resolution are used — the
    whole point of history is that we know how it ended.

    ``only_tokens`` restricts the build to a chosen batch — what the OOS
    pool cache uses to build INCREMENTALLY: settled series are immutable,
    so each token is ever built once and reused forever.

    ``stats``, when given, is filled with the admission census: how many
    settled markets were considered, how many were admitted, and how many
    were rejected for each distinct reason. Every rejection is accounted for,
    so "the OOS pool is small" can always be answered with *why*.
    """
    census = stats if stats is not None else {}
    census.setdefault("considered", 0)
    census.setdefault("admitted", 0)
    census.setdefault("rejected", 0)
    census.setdefault("rejectedBy", {})

    def _reject(reason: str) -> None:
        census["rejected"] += 1
        census["rejectedBy"][reason] = census["rejectedBy"].get(reason, 0) + 1

    resolutions = store.resolutions()
    census.setdefault("settledMarkets", len(resolutions))
    if not resolutions:
        return []

    # WALLET OUTCOME LEAKAGE — closed (§6).
    #
    # This used to be `store.load_scores()`: today's wallet ranking, applied to
    # trades from months ago. The comment defended it as asking "would
    # following the wallets we NOW rank highly have predicted these outcomes?"
    # — but those ranks are computed FROM how the markets settled, and the
    # settlements include the very markets being replayed. A rule discovered on
    # `wallet_weighted` was therefore reading, at every historical row, a
    # summary of what had not happened yet. It is the purest form of the
    # leak: a feature that looks predictive precisely because it already knows.
    #
    # Skill cannot be known before settlement, so no leak-free wallet SKILL
    # feature exists on a historical row. What does survive is wallet ACTIVITY
    # — who traded, which way, how much — which is a fact on the tape at the
    # moment it printed. Those columns are kept and are unaffected. The two
    # score-weighted columns are pinned to zero, which makes them constant,
    # which makes the feature-validity domain refuse any rule that depends on
    # them. The three mechanisms agree by construction.
    scores: dict = {}

    # The real settlement clock, from the source record — never the moment
    # this database wrote the row (§6). Absent for a market whose settlement
    # date was never published, and `hours_to_resolution` is then left at zero
    # rather than fabricated from the end of the tape.
    settlements = store.settlement_times()

    # token -> trades, keeping the market linkage.
    rows = store.query(
        "SELECT wallet, ts, market_id, token_id, side, price, size, usdc, "
        "question FROM wallet_trades WHERE token_id != '' ORDER BY ts")
    by_token: dict[str, list[dict]] = collections.defaultdict(list)
    for row in rows:
        token = str(row["token_id"])
        if token in resolutions:
            by_token[token].append(row)

    # Busiest tapes first: they make the richest series.
    ranked_tokens = sorted(by_token.items(), key=lambda kv: -len(kv[1]))

    out: list[dict] = []
    for token_id, trades in ranked_tokens:
        if len(out) >= max_tokens:
            break
        if only_tokens is not None and token_id not in only_tokens:
            continue
        census["considered"] += 1
        plan = plan_series(
            len(trades),
            int(trades[-1]["ts"]) - int(trades[0]["ts"]),
            min_rows)
        if not plan.admitted:
            _reject(plan.reason)
            continue
        series = _series_for(token_id, trades, scores,
                             settled_ts=settlements.get(token_id, (0.0, ""))[0])
        if len(series) < min_rows:
            # Planned enough rows, produced fewer: buckets that never saw a
            # priced print are dropped rather than carried forward, so a tape
            # clustered into a few seconds can still come up short.
            _reject("sparse_after_bucketing")
            continue
        census["admitted"] += 1
        first = trades[0]
        out.append({
            "tokenId": token_id,
            "marketId": str(first.get("market_id") or ""),
            "outcome": "",
            "category": "",
            "question": str(first.get("question") or ""),
            "series": series,
        })
    return out


def _series_for(token_id: str, trades: list[dict], scores: dict,
                settled_ts: float = 0.0) -> list[dict]:
    """Replay one token's tape into density-planned buckets of feature rows.

    `settled_ts` is the market's real settlement moment. When it is unknown,
    the countdown columns stay at zero: the end of the tape is NOT a
    substitute, because the tape's last print is only knowable once the tape
    is over, and a row that quietly encodes its own distance from an unknown
    future is the end-of-tape leak §6 asks to close.
    """
    if len(trades) < 8:
        return []
    first_ts, last_ts = int(trades[0]["ts"]), int(trades[-1]["ts"])
    span = max(1, last_ts - first_ts)
    plan = plan_series(len(trades), span)
    if not plan.admitted:
        return []
    bucket = plan.bucket_seconds

    # Best-effort non-print replay over the historical tape. Prints-only by
    # API limitation: no historical book depth exists, so the trade price
    # stands in for both quotes with zero sizes — degraded structural signal,
    # stated plainly to the operator. Full fidelity is live-capture-forward.
    nonprint = _nonprint_replayer(token_id)
    # The Market-State layer replayed over the SAME tape: identical
    # construction by construction — the ms_ scores, states and sequences a
    # live cycle would have computed, computed from the historical prints. The
    # operator's impulse framework becomes searchable on years of history.
    market_state = _market_state_replayer(token_id)

    series: list[dict] = []
    price = 0.0
    cum_notional = 0.0
    window_24h: collections.deque = collections.deque()   # (ts, usdc)
    index = 0
    ts = first_ts

    while ts <= last_ts:
        end = ts + bucket
        prints: list[dict] = []
        while index < len(trades) and int(trades[index]["ts"]) < end:
            prints.append(trades[index])
            index += 1

        row = _empty_row()
        buys = sells = 0
        notional = 0.0
        net = 0.0
        sizes: list[float] = []
        wallet_entries = wallet_exits = 0
        wallet_net = 0.0
        weighted = 0.0
        best_score = 0.0
        flow_wallets: set[str] = set()
        wallet_usdc: dict[str, float] = {}
        buy_notional = 0.0

        first_price = price
        for p in prints:
            nonprint.feed(p)
            market_state.feed(p)
            side_buy = str(p.get("side") or "BUY").upper() == "BUY"
            usdc = float(p.get("usdc") or 0.0)
            px = float(p.get("price") or 0.0)
            if px > 0:
                price = px
                if first_price <= 0:
                    first_price = px
            notional += usdc
            net += usdc if side_buy else -usdc
            sizes.append(float(p.get("size") or 0.0))
            buys += 1 if side_buy else 0
            sells += 0 if side_buy else 1
            wallet = str(p.get("wallet") or "")
            flow_wallets.add(wallet)
            wallet_usdc[wallet] = wallet_usdc.get(wallet, 0.0) + usdc
            if side_buy:
                buy_notional += usdc
            # Wallet ACTIVITY: a tape fact, true at the moment it printed, and
            # carrying no information about how the market ended. Counted for
            # every wallet, because restricting it to today's ranked cohort
            # would reintroduce the outcome leak through the filter.
            wallet_entries += 1 if side_buy else 0
            wallet_exits += 0 if side_buy else 1
            wallet_net += usdc if side_buy else -usdc
            # Wallet SKILL. `scores` is empty on the historical path, so these
            # stay zero — see the note in `build_series`. Left in place, and
            # reachable, because the LIVE path may legitimately pass scores
            # that were computed only from markets settled before the row.
            intel = scores.get(wallet)
            if intel is not None and intel.rank:
                weighted += intel.score * (usdc if side_buy else -usdc)
                best_score = max(best_score, intel.score)
            cum_notional += usdc
            window_24h.append((int(p["ts"]), usdc))

        cutoff = end - 86_400
        while window_24h and window_24h[0][0] < cutoff:
            window_24h.popleft()

        if price > 0:
            row.update({
                "price": price,
                # Book columns pinned to neutral constants — see module doc.
                "bid": price, "ask": price, "mid": price,
                "volume_24h": sum(u for _t, u in window_24h),
                "volume_total": cum_notional,
                "price_band": 1.0 - abs(price - 0.5) * 2.0,
                "outcome_count": 2.0,
                # Measured against the PUBLISHED settlement time, which is
                # knowable while the market is live. Was `last_ts - ts`: the
                # distance to the final print, which nobody could know until
                # the final print happened. Zero when no settlement time is
                # on record — an honest "unknown", and constant, so the
                # feature-validity domain refuses rules that depend on it.
                "hours_to_resolution": (max(0.0, (settled_ts - ts) / 3600.0)
                                        if settled_ts > 0 else 0.0),
                "is_active": 1.0,
                "tape_trades": float(len(prints)),
                "tape_notional": notional,
                "tape_buy_ratio": buys / len(prints) if prints else 0.0,
                "tape_avg_size": sum(sizes) / len(sizes) if sizes else 0.0,
                "tape_price_drift": ((price - first_price) / first_price
                                     if first_price > 0 else 0.0),
                "wallet_entries": float(wallet_entries),
                "wallet_exits": float(wallet_exits),
                "wallet_net_usdc": wallet_net,
                "wallet_weighted": weighted,
                "wallet_best_score": best_score,
                "flow_net_usdc": net,
                "flow_wallets": float(len(flow_wallets)),
                # -- the operator's dataset schema, same formulas as live ----
                # Age is backward-looking and legitimate: at any row we know
                # how long the market has been trading.
                "market_age_hours": max(0.0, (ts - first_ts) / 3600.0),
                # Lifecycle percentage was `(ts - first_ts) / span`, and
                # `span` is the tape's EVENTUAL length. A row two hours in
                # therefore carried "you are 12% through" — a statement that
                # cannot be made without knowing how long the market will run.
                # Against a published settlement time it is a real quantity;
                # without one it is zero, not guessed.
                "lifecycle_pct": (
                    min(1.0, max(0.0, (ts - first_ts)
                                 / max(1.0, settled_ts - first_ts)))
                    if settled_ts > first_ts else 0.0),
                "tape_buy_volume": buy_notional,
                "tape_sell_volume": notional - buy_notional,
                "tape_large_ratio": _large_ratio(prints, notional),
                "tape_velocity": (((price - first_price) / first_price)
                                  * 3600.0 / bucket
                                  if first_price > 0 else 0.0),
                "tape_trade_rate": len(prints) * 60.0 / bucket,
                "wallet_count_tape": float(len(wallet_usdc)),
                "wallet_concentration": (max(wallet_usdc.values())
                                         / sum(wallet_usdc.values())
                                         if wallet_usdc
                                         and sum(wallet_usdc.values()) > 0
                                         else 0.0),
            })
            row.update(market_state.snapshot(float(end)))
            row.update(nonprint.snapshot(float(end)))
            row["ts"] = float(ts)
            series.append(row)
        ts = end
    return series


def _market_state_replayer(token_id: str):
    """The live MarketStateTracker, fed the historical tape.

    Identical construction is not an aspiration here — it is literal: the same
    class that scores live markets scores the history, so ms_impulse on a 2024
    row means exactly what ms_impulse means at runtime today.
    """
    try:
        from ..bridge.market_state import MarketStateTracker
        from ..config import MarketStateConfig
        tracker = MarketStateTracker(MarketStateConfig())
    except Exception:                                     # noqa: BLE001
        return _NullReplayer()

    class _Replayer:
        def feed(self, p: dict) -> None:
            px = float(p.get("price") or 0.0)
            if px <= 0:
                return
            tracker.feed_trade(
                token_id, ts=float(p.get("ts") or 0.0), price=px,
                notional=float(p.get("usdc") or 0.0),
                is_buy=str(p.get("side") or "BUY").upper() == "BUY")

        def snapshot(self, ts: float) -> dict:
            return tracker.snapshot(token_id, ts)

    return _Replayer()


class _NullReplayer:
    def feed(self, print_row: dict) -> None:
        pass

    def snapshot(self, ts: float) -> dict:
        return {}


def _nonprint_replayer(token_id: str):
    """A per-token non-print engine for historical replay, or a null object.

    Kept behind a factory so a missing qc_lean_bridge costs nothing but the
    columns — the historical series still builds exactly as before.
    """
    try:
        from ..bridge.nonprint_feed import NonPrintFeed
        feed = NonPrintFeed()
        if not feed.available:
            return _NullReplayer()
    except Exception:                                     # noqa: BLE001
        return _NullReplayer()

    class _Replayer:
        def feed(self, p: dict) -> None:
            px = float(p.get("price") or 0.0)
            if px <= 0:
                return
            feed.feed_trade(
                token_id, ts=float(p.get("ts") or 0.0), price=px,
                bid=px, ask=px, bid_size=0.0, ask_size=0.0,
                tick_size=0.01)

        def snapshot(self, ts: float) -> dict:
            return feed.snapshot(token_id, ts)

    return _Replayer()
