"""The crash / panic meter.

Ten inputs, each normalised to 0..1, combined into a level and a numeric
confidence. Two design choices are worth defending:

**It is a max-with-support rule, not an average.** A liquidity vacuum with
everything else calm is still a liquidity vacuum, and averaging it against nine
calm inputs reports NORMAL right up until the fill fails. So the level is
driven by the strongest signal, and the *confidence* is driven by how many
independent inputs corroborate it. That gives "SEVERE, confidence 0.2" — one
alarming input, nothing else agreeing — which is a genuinely different and more
useful statement than "ELEVATED".

**Missing inputs lower confidence, they do not lower the level.** On a fresh
install with no book and no news, six of ten inputs are unavailable. The meter
still works off the four it has, and reports that it is working off four.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..core.canon import EvidenceState


class CrashLevel(str, Enum):
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    SEVERE = "SEVERE"
    EXTREME = "EXTREME"

    @property
    def rank(self) -> int:
        return list(CrashLevel).index(self)


_BANDS = ((0.85, CrashLevel.EXTREME), (0.65, CrashLevel.SEVERE),
          (0.45, CrashLevel.HIGH), (0.25, CrashLevel.ELEVATED))

# What each level authorises. Actions, not suggestions — `decide.py` reads this.
RESPONSES = {
    CrashLevel.NORMAL: (),
    CrashLevel.ELEVATED: ("delay_entry",),
    CrashLevel.HIGH: ("delay_entry", "reduce_exposure", "search_reversal"),
    CrashLevel.SEVERE: ("halt_new_entries", "reduce_exposure", "hedge",
                        "search_panic_mispricing"),
    CrashLevel.EXTREME: ("halt_new_entries", "exit", "hedge"),
}


@dataclass
class CrashReading:
    level: CrashLevel = CrashLevel.NORMAL
    score: float = 0.0
    confidence: float = 0.0
    inputs: dict = field(default_factory=dict)
    unavailable: list = field(default_factory=list)
    drivers: list = field(default_factory=list)

    @property
    def actions(self) -> tuple:
        return RESPONSES[self.level]

    @property
    def blocks_entry(self) -> bool:
        return "halt_new_entries" in self.actions

    def to_dict(self) -> dict:
        return {"level": self.level.value, "score": round(self.score, 4),
                "confidence": round(self.confidence, 4),
                "inputs": {k: round(v, 4) for k, v in self.inputs.items()},
                "unavailable": self.unavailable, "drivers": self.drivers,
                "actions": list(self.actions)}


def _norm(value: float, calm: float, extreme: float) -> float:
    if extreme == calm:
        return 0.0
    return max(0.0, min(1.0, (value - calm) / (extreme - calm)))


def read(ev: EvidenceState, *, prior: CrashReading | None = None) -> CrashReading:
    r = CrashReading()
    inp: dict = {}
    missing: list[str] = []

    px, liq, vol, book, news = (ev.price, ev.liquidity, ev.volume,
                                ev.order_book, ev.news)

    if px.ok:
        inp["price_velocity"] = _norm(abs(float(px.get("velocity_1h") or 0)),
                                      0.01, 0.20)
        inp["price_acceleration"] = _norm(abs(float(px.get("acceleration") or 0)),
                                          0.01, 0.15)
        inp["volatility_expansion"] = _norm(float(px.get("volatility_1h") or 0),
                                            0.01, 0.10)
    else:
        missing += ["price_velocity", "price_acceleration", "volatility_expansion"]

    if vol.ok:
        inp["volume_acceleration"] = _norm(abs(float(vol.get("acceleration") or 0)),
                                           0.0, 5000.0)
    else:
        missing.append("volume_acceleration")

    if book.ok:
        sp = book.get("spread")
        inp["spread_expansion"] = _norm(float(sp or 0), 0.01, 0.15)
        bd = float(book.get("bid_depth") or 0)
        ad = float(book.get("ask_depth") or 0)
        tot = bd + ad
        inp["liquidity_disappearance"] = _norm(-tot, -5000.0, -100.0)
        inp["trade_imbalance"] = _norm(abs(float(book.get("imbalance") or 0)),
                                       0.1, 0.9)
    else:
        missing += ["spread_expansion", "liquidity_disappearance",
                    "trade_imbalance"]

    if liq.ok:
        # A COLLAPSE in print rate is the alarm, not a low rate. Measured
        # against this market's own 24h baseline: most Polymarket markets print
        # a few times an hour normally, so an absolute threshold would flag
        # nearly every market as a permanent liquidity vacuum and halt all
        # trading forever. Ratio 1.0 = normal, 0.0 = nothing is printing.
        ratio = liq.get("liquidity_ratio")
        if ratio is None:
            missing.append("liquidity_vacuum (no baseline)")
        else:
            inp["liquidity_vacuum"] = _norm(-float(ratio), -1.0, -0.15)
    else:
        missing.append("liquidity_vacuum")

    if news.ok:
        inp["news_shock"] = _norm(float(news.get("max_magnitude") or 0), 0.1, 0.8)
    else:
        missing.append("news_shock")

    if ev.wallets.ok and ev.top_wallet_signals.ok:
        # Concentrated flow during a fast move reads as a single participant
        # exiting, which is the shape of a cascade start.
        hhi = float(ev.top_wallet_signals.get("herfindahl") or 0)
        vel = abs(float(px.get("velocity_1h") or 0)) if px.ok else 0.0
        inp["wallet_exit"] = _norm(hhi * (1 if vel > 0.05 else 0), 0.2, 0.7)
    else:
        missing.append("wallet_exit")

    # Cross-market divergence needs sibling prices, which the state builder
    # does not fetch (that would turn one state build into N). The scanner
    # passes them separately; until then this input is honestly absent rather
    # than reported as 0.0, which would read as "checked, and calm".
    missing.append("cross_market_divergence")

    r.inputs = inp
    r.unavailable = missing

    if not inp:
        r.level = CrashLevel.NORMAL
        r.confidence = 0.0
        r.drivers = ["no inputs available; the meter is not measuring anything"]
        return r

    r.score = max(inp.values())
    # Corroboration: how many other inputs are also elevated.
    supporting = sum(1 for v in inp.values() if v >= r.score * 0.5)
    coverage = len(inp) / 10.0
    r.confidence = round(min(1.0, (supporting / max(len(inp), 1)) * 0.6
                             + coverage * 0.4), 4)

    r.level = CrashLevel.NORMAL
    for cut, lvl in _BANDS:
        if r.score >= cut:
            r.level = lvl
            break
    r.drivers = [f"{k}={v:.2f}" for k, v in
                 sorted(inp.items(), key=lambda kv: -kv[1])[:4]]

    # Hysteresis: escalate immediately, de-escalate one step at a time. A meter
    # that snaps back to NORMAL the instant one reading calms will flap through
    # a cascade and authorise entries in the gaps.
    if prior is not None and r.level.rank < prior.level.rank - 1:
        r.level = list(CrashLevel)[prior.level.rank - 1]
        r.drivers.append(f"de-escalation damped from {prior.level.value}")
    return r
