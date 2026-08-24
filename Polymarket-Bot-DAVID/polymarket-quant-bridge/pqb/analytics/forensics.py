"""WHY IS THE ACCOUNT LOSING MONEY? — forensics before intervention.

`attribution.py` already answers "where does the money go" in buckets. This
module answers the harder question the operator actually asked: of the loss
that has happened, **which part was the validated strategy's risk being
expressed, and which part was that strategy being monetised badly**?

Those are different failures with opposite fixes, and the difference cannot be
seen in the P&L column. It is only visible by asking, trade by trade, what
happened AFTER we got out — which is what the counterfactual half of this
module does.

Three disciplines are load-bearing and are enforced in code rather than in
this docstring:

* **Counterfactual results never become evidence.** They live in their own
  keys, they are never summed into realised P&L, and no function here writes
  to the journal. `report()` opens every database `mode=ro`. What actually
  happened and what would have happened are different kinds of fact, and a
  system that mixes them has learned to trade on hindsight.
* **A finding is a hypothesis, not a change.** The output is a list of
  proposed policy versions with the evidence behind each and the test each
  would have to pass. Nothing here can alter an exit rule, a stop, a size or a
  strategy. `no change recommended` is a first-class result and is returned
  whenever the sample cannot support one.
* **The sample size travels with every number.** A bucket of three trades is
  an anecdote. Every rate, expectancy and ratio in this report is emitted next
  to the count behind it, and the hypothesis generator refuses to speak below
  a floor.

The one thing this module must never learn is *"the account is down, therefore
reduce whatever is currently losing"*. That is not analysis, it is curve
fitting against the only 129 trades in existence — and it destroys the few
large winners that a positive-expectancy strategy depends on. Hence
:func:`upside_capture` and the explicit winner-preservation check on every
proposal.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# Post-exit horizons, in seconds. Only those the data can actually support are
# reported; the rest come back as NOT AVAILABLE with the reason, rather than
# quietly shrinking the denominator.
HORIZONS: tuple[tuple[str, float], ...] = (
    ("+1m", 60.0), ("+5m", 300.0), ("+15m", 900.0), ("+30m", 1_800.0),
    ("+60m", 3_600.0), ("+3h", 10_800.0),
)

# How far from the requested horizon a captured snapshot may sit and still be
# used. The research capture runs at 60s, so a tolerance under that would
# discard almost every horizon; one over ~4 minutes would answer a different
# question from the one asked.
HORIZON_TOLERANCE_S = 240.0

# Below this many closed trades in a bucket, the bucket is described but never
# used to justify a proposed change.
MIN_BUCKET_FOR_A_CLAIM = 12

# ...and below this in total, the whole module declines to propose anything.
MIN_TRADES_FOR_ANY_CLAIM = 40

# NOTE on vocabulary: the mechanism that closed a position is recorded as
# `lifecycles.exit_style` (stop | edge_gone | wallet | take_profit | trailing |
# resolution | time | time_decay | stagnant | reduce | doubling | kill_switch |
# reconciled); `exit_reason` carries the engine's sentence. Everything here
# groups on the STYLE, and no list of styles is hard-coded — a style this build
# has never seen still gets its own bucket rather than vanishing into "other".


def _connect_ro(path: str | Path) -> Optional[sqlite3.Connection]:
    path = Path(path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _rows(conn: Optional[sqlite3.Connection], sql: str,
          params: tuple = ()) -> list[dict]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# 1. Reconstruction
# ---------------------------------------------------------------------------


@dataclass
class TradeRecord:
    """One completed trade, reconstructed from everything that touched it.

    `unavailable` is not decoration. The operator asked which fields the
    history cannot support, and the honest answer differs per trade: a
    position opened before slippage was journalled has no slippage, and
    reporting 0.0 for it would put a real number and a missing one in the same
    column. Anything listed here is excluded from the aggregates that would
    otherwise average it as a zero.
    """

    lifecycle_id: int = 0
    token_id: str = ""
    market_id: str = ""
    outcome: str = ""
    question: str = ""
    category: str = ""
    entry_ts: float = 0.0
    entry_price: float = 0.0
    entry_size: float = 0.0
    entry_cost: float = 0.0
    exit_ts: float = 0.0
    exit_price: float = 0.0
    exit_size: float = 0.0
    exit_style: str = ""
    exit_reason: str = ""
    realized_pnl: float = 0.0
    return_pct: float = 0.0
    hold_seconds: float = 0.0
    peak_price: float = 0.0
    trough_price: float = 0.0
    max_unrealized: float = 0.0
    min_unrealized: float = 0.0
    liquidity_bucket: str = ""
    ttr_bucket: str = ""
    wallet_influence: str = ""
    mode: str = ""
    # execution
    fees: float = 0.0
    slippage: float = 0.0
    fills: int = 0
    unfilled: int = 0
    # the decision that opened it
    entry_score: float = 0.0
    entry_confidence: float = 0.0
    entry_reason: str = ""
    entry_features: dict = field(default_factory=dict)
    # portfolio context at entry
    equity_at_entry: float = 0.0
    cash_at_entry: float = 0.0
    open_positions_at_entry: int = 0
    unavailable: list = field(default_factory=list)

    @property
    def gross_pnl(self) -> float:
        """Realised P&L before our own costs. `realized_pnl` is net."""
        return self.realized_pnl + self.fees

    @property
    def is_win(self) -> bool:
        return self.realized_pnl > 0

    @property
    def size_pct_of_equity(self) -> float:
        return (self.entry_cost / self.equity_at_entry) \
            if self.equity_at_entry > 0 else 0.0

    @property
    def mfe(self) -> float:
        """Maximum FAVOURABLE excursion, as a return on entry price."""
        if self.entry_price <= 0 or self.peak_price <= 0:
            return 0.0
        return self.peak_price / self.entry_price - 1.0

    @property
    def mae(self) -> float:
        """Maximum ADVERSE excursion, as a (negative) return on entry."""
        if self.entry_price <= 0 or self.trough_price <= 0:
            return 0.0
        return self.trough_price / self.entry_price - 1.0

    def to_dict(self) -> dict:
        return {
            "id": self.lifecycle_id, "token": self.token_id,
            "market": self.market_id, "outcome": self.outcome,
            "category": self.category, "exitStyle": self.exit_style,
            "entryTs": self.entry_ts, "exitTs": self.exit_ts,
            "entryPrice": round(self.entry_price, 6),
            "exitPrice": round(self.exit_price, 6),
            "size": round(self.entry_size, 4),
            "cost": round(self.entry_cost, 4),
            "netPnl": round(self.realized_pnl, 6),
            "grossPnl": round(self.gross_pnl, 6),
            "fees": round(self.fees, 6),
            "slippage": round(self.slippage, 6),
            "returnPct": round(self.return_pct, 6),
            "holdSeconds": round(self.hold_seconds, 1),
            "mae": round(self.mae, 6), "mfe": round(self.mfe, 6),
            "sizePctOfEquity": round(self.size_pct_of_equity, 6),
            "openPositionsAtEntry": self.open_positions_at_entry,
            "walletInfluence": self.wallet_influence,
            "unavailable": list(self.unavailable),
        }


def reconstruct(journal_path: str | Path) -> list[TradeRecord]:
    """Every closed trade, with everything the journal can say about it.

    Three tables and one join each: `lifecycles` is the spine, `executions`
    carries what the fills actually cost, and `decisions` carries what the
    engine believed at the moment it entered. Missing pieces are NAMED rather
    than defaulted, because a zero fee and an unrecorded fee lead to opposite
    conclusions about cost drag.
    """
    conn = _connect_ro(journal_path)
    if conn is None:
        return []
    try:
        closed = _rows(conn, "SELECT * FROM lifecycles WHERE status='CLOSED' "
                             "ORDER BY entry_ts")
        fills = _rows(conn, "SELECT * FROM executions")
        decisions = _rows(conn, "SELECT id, score, confidence, reason, "
                                "features FROM decisions")
        cycles = _rows(conn, "SELECT ts, portfolio_value, balance, positions "
                             "FROM cycles ORDER BY ts")
    finally:
        conn.close()

    fills_by_life: dict[int, list[dict]] = {}
    for fill in fills:
        fills_by_life.setdefault(int(fill.get("lifecycle_id") or 0),
                                 []).append(fill)
    decision_by_id = {int(d["id"]): d for d in decisions}

    out: list[TradeRecord] = []
    for row in closed:
        record = TradeRecord(
            lifecycle_id=int(row.get("id") or 0),
            token_id=str(row.get("token_id") or ""),
            market_id=str(row.get("market_id") or ""),
            outcome=str(row.get("outcome") or ""),
            question=str(row.get("question") or ""),
            category=str(row.get("category") or ""),
            entry_ts=float(row.get("entry_ts") or 0.0),
            entry_price=float(row.get("entry_price") or 0.0),
            entry_size=float(row.get("entry_size") or 0.0),
            entry_cost=float(row.get("entry_cost") or 0.0),
            exit_ts=float(row.get("exit_ts") or 0.0),
            exit_price=float(row.get("exit_price") or 0.0),
            exit_size=float(row.get("exit_size") or 0.0),
            exit_style=str(row.get("exit_style") or "") or "unknown",
            exit_reason=str(row.get("exit_reason") or ""),
            realized_pnl=float(row.get("realized_pnl") or 0.0),
            return_pct=float(row.get("return_pct") or 0.0),
            hold_seconds=float(row.get("hold_seconds") or 0.0),
            peak_price=float(row.get("peak_price") or 0.0),
            trough_price=float(row.get("trough_price") or 0.0),
            max_unrealized=float(row.get("max_unrealized") or 0.0),
            min_unrealized=float(row.get("min_unrealized") or 0.0),
            liquidity_bucket=str(row.get("liquidity_bucket") or ""),
            ttr_bucket=str(row.get("ttr_bucket") or ""),
            wallet_influence=str(row.get("wallet_influence") or ""),
            mode=str(row.get("mode") or ""))

        legs = fills_by_life.get(record.lifecycle_id) or []
        if not legs:
            record.unavailable.append("execution_detail")
        for leg in legs:
            record.fees += float(leg.get("fee") or 0.0)
            got = float(leg.get("filled_size") or 0.0)
            if got <= 0:
                record.unfilled += 1
                continue
            record.fills += 1
            limit = float(leg.get("limit_price") or 0.0)
            avg = float(leg.get("avg_price") or 0.0)
            if limit > 0 and avg > 0:
                side = str(leg.get("side") or "BUY").upper()
                worse = (avg - limit) if side.startswith("B") else (limit - avg)
                if worse > 0:
                    record.slippage += worse * got
        if legs and not any(float(l.get("fee") or 0.0) for l in legs):
            # Polymarket publishes no per-fill fee today. The field exists and
            # is journalled, so "0.0" here is genuinely 'not reported by the
            # venue' rather than 'free' — and cost analysis must say which.
            record.unavailable.append("venue_fees")

        decision = decision_by_id.get(int(row.get("entry_decision_id") or 0))
        if decision is None:
            record.unavailable.append("entry_decision")
        else:
            record.entry_score = float(decision.get("score") or 0.0)
            record.entry_confidence = float(decision.get("confidence") or 0.0)
            record.entry_reason = str(decision.get("reason") or "")
            try:
                record.entry_features = json.loads(
                    decision.get("features") or "{}") or {}
            except (TypeError, ValueError):
                record.entry_features = {}
        if not record.entry_features:
            record.unavailable.append("entry_features")

        cycle = _cycle_at(cycles, record.entry_ts)
        if cycle is None:
            record.unavailable.append("portfolio_context")
        else:
            record.equity_at_entry = float(cycle.get("portfolio_value") or 0.0)
            record.cash_at_entry = float(cycle.get("balance") or 0.0)
            record.open_positions_at_entry = int(cycle.get("positions") or 0)
        if record.peak_price <= 0 or record.trough_price <= 0:
            record.unavailable.append("excursions")
        out.append(record)
    return out


def _cycle_at(cycles: list[dict], ts: float) -> Optional[dict]:
    """The last cycle snapshot at or before ``ts``. Linear scan is fine: the
    caller does this once per trade over a table with one row per cycle."""
    if not cycles or ts <= 0:
        return None
    found = None
    for cycle in cycles:
        if float(cycle.get("ts") or 0.0) <= ts:
            found = cycle
        else:
            break
    return found


# ---------------------------------------------------------------------------
# 2. Bucketed economics
# ---------------------------------------------------------------------------


def _stats(trades: list[TradeRecord]) -> dict:
    """The honest per-bucket economics. Sample size is never optional."""
    n = len(trades)
    if not n:
        return {"trades": 0}
    pnls = [t.realized_pnl for t in trades]
    returns = [t.return_pct for t in trades]
    holds = [t.hold_seconds for t in trades if t.hold_seconds > 0]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": n,
        "winners": len(wins),
        "losers": len(losses),
        "winRate": round(len(wins) / n, 4),
        "netPnl": round(sum(pnls), 4),
        "grossPnl": round(sum(t.gross_pnl for t in trades), 4),
        "fees": round(sum(t.fees for t in trades), 4),
        "expectancy": round(sum(pnls) / n, 6),
        "avgReturn": round(sum(returns) / n, 6),
        "medianReturn": round(statistics.median(returns), 6),
        "avgWinner": round(sum(wins) / len(wins), 6) if wins else 0.0,
        "avgLoser": round(sum(losses) / len(losses), 6) if losses else 0.0,
        "largestWinner": round(max(pnls), 4),
        "largestLoser": round(min(pnls), 4),
        # Profit factor is meaningless without both sides present, so it is
        # omitted rather than reported as infinity or zero.
        "profitFactor": (round(gross_win / gross_loss, 4)
                         if gross_win > 0 and gross_loss > 0 else None),
        "avgHoldSeconds": round(sum(holds) / len(holds), 1) if holds else 0.0,
        "medianHoldSeconds": (round(statistics.median(holds), 1)
                              if holds else 0.0),
        "claimable": n >= MIN_BUCKET_FOR_A_CLAIM,
    }


def _by(trades: list[TradeRecord], key_of) -> dict[str, dict]:
    buckets: dict[str, list[TradeRecord]] = {}
    for trade in trades:
        key = key_of(trade)
        if key is None or key == "":
            continue
        buckets.setdefault(str(key), []).append(trade)
    return {key: _stats(rows) for key, rows in buckets.items()}


def hold_bucket(trade: TradeRecord) -> str:
    seconds = trade.hold_seconds
    if seconds <= 0:
        return ""
    if seconds < 300:
        return "very-short (<5m)"
    if seconds < 1_800:
        return "short (5-30m)"
    if seconds < 7_200:
        return "medium (30m-2h)"
    if seconds < 86_400:
        return "long (2-24h)"
    return "settlement-oriented (>24h)"


def size_bucket(trade: TradeRecord) -> str:
    share = trade.size_pct_of_equity
    if share <= 0:
        return ""
    if share < 0.05:
        return "under-5% of equity"
    if share < 0.10:
        return "5-10%"
    if share < 0.20:
        return "10-20%"
    if share < 0.35:
        return "20-35%"
    return "over-35%"


def exit_attribution(trades: list[TradeRecord]) -> dict:
    """§4. Which exit mechanism is destroying value, and which is saving it.

    Contribution shares are computed against the total LOSS and total PROFIT
    separately rather than against net P&L. Against a negative net, a share
    would carry a sign that reverses the ranking — the biggest loser would
    read as the biggest contributor to profit.
    """
    by_style = _by(trades, lambda t: t.exit_style)
    total_loss = -sum(min(0.0, t.realized_pnl) for t in trades)
    total_profit = sum(max(0.0, t.realized_pnl) for t in trades)
    for style, stats in by_style.items():
        rows = [t for t in trades if t.exit_style == style]
        loss = -sum(min(0.0, t.realized_pnl) for t in rows)
        profit = sum(max(0.0, t.realized_pnl) for t in rows)
        stats["shareOfTrades"] = round(len(rows) / len(trades), 4) \
            if trades else 0.0
        stats["shareOfTotalLoss"] = round(loss / total_loss, 4) \
            if total_loss > 0 else 0.0
        stats["shareOfTotalProfit"] = round(profit / total_profit, 4) \
            if total_profit > 0 else 0.0
    ranked_cost = sorted(by_style.items(),
                         key=lambda kv: -kv[1].get("shareOfTotalLoss", 0.0))
    ranked_value = sorted(by_style.items(),
                          key=lambda kv: -kv[1].get("shareOfTotalProfit", 0.0))
    return {
        "byExitStyle": by_style,
        "destroyingMostValue": ranked_cost[0][0] if ranked_cost else "",
        "preservingMostValue": ranked_value[0][0] if ranked_value else "",
        "totalLoss": round(total_loss, 4),
        "totalProfit": round(total_profit, 4),
    }


# ---------------------------------------------------------------------------
# 3. Counterfactuals — research only, never evidence
# ---------------------------------------------------------------------------


class PriceHistory:
    """Post-exit prices, from the capture the research layer already keeps.

    `research_rows` is the only record of what a token was worth at a moment
    that has passed — an order book that existed for four seconds three weeks
    ago is gone — so it is also the only possible basis for "what would
    holding have done". Settlement comes from `resolutions`, which is ground
    truth rather than a mark.
    """

    def __init__(self, intel_path: str | Path):
        self._conn = _connect_ro(intel_path)
        self._cache: dict[str, list[tuple[float, float]]] = {}
        self._settled: Optional[dict[str, tuple[float, float]]] = None

    @property
    def available(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def series(self, token_id: str) -> list[tuple[float, float]]:
        """``[(ts, price), ...]`` for one token, ascending. Cached per token."""
        if token_id in self._cache:
            return self._cache[token_id]
        rows = _rows(self._conn,
                     "SELECT ts, features FROM research_rows "
                     "WHERE token_id=? ORDER BY ts", (token_id,))
        series: list[tuple[float, float]] = []
        for row in rows:
            try:
                features = json.loads(row.get("features") or "{}")
            except (TypeError, ValueError):
                continue
            # The exit side of a long is the BID. Marking a counterfactual
            # exit at the mid would credit half a spread per trade that no
            # seller could ever have collected — small per trade, and exactly
            # the size of the effect being measured.
            price = (_positive(features.get("bid"))
                     or _positive(features.get("price"))
                     or _positive(features.get("mid")))
            if price:
                series.append((float(row.get("ts") or 0.0), price))
        self._cache[token_id] = series
        return series

    def price_at(self, token_id: str, ts: float,
                 tolerance: float = HORIZON_TOLERANCE_S) -> Optional[float]:
        """The captured price nearest ``ts``, or None if nothing is close."""
        series = self.series(token_id)
        if not series:
            return None
        best, best_gap = None, None
        for stamp, price in series:
            gap = abs(stamp - ts)
            if best_gap is None or gap < best_gap:
                best, best_gap = price, gap
            if stamp > ts + tolerance:
                break
        if best_gap is None or best_gap > tolerance:
            return None
        return best

    def settlement(self, token_id: str) -> Optional[float]:
        if self._settled is None:
            self._settled = {}
            for row in _rows(self._conn,
                             "SELECT token_id, price, settled_ts FROM "
                             "resolutions"):
                self._settled[str(row.get("token_id"))] = (
                    float(row.get("price") or 0.0),
                    float(row.get("settled_ts") or 0.0))
        found = self._settled.get(str(token_id))
        return found[0] if found is not None else None


def _positive(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if number > 0 else 0.0


def counterfactuals(trades: list[TradeRecord], history: PriceHistory,
                    horizons: tuple = HORIZONS) -> dict:
    """§5. What happened AFTER each exit, at every horizon the data supports.

    The output deliberately never touches `realized_pnl`. It answers one
    question — would this position have been worth more or less had we kept
    it — and it answers it only where a captured price actually exists. A
    horizon with no snapshot is reported as NOT AVAILABLE with its count, not
    dropped from the denominator, because "we could not ask" and "we asked and
    it made no difference" are opposite findings.
    """
    if not history.available:
        return {"available": False,
                "reason": "no captured price history to compare against"}

    per_horizon: dict[str, dict] = {}
    for label, offset in horizons:
        better = worse = same = 0
        delta_sum = 0.0
        missing = 0
        deltas: list[float] = []
        for trade in trades:
            if trade.exit_ts <= 0 or trade.entry_size <= 0:
                missing += 1
                continue
            price = history.price_at(trade.token_id, trade.exit_ts + offset)
            if price is None:
                missing += 1
                continue
            # What the SAME position would have realised at that price. Fees
            # are unchanged: the exit still happens, just later.
            hypothetical = (price - trade.entry_price) * trade.entry_size \
                - trade.fees
            delta = hypothetical - trade.realized_pnl
            deltas.append(delta)
            delta_sum += delta
            if delta > 1e-9:
                better += 1
            elif delta < -1e-9:
                worse += 1
            else:
                same += 1
        answered = better + worse + same
        per_horizon[label] = {
            "answered": answered,
            "notAvailable": missing,
            "holdingWouldHaveBeenBetter": better,
            "holdingWouldHaveBeenWorse": worse,
            "unchanged": same,
            "totalDelta": round(delta_sum, 4),
            "meanDelta": round(delta_sum / answered, 6) if answered else 0.0,
            "medianDelta": (round(statistics.median(deltas), 6)
                            if deltas else 0.0),
            "claimable": answered >= MIN_BUCKET_FOR_A_CLAIM,
        }

    # Settlement is the one horizon that is ground truth rather than a mark.
    settled_better = settled_worse = 0
    settled_delta = 0.0
    settled_answered = 0
    for trade in trades:
        price = history.settlement(trade.token_id)
        if price is None or trade.entry_size <= 0:
            continue
        hypothetical = (price - trade.entry_price) * trade.entry_size \
            - trade.fees
        delta = hypothetical - trade.realized_pnl
        settled_answered += 1
        settled_delta += delta
        if delta > 0:
            settled_better += 1
        elif delta < 0:
            settled_worse += 1
    per_horizon["settlement"] = {
        "answered": settled_answered,
        "notAvailable": len(trades) - settled_answered,
        "holdingWouldHaveBeenBetter": settled_better,
        "holdingWouldHaveBeenWorse": settled_worse,
        "unchanged": 0,
        "totalDelta": round(settled_delta, 4),
        "meanDelta": (round(settled_delta / settled_answered, 6)
                      if settled_answered else 0.0),
        "medianDelta": 0.0,
        "claimable": settled_answered >= MIN_BUCKET_FOR_A_CLAIM,
    }

    by_style = _counterfactual_by_style(trades, history)
    return {"available": True, "byHorizon": per_horizon,
            "byExitStyle": by_style,
            "note": "Counterfactual only. Never summed into realised P&L, "
                    "never used as validation evidence, never used to "
                    "approve a rule on its own."}


def _counterfactual_by_style(trades: list[TradeRecord],
                             history: PriceHistory) -> dict:
    """Was each exit MECHANISM systematically premature, or protective?

    Uses one mid-range horizon rather than all of them, because the question
    per mechanism is directional ("does this exit tend to leave money on the
    table") and six horizons per style produces a table nobody reads.
    """
    out: dict[str, dict] = {}
    styles = {t.exit_style for t in trades}
    for style in styles:
        rows = [t for t in trades if t.exit_style == style]
        better = worse = 0
        delta = 0.0
        answered = 0
        for trade in rows:
            price = history.price_at(trade.token_id, trade.exit_ts + 1_800.0)
            if price is None or trade.entry_size <= 0:
                continue
            hypothetical = (price - trade.entry_price) * trade.entry_size \
                - trade.fees
            step = hypothetical - trade.realized_pnl
            answered += 1
            delta += step
            better += 1 if step > 0 else 0
            worse += 1 if step < 0 else 0
        out[style] = {
            "answered": answered, "of": len(rows),
            "exitedTooEarly": better, "exitProtectedCapital": worse,
            "totalDelta": round(delta, 4),
            "meanDelta": round(delta / answered, 6) if answered else 0.0,
            "claimable": answered >= MIN_BUCKET_FOR_A_CLAIM,
            # The verdict is deliberately a WORD, and deliberately hedged
            # until the sample supports it. "premature" here means the market
            # kept moving our way for 30 minutes — not that the exit was wrong.
            "reading": _exit_reading(answered, better, worse, delta),
        }
    return out


def _exit_reading(answered: int, better: int, worse: int,
                  delta: float) -> str:
    if answered < MIN_BUCKET_FOR_A_CLAIM:
        return (f"not enough post-exit data yet ({answered} trade(s) "
                f"answered); no reading")
    if better > worse * 1.5 and delta > 0:
        return ("looks systematically EARLY: holding 30 minutes longer would "
                f"have helped in {better} of {answered} cases "
                f"({delta:+.2f} total). A hypothesis, not a fix.")
    if worse > better * 1.5 and delta < 0:
        return ("looks PROTECTIVE: holding longer would have hurt in "
                f"{worse} of {answered} cases ({delta:+.2f} total). This exit "
                "is doing valuable work.")
    return (f"no clear direction over {answered} trade(s) "
            f"({better} better / {worse} worse, {delta:+.2f} total)")


def upside_capture(trades: list[TradeRecord]) -> dict:
    """§11. How much of the favourable movement we actually banked.

    The guard against "improving" the equity curve by cutting winners. Realised
    return over the best return the position ever showed: a rule that reduces
    drawdown while pushing this DOWN has not improved anything, it has moved
    the loss somewhere the report does not look.
    """
    usable = [t for t in trades if t.mfe > 0 and t.entry_price > 0]
    if not usable:
        return {"available": False, "reason": "no excursion data captured"}
    captured = sum(t.return_pct for t in usable)
    available = sum(t.mfe for t in usable)
    winners = sorted(trades, key=lambda t: -t.realized_pnl)[:5]
    total_profit = sum(max(0.0, t.realized_pnl) for t in trades)
    top_share = (sum(max(0.0, t.realized_pnl) for t in winners) / total_profit
                 if total_profit > 0 else 0.0)
    return {
        "available": True,
        "sample": len(usable),
        "capturedReturn": round(captured, 4),
        "availableReturn": round(available, 4),
        "upsideCaptureRatio": round(captured / available, 4)
        if available else 0.0,
        # The tail. If five trades are most of the profit, any change that
        # touches them is a change to the strategy's whole payoff.
        "topFiveWinnerShareOfProfit": round(top_share, 4),
        "topFiveWinners": [t.to_dict() for t in winners],
    }


# ---------------------------------------------------------------------------
# 4. Concentration, clustering and correlated exposure
# ---------------------------------------------------------------------------


def loss_clusters(trades: list[TradeRecord]) -> dict:
    """§12. Are the losses independent, or do they arrive together?

    The distinction changes what money management can do. Many small
    independent losses are the strategy's own variance and sizing is the only
    lever. Clustered losses are a condition — a market, a category, a regime,
    a run — and a condition can be filtered.
    """
    ordered = sorted(trades, key=lambda t: t.exit_ts)
    streak = worst_streak = 0
    streak_pnl = worst_streak_pnl = 0.0
    for trade in ordered:
        if trade.realized_pnl < 0:
            streak += 1
            streak_pnl += trade.realized_pnl
            if streak > worst_streak:
                worst_streak, worst_streak_pnl = streak, streak_pnl
        else:
            streak, streak_pnl = 0, 0.0

    by_market = _by(trades, lambda t: t.market_id or t.token_id)
    losers = sorted(((k, v) for k, v in by_market.items()
                     if v.get("netPnl", 0.0) < 0),
                    key=lambda kv: kv[1]["netPnl"])
    total_loss = -sum(min(0.0, t.realized_pnl) for t in trades)
    concentration = 0.0
    if total_loss > 0 and losers:
        concentration = -losers[0][1]["netPnl"] / total_loss

    return {
        "longestLosingStreak": worst_streak,
        "longestLosingStreakPnl": round(worst_streak_pnl, 4),
        "worstMarkets": [{"market": k, **v} for k, v in losers[:5]],
        "worstMarketShareOfLoss": round(concentration, 4),
        "byCategory": _by(trades, lambda t: t.category),
        "byLiquidity": _by(trades, lambda t: t.liquidity_bucket),
        "byTimeToResolution": _by(trades, lambda t: t.ttr_bucket),
        "byOpenPositionsAtEntry": _by(
            trades, lambda t: (f"{t.open_positions_at_entry} open"
                               if t.open_positions_at_entry else "")),
        "verdict": (
            "loss is CONCENTRATED — one market carries "
            f"{concentration:.0%} of it; a condition, not variance"
            if concentration > 0.35 else
            "loss is DISPERSED across markets — this looks like the "
            "strategy's own variance, and sizing is the lever, not filtering"),
    }


def correlated_exposure(trades: list[TradeRecord]) -> dict:
    """§13. Four open positions are not necessarily four units of risk.

    Computed over the closed record by overlap: two positions whose holding
    windows intersect were live at the same time, and if they also share a
    market, a category or a wallet thesis they were one bet held twice.
    """
    ordered = sorted(trades, key=lambda t: t.entry_ts)
    overlaps = 0
    correlated = 0
    worst: dict = {}
    for i, a in enumerate(ordered):
        for b in ordered[i + 1:]:
            if b.entry_ts >= a.exit_ts:
                break                      # ordered by entry: no later overlap
            overlaps += 1
            shared = []
            if a.market_id and a.market_id == b.market_id:
                shared.append("market")
            if a.category and a.category == b.category:
                shared.append("category")
            if a.wallet_influence and a.wallet_influence == b.wallet_influence:
                shared.append("wallet thesis")
            if shared:
                correlated += 1
                joint = a.realized_pnl + b.realized_pnl
                if joint < worst.get("jointPnl", 0.0):
                    worst = {"a": a.outcome[:24], "b": b.outcome[:24],
                             "shared": shared, "jointPnl": round(joint, 4)}
    return {
        "simultaneousPairs": overlaps,
        "correlatedPairs": correlated,
        "correlatedShare": round(correlated / overlaps, 4) if overlaps else 0.0,
        "worstCorrelatedPair": worst,
        "reading": (
            f"{correlated} of {overlaps} simultaneous pairs shared a market, "
            "category or wallet thesis — effective risk was higher than the "
            "position count suggested"
            if overlaps and correlated / overlaps > 0.3 else
            "simultaneous positions were mostly unrelated"),
    }


def sizing_forensics(trades: list[TradeRecord]) -> dict:
    """§9. Is this strategy losing, or is it losing LOUDLY?

    The test that separates them: compare expectancy per trade with expectancy
    per dollar risked. If big positions lose no more per dollar than small
    ones, sizing is not the problem and shrinking everything would only shrink
    the winners too.
    """
    sized = [t for t in trades if t.size_pct_of_equity > 0]
    if not sized:
        return {"available": False, "reason": "no portfolio context recorded"}
    by_size = _by(sized, size_bucket)
    per_dollar = []
    for trade in sized:
        if trade.entry_cost > 0:
            per_dollar.append(trade.realized_pnl / trade.entry_cost)
    big = [t for t in sized if t.size_pct_of_equity >= 0.20]
    small = [t for t in sized if t.size_pct_of_equity < 0.20]
    big_rate = (sum(t.realized_pnl / t.entry_cost for t in big
                    if t.entry_cost > 0) / len(big)) if big else None
    small_rate = (sum(t.realized_pnl / t.entry_cost for t in small
                      if t.entry_cost > 0) / len(small)) if small else None
    amplifying = (big_rate is not None and small_rate is not None
                  and len(big) >= MIN_BUCKET_FOR_A_CLAIM
                  and len(small) >= MIN_BUCKET_FOR_A_CLAIM
                  and big_rate < small_rate - 0.02)
    return {
        "available": True,
        "sample": len(sized),
        "bySizeBucket": by_size,
        "meanSizePctOfEquity": round(
            sum(t.size_pct_of_equity for t in sized) / len(sized), 4),
        "maxSizePctOfEquity": round(
            max(t.size_pct_of_equity for t in sized), 4),
        "returnPerDollarRisked": round(
            sum(per_dollar) / len(per_dollar), 6) if per_dollar else 0.0,
        "largePositionReturnPerDollar": (round(big_rate, 6)
                                         if big_rate is not None else None),
        "smallPositionReturnPerDollar": (round(small_rate, 6)
                                         if small_rate is not None else None),
        "sizingIsAmplifying": amplifying,
        "reading": (
            "large positions lose MORE per dollar than small ones — sizing is "
            "amplifying an otherwise smaller problem" if amplifying else
            "per-dollar returns do not worsen with size — the loss is the "
            "strategy's own expectancy, not its sizing. Shrinking every "
            "position would shrink the winners by the same factor."),
    }


def cost_analysis(trades: list[TradeRecord]) -> dict:
    """§17. Is there a positive GROSS edge being eaten by costs?"""
    n = len(trades)
    if not n:
        return {"available": False}
    gross = sum(t.gross_pnl for t in trades)
    net = sum(t.realized_pnl for t in trades)
    fees = sum(t.fees for t in trades)
    slippage = sum(t.slippage for t in trades)
    unreported = sum(1 for t in trades if "venue_fees" in t.unavailable)
    return {
        "available": True,
        "trades": n,
        "grossPnl": round(gross, 4),
        "netPnl": round(net, 4),
        "fees": round(fees, 4),
        "slippage": round(slippage, 4),
        "feePerTrade": round(fees / n, 6),
        "tradesWithNoVenueFeeReported": unreported,
        "classification": (
            "COST_DRAG — the gross edge is positive and costs turn it "
            "negative. That is a cost problem, not a dead strategy."
            if gross > 0 >= net else
            "NEGATIVE_GROSS — the loss is there before any cost is charged. "
            "Costs are not the explanation."
            if gross <= 0 else
            "net positive — costs are being covered"),
    }


def entry_quality(trades: list[TradeRecord]) -> dict:
    """§6. Correct strategy in poor conditions, or poor strategy?"""
    return {
        "byEntryPrice": _by(trades, _price_bucket),
        "byLiquidity": _by(trades, lambda t: t.liquidity_bucket),
        "byTimeToResolution": _by(trades, lambda t: t.ttr_bucket),
        "byConfidence": _by(trades, _confidence_bucket),
        "byWalletInfluence": _by(
            trades, lambda t: ("wallet-influenced" if t.wallet_influence
                               else "no wallet influence")),
    }


def _price_bucket(trade: TradeRecord) -> str:
    price = trade.entry_price
    if price <= 0:
        return ""
    if price < 0.20:
        return "under-20c"
    if price < 0.50:
        return "20-49c"
    if price < 0.80:
        return "50-79c"
    return "80c+"


def _confidence_bucket(trade: TradeRecord) -> str:
    score = trade.entry_score
    if score <= 0:
        return ""
    if score < 0.6:
        return "score <0.60"
    if score < 0.7:
        return "score 0.60-0.69"
    if score < 0.8:
        return "score 0.70-0.79"
    return "score 0.80+"


def excursion_analysis(trades: list[TradeRecord]) -> dict:
    """§8. Do winners have to survive drawdown first?

    The question a stop policy actually turns on. If eventual winners routinely
    trade 15% against us first and the stop sits at 25%, the stop is not the
    problem. If they routinely trade 30% against us, the stop is harvesting
    winners — but WIDENING it also lets every loser run further, so the finding
    is a hypothesis and the trade-off is stated with it.
    """
    usable = [t for t in trades if "excursions" not in t.unavailable]
    if not usable:
        return {"available": False, "reason": "no peak/trough recorded"}
    winners = [t for t in usable if t.is_win]
    losers = [t for t in usable if not t.is_win]
    stopped = [t for t in usable if t.exit_style == "stop"]

    def _mae_profile(rows):
        maes = sorted(t.mae for t in rows)
        if not maes:
            return {}
        return {
            "sample": len(maes),
            "medianMae": round(statistics.median(maes), 4),
            "worstMae": round(min(maes), 4),
            "p25Mae": round(maes[len(maes) // 4], 4),
        }

    return {
        "available": True,
        "winners": _mae_profile(winners),
        "losers": _mae_profile(losers),
        "stopped": _mae_profile(stopped),
        "medianMfeWinners": round(
            statistics.median([t.mfe for t in winners]), 4) if winners else 0.0,
        "medianMfeLosers": round(
            statistics.median([t.mfe for t in losers]), 4) if losers else 0.0,
        "note": ("Winners' median MAE is how far a good trade normally goes "
                 "against us before it works. A stop tighter than that is "
                 "harvesting winners; a stop wider than it also lets every "
                 "loser run further. Both directions cost money, which is why "
                 "this is reported and not acted on."),
    }


def trade_quality(trade: TradeRecord) -> float:
    """§18. A diagnostic 0..1 score for one completed trade.

    Explicitly NOT a validation score and explicitly not retroactive: it
    summarises the CONDITIONS the trade was taken in, not whether it made
    money, so that "we keep taking trades in bad conditions" and "we keep
    being unlucky" stay distinguishable. Outcome is deliberately absent from
    every term.
    """
    score = 1.0
    if trade.entry_score > 0:
        score *= max(0.3, min(1.0, trade.entry_score / 0.8))
    if trade.liquidity_bucket in ("thin", "very_thin", "low"):
        score *= 0.6
    if trade.size_pct_of_equity > 0.25:
        score *= 0.7
    if trade.open_positions_at_entry >= 5:
        score *= 0.85
    if trade.entry_cost > 0:
        drag = (2.0 * trade.fees) / trade.entry_cost if trade.fees else 0.0
        score *= max(0.3, 1.0 - min(0.7, drag))
    if trade.hold_seconds and trade.hold_seconds < 120:
        score *= 0.8            # round-tripped inside two minutes: churn
    if "entry_features" in trade.unavailable:
        score *= 0.9            # judged on less
    return round(max(0.0, min(1.0, score)), 4)


# ---------------------------------------------------------------------------
# 5. Hypotheses — the only thing this module is allowed to produce
# ---------------------------------------------------------------------------


@dataclass
class PolicyHypothesis:
    """A proposed money-management change, and what would have to be true.

    It is a QUESTION with an address, not an instruction. `applied` is absent
    on purpose: there is no field here that any part of the running system
    reads, so a hypothesis cannot become a live change by accident. It becomes
    one only by a person editing config, or by the same validation ladder any
    strategy walks.
    """

    key: str
    title: str
    evidence: str
    proposal: str
    test: str
    risk: str = ""
    sample: int = 0
    status: str = "PROPOSED"

    def to_dict(self) -> dict:
        return {"key": self.key, "title": self.title, "evidence": self.evidence,
                "proposal": self.proposal, "test": self.test, "risk": self.risk,
                "sample": self.sample, "status": self.status}


def hypotheses(report_data: dict) -> list[PolicyHypothesis]:
    """§19. Turn findings into proposals — or decline to.

    Every branch here requires a sample floor AND a named mechanism. The
    default return is an empty list, and an empty list is the correct answer
    far more often than not; `daily_report` prints "No change recommended"
    when it gets one.
    """
    out: list[PolicyHypothesis] = []
    trades = int((report_data.get("account") or {}).get("closedTrades") or 0)
    if trades < MIN_TRADES_FOR_ANY_CLAIM:
        return out

    exits = (report_data.get("exitAttribution") or {}).get("byExitStyle") or {}
    counter = (report_data.get("counterfactual") or {}).get("byExitStyle") or {}
    capture = report_data.get("upsideCapture") or {}

    for style, reading in counter.items():
        if not reading.get("claimable"):
            continue
        early = int(reading.get("exitedTooEarly") or 0)
        protected = int(reading.get("exitProtectedCapital") or 0)
        answered = int(reading.get("answered") or 0)
        delta = float(reading.get("totalDelta") or 0.0)
        stats = exits.get(style) or {}
        if early > protected * 1.5 and delta > 0:
            out.append(PolicyHypothesis(
                key=f"exit-later::{style}",
                title=f"'{style}' exits may be firing too early",
                evidence=(
                    f"{early} of {answered} '{style}' exits would have been "
                    f"worth more 30 minutes later ({delta:+.2f} in total). "
                    f"That style closed {stats.get('trades', 0)} trades and "
                    f"carries {float(stats.get('shareOfTotalLoss') or 0):.0%} "
                    "of all realised loss."),
                proposal=(
                    f"A NEW risk-policy version that delays or loosens the "
                    f"'{style}' exit — not a change to the validated "
                    "strategy's signal."),
                test=("Must be replayed on markets not used to find it and "
                      "beat the current policy on expectancy AND upside "
                      "capture AND drawdown before it may run. Re-tuning "
                      "against these same trades is not a test."),
                risk=("Holding longer converts some small losses into larger "
                      "ones. The counterfactual measures the median case, not "
                      "the tail."),
                sample=answered))
        elif protected > early * 1.5 and delta < 0:
            out.append(PolicyHypothesis(
                key=f"exit-keep::{style}",
                title=f"'{style}' exits are earning their keep",
                evidence=(
                    f"holding 30 minutes longer would have been WORSE in "
                    f"{protected} of {answered} '{style}' exits "
                    f"({delta:+.2f} in total)."),
                proposal="No change. Recorded so this exit is not 'optimised "
                         "away' later on the grounds that it closes losers.",
                test="n/a — this is a finding that argues against a change.",
                sample=answered,
                status="NO_CHANGE"))

    sizing = report_data.get("sizing") or {}
    if sizing.get("sizingIsAmplifying"):
        out.append(PolicyHypothesis(
            key="sizing-ceiling",
            title="Position sizing is amplifying the loss",
            evidence=(
                f"large positions returned "
                f"{sizing.get('largePositionReturnPerDollar')} per dollar "
                f"against {sizing.get('smallPositionReturnPerDollar')} for "
                f"small ones over {sizing.get('sample')} trades."),
            proposal="A lower `engine.portfolio.max_position_fraction` as a "
                     "new, versioned risk policy.",
            test="Forward-tested. A ceiling that also cuts the top winners is "
                 "not an improvement however much the drawdown falls.",
            risk=f"The top five winners are "
                 f"{float(capture.get('topFiveWinnerShareOfProfit') or 0):.0%}"
                 " of all profit; a smaller ceiling shrinks them by the same "
                 "factor.",
            sample=int(sizing.get("sample") or 0)))

    clusters = report_data.get("lossClusters") or {}
    if float(clusters.get("worstMarketShareOfLoss") or 0.0) > 0.35:
        worst = (clusters.get("worstMarkets") or [{}])[0]
        out.append(PolicyHypothesis(
            key="concentration-cap",
            title="One market carries most of the loss",
            evidence=(
                f"{float(clusters['worstMarketShareOfLoss']):.0%} of all "
                f"realised loss came from a single market "
                f"({worst.get('trades', 0)} trades, "
                f"{worst.get('netPnl', 0)} net)."),
            proposal="A per-market capital cap in the capital-preservation "
                     "policy, so no single market can carry this share again.",
            test="A cap is a RISK control and does not need OOS strategy "
                 "validation — but it must be shown not to have removed the "
                 "best trades as well as the worst on the historical record, "
                 "and then forward-observed.",
            sample=int(worst.get("trades") or 0)))

    correlated = report_data.get("correlatedExposure") or {}
    if float(correlated.get("correlatedShare") or 0.0) > 0.3 \
            and int(correlated.get("simultaneousPairs") or 0) >= 20:
        out.append(PolicyHypothesis(
            key="correlated-exposure-cap",
            title="Simultaneous positions were not independent",
            evidence=(
                f"{correlated.get('correlatedPairs')} of "
                f"{correlated.get('simultaneousPairs')} simultaneous pairs "
                "shared a market, category or wallet thesis."),
            proposal="Cap correlated exposure (same market/category/wallet "
                     "thesis) rather than counting open positions.",
            test="Compare effective exposure before and after on the "
                 "historical record, then forward-observe. Must not reduce "
                 "the number of independent opportunities taken.",
            sample=int(correlated.get("simultaneousPairs") or 0)))

    costs = report_data.get("costs") or {}
    if str(costs.get("classification", "")).startswith("COST_DRAG"):
        out.append(PolicyHypothesis(
            key="cost-drag",
            title="A positive gross edge is being eaten by costs",
            evidence=(f"gross {costs.get('grossPnl')} vs net "
                      f"{costs.get('netPnl')} over {costs.get('trades')} "
                      f"trades; {costs.get('fees')} in fees."),
            proposal="Raise the minimum stake or the minimum expected value so "
                     "each trade carries its own round-trip cost, as a new "
                     "risk-policy version.",
            test="Forward-observed. Fewer, larger trades is a different "
                 "strategy profile and must be measured as one.",
            sample=int(costs.get("trades") or 0)))

    return out


# ---------------------------------------------------------------------------
# 6. The report
# ---------------------------------------------------------------------------


def account_state(trades: list[TradeRecord], journal_path: str | Path,
                  starting_balance: float = 0.0) -> dict:
    """Headline account numbers, including the drawdown path."""
    conn = _connect_ro(journal_path)
    cycle = (_rows(conn, "SELECT * FROM cycles ORDER BY id DESC LIMIT 1")
             or [{}])[0]
    open_rows = _rows(conn, "SELECT * FROM lifecycles WHERE status='OPEN'")
    if conn is not None:
        conn.close()

    net = sum(t.realized_pnl for t in trades)
    equity = float(cycle.get("portfolio_value") or 0.0)
    start = float(starting_balance or 0.0)

    # Peak-to-trough over the REALISED sequence, in order.
    running = start
    peak = start
    max_dd = 0.0
    for trade in sorted(trades, key=lambda t: t.exit_ts):
        running += trade.realized_pnl
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
    current_dd = peak - running

    stats = _stats(trades)
    return {
        "closedTrades": len(trades),
        "openTrades": len(open_rows),
        "startingEquity": round(start, 4),
        "currentEquity": round(equity, 4),
        "cash": round(float(cycle.get("balance") or 0.0), 4),
        "totalPnl": round(equity - start, 4) if equity else round(net, 4),
        "realisedPnl": round(net, 4),
        "maxDrawdown": round(max_dd, 4),
        "currentDrawdown": round(current_dd, 4),
        **{k: v for k, v in stats.items() if k != "trades"},
    }


def report(journal_path: str | Path, intel_path: str | Path = "",
           starting_balance: float = 0.0) -> dict:
    """The whole diagnostic, read-only, in one dict.

    Order is deliberate: reconstruct, then measure, then compare against
    counterfactuals, and only then generate hypotheses — which read the
    finished measurements rather than the trades, so no proposal can be
    derived from a number that is not in the report the operator sees.
    """
    trades = reconstruct(journal_path)
    if not trades:
        return {"available": False,
                "reason": "no closed trades in the journal yet"}

    out: dict[str, Any] = {"available": True}
    out["account"] = account_state(trades, journal_path, starting_balance)
    out["exitAttribution"] = exit_attribution(trades)
    out["byHoldingPeriod"] = _by(trades, hold_bucket)
    out["entryQuality"] = entry_quality(trades)
    out["excursions"] = excursion_analysis(trades)
    out["sizing"] = sizing_forensics(trades)
    out["lossClusters"] = loss_clusters(trades)
    out["correlatedExposure"] = correlated_exposure(trades)
    out["costs"] = cost_analysis(trades)
    out["upsideCapture"] = upside_capture(trades)

    history = PriceHistory(intel_path) if intel_path else None
    if history is not None:
        try:
            out["counterfactual"] = counterfactuals(trades, history)
        finally:
            history.close()
    else:
        out["counterfactual"] = {"available": False,
                                 "reason": "no intel store supplied"}

    out["contributors"] = _contributors(trades)
    out["tradeQuality"] = _quality_summary(trades)
    out["hypotheses"] = [h.to_dict() for h in hypotheses(out)]
    out["instrumentationGaps"] = _gaps(trades)
    return out


def _contributors(trades: list[TradeRecord]) -> dict:
    """§23: what is hurting the equity curve, and what is saving it."""
    def _rank(key_of, label):
        buckets: dict[str, float] = {}
        for trade in trades:
            key = key_of(trade)
            if key:
                buckets[str(key)] = buckets.get(str(key), 0.0) \
                    + trade.realized_pnl
        ordered = sorted(buckets.items(), key=lambda kv: kv[1])
        return {
            "dimension": label,
            "hurting": [{"key": k, "pnl": round(v, 4)}
                        for k, v in ordered[:3] if v < 0],
            "helping": [{"key": k, "pnl": round(v, 4)}
                        for k, v in reversed(ordered[-3:]) if v > 0],
        }

    return {
        "byExitStyle": _rank(lambda t: t.exit_style, "why it exited"),
        "byMarket": _rank(lambda t: t.market_id or t.token_id, "market"),
        "byCategory": _rank(lambda t: t.category, "category"),
        "byHoldingPeriod": _rank(hold_bucket, "holding period"),
        "byWallet": _rank(lambda t: t.wallet_influence, "wallet thesis"),
        "bySize": _rank(size_bucket, "position size"),
    }


def _quality_summary(trades: list[TradeRecord]) -> dict:
    scores = [(t, trade_quality(t)) for t in trades]
    values = [s for _t, s in scores]
    good = [t for t, s in scores if s >= 0.6]
    poor = [t for t, s in scores if s < 0.4]
    return {
        "meanScore": round(sum(values) / len(values), 4) if values else 0.0,
        "goodConditionTrades": len(good),
        "goodConditionExpectancy": round(
            sum(t.realized_pnl for t in good) / len(good), 6) if good else 0.0,
        "poorConditionTrades": len(poor),
        "poorConditionExpectancy": round(
            sum(t.realized_pnl for t in poor) / len(poor), 6) if poor else 0.0,
        "note": ("Conditions at entry only — outcome is deliberately not an "
                 "input, so 'we keep taking trades in bad conditions' stays "
                 "distinguishable from 'we keep being unlucky'."),
    }


def _gaps(trades: list[TradeRecord]) -> dict:
    """What the history cannot answer, and what to instrument next."""
    counts: dict[str, int] = {}
    for trade in trades:
        for gap in trade.unavailable:
            counts[gap] = counts.get(gap, 0) + 1
    advice = {
        "execution_detail": "no fills linked to the position — join "
                            "executions.lifecycle_id at close",
        "venue_fees": "the venue reports no per-fill fee; cost drag is "
                      "estimated from the configured fee only",
        "entry_decision": "the opening decision row is missing — cannot judge "
                          "entry quality for these",
        "entry_features": "no feature vector stored on the entry decision; "
                          "entry-condition analysis is limited to journal tags",
        "portfolio_context": "no cycle snapshot at entry — position size as a "
                             "share of equity cannot be reconstructed",
        "excursions": "peak/trough never moved off zero — MAE/MFE and any "
                      "stop analysis are unavailable for these",
    }
    return {"counts": counts,
            "instrumentNext": {k: advice.get(k, "") for k in counts}}


# ---------------------------------------------------------------------------
# 7. The daily report (§26)
# ---------------------------------------------------------------------------


def daily_report(data: dict) -> str:
    """The operator-facing summary. Says 'no change recommended' when true."""
    if not data.get("available"):
        return (f"MONEY MANAGEMENT: nothing to report — "
                f"{data.get('reason', 'no data')}.")
    account = data["account"]
    lines: list[str] = []

    def _section(title: str):
        lines.append("")
        lines.append(title)
        lines.append("-" * len(title))

    _section("ACCOUNT STATE")
    lines.append(
        f"  {account['closedTrades']} closed, {account['openTrades']} open. "
        f"Equity ${account['currentEquity']:.2f} from "
        f"${account['startingEquity']:.2f} (realised "
        f"{account['realisedPnl']:+.2f}).")
    lines.append(
        f"  win rate {account['winRate']:.0%} ({account['winners']}W/"
        f"{account['losers']}L)  expectancy {account['expectancy']:+.4f}  "
        f"avg return {account['avgReturn']:+.2%}  median "
        f"{account['medianReturn']:+.2%}")
    lines.append(
        f"  largest winner {account['largestWinner']:+.2f}  largest loser "
        f"{account['largestLoser']:+.2f}  profit factor "
        f"{account['profitFactor'] if account['profitFactor'] else 'n/a'}")

    _section("DRAWDOWN STATE")
    lines.append(f"  max ${account['maxDrawdown']:.2f}  current "
                 f"${account['currentDrawdown']:.2f}")

    _section("EXIT ANALYSIS")
    attribution = data["exitAttribution"]
    for style, stats in sorted(
            attribution["byExitStyle"].items(),
            key=lambda kv: kv[1].get("netPnl", 0.0)):
        lines.append(
            f"  {style:<14} {stats['trades']:>4} trades  "
            f"{stats['winRate']:>4.0%} win  net {stats['netPnl']:>+8.2f}  "
            f"{stats.get('shareOfTotalLoss', 0):>5.0%} of loss  "
            f"{stats.get('shareOfTotalProfit', 0):>5.0%} of profit  "
            f"median hold {stats['medianHoldSeconds'] / 60:.0f}m")
    lines.append(f"  destroying the most value: "
                 f"{attribution['destroyingMostValue'] or 'n/a'}")
    lines.append(f"  preserving the most value: "
                 f"{attribution['preservingMostValue'] or 'n/a'}")

    _section("COUNTERFACTUAL FINDINGS")
    counter = data.get("counterfactual") or {}
    if not counter.get("available"):
        lines.append(f"  not available: {counter.get('reason', '')}")
    else:
        for label, stats in (counter.get("byHorizon") or {}).items():
            if not stats["answered"]:
                continue
            lines.append(
                f"  {label:<11} answered {stats['answered']:>4} / not "
                f"available {stats['notAvailable']:>4}  holding better in "
                f"{stats['holdingWouldHaveBeenBetter']:>4}  total "
                f"{stats['totalDelta']:>+8.2f}")
        for style, reading in (counter.get("byExitStyle") or {}).items():
            lines.append(f"  {style}: {reading['reading']}")
        lines.append("  " + str(counter.get("note", "")))

    _section("HOLDING-PERIOD ANALYSIS")
    for bucket, stats in (data.get("byHoldingPeriod") or {}).items():
        lines.append(
            f"  {bucket:<28} {stats['trades']:>4} trades  "
            f"{stats['winRate']:>4.0%} win  expectancy "
            f"{stats['expectancy']:>+8.4f}"
            + ("" if stats["claimable"] else "   (too few to claim anything)"))

    _section("POSITION-SIZING ANALYSIS")
    sizing = data.get("sizing") or {}
    lines.append("  " + str(sizing.get("reading", sizing.get("reason", ""))))

    _section("CORRELATED-EXPOSURE ANALYSIS")
    lines.append("  " + str((data.get("correlatedExposure") or {})
                            .get("reading", "")))

    _section("COST ANALYSIS")
    costs = data.get("costs") or {}
    lines.append(f"  gross {costs.get('grossPnl')}  net {costs.get('netPnl')}  "
                 f"fees {costs.get('fees')}  slippage {costs.get('slippage')}")
    lines.append("  " + str(costs.get("classification", "")))

    _section("TOP LOSS / PROFIT CONTRIBUTORS")
    for _dim, block in (data.get("contributors") or {}).items():
        hurting = ", ".join(f"{e['key'][:22]} {e['pnl']:+.2f}"
                            for e in block["hurting"]) or "-"
        helping = ", ".join(f"{e['key'][:22]} {e['pnl']:+.2f}"
                            for e in block["helping"]) or "-"
        lines.append(f"  {block['dimension']:<16} hurting: {hurting}")
        lines.append(f"  {'':<16} helping: {helping}")

    _section("WINNER PRESERVATION")
    capture = data.get("upsideCapture") or {}
    if capture.get("available"):
        lines.append(
            f"  upside capture {capture['upsideCaptureRatio']:.2f} over "
            f"{capture['sample']} trades; the top five winners are "
            f"{capture['topFiveWinnerShareOfProfit']:.0%} of all profit. Any "
            "change that shrinks these is not an improvement.")
    else:
        lines.append(f"  not available: {capture.get('reason', '')}")

    _section("RESEARCH HYPOTHESES GENERATED")
    proposals = data.get("hypotheses") or []
    changes = [h for h in proposals if h["status"] == "PROPOSED"]
    if not changes:
        lines.append("  No change recommended. Either the evidence does not "
                     "support one or the sample is too small to support one. "
                     "That is a valid result, not a missing answer.")
    for proposal in proposals:
        lines.append(f"  [{proposal['status']}] {proposal['title']} "
                     f"(n={proposal['sample']})")
        lines.append(f"      evidence: {proposal['evidence']}")
        lines.append(f"      proposal: {proposal['proposal']}")
        lines.append(f"      test:     {proposal['test']}")
        if proposal.get("risk"):
            lines.append(f"      risk:     {proposal['risk']}")

    _section("CHANGES CURRENTLY UNDER TEST / NOT YET VALIDATED")
    lines.append("  None. Nothing in this report has been applied: this "
                 "module cannot alter an exit rule, a stop, a position size "
                 "or a strategy. Every proposal above is a new risk-policy "
                 "VERSION that must be tested on data it was not derived "
                 "from before it may run.")

    _section("WHAT THE HISTORY CANNOT ANSWER")
    gaps = (data.get("instrumentationGaps") or {}).get("instrumentNext") or {}
    if not gaps:
        lines.append("  Nothing — every field was available for every trade.")
    for gap, advice in gaps.items():
        count = (data["instrumentationGaps"]["counts"] or {}).get(gap, 0)
        lines.append(f"  {gap} ({count} trades): {advice}")

    return "\n".join(lines)


def write_report(journal_path: str | Path, out_path: str | Path,
                 intel_path: str | Path = "",
                 starting_balance: float = 0.0) -> dict:
    data = report(journal_path, intel_path, starting_balance)
    Path(out_path).write_text(json.dumps(data, indent=2, default=str),
                              encoding="utf-8")
    return data
