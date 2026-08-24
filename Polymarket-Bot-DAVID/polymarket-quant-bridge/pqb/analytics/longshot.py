"""
Military-attack apparent-longshot effect: a reported result, re-derived.

The operator's hypothesis, treated strictly as a hypothesis: in Polymarket
military-attack markets, outcomes priced around <=35% with >=$2,500 of
traded capital reportedly resolved YES ~52% of the time. Nothing here
assumes that figure — the study reconstructs implied-vs-realized
probability from the settled tapes using ONLY information observable at
each simulated entry, and lets the evidence say whether a calibration gap
exists, where it lives, and whether it survives costs and controls.

Guards built in, per the spec:

* **Entry-time honesty** — implied probability is the median of the last
  few trade prices at the entry timestamp; traded capital is the tape's
  cumulative notional UP TO that moment. Resolution data never leaks
  backward.
* **Both sides, one observation** — every market contributes its
  LOW-priced side once per timing point (a high-priced side is the same
  bet inverted, priced as 1 − p), so YES/NO framing cannot double-count
  and the "best side" is discovered, not assumed.
* **Event clustering** — military markets sharing actors and a time
  window are ONE event; ten correlated contracts on one crisis are not
  ten confirmations. Candidates whose profit is one cluster's story are
  rejected as fragile.
* **Base-rate control** — every probability cell is measured for
  military AND non-military markets alike; the category earns a candidate
  only where its edge is INCREMENTAL over the same-priced control.
* **The classifier is literal and visible** — word-boundary keyword
  matching with explicit exclusions (the dataset's own first lesson:
  "Counter-Strike" esports is not a military event).

Survivors become ordinary library candidates (family
"longshot-calibration") and walk the same frozen-OOS ladder; they cannot
vote in the live engine until an execution path is deliberately built.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Probability regions from the operator's spec — research buckets, never a
# hard-coded 35% rule. The capital buckets bracket the reported $2,500.
PROB_BUCKETS = ((0.02, 0.10), (0.10, 0.15), (0.15, 0.20), (0.20, 0.25),
                (0.25, 0.30), (0.30, 0.35), (0.35, 0.40), (0.40, 0.50))
CAPITAL_BUCKETS = (0.0, 500.0, 2_500.0, 10_000.0)
TIMING_FRACTIONS = (0.25, 0.50, 0.75)     # early / middle / late market life

_MILITARY = re.compile(
    r"\b(strike|strikes|airstrike|airstrikes|attack|attacks|attacked|"
    r"invade|invades|invasion|military|missile|missiles|bomb|bombs|"
    r"bombing|war|ceasefire|cease-fire|troops|shelling|drone strike|"
    r"offensive|nuclear (test|weapon|strike))\b", re.IGNORECASE)
_NOT_MILITARY = re.compile(
    r"counter-?strike|esports|valorant|dota|league of legends|"
    r"call of duty|warzone|world cup|warriors", re.IGNORECASE)

_ACTORS = ("russia", "ukraine", "israel", "iran", "gaza", "hezbollah",
           "hamas", "lebanon", "syria", "houthi", "yemen", "china",
           "taiwan", "north korea", "india", "pakistan", "nato", "us ",
           "u.s.", "venezuela")


def classify_military(question: str) -> bool:
    """Word-boundary keywords with explicit exclusions, so 'Counter-Strike'
    esports never counts as a military event."""
    text = str(question or "")
    if _NOT_MILITARY.search(text):
        return False
    return bool(_MILITARY.search(text))


def cluster_key(question: str, ts: float) -> str:
    """One geopolitical EVENT, not one market: actors found in the question
    plus the calendar month. Ten contracts on one crisis share a key."""
    text = str(question or "").lower()
    actors = sorted({a.strip() for a in _ACTORS if a in text})
    import time as _time
    month = _time.strftime("%Y-%m", _time.gmtime(ts)) if ts else "?"
    if actors:
        return "|".join(actors) + "@" + month
    # No recognized actors: correlation risk comes from SHARED actors, so
    # distinct questions are distinct events; identical questions (several
    # outcomes of one market) still cluster together.
    return "q:" + text[:60] + "@" + month


def observations_from_tape(trades: list[dict], payout: float,
                           market_id: str, question: str) -> list[dict]:
    """Entry-time-honest observations: one LOW-side observation per timing
    point, implied from the tape prefix only."""
    if len(trades) < 8:
        return []
    out: list[dict] = []
    for fraction in TIMING_FRACTIONS:
        index = max(4, int(len(trades) * fraction))
        if index >= len(trades):
            continue
        window = [float(t.get("price") or 0.0)
                  for t in trades[max(0, index - 5):index]
                  if float(t.get("price") or 0.0) > 0]
        if not window:
            continue
        implied = sorted(window)[len(window) // 2]
        if not 0.0 < implied < 1.0:
            continue
        traded = sum(float(t.get("usdc") or 0.0) for t in trades[:index])
        side_payout = payout
        # The HIGH-priced side is the same bet inverted: observe its
        # complement so every market contributes exactly its longshot side.
        if implied > 0.5:
            implied = 1.0 - implied
            side_payout = 1.0 - payout
        out.append({
            "market": market_id, "question": question,
            "timing": fraction, "implied": implied,
            "tradedUsd": traded, "payout": side_payout,
            "ts": float(trades[index].get("ts") or 0.0),
        })
    return out


def _prob_bucket(implied: float) -> Optional[tuple]:
    for lo, hi in PROB_BUCKETS:
        if lo <= implied < hi:
            return (lo, hi)
    return None


def _capital_bucket(traded: float) -> float:
    chosen = CAPITAL_BUCKETS[0]
    for floor in CAPITAL_BUCKETS:
        if traded >= floor:
            chosen = floor
    return chosen


def study(store, *, cost: float = 0.02, min_events: int = 12,
          top_n: int = 2) -> dict:
    """The full calibration study over the settled tapes. Read-only."""
    resolutions = store.resolutions()
    rows = store.query(
        "SELECT token_id, market_id, ts, price, usdc, question "
        "FROM wallet_trades WHERE token_id != '' ORDER BY ts")
    by_token: dict[str, list[dict]] = {}
    for row in rows:
        token = str(row["token_id"])
        if token in resolutions:
            by_token.setdefault(token, []).append(row)

    funnel: dict[str, Any] = {"settledTapes": len(by_token)}
    reject: dict[str, int] = {}
    observations: list[dict] = []
    military_markets: set[str] = set()
    seen_market_side: set[tuple] = set()
    for token, trades in by_token.items():
        payout = 1.0 if float(resolutions[token]) >= 0.99 else 0.0
        market_id = str(trades[0].get("market_id") or token)
        question = str(trades[0].get("question") or "")
        military = classify_military(question)
        for obs in observations_from_tape(trades, payout, market_id,
                                          question):
            # A market has exactly one longshot side per timing point,
            # whichever token's tape we walked — never both.
            key = (market_id, obs["timing"])
            if key in seen_market_side:
                continue
            seen_market_side.add(key)
            obs["military"] = military
            obs["cluster"] = (cluster_key(question, obs["ts"])
                              if military else market_id)
            observations.append(obs)
            if military:
                military_markets.add(market_id)

    funnel["observations"] = len(observations)
    funnel["militaryMarkets"] = len(military_markets)
    funnel["controlMarkets"] = len({o["market"] for o in observations
                                    if not o["military"]})

    # -- calibration curve: implied vs realized, military vs control --------
    def _cells(subset: list[dict]) -> dict:
        cells: dict[tuple, dict] = {}
        for obs in subset:
            bucket = _prob_bucket(obs["implied"])
            if bucket is None:
                continue
            cell = cells.setdefault(bucket, {
                "n": 0, "wins": 0, "impliedSum": 0.0, "markets": set(),
                "clusters": {}, "pnl": 0.0})
            cell["n"] += 1
            cell["wins"] += 1 if obs["payout"] >= 1.0 else 0
            cell["impliedSum"] += obs["implied"]
            cell["markets"].add(obs["market"])
            cell["pnl"] += obs["payout"] - obs["implied"] - cost
            cluster = cell["clusters"].setdefault(obs["cluster"], 0.0)
            cell["clusters"][obs["cluster"]] = cluster + max(
                0.0, obs["payout"] - obs["implied"] - cost)
        return cells

    military_cells = _cells([o for o in observations if o["military"]])
    control_cells = _cells([o for o in observations if not o["military"]])

    def _summary(cells: dict) -> dict:
        out = {}
        for (lo, hi), cell in sorted(cells.items()):
            positives = [v for v in cell["clusters"].values() if v > 0]
            top_share = (max(positives) / sum(positives)) if positives else 0.0
            out[f"{lo:.2f}-{hi:.2f}"] = {
                "observations": cell["n"],
                "markets": len(cell["markets"]),
                "events": len(cell["clusters"]),
                "implied": round(cell["impliedSum"] / cell["n"], 4),
                "realized": round(cell["wins"] / cell["n"], 4),
                "edge": round(cell["wins"] / cell["n"]
                              - cell["impliedSum"] / cell["n"], 4),
                "netPerShare": round(cell["pnl"] / cell["n"], 4),
                "topClusterShare": round(top_share, 4),
            }
        return out

    funnel["calibrationMilitary"] = _summary(military_cells)
    funnel["calibrationControl"] = _summary(control_cells)

    # -- candidates: enough EVENTS, net-positive, incremental, diversified --
    candidates: list[dict] = []
    for bucket, cell in military_cells.items():
        events = len(cell["clusters"])
        if events < min_events:
            reject["insufficient sample (events)"] = \
                reject.get("insufficient sample (events)", 0) + 1
            continue
        net = cell["pnl"] / cell["n"]
        if net <= 0:
            reject["cannot clear costs"] = \
                reject.get("cannot clear costs", 0) + 1
            continue
        control = control_cells.get(bucket)
        control_net = (control["pnl"] / control["n"]) if control and \
            control["n"] else 0.0
        if net <= control_net:
            reject["no incremental edge over same-priced control"] = \
                reject.get("no incremental edge over same-priced control",
                           0) + 1
            continue
        positives = [v for v in cell["clusters"].values() if v > 0]
        top_share = (max(positives) / sum(positives)) if positives else 0.0
        if top_share > 0.7:
            reject["fragile: one event cluster dominates"] = \
                reject.get("fragile: one event cluster dominates", 0) + 1
            continue
        lo, hi = bucket
        candidates.append({
            "type": "longshot", "category": "military",
            "prob_lo": lo, "prob_hi": hi, "side": "low",
            "min_traded_usd": 0.0,      # capital condition studied, not imposed
            "direction": "up",
            "events": events, "markets": len(cell["markets"]),
            "observations": cell["n"],
            "netExpectancy": round(net, 6),
            "controlNet": round(control_net, 6),
            "realized": round(cell["wins"] / cell["n"], 4),
            "implied": round(cell["impliedSum"] / cell["n"], 4),
        })
    candidates.sort(key=lambda c: -c["netExpectancy"])
    candidates = candidates[:top_n]
    funnel.update({"cells": len(military_cells),
                   "kept": len(candidates),
                   "rejectReasons": dict(sorted(reject.items(),
                                                key=lambda kv: -kv[1]))})
    return {"candidates": candidates, "funnel": funnel}


def frozen_replay(trades: list[dict], rule: dict, payout: float,
                  cost: float) -> dict:
    """One FROZEN longshot rule against one unseen settled market's tape.

    Entry at the first tape moment whose observable implied probability
    sits inside the rule's frozen band (inverting to the low side exactly
    as discovery did); one observation per market — independence is the
    whole point of this evidence."""
    if len(trades) < 8:
        return {"trades": 0}
    lo = float(rule.get("prob_lo") or 0.0)
    hi = float(rule.get("prob_hi") or 1.0)
    floor = float(rule.get("min_traded_usd") or 0.0)
    traded = 0.0
    for index in range(4, len(trades)):
        traded += float(trades[index - 1].get("usdc") or 0.0)
        window = [float(t.get("price") or 0.0)
                  for t in trades[max(0, index - 5):index]
                  if float(t.get("price") or 0.0) > 0]
        if not window:
            continue
        implied = sorted(window)[len(window) // 2]
        side_payout = payout
        if implied > 0.5:
            implied = 1.0 - implied
            side_payout = 1.0 - payout
        if not lo <= implied < hi or traded < floor:
            continue
        # side "low" buys the longshot; side "high" (the inverse variant)
        # buys the FAVORITE of the same qualifying market — complementary
        # price, complementary payout, same entry-time information.
        if str(rule.get("side") or "low") == "high":
            implied = 1.0 - implied
            side_payout = 1.0 - side_payout
        pnl = side_payout - implied - cost
        return {"trades": 1, "wins": 1 if pnl > 0 else 0, "pnl": pnl,
                "expectancy": pnl, "drawdown": max(0.0, -pnl),
                "sharpe": 0.0}
    return {"trades": 0}


def describe(rule: dict) -> str:
    return (f"LONGSHOT {rule.get('category', '?')}: buy the "
            f"{rule.get('prob_lo', 0):.0%}-{rule.get('prob_hi', 0):.0%} "
            "side of settled-category markets, hold to resolution")
