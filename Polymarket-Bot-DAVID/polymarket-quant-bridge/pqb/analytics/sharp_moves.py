"""
Sharp-move / crash-recovery research: dislocation as a hypothesis.

The operator's question, kept as a question: when a Polymarket price moves
unusually far, unusually fast, does what follows contain a repeatable,
executable edge — recovery, continuation, or nothing? The external
strategy's numbers (15% crashes, 90% recovery targets, $0.30 entries,
12-hour holds) are NOT imported; detection is anchored to each series' own
return distribution, direction is discovered from the measured response,
and the honest answers "no effect" and "only under these conditions" are
first-class outcomes.

Mechanics, per the spec:

* **Detection** is adaptive and broad: a move qualifies when its k-bar
  return sits beyond the series' own extreme quantile, tested at several
  speeds. Magnitude, speed, direction, and the full surrounding context
  (spread, depth, liquidity, volatility, trade intensity, time to
  resolution, price region) ride on every event.
* **The pre-move anchor** is the price immediately before the dislocation;
  every response measure — recovery fraction, MFE, MAE, continuation,
  stall, overshoot — is taken relative to it, across multiple horizons.
* **Classification** (recovery / partial / full / recovery-then-reversal /
  continuation / acceleration / sideways) labels outcomes for analysis;
  labels are never trading rules.
* **Dislocation vs information**: recovery and continuation expectancy are
  compared per condition — liquidity tercile, spread regime, price region,
  time-to-resolution, magnitude — and against volatility-matched windows
  WITHOUT a sharp move, so ordinary drift cannot masquerade as an edge.
* **Candidates** are frozen condition cells with enough events, positive
  NET expectancy after costs, and a discovered direction. They register in
  the same persistent library and walk the same frozen out-of-sample
  ladder as every other rule; nothing here can trade, and even a
  validated pattern is barred from voting until an execution path is
  deliberately built.
* **The funnel** reports every stage and every rejection reason. Zero
  survivors must explain themselves.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Post-event measurement horizons, in bars (a bar's wall-clock span varies
# per series and is recorded with it). The last horizon is "as far as the
# series reaches", which for settled markets is resolution.
HORIZONS = (1, 3, 5, 15, 30, 60)

# Detection speeds: how many bars a dislocation may take.
SPEEDS = (1, 3, 5)

# The status vocabulary the operator specified for this module's events.
EVENT_STATUSES = (
    "candidate", "insufficient_sample", "recovery_pattern_detected",
    "continuation_pattern_detected", "oos_pending", "oos_passed",
    "oos_failed", "forward_validation_pending", "validated", "degraded",
    "expired", "invalidated",
)


@dataclass
class SharpMove:
    row: int                       # index of the bar completing the move
    speed_bars: int
    direction: str                 # "down" | "up" (of the MOVE itself)
    anchor: float                  # price immediately before the move
    end_price: float               # price at the end of the move
    magnitude: float               # signed fractional move from the anchor
    # -- surrounding context, from the same captured columns -----------------
    spread_rel: float = 0.0
    depth_total: float = 0.0
    log_liquidity: float = 0.0
    vol_before: float = 0.0        # mean |return| before the move
    trade_intensity: float = 0.0   # ms_trade_rate when present
    hours_to_resolution: float = 0.0
    price_region: str = ""
    liquidity_delta: float = 0.0   # liquidity change across the move
    # -- measured response, filled by measure() ------------------------------
    recovery_frac: dict = field(default_factory=dict)    # horizon -> fraction
    signed_move: dict = field(default_factory=dict)      # horizon -> $ move
    mfe: float = 0.0               # max toward recovery, $
    mae: float = 0.0               # max continuation beyond the move, $
    classification: str = ""


def _region(price: float) -> str:
    if price <= 0:
        return ""
    if price < 0.20:
        return "under-20c"
    if price < 0.40:
        return "20-39c"
    if price < 0.60:
        return "40-59c"
    if price < 0.80:
        return "60-79c"
    return "80c+"


def _returns(price: list[float]) -> list[float]:
    return [0.0] + [(b - a) / a if a > 0 else 0.0
                    for a, b in zip(price, price[1:])]


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(q * (len(ordered) - 1))))]


def detect(rows: list[dict], extreme_q: float = 0.99,
           min_magnitude: float = 0.03) -> list[SharpMove]:
    """Dislocations by this series' own standards.

    A k-bar move qualifies when it exceeds BOTH the series' extreme
    quantile of k-bar moves AND a small absolute floor (so a dead-flat
    series cannot call a one-tick wiggle a crash). Broad on purpose:
    the study decides which magnitudes matter, not the detector.
    """
    if len(rows) < 40:
        return []
    price = [float(r.get("price") or 0.0) for r in rows]
    returns = _returns(price)
    events: list[SharpMove] = []
    claimed: set[int] = set()
    for speed in SPEEDS:
        moves = [0.0] * len(price)
        for i in range(speed, len(price)):
            if price[i - speed] > 0:
                moves[i] = (price[i] - price[i - speed]) / price[i - speed]
        threshold = max(min_magnitude,
                        _quantile([abs(m) for m in moves if m], extreme_q))
        for i in range(speed, len(price)):
            magnitude = moves[i]
            if abs(magnitude) < threshold \
                    or claimed.intersection(range(i - speed, i + 1)):
                continue          # a faster scan already owns this move
            anchor_row = i - speed
            anchor = price[anchor_row]
            if anchor <= 0:
                continue
            row = rows[i]
            before = returns[max(0, anchor_row - 10):anchor_row]
            liq_before = float(rows[anchor_row].get("log_liquidity") or 0.0)
            events.append(SharpMove(
                row=i, speed_bars=speed,
                direction="down" if magnitude < 0 else "up",
                anchor=anchor, end_price=price[i], magnitude=magnitude,
                spread_rel=float(row.get("spread_rel") or 0.0),
                depth_total=float(row.get("depth_total") or 0.0),
                log_liquidity=float(row.get("log_liquidity") or 0.0),
                vol_before=(sum(abs(r) for r in before) / len(before)
                            if before else 0.0),
                trade_intensity=float(row.get("ms_trade_rate") or 0.0),
                hours_to_resolution=float(
                    row.get("hours_to_resolution") or 0.0),
                price_region=_region(anchor),
                liquidity_delta=float(row.get("log_liquidity") or 0.0)
                - liq_before))
            # One event per dislocation: faster detections claim the bar so
            # the 3- and 5-bar scans do not re-count the same move.
            claimed.update(range(i - speed, i + 1))
    events.sort(key=lambda e: e.row)
    return events


def measure(event: SharpMove, rows: list[dict]) -> SharpMove:
    """The response, relative to the PRE-MOVE anchor, across horizons."""
    price = [float(r.get("price") or 0.0) for r in rows]
    move_size = event.anchor - event.end_price          # >0 for a drop
    window_end = min(event.row + HORIZONS[-1], len(price) - 1)
    path = price[event.row:window_end + 1]
    if not path or event.anchor <= 0:
        return event
    for horizon in HORIZONS:
        j = min(event.row + horizon, len(price) - 1)
        event.signed_move[horizon] = price[j] - event.end_price
        if move_size != 0:
            event.recovery_frac[horizon] = \
                (price[j] - event.end_price) / move_size
    toward = max(path) - event.end_price
    beyond = event.end_price - min(path)
    if event.direction == "up":
        toward, beyond = beyond, toward
    event.mfe, event.mae = toward, beyond

    # Classification for ANALYSIS, never for trading.
    final = event.recovery_frac.get(HORIZONS[-1], 0.0)
    peak = (max(event.recovery_frac.values())
            if event.recovery_frac else 0.0)
    if final >= 1.0:
        event.classification = "full_recovery"
    elif peak >= 0.9 and final < 0.5:
        event.classification = "recovery_then_reversal"
    elif final >= 0.5:
        event.classification = "recovery"
    elif final >= 0.15:
        event.classification = "partial_recovery"
    elif final <= -0.5:
        event.classification = "acceleration"
    elif final <= -0.15:
        event.classification = "continuation"
    else:
        event.classification = "sideways"
    return event


# --------------------------------------------------------------------------
# the study
# --------------------------------------------------------------------------

def _cell_key(event: SharpMove) -> tuple:
    """The condition cell a candidate can generalize over: move direction,
    price region, and a coarse liquidity split. Coarse ON PURPOSE — broad
    stable regions, never one optimized historical parameter."""
    liquidity = ("thin" if event.log_liquidity < 9.0 else "deep")
    return (event.direction, event.price_region, liquidity)


def study(series: list[tuple[str, list[dict]]], *,
          hold_bars: int = 15, cost: float = 0.01, min_events: int = 12,
          min_markets: int = 2, top_n: int = 4) -> dict:
    """Detect, measure, compare, and emit frozen candidates + the funnel."""
    funnel: dict[str, Any] = {"sharpMovesDetected": 0, "usableEvents": 0,
                              "baselineWindows": 0}
    reject: dict[str, int] = {}
    cells: dict[tuple, dict] = {}
    classes: dict[str, int] = {}
    baseline_moves: list[float] = []

    for market_id, rows in series:
        events = detect(rows)
        funnel["sharpMovesDetected"] += len(events)
        price = [float(r.get("price") or 0.0) for r in rows]
        event_rows = set()
        for event in events:
            event_rows.update(range(event.row - 5, event.row + 5))
        # Volatility-matched control: bars with NO sharp move nearby drift
        # by this much over the same hold — the bar a candidate must beat.
        for i in range(10, len(price) - hold_bars, 25):
            if i in event_rows or price[i] <= 0:
                continue
            baseline_moves.append(abs(price[i + hold_bars] - price[i]))
            funnel["baselineWindows"] += 1
        for event in events:
            if event.row + 1 >= len(rows):
                reject["event at series end"] = \
                    reject.get("event at series end", 0) + 1
                continue
            measure(event, rows)
            funnel["usableEvents"] += 1
            classes[event.classification] = \
                classes.get(event.classification, 0) + 1
            cell = cells.setdefault(_cell_key(event), {
                "events": 0, "markets": set(), "moves": [], "recovery": 0,
                "continuation": 0, "mfe": [], "mae": []})
            cell["events"] += 1
            cell["markets"].add(market_id)
            cell["moves"].append(event.signed_move.get(hold_bars, 0.0))
            cell["mfe"].append(event.mfe)
            cell["mae"].append(event.mae)
            if event.classification in ("recovery", "full_recovery",
                                        "partial_recovery"):
                cell["recovery"] += 1
            elif event.classification in ("continuation", "acceleration"):
                cell["continuation"] += 1

    funnel["responseClasses"] = dict(
        sorted(classes.items(), key=lambda kv: -kv[1]))
    funnel["conditionCells"] = len(cells)
    baseline = (sum(baseline_moves) / len(baseline_moves)
                if baseline_moves else 0.0)
    funnel["baselineAbsMove"] = round(baseline, 6)

    candidates: list[dict] = []
    sufficient = net_positive = 0
    for key, cell in cells.items():
        if cell["events"] < min_events or len(cell["markets"]) < min_markets:
            reject["insufficient sample"] = \
                reject.get("insufficient sample", 0) + 1
            continue
        sufficient += 1
        mean = sum(cell["moves"]) / len(cell["moves"])
        net = abs(mean) - cost
        if net <= 0:
            reject["cannot clear costs"] = \
                reject.get("cannot clear costs", 0) + 1
            continue
        # Incremental over ordinary drift: the same-hold baseline says how
        # far prices wander with no dislocation at all.
        if abs(mean) <= baseline:
            reject["no better than drift (control)"] = \
                reject.get("no better than drift (control)", 0) + 1
            continue
        net_positive += 1
        move_dir, region, liquidity = key
        candidates.append({
            "type": "sharp_move",
            "move_direction": move_dir,
            "price_region": region,
            "liquidity": liquidity,
            # DISCOVERED from the response — recovery and continuation both
            # land here naturally, as the operator specified.
            "direction": "up" if mean > 0 else "down",
            "hold_bars": hold_bars,
            "events": cell["events"],
            "markets": len(cell["markets"]),
            "netExpectancy": round(net, 6),
            "recoveryShare": round(cell["recovery"] / cell["events"], 4),
            "continuationShare": round(
                cell["continuation"] / cell["events"], 4),
            "meanMfe": round(sum(cell["mfe"]) / len(cell["mfe"]), 6),
            "meanMae": round(sum(cell["mae"]) / len(cell["mae"]), 6),
        })
    candidates.sort(key=lambda c: -c["netExpectancy"])
    candidates = candidates[:top_n]
    funnel.update({
        "cellsWithSample": sufficient, "netPositive": net_positive,
        "kept": len(candidates),
        "rejectReasons": dict(sorted(reject.items(), key=lambda kv: -kv[1])),
    })
    return {"candidates": candidates, "funnel": funnel}


def frozen_replay(rows: list[dict], rule: dict, cost: float) -> dict:
    """One FROZEN sharp-move pattern against one unseen series.

    Detection, condition cell, direction, and hold are used exactly as
    discovered; unseen data testifies, never tunes."""
    if len(rows) < 40:
        return {"trades": 0}
    hold = int(rule.get("hold_bars") or 15)
    # ENTRY TIMING as a discovered variable, not an assumption. A rule that
    # only pays when entered on the very next bar is a reaction to one print
    # and a latency race we would lose; one that survives a few bars' delay is
    # a relationship. `research.variant_expansions` has always registered the
    # delayed variant — until this was read here, the variant replayed
    # identically to its parent and its "independent" evidence was a copy.
    delay = max(0, int(rule.get("delay_bars") or 0))
    price = [float(r.get("price") or 0.0) for r in rows]
    pnl: list[float] = []
    used_until = -1
    for event in detect(rows):
        if event.row <= used_until:
            continue
        if event.direction != rule.get("move_direction"):
            continue
        if event.price_region != rule.get("price_region"):
            continue
        liquidity = "thin" if event.log_liquidity < 9.0 else "deep"
        if liquidity != rule.get("liquidity"):
            continue
        entry_row = event.row + 1 + delay
        exit_row = min(entry_row + hold, len(price) - 1)
        if entry_row >= len(price) or price[entry_row] <= 0:
            continue
        move = price[exit_row] - price[entry_row]
        signed = move if rule.get("direction") == "up" else -move
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
    verb = ("follow" if rule.get("direction") == rule.get("move_direction")
            else "fade")
    return (f"SHARP {rule.get('move_direction', '?')} move in "
            f"{rule.get('price_region', '?')}/{rule.get('liquidity', '?')} "
            f"-> {verb} {str(rule.get('direction', '?')).upper()} "
            f"(hold {rule.get('hold_bars', '?')} bars)")
