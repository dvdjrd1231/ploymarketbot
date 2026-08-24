"""
Market-wide regime: what kind of day Polymarket is having, as numbers.

The master spec's global view: the same impulse deserves different treatment in
a deep, orderly market than in a thin or chaotic one, so the engine keeps one
summary of the whole visible universe and scales its aggressiveness by it.
Nothing here picks trades — it dampens or permits, and its columns ride on
every captured row so discovery can learn whether regime context predicts.

Four numbers, each deliberately simple and inspectable:

* ``regime_liquidity``   — median tracked-market liquidity, in $1000s.
* ``regime_volatility``  — mean |tape velocity| across markets with a tape:
                           how fast the exchange as a whole is repricing.
* ``regime_breadth``     — share of tracked markets with a live, streamed
                           quote: how much of the universe is actually open
                           for business right now.
* ``regime_aggressiveness`` — the scalar the sizing layer multiplies by:
                           1.0 in a normal environment, shrinking toward 0.5
                           when the universe is thin or repricing violently,
                           capped at 1.1 when it is deep and calm. Bounded on
                           purpose — regime tunes exposure, it never doubles it.
"""

from __future__ import annotations

import statistics
from typing import Mapping


def measure(markets: Mapping[str, object]) -> dict[str, float]:
    """One regime summary for the whole visible universe."""
    liquidity: list[float] = []
    velocity: list[float] = []
    live = 0
    total = 0

    for market in markets.values():
        total += 1
        liquidity.append(float(getattr(market, "liquidity", 0.0) or 0.0))
        quotes = getattr(market, "quotes", {}) or {}
        if any(getattr(q, "source", "") == "stream" for q in quotes.values()):
            live += 1
        trades = getattr(market, "recent_trades", None) or []
        if len(trades) >= 2:
            ordered = sorted(trades, key=lambda t: t.get("ts") or 0)
            first, last = ordered[0], ordered[-1]
            p0 = float(first.get("price") or 0.0)
            p1 = float(last.get("price") or 0.0)
            span = max(1.0, float(last.get("ts") or 0) - float(first.get("ts") or 0))
            if p0 > 0:
                velocity.append(abs((p1 - p0) / p0) * 3600.0 / span)

    if not total:
        return {"regime_liquidity": 0.0, "regime_volatility": 0.0,
                "regime_breadth": 0.0, "regime_aggressiveness": 1.0}

    med_liq = statistics.median(liquidity) if liquidity else 0.0
    vol = (sum(velocity) / len(velocity)) if velocity else 0.0
    breadth = live / total

    # Aggressiveness: start at 1.0 and only earn deviations.
    scale = 1.0
    if med_liq < 2_000:                  # thin universe: everything is harder
        scale -= 0.25
    if breadth < 0.5:                    # half the universe has no live book
        scale -= 0.15
    if vol > 0.50:                       # >50%/hour mean repricing: chaos
        scale -= 0.20
    elif vol < 0.02 and med_liq > 10_000:
        scale += 0.10                    # deep and calm: slightly more room
    scale = max(0.5, min(1.1, scale))

    return {
        "regime_liquidity": round(med_liq / 1_000.0, 4),
        "regime_volatility": round(vol, 6),
        "regime_breadth": round(breadth, 4),
        "regime_aggressiveness": round(scale, 4),
    }
