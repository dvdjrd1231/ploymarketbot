"""
The liquidation-cascade hypothesis, treated as a hypothesis.

The operator's candidate: a large leveraged BTC position near liquidation,
followed by an actual liquidation event, may create a short directional move
that the 5-minute Polymarket BTC UP/DOWN market misprices for a moment.
Long liquidations are forced SELLS (predicting DOWN), short liquidations are
forced BUYS (predicting UP).

Everything here is capture and measurement — no rule is hard-coded, nothing
here trades. The operator's numbers ($5,000 liquidation, $100,000 position,
0.5% proximity) are STARTING-POINT qualifying flags recorded on each event,
never capture filters: every event above dust is stored, because the small
ones and the no-event baseline windows are the control groups that decide
whether liquidation adds information beyond ordinary momentum. Whether a
relationship is real is answered the same way as every other candidate: the
event log becomes evidence, the discovery engine searches the liq_* feature
columns alongside everything else, and only the standard frozen
out-of-sample path can ever make any of it tradable.

Data-source note, stated plainly: public exchange feeds broadcast
liquidation EVENTS (side, price, size) but not private per-position
liquidation distances. The event-triggered half of the hypothesis — which
the operator's own spec makes the entry condition ("wait for an actual
liquidation event") — is fully measurable now. The proximity half (a known
$100k position ≤0.5% from its liquidation price) needs a per-position
source; until one is chosen, proximity fields are recorded as absent, and
the trigger-importance comparison covers its measurable arms.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Response horizons after each event, in seconds — the cascade is measured,
# never assumed. Market expiration is captured separately as the outcome.
HORIZONS = (1, 5, 15, 30, 60, 120, 180)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS liq_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,          -- the LIQUIDATED side: long|short
    price         REAL DEFAULT 0,         -- fill price of the forced order
    qty           REAL DEFAULT 0,
    usd           REAL DEFAULT 0,
    qualifying    INTEGER DEFAULT 0,      -- >= starting-point size flag
    predicted     TEXT DEFAULT '',        -- down (long liq) | up (short liq)
    btc_before    REAL DEFAULT 0,         -- buffer price just before the event
    btc_momentum_30s REAL DEFAULT 0,      -- %, so momentum is separable
    btc_vol_300s  REAL DEFAULT 0,         -- stdev of 1s returns, %
    events_60s    INTEGER DEFAULT 0,      -- cascade so far, incl. this one
    usd_60s       REAL DEFAULT 0,
    pm_market     TEXT DEFAULT '',
    pm_question   TEXT DEFAULT '',
    pm_end_ts     REAL DEFAULT 0,
    pm_time_left  REAL DEFAULT 0,
    pm_up_bid     REAL DEFAULT 0,
    pm_up_ask     REAL DEFAULT 0,
    pm_down_bid   REAL DEFAULT 0,
    pm_down_ask   REAL DEFAULT 0,
    status        TEXT DEFAULT 'candidate_detected',
    detail        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_liq_ts ON liq_events(ts);

CREATE TABLE IF NOT EXISTS liq_responses (
    event_id      INTEGER NOT NULL,
    horizon_s     INTEGER NOT NULL,
    btc_price     REAL DEFAULT 0,
    btc_move_pct  REAL DEFAULT 0,         -- signed, from btc_before
    pm_up_mid     REAL DEFAULT 0,
    pm_down_mid   REAL DEFAULT 0,
    PRIMARY KEY (event_id, horizon_s)
);

CREATE TABLE IF NOT EXISTS liq_outcomes (
    event_id      INTEGER PRIMARY KEY,
    resolved_ts   REAL DEFAULT 0,
    outcome       TEXT DEFAULT '',        -- UP | DOWN of the 5m market
    hypo_entry    REAL DEFAULT 0,         -- ask of the predicted side at event
    hypo_net      REAL DEFAULT 0,         -- per share, after fee+spread
    correct       INTEGER DEFAULT 0
);

-- Volatility-matched windows with NO liquidation nearby: the control group
-- that decides whether liquidation adds anything beyond ordinary movement.
CREATE TABLE IF NOT EXISTS liq_baselines (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    btc_price     REAL DEFAULT 0,
    btc_momentum_30s REAL DEFAULT 0,
    btc_vol_300s  REAL DEFAULT 0,
    move_60s_pct  REAL DEFAULT 0,
    move_180s_pct REAL DEFAULT 0
);
"""

# The status ladder the operator specified — every event stays visible with
# where it is and why; failed hypotheses keep their evidence.
EVENT_STATUSES = (
    "candidate_detected", "near_liquidation", "liquidation_confirmed",
    "directional_response_detected", "insufficient_liquidity",
    "insufficient_sample", "no_measurable_edge", "gross_edge_positive",
    "net_edge_positive", "oos_pending", "oos_confirmed", "oos_failed",
    "forward_validation_pending", "trading_eligible",
)


def direction_of(liquidated_side: str) -> str:
    """Forced flow: a liquidated LONG is a forced sell -> DOWN, and a
    liquidated SHORT is a forced buy -> UP. Recorded per event so the
    direction test can later CONFIRM or overturn it from outcomes — the
    hypothesis is never assumed to hold."""
    return "down" if str(liquidated_side).lower() == "long" else "up"


def momentum_pct(buffer: list[tuple[float, float]], now: float,
                 lookback_s: float) -> float:
    """Signed % move over the lookback, from a [(ts, price)] buffer."""
    if not buffer:
        return 0.0
    latest = buffer[-1][1]
    past = None
    for ts, price in buffer:
        if ts >= now - lookback_s:
            past = price
            break
    if past is None or past <= 0:
        return 0.0
    return (latest - past) / past * 100.0


def volatility_pct(buffer: list[tuple[float, float]], now: float,
                   lookback_s: float = 300.0) -> float:
    """Stdev of ~1s returns over the lookback, in %."""
    window = [(ts, p) for ts, p in buffer if ts >= now - lookback_s and p > 0]
    if len(window) < 8:
        return 0.0
    returns = []
    for (_, a), (_, b) in zip(window, window[1:]):
        if a > 0:
            returns.append((b - a) / a * 100.0)
    if len(returns) < 4:
        return 0.0
    mean = sum(returns) / len(returns)
    var = sum((r - mean) ** 2 for r in returns) / (len(returns) - 1)
    return var ** 0.5


class CascadeStore:
    """SQLite event study: every event, every response, forever."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes --------------------------------------------------------------

    def record_event(self, *, ts: float, symbol: str, side: str, price: float,
                     qty: float, usd: float, qualifying: bool,
                     btc_before: float, momentum_30s: float, vol_300s: float,
                     events_60s: int, usd_60s: float,
                     pm: Optional[dict] = None) -> int:
        pm = pm or {}
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO liq_events(ts, symbol, side, price, qty, usd, "
                "qualifying, predicted, btc_before, btc_momentum_30s, "
                "btc_vol_300s, events_60s, usd_60s, pm_market, pm_question, "
                "pm_end_ts, pm_time_left, pm_up_bid, pm_up_ask, pm_down_bid, "
                "pm_down_ask, status) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, symbol, str(side).lower(), price, qty, usd,
                 1 if qualifying else 0, direction_of(side), btc_before,
                 momentum_30s, vol_300s, int(events_60s), usd_60s,
                 str(pm.get("market") or ""), str(pm.get("question") or ""),
                 float(pm.get("endTs") or 0.0),
                 float(pm.get("timeLeft") or 0.0),
                 float(pm.get("upBid") or 0.0), float(pm.get("upAsk") or 0.0),
                 float(pm.get("downBid") or 0.0),
                 float(pm.get("downAsk") or 0.0),
                 "liquidation_confirmed"))
            self._conn.commit()
            return int(cursor.lastrowid)

    def record_response(self, event_id: int, horizon_s: int, btc_price: float,
                        pm_up_mid: float = 0.0, pm_down_mid: float = 0.0
                        ) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT btc_before FROM liq_events WHERE id=?",
                (int(event_id),)).fetchone()
            before = float(row["btc_before"]) if row else 0.0
            move = ((btc_price - before) / before * 100.0) if before > 0 else 0.0
            self._conn.execute(
                "INSERT OR REPLACE INTO liq_responses(event_id, horizon_s, "
                "btc_price, btc_move_pct, pm_up_mid, pm_down_mid) "
                "VALUES(?,?,?,?,?,?)",
                (int(event_id), int(horizon_s), btc_price, move,
                 pm_up_mid, pm_down_mid))
            self._conn.commit()

    def record_outcome(self, event_id: int, outcome: str, fee: float,
                       assumed_spread: float) -> None:
        """Hypothetical economics: enter the predicted side at its ask when
        the event printed; a correct outcome pays $1.00. Fee and half the
        spread are charged — a right call that cannot pay for its costs is
        not an edge, per the operator's spec."""
        with self._lock:
            event = self._conn.execute(
                "SELECT predicted, pm_up_ask, pm_down_ask FROM liq_events "
                "WHERE id=?", (int(event_id),)).fetchone()
            if event is None:
                return
            predicted = str(event["predicted"])
            entry = float(event["pm_up_ask"] if predicted == "up"
                          else event["pm_down_ask"])
            correct = 1 if str(outcome).lower() == predicted else 0
            net = 0.0
            if entry > 0:
                payout = 1.0 if correct else 0.0
                net = payout - entry - float(fee) - float(assumed_spread) / 2.0
            self._conn.execute(
                "INSERT OR REPLACE INTO liq_outcomes(event_id, resolved_ts, "
                "outcome, hypo_entry, hypo_net, correct) VALUES(?,?,?,?,?,?)",
                (int(event_id), time.time(), str(outcome).upper(), entry, net,
                 correct))
            self._conn.commit()

    def record_baseline(self, *, ts: float, btc_price: float,
                        momentum_30s: float, vol_300s: float,
                        move_60s: float, move_180s: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO liq_baselines(ts, btc_price, btc_momentum_30s, "
                "btc_vol_300s, move_60s_pct, move_180s_pct) "
                "VALUES(?,?,?,?,?,?)",
                (ts, btc_price, momentum_30s, vol_300s, move_60s, move_180s))
            self._conn.commit()

    def set_status(self, event_id: int, status: str, detail: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE liq_events SET status=?, detail=? WHERE id=?",
                (status, detail, int(event_id)))
            self._conn.commit()

    # -- reads ---------------------------------------------------------------

    def events(self, limit: int = 5000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM liq_events ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def responses_for(self, event_id: int) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM liq_responses WHERE event_id=? "
                "ORDER BY horizon_s", (int(event_id),)).fetchall()
        return [dict(r) for r in rows]

    def outcomes(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.*, o.outcome, o.hypo_entry, o.hypo_net, o.correct "
                "FROM liq_events e JOIN liq_outcomes o ON o.event_id = e.id"
            ).fetchall()
        return [dict(r) for r in rows]

    def baselines(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM liq_baselines").fetchall()
        return [dict(r) for r in rows]

    def recent_events(self, since_ts: float) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM liq_events WHERE ts >= ?", (since_ts,)
            ).fetchall()
        return [dict(r) for r in rows]


def aggregates(events: Iterable[dict], now: float) -> dict[str, float]:
    """The liq_* feature columns the EXISTING discovery engine searches.

    This is how the hypothesis enters discovery without a hard-coded rule:
    the cascade becomes four columns riding on every captured row, and the
    engine decides — alongside every other feature — whether they predict
    anything. Neutral zeros when nothing happened.
    """
    long_60 = short_60 = 0.0
    count_300 = 0
    for event in events:
        age = now - float(event.get("ts") or 0.0)
        if age < 0 or age > 300:
            continue
        count_300 += 1
        if age <= 60:
            if str(event.get("side")) == "long":
                long_60 += float(event.get("usd") or 0.0)
            else:
                short_60 += float(event.get("usd") or 0.0)
    total = long_60 + short_60
    return {
        "liq_long_usd_60s": round(long_60, 2),
        "liq_short_usd_60s": round(short_60, 2),
        "liq_events_300s": float(count_300),
        # Signed pressure: +1 = all-short liquidations (forced buying),
        # -1 = all-long liquidations (forced selling), 0 = balanced or quiet.
        "liq_imbalance": ((short_60 - long_60) / total) if total > 0 else 0.0,
    }


def analyse(store: CascadeStore, min_sample: int = 30) -> dict:
    """The event study, honestly bucketed. Read-only; never a trade gate."""
    events = store.events()
    outcomes = store.outcomes()
    baselines = store.baselines()
    out: dict = {
        "events": len(events),
        "qualifying": sum(1 for e in events if e.get("qualifying")),
        "withOutcome": len(outcomes),
        "baselineWindows": len(baselines),
        "minSample": min_sample,
    }

    # Direction test: does the predicted side actually win, after costs?
    for side, predicted in (("long", "down"), ("short", "up")):
        rows = [o for o in outcomes if o.get("side") == side]
        wins = sum(1 for o in rows if o.get("correct"))
        net = sum(float(o.get("hypo_net") or 0.0) for o in rows)
        out[f"{side}_liq"] = {
            "events": len(rows), "predicted": predicted,
            "hitRate": round(wins / len(rows), 4) if rows else 0.0,
            "netPerTrade": round(net / len(rows), 4) if rows else 0.0,
            "sufficient": len(rows) >= min_sample,
        }

    # Response curve: mean |BTC move| by horizon, events vs. baseline drift.
    curve: dict[int, list[float]] = {h: [] for h in HORIZONS}
    signed: dict[int, list[float]] = {h: [] for h in HORIZONS}
    for event in events:
        sign = -1.0 if event.get("predicted") == "down" else 1.0
        for response in store.responses_for(int(event["id"])):
            h = int(response["horizon_s"])
            if h in curve:
                curve[h].append(abs(float(response["btc_move_pct"])))
                signed[h].append(sign * float(response["btc_move_pct"]))
    out["responseCurve"] = {
        str(h): {"n": len(curve[h]),
                 "meanAbsMovePct": (round(sum(curve[h]) / len(curve[h]), 5)
                                    if curve[h] else 0.0),
                 # Positive = the move went the PREDICTED way on average.
                 "meanPredictedMovePct": (round(sum(signed[h]) / len(signed[h]), 5)
                                          if signed[h] else 0.0)}
        for h in HORIZONS}
    base_60 = [abs(float(b.get("move_60s_pct") or 0.0)) for b in baselines]
    out["baselineAbsMove60sPct"] = (round(sum(base_60) / len(base_60), 5)
                                    if base_60 else 0.0)

    # Size relationship: bigger forced flow, bigger response?
    def _size_bucket(usd: float) -> str:
        if usd < 5_000:
            return "under-5k"
        if usd < 20_000:
            return "5-20k"
        if usd < 100_000:
            return "20-100k"
        return "100k+"

    buckets: dict[str, dict] = {}
    for event in events:
        bucket = buckets.setdefault(_size_bucket(float(event.get("usd") or 0)),
                                    {"events": 0, "move60": []})
        bucket["events"] += 1
        for response in store.responses_for(int(event["id"])):
            if int(response["horizon_s"]) == 60:
                bucket["move60"].append(abs(float(response["btc_move_pct"])))
    out["bySize"] = {
        name: {"events": b["events"],
               "meanAbsMove60sPct": (round(sum(b["move60"]) / len(b["move60"]), 5)
                                     if b["move60"] else 0.0)}
        for name, b in buckets.items()}

    # The verdict line the Discovery surface shows. Sample honesty first.
    if len(outcomes) < min_sample:
        out["verdict"] = "insufficient_sample"
        out["verdictWhy"] = (f"{len(outcomes)} resolved events; "
                             f"{min_sample} needed before any claim")
    else:
        net_total = sum(float(o.get("hypo_net") or 0.0) for o in outcomes)
        if net_total > 0:
            out["verdict"] = "net_edge_positive"
            out["verdictWhy"] = (f"{net_total:+.4f} net over {len(outcomes)} "
                                 "hypothetical trades - OOS validation still "
                                 "required before anything may trade")
        else:
            out["verdict"] = "no_measurable_edge"
            out["verdictWhy"] = (f"{net_total:+.4f} net over {len(outcomes)} "
                                 "hypothetical trades after costs")
    return out


def write_analysis(store: CascadeStore, out_path: str | Path) -> dict:
    data = analyse(store)
    Path(out_path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    return data


@dataclass
class CascadeConfig:
    """Starting points from the operator's hypothesis. Qualifying FLAGS,
    never capture filters — controls need the small events too."""
    enabled: bool = True
    # Liquidation feed. OKX's public stream is reachable from this region;
    # Binance's futures stream connects and then serves nothing (geo-block),
    # which is why it is the fallback rather than the default.
    source: str = "okx"                          # okx | binance
    symbol: str = "BTCUSDT"                      # binance symbol
    stream_host: str = "wss://fstream.binance.com"
    okx_host: str = "wss://ws.okx.com:8443/ws/v5/public"
    okx_inst_id: str = "BTC-USDT-SWAP"
    okx_ct_val: float = 0.01                     # BTC per contract
    qualify_liquidation_usd: float = 5_000.0
    qualify_position_usd: float = 100_000.0      # awaits a per-position source
    qualify_proximity_pct: float = 0.5           # awaits a per-position source
    min_record_usd: float = 100.0                # dust floor only
    baseline_interval_s: float = 600.0
    min_sample: int = 30
