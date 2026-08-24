"""
Sequential / event-chain discovery: is the ORDER of events informative?

The operator's second research question, alongside single-event discovery
(which continues unchanged): a profitable opportunity may not be one event
but a SEQUENCE — Event A, then Event B, within a bounded time, under a
market state — whose predictive content is invisible when each event is
scored alone.

Discipline over enthusiasm, per the spec:

* Events are extracted from the feature rows the system ALREADY captures,
  with thresholds taken from each series' own distribution — a fixed
  vocabulary of event KINDS, no fixed magic numbers.
* Chains are the n-grams actually OBSERVED (2 -> 3 -> 4 events, never
  longer), not a combinatorial search. Timing between events is recorded
  and bounded.
* A chain is kept only when it (a) recurs enough times across enough
  independent markets, and (b) beats its own best single component on NET
  expectancy after costs — a sequence that adds no incremental information
  is complexity, and complexity is overfitting fuel.
* Direction (UP/DOWN after the chain) is DISCOVERED from the signed
  response, never imposed; confirmation chains and contradiction/reversal
  chains fall out of the same search.
* Survivors become ordinary candidates in the persistent library and walk
  the exact same frozen out-of-sample ladder as every other rule. They are
  explicitly barred from voting in the live engine until an execution path
  is deliberately built after forward validation — discovery is never
  execution.
* Every pass emits a sequence funnel: events observed -> types -> chains
  generated -> sufficient sample -> gross-positive -> net-positive ->
  incremental -> registered, with the primary rejection reason at each
  stage. Zero survivors must explain themselves.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

# The event vocabulary. KINDS is fixed (so signatures are stable across
# passes and machines); what varies per series is where the thresholds sit,
# taken from that series' own quantiles.
KINDS = (
    "price_up_impulse", "price_down_impulse", "price_reversal_up",
    "price_reversal_down", "spread_widening", "spread_tightening",
    "book_flips_bid", "book_flips_ask", "liquidity_drop", "liquidity_surge",
    "tape_buy_pressure", "tape_sell_pressure", "wallet_concentration_spike",
    "anomaly_high", "state_impulse", "state_exhaustion", "state_reversal",
    "vol_expansion", "vol_contraction", "liq_long_pressure",
    "liq_short_pressure", "cluster_burst",
    # Sharp dislocations (analytics.sharp_moves) as EVENTS, so the chain
    # search can discover patterns like "sharp_drop -> liquidity_surge ->
    # recovery" — the operator's §9 coupling. A sharp move is an event,
    # never a complete strategy.
    "sharp_drop", "sharp_rise",
)


@dataclass
class Event:
    row: int                 # index into the series
    ts: float
    kind: str


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if out != out else out


def rows_from_csv(path: str | Path) -> list[dict[str, float]]:
    """An exported research series, numeric, in row order."""
    out: list[dict[str, float]] = []
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                parsed = {k: _num(v) for k, v in row.items()
                          if k != "timestamp"}
                parsed["_ts"] = _parse_stamp(row.get("timestamp"))
                out.append(parsed)
    except (OSError, csv.Error):
        return []
    return out


def _parse_stamp(raw) -> float:
    if not raw:
        return 0.0
    try:
        import datetime as dt
        return dt.datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError):
        return _num(raw)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1))))
    return ordered[index]


def extract_events(rows: list[dict[str, float]]) -> list[Event]:
    """The fixed vocabulary, with thresholds from THIS series' distribution.

    Nothing here claims predictiveness — extraction only names what
    happened, in order, so the mining step can ask whether order matters.
    """
    if len(rows) < 30:
        return []
    price = [r.get("price", 0.0) for r in rows]
    returns = [0.0] + [
        (b - a) / a if a > 0 else 0.0 for a, b in zip(price, price[1:])]
    abs_ret_hi = _quantile([abs(r) for r in returns if r], 0.90)
    spread_rel = [r.get("spread_rel", 0.0) for r in rows]
    spread_hi = _quantile([s for s in spread_rel if s > 0], 0.90)
    spread_lo = _quantile([s for s in spread_rel if s > 0], 0.10)
    conc_hi = _quantile([r.get("wallet_concentration", 0.0) for r in rows],
                        0.90)
    liq = [r.get("log_liquidity", 0.0) for r in rows]

    events: list[Event] = []

    def add(index: int, kind: str) -> None:
        events.append(Event(row=index, ts=rows[index].get("_ts", 0.0),
                            kind=kind))

    prev_state = rows[0].get("ms_state", 0.0)
    for i in range(1, len(rows)):
        row, prior = rows[i], rows[i - 1]
        ret, prev_ret = returns[i], returns[i - 1]

        if abs_ret_hi > 0 and ret >= abs_ret_hi:
            add(i, "price_up_impulse")
        if abs_ret_hi > 0 and ret <= -abs_ret_hi:
            add(i, "price_down_impulse")
        # A reversal is an impulse against the PREVIOUS bar's direction.
        if abs_ret_hi > 0 and ret >= abs_ret_hi and prev_ret < 0:
            add(i, "price_reversal_up")
        if abs_ret_hi > 0 and ret <= -abs_ret_hi and prev_ret > 0:
            add(i, "price_reversal_down")

        if spread_hi > 0 and spread_rel[i] >= spread_hi \
                and spread_rel[i - 1] < spread_hi:
            add(i, "spread_widening")
        if spread_lo > 0 and 0 < spread_rel[i] <= spread_lo \
                and spread_rel[i - 1] > spread_lo:
            add(i, "spread_tightening")

        imb, prev_imb = row.get("depth_imbalance", 0.0), \
            prior.get("depth_imbalance", 0.0)
        if imb > 0.3 and prev_imb <= 0.0:
            add(i, "book_flips_bid")
        if imb < -0.3 and prev_imb >= 0.0:
            add(i, "book_flips_ask")

        if liq[i - 1] > 0 and liq[i] < liq[i - 1] * 0.95:
            add(i, "liquidity_drop")
        if liq[i - 1] > 0 and liq[i] > liq[i - 1] * 1.05:
            add(i, "liquidity_surge")

        tape = row.get("tape_velocity_z", row.get("ms_imbalance", 0.0))
        if tape >= 1.0:
            add(i, "tape_buy_pressure")
        if tape <= -1.0:
            add(i, "tape_sell_pressure")

        if conc_hi > 0 and row.get("wallet_concentration", 0.0) >= conc_hi \
                and prior.get("wallet_concentration", 0.0) < conc_hi:
            add(i, "wallet_concentration_spike")

        if row.get("ms_anomaly", 0.0) >= 70.0 \
                and prior.get("ms_anomaly", 0.0) < 70.0:
            add(i, "anomaly_high")

        # Market-state TRANSITIONS (the change, not the state): the ms_state
        # column encodes DORMANT..REVERSAL ordinally.
        state = row.get("ms_state", 0.0)
        if state != prev_state:
            if state == 2.0:
                add(i, "state_impulse")
            elif state == 4.0:
                add(i, "state_exhaustion")
            elif state == 5.0:
                add(i, "state_reversal")
        prev_state = state

        if i >= 6:
            recent = sum(abs(r) for r in returns[i - 2:i + 1])
            before = sum(abs(r) for r in returns[i - 6:i - 2])
            if before > 0 and recent > before * 2.0:
                add(i, "vol_expansion")
            if recent > 0 and before > recent * 2.0:
                add(i, "vol_contraction")

        if row.get("liq_imbalance", 0.0) <= -0.5 \
                and row.get("liq_long_usd_60s", 0.0) > 0:
            add(i, "liq_long_pressure")
        if row.get("liq_imbalance", 0.0) >= 0.5 \
                and row.get("liq_short_usd_60s", 0.0) > 0:
            add(i, "liq_short_pressure")

    # Sharp dislocations, by the sharp-move module's own adaptive detector —
    # one vocabulary, one definition, shared between both research layers.
    from .sharp_moves import detect as _detect_sharp
    for move in _detect_sharp(rows):
        add(move.row, "sharp_drop" if move.direction == "down"
            else "sharp_rise")

    # Event concentration (the operator's cluster question): three or more
    # distinct kinds landing on one bar is itself an event.
    by_row: dict[int, int] = {}
    for event in events:
        by_row[event.row] = by_row.get(event.row, 0) + 1
    for index, count in by_row.items():
        if count >= 3:
            events.append(Event(row=index, ts=rows[index].get("_ts", 0.0),
                                kind="cluster_burst"))
    events.sort(key=lambda e: (e.row, e.kind))
    return events


# --------------------------------------------------------------------------
# mining
# --------------------------------------------------------------------------

@dataclass
class SequenceStats:
    chain: tuple                      # kinds, in order
    occurrences: int = 0
    markets: set = field(default_factory=set)
    moves: list = field(default_factory=list)     # signed $ move per occurrence
    mfe: list = field(default_factory=list)       # max favorable, per occ.
    mae: list = field(default_factory=list)       # max adverse, per occ.
    gaps: list = field(default_factory=list)      # bars between events

    def direction(self) -> str:
        """Discovered from the signed response — never imposed."""
        mean = sum(self.moves) / len(self.moves) if self.moves else 0.0
        return "up" if mean >= 0 else "down"

    def net_expectancy(self, cost: float) -> float:
        """Mean $/share in the DISCOVERED direction, after a round trip."""
        if not self.moves:
            return 0.0
        mean = sum(self.moves) / len(self.moves)
        return abs(mean) - cost


# How many following events a chain may choose its links from. Real event
# streams carry noise between the links that matter, so chains are ordered
# SUBSEQUENCES within this bounded window — never adjacency-only (one stray
# volatility tick between A and B must not erase A -> B), and never an
# unbounded combinatorial search.
_LOOKAHEAD = 6


def _observed_chains(events: list[Event], max_len: int,
                     gap_bars: int) -> Iterable[tuple[tuple, list[Event]]]:
    """The chains that actually occurred: ordered subsequences, gap-bounded.

    Short first, longer only as extensions of what was observed — never a
    cross-product over the vocabulary. Each distinct kinds-tuple is yielded
    once per anchor event, so one busy window cannot vote twice.
    """
    from itertools import combinations

    for start in range(len(events)):
        first = events[start]
        window: list[Event] = []
        for nxt in range(start + 1, len(events)):
            event = events[nxt]
            last_row = window[-1].row if window else first.row
            if event.row - last_row > gap_bars:
                break
            if event.row == last_row:
                continue                  # same-bar kinds are the cluster event
            window.append(event)
            if len(window) >= _LOOKAHEAD:
                break
        seen: set[tuple] = set()
        for size in range(1, max_len):
            for combo in combinations(range(len(window)), size):
                chain = [first] + [window[i] for i in combo]
                if any(b.row - a.row > gap_bars or b.row <= a.row
                       for a, b in zip(chain, chain[1:])):
                    continue
                kinds = tuple(e.kind for e in chain)
                if kinds in seen:
                    continue
                seen.add(kinds)
                yield kinds, chain


def mine(series: list[tuple[str, list[dict]]], *,
         max_len: int = 4, gap_bars: int = 15, hold_bars: int = 15,
         min_occurrences: int = 8, min_markets: int = 2,
         cost: float = 0.01, top_n: int = 6) -> dict:
    """Search every series' event stream for recurring, PAYING chains.

    Returns {"candidates": [...], "funnel": {...}} where each candidate is
    a frozen rule dict ready for the persistent library, and the funnel
    names the population at every stage plus why candidates died.
    """
    funnel: dict[str, Any] = {"eventsObserved": 0, "eventTypes": 0,
                              "chainsGenerated": 0}
    reject: dict[str, int] = {}
    stats: dict[tuple, SequenceStats] = {}
    singleton: dict[str, SequenceStats] = {}
    kinds_seen: set[str] = set()

    baseline_moves: list[float] = []
    for market_id, rows in series:
        events = extract_events(rows)
        funnel["eventsObserved"] += len(events)
        kinds_seen.update(e.kind for e in events)
        price = [r.get("price", 0.0) for r in rows]

        # Counterfactual control (the operator's §21): how far does this
        # series drift over the same hold with NO event nearby? A chain
        # must beat this, or it has only found periods that were already
        # moving.
        event_rows = {e.row for e in events}
        for i in range(5, len(price) - hold_bars, 20):
            if price[i] <= 0 or any(abs(i - r) <= 3 for r in event_rows):
                continue
            baseline_moves.append(abs(price[i + hold_bars] - price[i]))

        def response(last_row: int):
            entry_row = last_row + 1
            exit_row = min(entry_row + hold_bars, len(rows) - 1)
            if entry_row >= len(rows) or price[entry_row] <= 0:
                return None
            window = price[entry_row:exit_row + 1]
            move = window[-1] - window[0]
            return (move, max(w - window[0] for w in window),
                    min(w - window[0] for w in window))

        for event in events:                       # the comparison baseline
            measured = response(event.row)
            if measured is None:
                continue
            single = singleton.setdefault(event.kind,
                                          SequenceStats(chain=(event.kind,)))
            single.occurrences += 1
            single.markets.add(market_id)
            single.moves.append(measured[0])

        for chain_kinds, chain_events in _observed_chains(
                events, max_len, gap_bars):
            measured = response(chain_events[-1].row)
            if measured is None:
                continue
            entry = stats.setdefault(chain_kinds,
                                     SequenceStats(chain=chain_kinds))
            entry.occurrences += 1
            entry.markets.add(market_id)
            entry.moves.append(measured[0])
            entry.mfe.append(measured[1])
            entry.mae.append(measured[2])
            entry.gaps.extend(b.row - a.row for a, b in
                              zip(chain_events, chain_events[1:]))

    funnel["eventTypes"] = len(kinds_seen)
    funnel["chainsGenerated"] = len(stats)
    baseline = (sum(baseline_moves) / len(baseline_moves)
                if baseline_moves else 0.0)
    funnel["baselineAbsMove"] = round(baseline, 6)

    survivors: list[dict] = []
    sufficient = gross_pos = net_pos = incremental = 0
    for chain_kinds, entry in stats.items():
        # Complexity is paid for with evidence: each extra link raises the
        # occurrence floor, so a 4-chain cannot ride on a 2-chain's sample.
        floor = min_occurrences * (len(chain_kinds) - 1)
        if entry.occurrences < floor or len(entry.markets) < min_markets:
            reject["insufficient sample"] = \
                reject.get("insufficient sample", 0) + 1
            continue
        sufficient += 1
        mean = sum(entry.moves) / len(entry.moves)
        if abs(mean) <= 0:
            reject["no directional response"] = \
                reject.get("no directional response", 0) + 1
            continue
        gross_pos += 1
        net = entry.net_expectancy(cost)
        if net <= 0:
            reject["cannot clear costs"] = \
                reject.get("cannot clear costs", 0) + 1
            continue
        if abs(mean) <= baseline:
            reject["no better than drift (control)"] = \
                reject.get("no better than drift (control)", 0) + 1
            continue
        net_pos += 1
        # THE incremental test: a chain must beat its own best component,
        # measured identically — otherwise the order added nothing and the
        # complexity is dropped, exactly as specified.
        best_component = max(
            (singleton[k].net_expectancy(cost)
             for k in chain_kinds if k in singleton), default=0.0)
        if net <= best_component:
            reject["no incremental value over components"] = \
                reject.get("no incremental value over components", 0) + 1
            continue
        incremental += 1
        gaps = sorted(entry.gaps)
        survivors.append({
            "type": "sequence",
            "chain": list(chain_kinds),
            "direction": entry.direction(),
            "gap_bars": gap_bars,
            "typical_gap_bars": gaps[len(gaps) // 2] if gaps else 0,
            "hold_bars": hold_bars,
            "occurrences": entry.occurrences,
            "markets": len(entry.markets),
            "netExpectancy": round(net, 6),
            "componentBest": round(best_component, 6),
            "meanMfe": round(sum(entry.mfe) / len(entry.mfe), 6)
            if entry.mfe else 0.0,
            "meanMae": round(sum(entry.mae) / len(entry.mae), 6)
            if entry.mae else 0.0,
        })

    # Net expectancy ranks — never win rate. Cap retained: a thousand
    # near-identical chains is a data-mining artifact, not a library.
    survivors.sort(key=lambda s: -s["netExpectancy"])
    survivors = survivors[:top_n]
    funnel.update({
        "sufficientSample": sufficient, "grossPositive": gross_pos,
        "netPositive": net_pos, "incremental": incremental,
        "kept": len(survivors), "rejectReasons": dict(
            sorted(reject.items(), key=lambda kv: -kv[1])),
    })
    return {"candidates": survivors, "funnel": funnel}


def frozen_replay(rows: list[dict], rule: dict, cost: float) -> dict:
    """One FROZEN sequence against one unseen series — the same discipline
    as _frozen_run: the chain, direction, gaps, and hold are used exactly
    as discovered; unseen data may only testify, never tune."""
    chain = tuple(rule.get("chain") or [])
    if not chain or len(rows) < 30:
        return {"trades": 0}
    events = extract_events(rows)
    gap_bars = int(rule.get("gap_bars") or 15)
    hold = int(rule.get("hold_bars") or 15)
    direction = str(rule.get("direction") or "up")
    # ENTRY TIMING as a variable (§4's "altered entry timing"). Zero for
    # every mined chain; non-zero only on the delayed-entry variant the pass
    # registers. Reading it here is what makes that variant a genuine second
    # experiment rather than a re-run of the first under another id.
    delay = max(0, int(rule.get("delay_bars") or 0))
    price = [r.get("price", 0.0) for r in rows]

    pnl: list[float] = []
    used_until = -1
    for start, anchor in enumerate(events):
        if anchor.kind != chain[0] or anchor.row <= used_until:
            continue
        # Greedy earliest completion: match the remaining links in order,
        # skipping noise events, each within gap_bars of the last match.
        matched = [anchor]
        needed = 1
        for event in events[start + 1:]:
            if event.row - matched[-1].row > gap_bars:
                break
            if needed < len(chain) and event.kind == chain[needed] \
                    and event.row > matched[-1].row:
                matched.append(event)
                needed += 1
                if needed == len(chain):
                    break
        if needed != len(chain):
            continue
        entry_row = matched[-1].row + 1 + delay
        if entry_row <= used_until or entry_row >= len(rows) \
                or price[entry_row] <= 0:
            continue
        exit_row = min(entry_row + hold, len(rows) - 1)
        move = price[exit_row] - price[entry_row]
        signed = move if direction == "up" else -move
        pnl.append(signed - cost)
        used_until = exit_row
    if not pnl:
        return {"trades": 0}
    equity = peak = drawdown = 0.0
    for x in pnl:
        equity += x
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    mean = sum(pnl) / len(pnl)
    if len(pnl) >= 10:
        var = sum((x - mean) ** 2 for x in pnl) / (len(pnl) - 1)
        sharpe = (mean / var ** 0.5 * len(pnl) ** 0.5) if var > 0 else 0.0
    else:
        sharpe = 0.0
    return {"trades": len(pnl), "wins": sum(1 for x in pnl if x > 0),
            "pnl": sum(pnl), "expectancy": mean, "drawdown": drawdown,
            "sharpe": sharpe}


def describe(rule: dict) -> str:
    chain = " -> ".join(rule.get("chain") or [])
    return (f"SEQ {rule.get('direction', '?').upper()}: {chain} "
            f"(gaps <= {rule.get('gap_bars', '?')} bars, "
            f"hold {rule.get('hold_bars', '?')} bars)")
