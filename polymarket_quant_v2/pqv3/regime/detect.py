"""Regime classification.

A strategy that works in one regime must not be assumed to work in another, so
every decision and every backtest row is stamped with the regime it happened
in. That stamp is what lets `learning/` answer "did this strategy degrade, or
did the world change?" — two failures with opposite remedies that look
identical in an aggregate PnL curve.

Regimes here are **observable**, not latent. Each one is a function of measured
tape quantities with a written threshold, because a hidden-state model fitted
on 90 days of one venue's data would mostly be fitting noise, and its states
would be uninterpretable in exactly the moment you need to interpret them.

The thresholds are calibrated against this dataset's own distribution rather
than picked: see `calibrate()`, which recomputes them from percentiles and
writes them to the store. Until it has run, the defaults below are used and the
regime layer says so.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict

from ..config import Settings
from ..core.canon import Availability, EvidenceState, Layer


@dataclass(frozen=True)
class Thresholds:
    """Percentile-derived cutoffs. Defaults are this dataset's own quartiles.

    `calibrated` distinguishes "measured from your data" from "our starting
    guess", and the dashboard shows which — an uncalibrated threshold applied
    to a different venue is a silent source of misclassification.
    """

    calibrated: bool = False
    low_liquidity_prints_ph: float = 3.0
    high_liquidity_prints_ph: float = 40.0
    low_vol: float = 0.01
    high_vol: float = 0.05
    shock_velocity: float = 0.08          # price move per hour
    panic_velocity: float = 0.15
    momentum_persistence: float = 0.6
    resolution_window_secs: int = 6 * 3600
    news_shock_magnitude: float = 0.5


DEFAULTS = Thresholds()


# The label set. Ordered by precedence: the first match wins, so a market that
# is both illiquid and in an information shock is reported as the shock, which
# is the property that should drive the decision.
LABELS = (
    "INFORMATION_SHOCK", "PANIC", "RESOLUTION_DRIVEN", "NEWS_DRIVEN",
    "EVENT_DRIVEN", "HIGH_VOLATILITY", "MOMENTUM", "MEAN_REVERSION",
    "LOW_LIQUIDITY", "HIGH_LIQUIDITY", "INFORMATION_VACUUM", "STABLE",
)


def classify(ev: EvidenceState, st: Settings,
             th: Thresholds = DEFAULTS) -> Layer:
    """Return the regime layer for an evidence state.

    Returns UNAVAILABLE rather than STABLE when the price layer is missing.
    Defaulting to STABLE would be the worst possible failure mode: it is the
    regime under which the most permissive strategies are allowed to trade.
    """
    L = Layer("regime")
    px = ev.price
    if not px.ok:
        L.note = "no usable price layer; regime is unknown, not stable"
        return L

    vel = abs(float(px.get("velocity_1h") or 0.0))
    accel = abs(float(px.get("acceleration") or 0.0))
    vol = float(px.get("volatility_1h") or 0.0)
    prints_ph = float(ev.liquidity.get("prints_per_hour") or 0.0)

    news_mag = float(ev.news.get("max_magnitude") or 0.0) if ev.news.ok else 0.0
    news_items = int(ev.news.get("relevant") or 0) if ev.news.ok else 0
    close_ts = int(ev.market.get("close_ts") or 0) if ev.market.ok else 0
    secs_to_close = (close_ts - ev.as_of) if close_ts else -1

    flags: list[str] = []
    if vel >= th.panic_velocity and prints_ph >= th.high_liquidity_prints_ph:
        flags.append("PANIC")
    if news_mag >= th.news_shock_magnitude and vel >= th.shock_velocity:
        flags.append("INFORMATION_SHOCK")
    if 0 <= secs_to_close <= th.resolution_window_secs:
        flags.append("RESOLUTION_DRIVEN")
    if news_items > 0:
        flags.append("NEWS_DRIVEN")
    if vol >= th.high_vol:
        flags.append("HIGH_VOLATILITY")
    if vel >= th.shock_velocity and accel >= 0:
        flags.append("MOMENTUM")
    elif vel >= th.shock_velocity and accel < 0:
        flags.append("MEAN_REVERSION")
    if prints_ph <= th.low_liquidity_prints_ph:
        flags.append("LOW_LIQUIDITY")
    elif prints_ph >= th.high_liquidity_prints_ph:
        flags.append("HIGH_LIQUIDITY")
    if prints_ph <= th.low_liquidity_prints_ph and news_items == 0 \
            and vol <= th.low_vol:
        flags.append("INFORMATION_VACUUM")
    if not flags:
        flags.append("STABLE")

    primary = next(l for l in LABELS if l in flags)

    # Confidence is driven by how much of the evidence state we could actually
    # see. Classifying a regime from the price tape alone is a weaker claim
    # than classifying it with book and news present, and the number should say
    # so rather than being uniformly 1.0.
    inputs_present = sum(1 for l in (ev.price, ev.liquidity, ev.volume,
                                     ev.news, ev.order_book, ev.market) if l.ok)
    L.availability = Availability.OK
    L.as_of = px.as_of
    L.age_secs = px.age_secs
    L.rows = px.rows
    L.data = {
        "primary": primary,
        "flags": flags,
        "confidence": round(inputs_present / 6.0, 3),
        "inputs_present": inputs_present,
        "measurements": {"velocity_1h": round(vel, 5),
                         "acceleration": round(accel, 5),
                         "volatility_1h": round(vol, 5),
                         "prints_per_hour": round(prints_ph, 2),
                         "secs_to_close": secs_to_close,
                         "news_items": news_items},
        "thresholds_calibrated": th.calibrated,
    }
    if not th.calibrated:
        L.note = ("regime thresholds are defaults, not calibrated to this "
                  "dataset; run `pqv3 calibrate-regime`")
    return L


def calibrate(store, source, sample_tokens: int = 400) -> Thresholds:
    """Derive thresholds from this dataset's own distribution.

    Uses percentiles rather than fixed numbers so the same code behaves
    sensibly on a venue with different activity. Persisted to `meta` so the
    dashboard can show when it last ran.
    """
    import json
    import statistics
    import time

    if not source.available:
        return DEFAULTS

    now = int(time.time())
    prints_ph: list[float] = []
    vols: list[float] = []
    vels: list[float] = []

    markets = source.active_markets(now, lookback_secs=90 * 86_400,
                                    limit=sample_tokens)
    for m in markets:
        toks = source.tokens_for_market(m["market_id"], now)
        if not toks:
            continue
        rows = source.prints(toks[0]["token_id"], now,
                             lookback_secs=90 * 86_400, limit=2000)
        if len(rows) < 10:
            continue
        ts = [r[0] for r in rows]
        px = [r[1] for r in rows]
        hours = max((ts[-1] - ts[0]) / 3600.0, 1e-6)
        prints_ph.append(len(rows) / hours)
        vols.append(statistics.pstdev(px))
        vels.append(abs(px[-1] - px[0]) / hours)

    if len(prints_ph) < 20:
        return DEFAULTS

    def pct(xs: list[float], p: float) -> float:
        xs = sorted(xs)
        i = min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))
        return round(xs[i], 6)

    th = Thresholds(
        calibrated=True,
        low_liquidity_prints_ph=pct(prints_ph, 0.25),
        high_liquidity_prints_ph=pct(prints_ph, 0.75),
        low_vol=pct(vols, 0.25),
        high_vol=pct(vols, 0.75),
        shock_velocity=pct(vels, 0.90),
        panic_velocity=pct(vels, 0.99),
    )
    store.set_meta("regime_thresholds", json.dumps(asdict(th)))
    store.set_meta("regime_calibrated_ts", str(now))
    store.set_meta("regime_calibration_sample", str(len(prints_ph)))
    return th


def load_thresholds(store) -> Thresholds:
    import json
    raw = store.get_meta("regime_thresholds", "")
    if not raw:
        return DEFAULTS
    try:
        return Thresholds(**json.loads(raw))
    except Exception:                                          # noqa: BLE001
        return DEFAULTS
