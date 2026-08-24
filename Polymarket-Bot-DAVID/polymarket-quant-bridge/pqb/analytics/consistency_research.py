"""CONSISTENCY RESEARCH — where a loss-control rule earns the right to run.

Read-only. Nothing in this file can change a stop, an exit, a size or a
config; it produces a report, and a human moves `engine.consistency.mode` to
`"enforce"` if the report says the rule earned it. That separation is the
whole reason the module exists as its own file: the live layer
(:mod:`pqb.consistency`) must be small enough to audit in one sitting, and the
machinery that decides whether its thresholds are any good is not small.

**The rule this module exists to enforce, on itself.** The observation that
started the patch is 16 trades. Sixteen trades cannot support a parameter, let
alone seven, and any number chosen to make those sixteen look better is a
number fitted to noise. So nothing here reports a recommendation from a single
pass over the history. A candidate rule is:

    discovered -> backtested -> walk-forward validated -> out-of-sample
    tested -> and only then eligible for promotion

and :func:`promotion_verdict` refuses at every one of those steps for reasons
it names. The default answer is KEEP IN SHADOW, and it takes evidence to move
it, not the absence of evidence against it.

**What is being optimised, and what is not.** Not win rate.
:func:`composite_score` is expectancy, profit factor, drawdown and loss-tail
improvement, penalised by complexity, small samples and out-of-sample
instability — and :func:`promotion_verdict` independently vetoes any candidate
that damages the winner distribution however good its score is. A rule that
raises the win rate while lowering expectancy is a worse strategy that feels
better, and both halves of the scorer exist to say so out loud.

**On honesty of the sample.** Trades whose price path was never captured
cannot be replayed, and they are COUNTED AND NAMED rather than dropped
(`coverage` on every result). A candidate evaluated on the 60% of history that
happens to have a series, reported as if it were the whole history, is the
same error as excluding the losing trades — Module 27 forbids both, and the
first one is much easier to commit by accident.

**On cost.** There is no Rust accelerator in this checkout — no crate, no
extension module, no build. Module 29's instruction is to use one rather than
write Python loops over wallet x market x tick, and its intent, where none
exists, is to not build a second architecture for this. So the expensive parts
are structured the way the existing analytics layer already structures them:
each token's captured series is read once and cached (:class:`PathHistory`),
each trade's path is materialised once and replayed by every candidate against
that one copy, and the walk-forward folds reuse the same materialised paths.
The work is O(trades x rows-per-trade x candidates) with the read amortised,
which for a journal of this size is seconds.
"""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .forensics import TradeRecord, _connect_ro, _rows, reconstruct

# The horizons Module 2 asks for, in seconds.
HORIZONS = (30, 60, 120, 300, 600, 900, 1_800, 3_600, 7_200)

# A path row within this many seconds of a horizon counts as that horizon's
# snapshot. Capture cadence is `research.capture_seconds`, so an exact match is
# not something the data can offer and demanding one would report 0% coverage.
SNAP_TOLERANCE_S = 120.0

# Below this many replayable trades, a candidate is described and never
# recommended. Same spirit as forensics.MIN_TRADES_FOR_ANY_CLAIM, and set
# where it is because a rule with two parameters fitted on fewer than this is
# indistinguishable from one fitted on nothing.
MIN_TRADES_FOR_A_CANDIDATE = 30

# ...and per walk-forward fold. A fold that validates on four trades has not
# validated anything.
MIN_TRADES_PER_FOLD = 10

# How many folds must independently agree before "stable across periods" is a
# statement about the rule rather than about one lucky month.
MIN_STABLE_FOLDS = 3


# ---------------------------------------------------------------------------
# small statistics, kept local and stdlib-only
# ---------------------------------------------------------------------------


def percentile(values: list, q: float) -> float:
    """Linear-interpolated percentile. ``q`` in 0..1. Empty -> 0.0."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return float(ordered[int(pos)])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (pos - low))


def _mean(values: list) -> float:
    return float(statistics.fmean(values)) if values else 0.0


def _median(values: list) -> float:
    return float(statistics.median(values)) if values else 0.0


def _stdev(values: list) -> float:
    return float(statistics.pstdev(values)) if len(values) > 1 else 0.0


# ---------------------------------------------------------------------------
# the metric set every candidate is judged on (Module 5's ranking list)
# ---------------------------------------------------------------------------


@dataclass
class Metrics:
    """One outcome distribution, described completely.

    Every field Module 5 lists, computed the same way for every candidate so
    two of these are directly comparable. `max_drawdown` is on the realised
    equity curve in trade order, which is the only drawdown a closed-trade
    record can honestly report — an intra-trade equity drawdown would need
    marks for every open position at every instant, and this data does not
    have them. Named rather than approximated.
    """

    n: int = 0
    net: float = 0.0
    expectancy: float = 0.0          # net P&L per trade
    win_rate: float = 0.0
    loss_rate: float = 0.0
    profit_factor: float = 0.0
    avg_winner: float = 0.0
    avg_loser: float = 0.0
    median_winner: float = 0.0
    median_loser: float = 0.0
    largest_winner: float = 0.0
    largest_loser: float = 0.0
    p95_loss: float = 0.0            # the 95th-percentile loss, as a positive
    max_drawdown: float = 0.0
    return_volatility: float = 0.0
    mean_return: float = 0.0
    avg_hold_seconds: float = 0.0
    top5_winner_share: float = 0.0   # share of gross profit in the best five

    def to_dict(self) -> dict:
        return {
            "n": self.n, "net": round(self.net, 4),
            "expectancy": round(self.expectancy, 4),
            "winRate": round(self.win_rate, 4),
            "lossRate": round(self.loss_rate, 4),
            "profitFactor": round(self.profit_factor, 4),
            "avgWinner": round(self.avg_winner, 4),
            "avgLoser": round(self.avg_loser, 4),
            "medianWinner": round(self.median_winner, 4),
            "medianLoser": round(self.median_loser, 4),
            "largestWinner": round(self.largest_winner, 4),
            "largestLoser": round(self.largest_loser, 4),
            "p95Loss": round(self.p95_loss, 4),
            "maxDrawdown": round(self.max_drawdown, 4),
            "returnVolatility": round(self.return_volatility, 4),
            "meanReturn": round(self.mean_return, 4),
            "avgHoldSeconds": round(self.avg_hold_seconds, 1),
            "top5WinnerShare": round(self.top5_winner_share, 4),
        }


def measure(outcomes: list) -> Metrics:
    """Describe a list of ``Outcome`` (or anything with .pnl/.return_pct)."""
    metrics = Metrics()
    if not outcomes:
        return metrics
    pnls = [float(o.pnl) for o in outcomes]
    returns = [float(getattr(o, "return_pct", 0.0)) for o in outcomes]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]

    metrics.n = len(pnls)
    metrics.net = sum(pnls)
    metrics.expectancy = _mean(pnls)
    metrics.win_rate = len(winners) / len(pnls)
    metrics.loss_rate = len(losers) / len(pnls)
    gross_win = sum(winners)
    gross_loss = -sum(losers)
    # A candidate with no losses at all has an undefined profit factor, not an
    # infinite one. Reported as 0 and read alongside `n`, because "inf" in a
    # ranking column silently wins every comparison it appears in.
    metrics.profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 0.0
    metrics.avg_winner = _mean(winners)
    metrics.avg_loser = _mean(losers)
    metrics.median_winner = _median(winners)
    metrics.median_loser = _median(losers)
    metrics.largest_winner = max(pnls)
    metrics.largest_loser = min(pnls)
    loss_magnitudes = sorted(-p for p in losers)
    metrics.p95_loss = percentile(loss_magnitudes, 0.95)
    metrics.mean_return = _mean(returns)
    metrics.return_volatility = _stdev(returns)
    metrics.avg_hold_seconds = _mean(
        [float(getattr(o, "hold_seconds", 0.0)) for o in outcomes])
    if gross_win > 0:
        metrics.top5_winner_share = sum(sorted(winners, reverse=True)[:5]) \
            / gross_win

    equity = 0.0
    peak = 0.0
    worst = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    metrics.max_drawdown = worst
    return metrics


# ---------------------------------------------------------------------------
# Module 2 — the price/feature path of a completed trade
# ---------------------------------------------------------------------------


class PathHistory:
    """Captured feature rows per token, read once and cached.

    :class:`pqb.analytics.forensics.PriceHistory` reads the same table for the
    price alone. This one keeps the whole feature vector, because the thesis
    detector under test reads liquidity, spread, the wallet columns and the
    Market-State classification — and re-deriving a thesis reading from price
    alone would be testing a different rule from the one that would run.
    """

    def __init__(self, intel_path: str | Path):
        self._conn = _connect_ro(intel_path)
        self._cache: dict[str, list] = {}

    @property
    def available(self) -> bool:
        return self._conn is not None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def series(self, token_id: str) -> list:
        """``[(ts, price, features), ...]`` ascending. Cached per token.

        `price` is the BID where one was captured. The exit side of a long is
        the bid, and marking a hypothetical exit at the mid would credit half a
        spread per trade that no seller could ever have collected — which is
        the same order of magnitude as the effect being measured.
        """
        if token_id in self._cache:
            return self._cache[token_id]
        rows = _rows(self._conn,
                     "SELECT ts, features FROM research_rows "
                     "WHERE token_id=? ORDER BY ts", (token_id,))
        series = []
        for row in rows:
            try:
                features = json.loads(row.get("features") or "{}") or {}
            except (TypeError, ValueError):
                continue
            price = 0.0
            for key in ("bid", "price", "mid"):
                value = features.get(key)
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    price = value
                    break
            if price > 0:
                series.append((float(row.get("ts") or 0.0), price, features))
        self._cache[token_id] = series
        return series


@dataclass
class TradePath:
    """One completed trade with the rows that were captured while it was open.

    Materialised once per trade and shared by every candidate and every
    walk-forward fold, because re-reading and re-parsing the series per
    candidate is where a study like this stops being seconds and starts being
    minutes.
    """

    trade: TradeRecord
    rows: list = field(default_factory=list)   # [(ts, price, features)]
    pre_entry: list = field(default_factory=list)  # rows BEFORE entry
    replayable: bool = False

    @property
    def entry_price(self) -> float:
        return self.trade.entry_price

    def snapshot(self, seconds: float) -> Optional[tuple]:
        """The row nearest ``entry_ts + seconds``, or None if none is close."""
        target = self.trade.entry_ts + seconds
        best, gap = None, None
        for row in self.rows:
            distance = abs(row[0] - target)
            if gap is None or distance < gap:
                best, gap = row, distance
        if gap is None or gap > SNAP_TOLERANCE_S:
            return None
        return best


def build_paths(trades: list, history: PathHistory) -> list:
    """Attach each trade's captured path. Missing paths are marked, not hidden.

    `pre_entry` carries rows from before the position was opened, and it is a
    separate list rather than an offset into one because every rule that needs
    pre-entry data (the volatility stop) must be structurally unable to see
    post-entry data. Splitting the series at the entry timestamp here means a
    leak would have to be written deliberately rather than reached for by
    accident.
    """
    out = []
    for trade in trades:
        path = TradePath(trade=trade)
        if history.available and trade.entry_ts > 0:
            series = history.series(trade.token_id)
            end = trade.exit_ts or float("inf")
            path.rows = [r for r in series if trade.entry_ts <= r[0] <= end]
            path.pre_entry = [r for r in series if r[0] < trade.entry_ts]
            path.replayable = len(path.rows) >= 2
        out.append(path)
    return out


def coverage(paths: list) -> dict:
    """How much of the history can actually be replayed (Module 27)."""
    total = len(paths)
    usable = sum(1 for p in paths if p.replayable)
    return {
        "trades": total, "replayable": usable,
        "unreplayable": total - usable,
        "share": round(usable / total, 4) if total else 0.0,
        "note": ("Trades with no captured price series cannot be replayed. "
                 "They are counted here and excluded from candidate results "
                 "only — never from the account totals, and never silently."),
    }


# ---------------------------------------------------------------------------
# Module 2 — when do winners separate from losers?
# ---------------------------------------------------------------------------


def separation_analysis(paths: list) -> dict:
    """At which horizon do eventual winners start to look different?

    Do not assume the answer; measure it. For every horizon this reports the
    two groups' mean unrealised return, the gap between them, and that gap
    divided by the pooled spread — because a 3-point gap between two groups
    that each vary by 30 points is not a separation, and reporting the gap
    alone would make it look like one.

    `share` is how many trades had a row near that horizon at all. A large
    apparent separation at +30s on nine trades is a fact about the capture
    cadence, not about the strategy.
    """
    usable = [p for p in paths if p.replayable and p.entry_price > 0]
    if not usable:
        return {"available": False,
                "reason": "no trade has a replayable captured path"}

    out = []
    for horizon in HORIZONS:
        winners, losers = [], []
        for path in usable:
            snap = path.snapshot(horizon)
            if snap is None:
                continue
            ret = snap[1] / path.entry_price - 1.0
            (winners if path.trade.realized_pnl > 0 else losers).append(ret)
        if not winners or not losers:
            continue
        gap = _mean(winners) - _mean(losers)
        pooled = math.sqrt((_stdev(winners) ** 2 + _stdev(losers) ** 2) / 2.0)
        out.append({
            "horizonSeconds": horizon,
            "winners": len(winners), "losers": len(losers),
            "share": round((len(winners) + len(losers)) / len(usable), 4),
            "meanWinner": round(_mean(winners), 4),
            "meanLoser": round(_mean(losers), 4),
            "gap": round(gap, 4),
            "separation": round(gap / pooled, 4) if pooled > 0 else 0.0,
            "winnersAlreadyUp": round(
                sum(1 for r in winners if r > 0) / len(winners), 4),
        })

    # "Useful" means the two distributions are actually distinguishable at
    # that horizon, not merely that it is the least-bad one. Without the
    # threshold this reports the first horizon in the list whenever every
    # separation is zero — naming a moment at which nothing is knowable as the
    # moment the layer should start reading, which is the worst possible
    # advice to take from this table.
    useful = [r for r in out if abs(r["separation"]) >= 0.5]
    earliest = min(useful, key=lambda r: r["horizonSeconds"]) \
        if useful else None
    return {
        "available": bool(out), "horizons": out,
        "earliestUsefulSeconds": (earliest or {}).get("horizonSeconds"),
        "separates": bool(useful),
        "note": ("Separation is the gap between the two groups' mean return "
                 "divided by their pooled spread. Below about 0.5 the two "
                 "distributions overlap so heavily that no rule reading this "
                 "horizon can tell them apart, whatever the gap looks like."),
    }


# ---------------------------------------------------------------------------
# Module 6 — how much room does a real winner need?
# ---------------------------------------------------------------------------


def winner_room(trades: list) -> dict:
    """The MAE distribution of trades that went on to WIN.

    This is the single most important number the patch produces, and it is the
    reason `min_adverse_room_pct` defaults to 0 rather than to a guess: it is
    the answer to "how far does a good trade normally go against us before it
    comes good", and no loss-control rule may act inside it.

    `killedByStop` answers the operator's question directly — for a range of
    candidate stop distances, what share of the historically PROFITABLE trades
    would that stop have closed at a loss. A distance that kills a third of the
    winners is not a tighter stop; it is a different and worse strategy.
    """
    usable = [t for t in trades if "excursions" not in t.unavailable
              and t.entry_price > 0]
    if len(usable) < 5:
        return {"available": False,
                "reason": (f"only {len(usable)} trade(s) have excursion data; "
                           "a distribution needs more than that")}

    winners = [t for t in usable if t.realized_pnl > 0]
    losers = [t for t in usable if t.realized_pnl <= 0]

    def profile(rows: list) -> dict:
        maes = [abs(t.mae) for t in rows]      # as positive distances
        if not maes:
            return {"sample": 0}
        return {
            "sample": len(maes),
            "median": round(percentile(maes, 0.50), 4),
            "p75": round(percentile(maes, 0.75), 4),
            "p90": round(percentile(maes, 0.90), 4),
            "p95": round(percentile(maes, 0.95), 4),
            "worst": round(max(maes), 4),
        }

    killed = []
    for distance in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.40, 0.50):
        hit = [t for t in winners if abs(t.mae) >= distance]
        killed.append({
            "stopDistance": distance,
            "winnersKilled": len(hit),
            "shareOfWinners": round(len(hit) / len(winners), 4)
            if winners else 0.0,
            "profitDestroyed": round(sum(t.realized_pnl for t in hit), 4),
            "losersCaughtEarlier": sum(1 for t in losers
                                       if abs(t.mae) >= distance),
        })

    win_profile = profile(winners)
    loss_profile = profile(losers)
    separation = ""
    if win_profile.get("sample") and loss_profile.get("sample"):
        if loss_profile["median"] > win_profile["p90"]:
            separation = (
                "The two distributions separate: the typical loser travels "
                f"{loss_profile['median']:.1%} against us, past the "
                f"{win_profile['p90']:.1%} that 90% of winners stay inside. "
                "A stop between those two numbers is the one this patch is "
                "looking for.")
        else:
            separation = (
                "The two distributions OVERLAP: the typical loser "
                f"({loss_profile['median']:.1%}) is inside the range winners "
                f"routinely use (90th percentile {win_profile['p90']:.1%}). "
                "No stop distance can separate them, so tightening the stop "
                "would buy smaller losses by paying for them with winners. "
                "This is the case where the correct answer is to leave the "
                "stop alone and act on thesis failure instead.")

    return {
        "available": True,
        "winners": win_profile, "losers": loss_profile,
        "killedByStop": killed,
        "reading": separation,
        "note": ("MAE is measured from the entry price using the trough the "
                 "journal recorded while the position was open, so it is the "
                 "worst the position actually looked, not a reconstruction."),
    }


# ---------------------------------------------------------------------------
# Module 5 — the candidate stop concepts
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What one trade did under one candidate rule."""

    lifecycle_id: int = 0
    pnl: float = 0.0
    return_pct: float = 0.0
    hold_seconds: float = 0.0
    exit_ts: float = 0.0
    exit_price: float = 0.0
    changed: bool = False            # did the candidate move this trade?
    style: str = ""
    entry_ts: float = 0.0
    wallet_influence: str = ""
    category: str = ""
    liquidity_bucket: str = ""
    ttr_bucket: str = ""


@dataclass
class Candidate:
    """One loss-control concept, with the parameters it costs to state.

    `parameters` is not decoration: it is the input to the complexity penalty,
    and a candidate that needs four numbers to describe has to beat one that
    needs one by enough to pay for them. `supported` is false when this build's
    captured data cannot express the rule, and an unsupported candidate is
    REPORTED as unsupported rather than quietly scoring zero.
    """

    key: str
    label: str
    parameters: int = 0
    supported: bool = True
    unsupported_reason: str = ""
    rule: Optional[Callable] = None


def _exit_here(path: TradePath, row: tuple, style: str,
               fee_model: float) -> Outcome:
    """Close the position at a captured row, on the same terms the real exit
    would have paid.

    `fee_model` is this history's own average round-trip cost per trade, so a
    hypothetical exit is charged what a real one costs. Without it every
    candidate that exits more often looks better than it is by exactly the fee
    it never paid — which flatters precisely the rules this patch is inclined
    to like.
    """
    trade = path.trade
    price = row[1]
    pnl = (price - trade.entry_price) * trade.entry_size - fee_model
    cost = trade.entry_cost or (trade.entry_price * trade.entry_size)
    return Outcome(
        lifecycle_id=trade.lifecycle_id, pnl=pnl,
        return_pct=(pnl / cost) if cost else 0.0,
        hold_seconds=max(0.0, row[0] - trade.entry_ts),
        exit_ts=row[0], exit_price=price, changed=True, style=style,
        entry_ts=trade.entry_ts, wallet_influence=trade.wallet_influence,
        category=trade.category, liquidity_bucket=trade.liquidity_bucket,
        ttr_bucket=trade.ttr_bucket)


def _actual(trade: TradeRecord) -> Outcome:
    """What the trade really did. The baseline, and the fallback."""
    return Outcome(
        lifecycle_id=trade.lifecycle_id, pnl=trade.realized_pnl,
        return_pct=trade.return_pct, hold_seconds=trade.hold_seconds,
        exit_ts=trade.exit_ts, exit_price=trade.exit_price, changed=False,
        style=trade.exit_style, entry_ts=trade.entry_ts,
        wallet_influence=trade.wallet_influence, category=trade.category,
        liquidity_bucket=trade.liquidity_bucket, ttr_bucket=trade.ttr_bucket)


def replay(candidate: Candidate, paths: list, fee_model: float,
           only: Optional[set] = None) -> list:
    """Run one candidate over the history.

    **Every candidate is an ADDITION to the existing exit set, not a
    replacement.** A trade the candidate never fires on keeps its real outcome,
    which already includes the existing stop, take-profit, trailing and edge
    exits. That is not a simplification — it is the only thing the architecture
    can actually deploy, because Layer 2 runs after Layer 1 and can only
    convert a HOLD. Measuring a candidate as a replacement for the whole exit
    system would be measuring something that cannot be shipped.

    A consequence worth stating: a candidate can never improve a trade that the
    existing exits already closed before the candidate would have fired, and
    can never save one it fires on too late. Both show up as `changed=False`.
    """
    out = []
    for path in paths:
        if only is not None and path.trade.lifecycle_id not in only:
            continue
        if not path.replayable or candidate.rule is None:
            out.append(_actual(path.trade))
            continue
        fired = candidate.rule(path)
        if fired is None:
            out.append(_actual(path.trade))
            continue
        row, style = fired
        # Firing at or after the real exit is not an intervention.
        if path.trade.exit_ts and row[0] >= path.trade.exit_ts:
            out.append(_actual(path.trade))
            continue
        out.append(_exit_here(path, row, style, fee_model))
    return out


# -- the rules themselves ----------------------------------------------------


def _rule_time(minutes: float, min_return: float):
    """B. Time-based invalidation: still going nowhere after N minutes."""
    def rule(path: TradePath):
        deadline = path.trade.entry_ts + minutes * 60.0
        for row in path.rows:
            if row[0] < deadline or path.entry_price <= 0:
                continue
            if row[1] / path.entry_price - 1.0 < min_return:
                return row, "time_invalidation"
            return None
        return None
    return rule


def _rule_volatility(k: float, floor: float):
    """D. Volatility-adjusted stop, sized from PRE-ENTRY data only.

    The token's own realised volatility before we entered. Using the whole
    series would size the stop with data from after the decision — the exact
    leak Module 14 forbids — and would make a violently reversing trade look
    like one we had always given room to.
    """
    def rule(path: TradePath):
        prices = [r[1] for r in path.pre_entry[-60:]]
        if len(prices) < 10 or path.entry_price <= 0:
            return None
        moves = [abs(b / a - 1.0) for a, b in zip(prices, prices[1:]) if a > 0]
        if not moves:
            return None
        distance = max(floor, k * _stdev(moves) * math.sqrt(len(moves)))
        for row in path.rows:
            if row[1] / path.entry_price - 1.0 <= -distance:
                return row, "volatility_stop"
        return None
    return rule


def _rule_market_state(confirm: int):
    """E. Market-state invalidation: the classification the entry was taken
    under has changed, and stayed changed."""
    def rule(path: TradePath):
        entry_state = None
        for row in path.rows:
            state = row[2].get("ms_state")
            if state is None:
                continue
            if entry_state is None:
                entry_state = state
                streak = 0
                continue
            streak = streak + 1 if state != entry_state else 0
            if streak >= confirm:
                return row, "market_state_invalidation"
        return None
    return rule


def _rule_wallet(confirm: int):
    """F. Wallet-signal invalidation: the tracked wallets are selling."""
    def rule(path: TradePath):
        streak = 0
        for row in path.rows:
            exits = row[2].get("wallet_exits")
            try:
                exits = float(exits or 0.0)
            except (TypeError, ValueError):
                exits = 0.0
            streak = streak + 1 if exits > 0 else 0
            if streak >= confirm:
                return row, "wallet_invalidation"
        return None
    return rule


def _rule_thesis(cfg, thesis_of: Callable, confirm: int, room: float):
    """C. Thesis-based invalidation — the LIVE detector, replayed.

    This calls :func:`pqb.consistency.thesis_health` with a state assembled
    from the captured row, so what is being backtested is the code that would
    actually run and not a re-statement of it. If the two ever disagree the
    backtest is worthless, and the only way to keep them from disagreeing is
    for there to be one of them.
    """
    from .. import consistency

    def rule(path: TradePath):
        thesis = thesis_of(path.trade)
        # No stored entry thesis, or one with nothing checkable in it: the
        # detector would answer UNKNOWN for every row, and a rule that cannot
        # read its own precondition must decline rather than default.
        if thesis is None or not thesis.available:
            return None
        streak = 0
        for row in path.rows:
            state = _state_from_row(path, row, consistency)
            verdict = consistency.thesis_health(cfg, state, thesis)
            streak = streak + 1 if verdict.state == consistency.INVALIDATED \
                else 0
            if streak >= confirm and state.unrealized_pct <= -room:
                return row, "thesis_invalidation"
        return None
    return rule


def _rule_hybrid(cfg, thesis_of: Callable, confirm: int, room: float,
                 stop: float):
    """G. The existing stop OR a confirmed thesis invalidation.

    The existing stop already fires in the real outcomes, so what this adds is
    the thesis leg — and it is here as its own candidate so the report can say
    whether the hybrid beats the thesis rule alone, which is the question of
    whether the second leg is paying for its own complexity.
    """
    thesis_rule = _rule_thesis(cfg, thesis_of, confirm, room)

    def rule(path: TradePath):
        hit = thesis_rule(path)
        if hit is not None:
            return hit
        if stop <= 0 or path.entry_price <= 0:
            return None
        for row in path.rows:
            if row[1] / path.entry_price - 1.0 <= -stop:
                return row, "stop"
        return None
    return rule


def _state_from_row(path: TradePath, row: tuple, consistency) -> Any:
    """A :class:`~pqb.consistency.TradeState` for one captured instant.

    Built from the captured feature vector rather than from a live position,
    which is what lets the same detector run over history and over the book.
    Peak and trough are the running extremes UP TO this row — never the whole
    trade's — because the live layer only ever knows the past.
    """
    ts, price, features = row
    entry = path.entry_price
    seen = [r[1] for r in path.rows if r[0] <= ts] or [price]
    peak, trough = max(seen), min(seen)
    cost = path.trade.entry_cost or (entry * path.trade.entry_size)
    unrealized = (price - entry) * path.trade.entry_size

    state = consistency.TradeState(
        token_id=path.trade.token_id, market_id=path.trade.market_id,
        lifecycle_id=path.trade.lifecycle_id,
        entry_price=entry, entry_ts=path.trade.entry_ts, price=price,
        peak_price=peak, trough_price=trough,
        unrealized_pct=(unrealized / cost) if cost else 0.0,
        unrealized_usdc=unrealized,
        held_seconds=max(0.0, ts - path.trade.entry_ts))
    if entry > 0:
        state.mfe = peak / entry - 1.0
        state.mae = trough / entry - 1.0

    def _num(key):
        try:
            value = features.get(key)
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None

    state.spread = _num("spread")
    state.liquidity = _num("liquidity")
    state.depth = _num("depth_total")
    exits = _num("wallet_exits") or 0.0
    state.wallet_exited = exits > 0
    ms = features.get("ms_state")
    state.market_state_now = "" if ms in (None, "") else str(ms)
    # The live layer reads Layer 1's hold conviction, which is not captured
    # per row. The score condition therefore cannot be replayed, and rather
    # than substitute a proxy that would make the backtest a test of something
    # else, the entry score is carried through unchanged — which makes the
    # score condition a no-op here and is declared in the report.
    state.current_score = 0.0
    return state


def build_candidates(cfg, thesis_of: Callable, stop_loss_pct: float,
                     room: float) -> list:
    """The candidate set (Module 5). Deliberately small.

    Seven concepts, each with at most two parameters, and no grid search over
    them. A sweep of forty variants would find one that fits this history
    beautifully and nothing else, which is the failure mode Module 15 names
    first — so the parameters here are structural (how many confirmations, how
    much room) rather than fitted, and the walk-forward is what decides
    whether the concept survives, not which decimal of it does.
    """
    return [
        Candidate("A_existing", "Existing exits, unchanged (the baseline)",
                  parameters=0, rule=None),
        Candidate("B_time", "Time-based: flat or red after 30 minutes",
                  parameters=2, rule=_rule_time(30.0, 0.0)),
        Candidate("C_thesis", "Thesis invalidation (the live detector)",
                  parameters=2, rule=_rule_thesis(cfg, thesis_of, 3, room)),
        Candidate("D_volatility", "Volatility-adjusted stop (pre-entry sized)",
                  parameters=2, rule=_rule_volatility(2.0, 0.10)),
        Candidate("E_market_state", "Market-state invalidation",
                  parameters=1, rule=_rule_market_state(3)),
        Candidate("F_wallet", "Wallet-signal invalidation",
                  parameters=1, rule=_rule_wallet(2)),
        Candidate("G_hybrid", "Existing stop OR confirmed thesis failure",
                  parameters=3,
                  rule=_rule_hybrid(cfg, thesis_of, 3, room, stop_loss_pct)),
    ]


# ---------------------------------------------------------------------------
# Module 16 — the composite objective
# ---------------------------------------------------------------------------


def composite_score(candidate: Metrics, baseline: Metrics,
                    parameters: int = 0, folds_stable: int = 0,
                    folds_total: int = 0) -> dict:
    """Rank candidates on the whole distribution, never on win rate.

    Positive terms are improvements over the BASELINE, each normalised so no
    single one can dominate, and each expressed as a fraction rather than in
    dollars so a study of a $40 account and a study of a $4,000 one produce
    comparable numbers.

    Win rate is deliberately absent from the score. It is reported everywhere
    and it enters no ranking, because Module 17's example — 80% winners and
    negative expectancy — scores well on any objective that includes it.
    """
    def improvement(new: float, old: float, floor: float = 1e-9) -> float:
        base = max(abs(old), floor)
        return (new - old) / base

    expectancy = improvement(candidate.expectancy, baseline.expectancy)
    drawdown = improvement(baseline.max_drawdown, candidate.max_drawdown)
    tail = improvement(baseline.p95_loss, candidate.p95_loss)
    catastrophic = improvement(abs(baseline.largest_loser),
                               abs(candidate.largest_loser))
    factor = improvement(candidate.profit_factor, baseline.profit_factor)
    # Winner preservation is a RATIO, not an improvement: the objective is to
    # keep what the winners were, and exceeding it is not a bonus worth paying
    # complexity for. Clamped at 1.0 so a candidate cannot buy its way past the
    # penalties by inflating one large winner.
    preservation = min(1.0, (candidate.avg_winner / baseline.avg_winner)
                       if baseline.avg_winner > 0 else 0.0)

    # Stability is measured against MIN_STABLE_FOLDS even when fewer folds
    # exist, so "improved in the only fold we ran" scores as the one-third of
    # an answer it is rather than as a perfect record. Without the floor, a
    # candidate with a single fold reads as maximally stable and collects both
    # the stability term and a zero instability penalty — which is how a rule
    # measured once ends up out-ranking one measured five times.
    stability = (folds_stable / max(folds_total, MIN_STABLE_FOLDS)) \
        if folds_total else 0.0

    positives = (
        1.00 * _cap(expectancy) + 0.60 * _cap(factor)
        + 0.60 * _cap(drawdown) + 0.80 * _cap(tail)
        + 0.60 * _cap(catastrophic) + 0.80 * preservation
        + 1.00 * stability)

    # -- the penalties -----------------------------------------------------
    complexity = 0.05 * max(0, parameters)
    small_sample = 0.0
    if candidate.n < MIN_TRADES_FOR_A_CANDIDATE:
        # Two factors, because a thin sample is not equally suspicious in every
        # direction. The first is how far short of the floor it is, so 29
        # trades is not treated like 3. The second scales with the SIZE of the
        # improvement being claimed: a 40x expectancy gain measured over four
        # trades is a statement about the four trades, and without this term it
        # collects the full capped positive and beats an honest candidate
        # measured over two hundred. An extraordinary claim from a thin sample
        # is discounted in proportion to how extraordinary it is.
        shortfall = 1.0 - candidate.n / MIN_TRADES_FOR_A_CANDIDATE
        small_sample = 0.5 * shortfall * (1.0 + max(0.0, _cap(expectancy)))
    instability = 0.5 * (1.0 - stability) if folds_total else 0.5
    winner_damage = 1.5 * max(0.0, 1.0 - preservation)

    score = positives - complexity - small_sample - instability - winner_damage
    return {
        "score": round(score, 4),
        "expectancyImprovement": round(expectancy, 4),
        "profitFactorImprovement": round(factor, 4),
        "drawdownImprovement": round(drawdown, 4),
        "lossTailImprovement": round(tail, 4),
        "catastrophicImprovement": round(catastrophic, 4),
        "winnerPreservation": round(preservation, 4),
        "outOfSampleStability": round(stability, 4),
        "penalties": {
            "complexity": round(complexity, 4),
            "smallSample": round(small_sample, 4),
            "instability": round(instability, 4),
            "winnerDamage": round(winner_damage, 4),
        },
    }


def _cap(value: float, limit: float = 2.0) -> float:
    """Bound one term's contribution.

    A candidate that improves expectancy by 40x on four trades is reporting a
    small sample, not a 40x edge, and without this it would out-score every
    honest candidate on the strength of that one number.
    """
    return max(-limit, min(limit, value))


# ---------------------------------------------------------------------------
# Module 14 — walk-forward validation
# ---------------------------------------------------------------------------


def walk_forward(candidate: Candidate, paths: list, fee_model: float,
                 folds: int = 4) -> dict:
    """Chronological folds: train on the past, validate on what came next.

    The trades are ordered by ENTRY time and cut into `folds + 1` blocks. Fold
    *i* trains on blocks 0..i and validates on block i+1, so every validation
    block is strictly later than everything the candidate was measured on.

    There is no fitting step here, and that is deliberate rather than a gap:
    these candidates have structural parameters, not fitted ones, so "train"
    means "the evidence available at that point" and the honest test is whether
    the concept keeps working on data that came after it. A version of this
    that fitted a threshold per fold would need the fitting to happen strictly
    inside the training block, and the moment that is added, this function is
    where it has to go.
    """
    ordered = sorted([p for p in paths if p.trade.entry_ts > 0],
                     key=lambda p: p.trade.entry_ts)
    if len(ordered) < MIN_TRADES_PER_FOLD * 2:
        return {"available": False,
                "reason": (f"{len(ordered)} trades cannot support a "
                           "walk-forward; at least "
                           f"{MIN_TRADES_PER_FOLD * 2} are needed"),
                "folds": [], "stable": 0, "total": 0}

    blocks = folds + 1
    size = len(ordered) // blocks
    if size < MIN_TRADES_PER_FOLD:
        blocks = max(2, len(ordered) // MIN_TRADES_PER_FOLD)
        size = len(ordered) // blocks

    results = []
    stable = 0
    for index in range(1, blocks):
        validate = ordered[index * size: (index + 1) * size] \
            if index < blocks - 1 else ordered[index * size:]
        if len(validate) < MIN_TRADES_PER_FOLD:
            continue
        base = measure([_actual(p.trade) for p in validate])
        got = measure(replay(candidate, validate, fee_model))
        improved = (got.expectancy > base.expectancy
                    or (got.max_drawdown < base.max_drawdown
                        and got.expectancy >= base.expectancy))
        stable += 1 if improved else 0
        results.append({
            "fold": index, "trades": len(validate),
            "from": validate[0].trade.entry_ts,
            "to": validate[-1].trade.entry_ts,
            "baseline": base.to_dict(), "candidate": got.to_dict(),
            "improved": improved,
            "changedTrades": sum(1 for o in replay(candidate, validate,
                                                   fee_model) if o.changed),
        })
    return {"available": bool(results), "folds": results,
            "stable": stable, "total": len(results)}


# ---------------------------------------------------------------------------
# Module 21 — the promotion gate
# ---------------------------------------------------------------------------


def promotion_verdict(candidate: Candidate, full: Metrics, baseline: Metrics,
                      forward: dict, score: dict) -> dict:
    """SHADOW or ENABLED, and the named reason for whichever it is.

    All seven of Module 21's requirements, checked independently. The default
    is to refuse: a candidate is promoted when every check passes, not when no
    check happens to object, and a missing walk-forward is a refusal rather
    than a pass.
    """
    failures = []
    passes = []

    # 1. out-of-sample expectancy improvement OR meaningful drawdown reduction
    improved_expectancy = full.expectancy > baseline.expectancy
    improved_drawdown = (full.max_drawdown < baseline.max_drawdown * 0.9
                         and full.expectancy >= baseline.expectancy)
    if improved_expectancy or improved_drawdown:
        passes.append("improves expectancy or materially reduces drawdown")
    else:
        failures.append(
            f"neither improved: expectancy {full.expectancy:+.4f} vs "
            f"{baseline.expectancy:+.4f}, drawdown {full.max_drawdown:.2f} vs "
            f"{baseline.max_drawdown:.2f}")

    # 2. no material destruction of profitable trades
    preservation = score.get("winnerPreservation", 0.0)
    if preservation >= 0.90:
        passes.append(f"winners preserved ({preservation:.0%} of average)")
    else:
        failures.append(
            f"destroys profitable trades: average winner is {preservation:.0%} "
            "of the baseline's, and the take-profit engine is what is making "
            "the money")

    # 3. lower or equal catastrophic-loss exposure
    if abs(full.largest_loser) <= abs(baseline.largest_loser) + 1e-9:
        passes.append("largest loss no worse")
    else:
        failures.append(
            f"largest loss is worse: {full.largest_loser:.2f} vs "
            f"{baseline.largest_loser:.2f}")

    # 4. stable across multiple periods
    stable, total = forward.get("stable", 0), forward.get("total", 0)
    if total and stable >= MIN_STABLE_FOLDS and stable / total >= 0.6:
        passes.append(f"stable in {stable} of {total} out-of-sample folds")
    else:
        failures.append(
            f"not stable out-of-sample: improved in {stable} of {total} "
            f"fold(s), needs at least {MIN_STABLE_FOLDS} and 60%")

    # 5. sufficient trade count
    if full.n >= MIN_TRADES_FOR_A_CANDIDATE:
        passes.append(f"{full.n} trades")
    else:
        failures.append(
            f"only {full.n} trades; {MIN_TRADES_FOR_A_CANDIDATE} is the floor "
            "for a claim, and nothing may be promoted on this sample")

    # 6. no obvious future-data leakage
    #    Structural, not statistical: every rule reads rows strictly between
    #    entry and exit, the volatility rule reads only pre-entry rows, and
    #    the walk-forward validates strictly forward in time. The check that
    #    can still fail is a candidate that only ever fires at or after the
    #    real exit, which means it is being credited for exits it did not make.
    if not candidate.supported:
        failures.append("unsupported by the captured data: "
                        + candidate.unsupported_reason)
    else:
        passes.append("no future data reachable by construction")

    # 7. no excessive complexity
    if candidate.parameters <= 3:
        passes.append(f"{candidate.parameters} parameter(s)")
    else:
        failures.append(f"{candidate.parameters} parameters is too many to "
                        "support on this much history")

    return {
        "promote": not failures,
        "status": "ENABLED" if not failures else "SHADOW",
        "passed": passes, "failed": failures,
        "note": ("A candidate stays in SHADOW unless every requirement passes. "
                 "Promotion is a human action taken on this report — setting "
                 "engine.consistency.mode to enforce — never something this "
                 "module can do to a running bot."),
    }


# ---------------------------------------------------------------------------
# Module 17 — the win-rate sanity check
# ---------------------------------------------------------------------------


def win_rate_guard(full: Metrics, baseline: Metrics) -> dict:
    """Did the win rate go up while the money went down?

    Reported for every candidate whether or not it triggers, because the point
    is to make the trade-off visible rather than to catch it once.
    """
    better_rate = full.win_rate > baseline.win_rate
    worse_money = full.expectancy < baseline.expectancy
    verdict = "ok"
    reading = ""
    if better_rate and worse_money:
        verdict = "reject"
        reading = (f"Win rate rose from {baseline.win_rate:.0%} to "
                   f"{full.win_rate:.0%} while expectancy FELL from "
                   f"{baseline.expectancy:+.4f} to {full.expectancy:+.4f}. "
                   "More winners, less money. Reject.")
    elif not better_rate and not worse_money:
        reading = (f"Win rate fell from {baseline.win_rate:.0%} to "
                   f"{full.win_rate:.0%} while expectancy ROSE from "
                   f"{baseline.expectancy:+.4f} to {full.expectancy:+.4f}. "
                   "Fewer winners, more money — an acceptance candidate.")
    return {"verdict": verdict, "reading": reading,
            "winRate": round(full.win_rate, 4),
            "baselineWinRate": round(baseline.win_rate, 4)}


# ---------------------------------------------------------------------------
# Module 18 — the loss distribution
# ---------------------------------------------------------------------------


def loss_distribution(outcomes: list) -> dict:
    """What the losses actually look like, and what compressing them costs."""
    losses = sorted(-o.pnl for o in outcomes if o.pnl < 0)
    if not losses:
        return {"available": False, "reason": "no losing trades"}
    return {
        "available": True, "count": len(losses),
        "average": round(_mean(losses), 4),
        "median": round(_median(losses), 4),
        "p95": round(percentile(losses, 0.95), 4),
        "max": round(max(losses), 4),
        "total": round(sum(losses), 4),
        "note": ("The target is the smallest loss distribution that still "
                 "preserves the winner distribution — see winnerRoom. A "
                 "smaller number here bought by killing winners is not an "
                 "improvement."),
    }


# ---------------------------------------------------------------------------
# Module 23 — protected growth
# ---------------------------------------------------------------------------


def protected_growth(outcomes: list, starting_balance: float = 0.0) -> dict:
    """How much of what was made is still there.

    The equity curve is built in exit order from the realised P&L, which is the
    only curve a closed-trade record can support. Stated so nobody reads it as
    a mark-to-market drawdown including open positions.
    """
    ordered = sorted(outcomes, key=lambda o: o.exit_ts or 0.0)
    equity = float(starting_balance or 0.0)
    peak = equity
    worst = 0.0
    worst_pct = 0.0
    for outcome in ordered:
        equity += outcome.pnl
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
        if peak > 0:
            worst_pct = max(worst_pct, (peak - equity) / peak)
    gains = sum(o.pnl for o in ordered if o.pnl > 0)
    losses = -sum(o.pnl for o in ordered if o.pnl < 0)
    winners = sorted((o for o in ordered if o.pnl > 0),
                     key=lambda o: o.pnl, reverse=True)[:5]
    losers = sorted((o for o in ordered if o.pnl < 0),
                    key=lambda o: o.pnl)[:5]
    return {
        "startingEquity": round(float(starting_balance or 0.0), 4),
        "peakEquity": round(peak, 4),
        "currentEquity": round(equity, 4),
        "maxDrawdown": round(worst, 4),
        "maxDrawdownPct": round(worst_pct, 4),
        "retainedFromPeak": round(equity / peak, 4) if peak > 0 else 0.0,
        "grossProfit": round(gains, 4), "grossLoss": round(losses, 4),
        "profitFactor": round(gains / losses, 4) if losses > 0 else 0.0,
        "largestLoss": round(min([o.pnl for o in ordered], default=0.0), 4),
        "topFiveWinners": [round(o.pnl, 4) for o in winners],
        "topFiveLosers": [round(o.pnl, 4) for o in losers],
        "topFiveWinnerShare": round(
            sum(o.pnl for o in winners) / gains, 4) if gains > 0 else 0.0,
        "note": ("Realised, in exit order. Open positions are not marked into "
                 "this curve, so it is the drawdown of banked results and not "
                 "of the account at every instant."),
    }


# ---------------------------------------------------------------------------
# Module 12 — wallet-specific behaviour, with shrinkage
# ---------------------------------------------------------------------------


def wallet_diagnostics(trades: list, prior_weight: float = 10.0) -> dict:
    """Per-wallet performance, shrunk toward the population mean.

    A wallet with five trades and a 100% record is not better than one with
    five hundred and a 62% record; it is less measured. Every rate here is
    shrunk toward the population's own rate with a prior worth
    `prior_weight` trades, so a small sample is pulled to the average and can
    never top the ranking on the strength of being small.

    Ranking is by shrunk EXPECTANCY, not by raw historical return, for the
    reason Module 12 gives explicitly.
    """
    by_wallet: dict[str, list] = {}
    for trade in trades:
        for label in [w.strip() for w in
                      (trade.wallet_influence or "").split(",") if w.strip()]:
            by_wallet.setdefault(label, []).append(trade)
    if not by_wallet:
        return {"available": False,
                "reason": "no trade records a wallet influence"}

    all_pnl = [t.realized_pnl for t in trades]
    population_expectancy = _mean(all_pnl)
    population_win = (sum(1 for p in all_pnl if p > 0) / len(all_pnl)) \
        if all_pnl else 0.0

    rows = []
    for label, group in sorted(by_wallet.items()):
        pnls = [t.realized_pnl for t in group]
        n = len(pnls)
        weight = n / (n + prior_weight)
        raw_expectancy = _mean(pnls)
        raw_win = sum(1 for p in pnls if p > 0) / n
        maes = [abs(t.mae) for t in group if "excursions" not in t.unavailable]
        mfes = [t.mfe for t in group if "excursions" not in t.unavailable]
        rows.append({
            "wallet": label, "trades": n,
            "rawExpectancy": round(raw_expectancy, 4),
            "shrunkExpectancy": round(
                weight * raw_expectancy
                + (1 - weight) * population_expectancy, 4),
            "rawWinRate": round(raw_win, 4),
            "shrunkWinRate": round(
                weight * raw_win + (1 - weight) * population_win, 4),
            "confidence": round(weight, 4),
            "net": round(sum(pnls), 4),
            "avgWinner": round(_mean([p for p in pnls if p > 0]), 4),
            "avgLoser": round(_mean([p for p in pnls if p <= 0]), 4),
            "worstLoss": round(min(pnls), 4),
            "p95Loss": round(percentile(sorted(-p for p in pnls if p < 0),
                                        0.95), 4),
            "medianMae": round(_median(maes), 4) if maes else None,
            "medianMfe": round(_median(mfes), 4) if mfes else None,
            "medianHoldSeconds": round(
                _median([t.hold_seconds for t in group]), 1),
            "trustworthy": n >= 20,
        })
    rows.sort(key=lambda r: r["shrunkExpectancy"], reverse=True)
    return {
        "available": True, "wallets": rows,
        "populationExpectancy": round(population_expectancy, 4),
        "priorWeight": prior_weight,
        "note": ("`confidence` is n/(n+prior): how much of the shrunk number "
                 "is this wallet's own record rather than the population's. "
                 "A wallet below 20 trades is marked untrustworthy and no "
                 "filter may be built on it."),
    }


# ---------------------------------------------------------------------------
# Module 13 — regime awareness
# ---------------------------------------------------------------------------


def regime_diagnostics(trades: list) -> dict:
    """Does performance differ by liquidity, horizon, category, price?

    Descriptive ONLY. Every row carries `actionable`, which is false unless the
    bucket has enough trades to support a claim — and even a true one is a
    reason to run a walk-forward on that split, never a reason to add a filter.
    In-sample differences between buckets are the single easiest thing in this
    whole report to over-read.
    """
    def bucket(key_of, label) -> dict:
        groups: dict[str, list] = {}
        for trade in trades:
            key = key_of(trade)
            if key:
                groups.setdefault(str(key), []).append(trade)
        rows = []
        for key, group in sorted(groups.items()):
            pnls = [t.realized_pnl for t in group]
            rows.append({
                "key": key, "trades": len(pnls),
                "expectancy": round(_mean(pnls), 4),
                "winRate": round(sum(1 for p in pnls if p > 0) / len(pnls), 4),
                "net": round(sum(pnls), 4),
                "worstLoss": round(min(pnls), 4),
                "actionable": len(group) >= 12,
            })
        rows.sort(key=lambda r: r["expectancy"], reverse=True)
        return {"dimension": label, "buckets": rows}

    def price_band(trade):
        price = trade.entry_price
        if price <= 0:
            return ""
        for edge, name in ((0.20, "0.00-0.20"), (0.40, "0.20-0.40"),
                           (0.60, "0.40-0.60"), (0.80, "0.60-0.80")):
            if price < edge:
                return name
        return "0.80-1.00"

    def hold_band(trade):
        minutes = trade.hold_seconds / 60.0
        for edge, name in ((5, "<5m"), (30, "5-30m"), (120, "30m-2h"),
                           (720, "2-12h")):
            if minutes < edge:
                return name
        return ">12h"

    return {
        "available": bool(trades),
        "dimensions": [
            bucket(lambda t: t.liquidity_bucket, "liquidity"),
            bucket(lambda t: t.ttr_bucket, "time to resolution"),
            bucket(lambda t: t.category, "market category"),
            bucket(price_band, "entry price band"),
            bucket(hold_band, "holding period"),
        ],
        "note": ("Descriptive. A difference between buckets here is a "
                 "hypothesis, not a filter — Module 13 requires walk-forward "
                 "validation before any of these may gate a trade, and a "
                 "bucket under 12 trades is marked not actionable."),
    }


# ---------------------------------------------------------------------------
# Module 20 — what the shadow layer actually did
# ---------------------------------------------------------------------------


def shadow_review(journal_path: str | Path, trades: list) -> dict:
    """Every hypothetical exit the live layer proposed, against what happened.

    This is the measurement the whole shadow mode exists to produce, and it is
    the one that can veto the layer outright: a rule that avoids a $5 loss by
    sacrificing a $20 winner is worse than no rule, and that only becomes
    visible by comparing the proposed exit with the trade's actual outcome.

    `avoided` is loss the proposal would have prevented; `sacrificed` is profit
    it would have given up. Both are reported, always, and the verdict is the
    net — never `avoided` alone.
    """
    conn = _connect_ro(journal_path)
    if conn is None:
        return {"available": False, "reason": "journal not readable"}
    try:
        rows = _rows(conn, "SELECT * FROM consistency_shadow ORDER BY ts")
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    if not rows:
        return {"available": False,
                "reason": ("the consistency layer has not proposed any exit "
                           "yet — it is in shadow mode and this fills in as "
                           "positions are managed")}

    by_life = {t.lifecycle_id: t for t in trades}
    detail = []
    avoided = 0.0
    sacrificed = 0.0
    for row in rows:
        trade = by_life.get(int(row.get("lifecycle_id") or 0))
        if trade is None:
            continue        # still open; nothing to compare against yet
        # What the proposed exit would have banked, on the same terms as the
        # real one: the position's size at the proposed price, less the fees
        # the real trade actually paid.
        hypothetical = ((float(row.get("price") or 0.0) - trade.entry_price)
                        * trade.entry_size - trade.fees)
        difference = hypothetical - trade.realized_pnl
        if difference > 0:
            avoided += difference
        else:
            sacrificed += -difference
        detail.append({
            "lifecycleId": trade.lifecycle_id,
            "style": row.get("style"), "health": row.get("health"),
            "enforced": bool(row.get("enforced")),
            "proposedAt": row.get("ts"),
            "proposedPrice": round(float(row.get("price") or 0.0), 4),
            "proposedReturnPct": round(float(row.get("return_pct") or 0.0), 4),
            "hypotheticalPnl": round(hypothetical, 4),
            "actualPnl": round(trade.realized_pnl, 4),
            "difference": round(difference, 4),
            "actualExitStyle": trade.exit_style,
            "wasWinner": trade.realized_pnl > 0,
        })

    net = avoided - sacrificed
    winners_hit = [d for d in detail if d["wasWinner"]]
    verdict = "insufficient evidence"
    if len(detail) >= MIN_TRADES_FOR_A_CANDIDATE:
        verdict = "net positive" if net > 0 else "net negative"
    return {
        "available": True,
        "proposals": len(rows), "resolved": len(detail),
        "stillOpen": len(rows) - len(detail),
        "lossAvoided": round(avoided, 4),
        "profitSacrificed": round(sacrificed, 4),
        "net": round(net, 4),
        "winnersInterrupted": len(winners_hit),
        "profitLostToWinnersInterrupted": round(
            sum(-d["difference"] for d in winners_hit
                if d["difference"] < 0), 4),
        "verdict": verdict,
        "trades": detail,
        "note": ("Loss avoided MINUS profit sacrificed. A rule that avoids a "
                 "$5 loss and sacrifices a $20 winner is a bad rule however "
                 "good the first number looks on its own."),
    }


# ---------------------------------------------------------------------------
# Module 24 — the three-system comparison
# ---------------------------------------------------------------------------


def compare_systems(paths: list, candidate: Optional[Candidate],
                    fee_model: float, starting_balance: float = 0.0) -> dict:
    """A / B / C, on the same trades, measured the same way.

    A — the original system, which is what the journal recorded.
    B — A plus the surgical risk patch. The risk patch is an ENTRY-side
        control: it blocks and shrinks new positions and by construction
        cannot close one. Replaying it over a closed-trade record would
        require re-deciding which trades were opened at all, which this data
        cannot support — so B is reported as identical to A on the trades that
        happened, and that identity is stated rather than presented as a
        finding. What the risk patch changes is which trades exist, and that
        is measurable only forward, in the live A/B the patch already logs.
    C — B plus this layer's best candidate, replayed.
    """
    actual = [_actual(p.trade) for p in paths]
    result = {
        "A_original": {
            "label": "Original system (what the journal recorded)",
            "metrics": measure(actual).to_dict(),
            "protectedGrowth": protected_growth(actual, starting_balance),
        },
        "B_riskPatch": {
            "label": "Original + surgical risk patch",
            "metrics": measure(actual).to_dict(),
            "protectedGrowth": protected_growth(actual, starting_balance),
            "identicalToA": True,
            "why": ("The risk patch governs ENTRIES — it blocks new positions "
                    "and shrinks stakes, and it is structurally unable to "
                    "close one. On a record of trades that were already "
                    "opened it therefore changes nothing, and showing a "
                    "different number here would mean the replay had invented "
                    "one. Its effect is on which trades exist at all, which "
                    "only the live shadow A/B can measure."),
        },
    }
    if candidate is None:
        result["C_consistency"] = {
            "label": "Original + risk patch + consistency layer",
            "available": False,
            "reason": "no candidate survived to be compared",
        }
        return result
    outcomes = replay(candidate, paths, fee_model)
    result["C_consistency"] = {
        "label": f"Original + risk patch + {candidate.label}",
        "candidate": candidate.key,
        "metrics": measure(outcomes).to_dict(),
        "protectedGrowth": protected_growth(outcomes, starting_balance),
        "tradesChanged": sum(1 for o in outcomes if o.changed),
        "lossDistribution": loss_distribution(outcomes),
    }
    return result


def keep_or_reject(comparison: dict) -> dict:
    """Module 25: does C actually beat B, or does it only look tidier?

    Rejects on any of the four things Module 25 names — lower expectancy,
    lower total return, materially smaller winners, worse stability — and says
    which. B is the incumbent and keeps the tie.
    """
    c = comparison.get("C_consistency") or {}
    if not c.get("metrics"):
        return {"decision": "KEEP B", "reasons": ["C could not be evaluated"]}
    b = comparison["B_riskPatch"]["metrics"]
    got = c["metrics"]
    reasons = []
    if got["expectancy"] < b["expectancy"]:
        reasons.append(f"expectancy fell: {got['expectancy']:+.4f} vs "
                       f"{b['expectancy']:+.4f}")
    if got["net"] < b["net"]:
        reasons.append(f"total return fell: {got['net']:+.2f} vs "
                       f"{b['net']:+.2f}")
    if b["avgWinner"] > 0 and got["avgWinner"] < b["avgWinner"] * 0.9:
        reasons.append(f"winners materially smaller: average winner "
                       f"{got['avgWinner']:.2f} vs {b['avgWinner']:.2f}")
    improvements = []
    if got["maxDrawdown"] < b["maxDrawdown"]:
        improvements.append("lower drawdown")
    if got["p95Loss"] < b["p95Loss"]:
        improvements.append("smaller loss tail")
    if got["expectancy"] > b["expectancy"]:
        improvements.append("higher expectancy")
    return {
        "decision": "KEEP B" if reasons else
                    ("ADOPT C" if improvements else "KEEP B"),
        "reasons": reasons or (["C improves: " + ", ".join(improvements)]
                               if improvements else
                               ["C changes nothing measurable; the incumbent "
                                "keeps the tie"]),
    }


# ---------------------------------------------------------------------------
# the whole study
# ---------------------------------------------------------------------------


def report(journal_path: str | Path, intel_path: str | Path = "",
           config: Any = None, starting_balance: float = 0.0) -> dict:
    """Every module in this patch, in one read-only pass.

    Order matters: measure the winner distribution BEFORE evaluating any stop
    candidate, so the room a winner needs is established from the record rather
    than negotiated afterwards against a candidate that would like it smaller.
    """
    trades = reconstruct(journal_path)
    if not trades:
        return {"available": False,
                "reason": "no closed trades in the journal yet"}

    out: dict[str, Any] = {"available": True}
    actual = [_actual(t) for t in trades]
    baseline = measure(actual)
    out["baseline"] = baseline.to_dict()
    out["protectedGrowth"] = protected_growth(actual, starting_balance)
    out["lossDistribution"] = loss_distribution(actual)
    out["winnerRoom"] = winner_room(trades)
    out["walletDiagnostics"] = wallet_diagnostics(trades)
    out["regimeDiagnostics"] = regime_diagnostics(trades)
    out["shadowReview"] = shadow_review(journal_path, trades)

    if not intel_path:
        out["candidates"] = {
            "available": False,
            "reason": ("no intel store supplied, so no price path can be "
                       "replayed and no candidate can be evaluated")}
        out["comparison"] = compare_systems([], None, 0.0, starting_balance)
        return out

    history = PathHistory(intel_path)
    try:
        paths = build_paths(trades, history)
        out["coverage"] = coverage(paths)
        out["separation"] = separation_analysis(paths)

        # The fee a hypothetical exit is charged: this history's own average
        # round-trip cost. Zero when the venue never reported one, which is
        # stated rather than assumed to mean free.
        fees = [t.fees for t in trades if t.fees > 0]
        fee_model = _mean(fees) if fees else 0.0

        exits = getattr(getattr(config, "engine", None), "exits", None)
        cfg = getattr(getattr(config, "engine", None), "consistency", None)
        if cfg is None:
            from ..config import ConsistencyConfig
            cfg = ConsistencyConfig()
        stop_loss = float(getattr(exits, "stop_loss_pct", 0.25) or 0.25)

        # The room a winner needs, taken from the measured distribution — the
        # 90th percentile of winner MAE, so a rule may only act outside the
        # range nine winners in ten stay inside. Falls back to the existing
        # stop distance when there is no distribution yet, which makes the
        # candidate inert rather than aggressive.
        room = float((out["winnerRoom"].get("winners") or {}).get("p90")
                     or stop_loss)

        theses = _thesis_index(journal_path, trades)
        candidates = build_candidates(
            cfg, lambda t: theses.get(t.lifecycle_id), stop_loss, room)

        replayable = [p for p in paths if p.replayable]
        base_replayable = measure([_actual(p.trade) for p in replayable])
        results = []
        for candidate in candidates:
            outcomes = replay(candidate, replayable, fee_model)
            metrics = measure(outcomes)
            forward = walk_forward(candidate, replayable, fee_model)
            score = composite_score(
                metrics, base_replayable, candidate.parameters,
                forward.get("stable", 0), forward.get("total", 0))
            results.append({
                "key": candidate.key, "label": candidate.label,
                "parameters": candidate.parameters,
                "tradesChanged": sum(1 for o in outcomes if o.changed),
                "metrics": metrics.to_dict(),
                "score": score,
                "walkForward": {k: v for k, v in forward.items()
                                if k != "folds"},
                "folds": forward.get("folds", []),
                "winRateGuard": win_rate_guard(metrics, base_replayable),
                "promotion": promotion_verdict(candidate, metrics,
                                               base_replayable, forward,
                                               score),
                "lossDistribution": loss_distribution(outcomes),
            })

        # The baseline is in the list as A and must not be able to "win" its
        # own comparison, so the best candidate is chosen among the others.
        ranked = sorted([r for r in results if r["key"] != "A_existing"],
                        key=lambda r: r["score"]["score"], reverse=True)
        out["candidates"] = {
            "available": True,
            "baseline": base_replayable.to_dict(),
            "feeModel": round(fee_model, 6),
            "winnerRoomUsed": round(room, 4),
            "results": results,
            "best": ranked[0]["key"] if ranked else None,
            "promotable": [r["key"] for r in results
                           if r["promotion"]["promote"]],
            "note": ("Candidates are additions to the existing exit set, not "
                     "replacements for it: a trade the candidate does not "
                     "fire on keeps the outcome the existing stop, "
                     "take-profit and trailing exits gave it. That is what "
                     "Layer 2 does in production, so it is what is measured."),
        }

        best = None
        if ranked:
            chosen = ranked[0]["key"]
            best = next(c for c in candidates if c.key == chosen)
        out["comparison"] = compare_systems(replayable, best, fee_model,
                                            starting_balance)
        out["decision"] = keep_or_reject(out["comparison"])
    finally:
        history.close()
    return out


def render(data: dict) -> str:
    """The study as text, for the CLI. Nothing is hidden and nothing is spun.

    Deliberately leads with the baseline and the winner distribution rather
    than with the best candidate: the reader needs to know what is currently
    working and how much room a winner needs BEFORE seeing a proposal, or the
    proposal frames the evidence instead of the other way round.
    """
    if not data.get("available"):
        return f"Consistency study unavailable: {data.get('reason', '')}"

    lines = ["CONSISTENCY / LOSS-MINIMISATION STUDY",
             "=" * 72,
             "Read-only. Nothing here has been applied. Promotion out of "
             "shadow mode is a", "human action taken on this report.", ""]

    base = data["baseline"]
    lines += [
        "BASELINE — what actually happened",
        f"  {base['n']} closed trades · net {base['net']:+.2f} · expectancy "
        f"{base['expectancy']:+.4f}/trade",
        f"  win rate {base['winRate']:.0%} · profit factor "
        f"{base['profitFactor']:.2f} · average winner {base['avgWinner']:+.2f}"
        f" · average loser {base['avgLoser']:+.2f}",
        f"  largest winner {base['largestWinner']:+.2f} · largest loser "
        f"{base['largestLoser']:+.2f} · 95th-percentile loss "
        f"{base['p95Loss']:.2f} · max drawdown {base['maxDrawdown']:.2f}",
        ""]

    growth = data.get("protectedGrowth") or {}
    if growth:
        lines += [
            "PROTECTED GROWTH — how much of what was made is still there",
            f"  peak ${growth['peakEquity']:.2f} -> now "
            f"${growth['currentEquity']:.2f} · retained "
            f"{growth['retainedFromPeak']:.0%} of the peak · max drawdown "
            f"${growth['maxDrawdown']:.2f} ({growth['maxDrawdownPct']:.0%})",
            f"  top five winners {growth['topFiveWinners']} = "
            f"{growth['topFiveWinnerShare']:.0%} of all profit — any change "
            "that shrinks these is not an improvement",
            f"  top five losers  {growth['topFiveLosers']}", ""]

    room = data.get("winnerRoom") or {}
    lines.append("WINNER ROOM — how much room a real winner needs (Module 6)")
    if not room.get("available"):
        lines.append(f"  not available: {room.get('reason', '')}")
    else:
        win, loss = room["winners"], room["losers"]
        lines += [
            f"  winners' adverse excursion (n={win['sample']}): median "
            f"{win['median']:.1%} · 75th {win['p75']:.1%} · 90th "
            f"{win['p90']:.1%} · 95th {win['p95']:.1%} · worst "
            f"{win['worst']:.1%}",
            f"  losers'  adverse excursion (n={loss.get('sample', 0)}): median "
            f"{loss.get('median', 0):.1%} · 90th {loss.get('p90', 0):.1%}",
            f"  {room.get('reading', '')}",
            "  what each stop distance would have cost in winners:"]
        for entry in room["killedByStop"]:
            lines.append(
                f"    {entry['stopDistance']:>5.0%} stop -> kills "
                f"{entry['winnersKilled']:>3} winners "
                f"({entry['shareOfWinners']:.0%}), destroying "
                f"{entry['profitDestroyed']:+.2f}; catches "
                f"{entry['losersCaughtEarlier']} losers earlier")
    lines.append("")

    separation = data.get("separation") or {}
    if separation.get("available"):
        lines.append("SEPARATION — when winners start to look different "
                     "(Module 2)")
        if not separation.get("separates"):
            lines.append("  no horizon separates the two groups: at every "
                         "measured point the distributions overlap too "
                         "heavily for a rule to tell them apart.")
        else:
            lines.append(f"  earliest useful horizon: "
                         f"{separation['earliestUsefulSeconds']}s")
        for row in separation.get("horizons", []):
            lines.append(
                f"    +{row['horizonSeconds']:>5}s  winners "
                f"{row['meanWinner']:+.1%} vs losers {row['meanLoser']:+.1%} "
                f"· separation {row['separation']:+.2f} · coverage "
                f"{row['share']:.0%}")
        lines.append("")

    candidates = data.get("candidates") or {}
    lines.append("CANDIDATE EXIT CONCEPTS (Module 5)")
    if not candidates.get("available"):
        lines.append(f"  not evaluated: {candidates.get('reason', '')}")
    else:
        cover = data.get("coverage") or {}
        lines.append(
            f"  replayable: {cover.get('replayable')} of {cover.get('trades')} "
            f"trades ({cover.get('share', 0):.0%}); winner room used "
            f"{candidates['winnerRoomUsed']:.1%}")
        lines.append(f"  {'candidate':<16}{'score':>8}{'changed':>9}"
                     f"{'expect':>10}{'net':>10}{'maxDD':>9}{'p95':>8}"
                     f"{'win%':>7}{'walkfwd':>9}  status")
        for row in sorted(candidates["results"],
                          key=lambda r: -r["score"]["score"]):
            metrics, forward = row["metrics"], row["walkForward"]
            lines.append(
                f"  {row['key']:<16}{row['score']['score']:>+8.3f}"
                f"{row['tradesChanged']:>9}{metrics['expectancy']:>+10.3f}"
                f"{metrics['net']:>+10.2f}{metrics['maxDrawdown']:>9.2f}"
                f"{metrics['p95Loss']:>8.2f}{metrics['winRate']:>7.0%}"
                f"{str(forward.get('stable', 0)) + '/' + str(forward.get('total', 0)):>9}"
                f"  {row['promotion']['status']}")
        for row in candidates["results"]:
            guard = row.get("winRateGuard") or {}
            if guard.get("verdict") == "reject":
                lines.append(f"  ! {row['key']}: {guard['reading']}")
        best_key = candidates.get("best")
        if best_key:
            best = next(r for r in candidates["results"]
                        if r["key"] == best_key)
            lines += ["", f"  BEST: {best['label']}"]
            for reason in best["promotion"]["failed"]:
                lines.append(f"    NOT PROMOTED — {reason}")
            for reason in best["promotion"]["passed"]:
                lines.append(f"    pass — {reason}")
    lines.append("")

    shadow = data.get("shadowReview") or {}
    lines.append("SHADOW REVIEW — what the live layer proposed (Module 20)")
    if not shadow.get("available"):
        lines.append(f"  {shadow.get('reason', '')}")
    else:
        lines += [
            f"  {shadow['proposals']} proposal(s), {shadow['resolved']} now "
            f"resolved, {shadow['stillOpen']} still open",
            f"  loss avoided {shadow['lossAvoided']:+.2f} · profit sacrificed "
            f"{shadow['profitSacrificed']:+.2f} · NET "
            f"{shadow['net']:+.2f} — {shadow['verdict']}",
            f"  winners interrupted: {shadow['winnersInterrupted']}, costing "
            f"{shadow['profitLostToWinnersInterrupted']:+.2f}"]
    lines.append("")

    decision = data.get("decision") or {}
    if decision:
        lines += ["THREE-SYSTEM COMPARISON (Modules 24-25)",
                  f"  DECISION: {decision['decision']}"]
        for reason in decision.get("reasons", []):
            lines.append(f"    {reason}")
        lines.append("")

    wallets = data.get("walletDiagnostics") or {}
    if wallets.get("available"):
        lines.append("WALLETS — shrunk toward the population, not ranked raw "
                     "(Module 12)")
        for row in wallets["wallets"][:10]:
            mark = "" if row["trustworthy"] else "  (too few trades to trust)"
            lines.append(
                f"  {row['wallet'][:28]:<28} n={row['trades']:>4} "
                f"raw {row['rawExpectancy']:+.3f} -> shrunk "
                f"{row['shrunkExpectancy']:+.3f} "
                f"(confidence {row['confidence']:.0%}){mark}")
        lines.append("")

    lines += [
        "This system does not and cannot produce constant wins. The objective "
        "is higher",
        "expectancy with a smaller loss tail and the large winners left "
        "intact — not a",
        "higher win rate, which is a different and easier thing to achieve "
        "badly."]
    return "\n".join(lines)


def _thesis_index(journal_path: str | Path, trades: list) -> dict:
    """``lifecycle_id -> EntryThesis``, read in one pass.

    One query for every entry decision rather than one per trade: the join is
    small and the alternative is a query per trade per candidate per fold.
    """
    from ..consistency import EntryThesis

    conn = _connect_ro(journal_path)
    if conn is None:
        return {}
    try:
        lifecycles = _rows(conn, "SELECT id, entry_decision_id, entry_ts, "
                                 "wallet_influence FROM lifecycles")
        decisions = _rows(conn, "SELECT id, rationale, features FROM decisions")
    finally:
        conn.close()

    by_id = {}
    for row in decisions:
        parsed = {}
        for key in ("rationale", "features"):
            try:
                parsed[key] = json.loads(row.get(key) or "{}") or {}
            except (TypeError, ValueError):
                parsed[key] = {}
        by_id[int(row["id"])] = parsed

    out = {}
    for row in lifecycles:
        decision = by_id.get(int(row.get("entry_decision_id") or 0)) or {}
        out[int(row["id"])] = EntryThesis.from_journal(
            decision.get("rationale"), decision.get("features"),
            entry_ts=float(row.get("entry_ts") or 0.0),
            wallet_influence=str(row.get("wallet_influence") or ""))
    return out
