"""`get_information_state(timestamp, market_id)` — the point-in-time API.

This is the single most important function in V3, because everything that can
lie about performance flows through it. Backtesting, the scanner, the agents
and the gates all consume its output and nothing else.

Three rules it enforces mechanically:

  1. **Never read past `as_of`.** Every query is bounded with `<= as_of`.
     Layers sourced from V3's own store filter on `capture_ts`, not `ts`: a
     news item published at 10:00 that we scraped at 10:40 was not available to
     a decision at 10:15, and filtering on publication time would hand the
     backtest forty minutes of hindsight.

  2. **Absent is not zero.** A layer with no rows returns
     `Availability.UNAVAILABLE` with an empty `data`, so a downstream gate sees
     "I cannot judge this" rather than a tight spread or a calm news
     environment that was never measured.

  3. **Stale is not fresh.** Each layer computes its own age and compares it
     against `FreshnessConfig`. Past the limit it degrades to `STALE`, which
     the DATA_VALIDITY gate treats as a hard stop.

Cost note: a full state build issues ~8 bounded queries. `StateBuilder` caches
per (market, as_of bucket) so the scanner sweeping 400 markets does not rebuild
the same wallet layer 400 times.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from collections import OrderedDict

from ..config import Settings
from .canon import Availability, EvidenceState, Layer
from .source import HistoricalSource
from .store import Store


def _age(as_of: int, ts: int) -> int:
    return max(0, as_of - ts) if ts else -1


class StateBuilder:
    """Builds `EvidenceState` objects. Reusable and cheap to hold."""

    def __init__(self, st: Settings, store: Store,
                 source: HistoricalSource | None = None) -> None:
        self.st = st
        self.store = store
        self.source = source or HistoricalSource(st)
        self._cache: OrderedDict = OrderedDict()
        self._cache_max = 512
        # Bucket cache keys so a scan at t and t+3s reuses one build.
        self._bucket = 60

    # -- public -------------------------------------------------------------
    def get(self, as_of: int, market_id: str = "", token_id: str = "",
            *, use_cache: bool = True) -> EvidenceState:
        key = (market_id, token_id, as_of // self._bucket)
        if use_cache and key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        ev = self._build(as_of, market_id, token_id)
        if use_cache:
            self._cache[key] = ev
            if len(self._cache) > self._cache_max:
                self._cache.popitem(last=False)
        return ev

    def invalidate(self) -> None:
        self._cache.clear()

    # -- build --------------------------------------------------------------
    def _build(self, as_of: int, market_id: str, token_id: str) -> EvidenceState:
        ev = EvidenceState(as_of=as_of, market_id=market_id, token_id=token_id)
        if not token_id and market_id:
            toks = self.source.tokens_for_market(market_id, as_of) \
                if self.source.available else []
            if toks:
                token_id = toks[0]["token_id"]
                ev.token_id = token_id

        ev.market = self._market_layer(as_of, market_id)
        ev.price, ev.volume, ev.liquidity = self._tape_layers(as_of, token_id)
        ev.order_book = self._book_layer(as_of, token_id)
        ev.wallets, ev.top_wallet_signals, ev.cross_wallet = \
            self._wallet_layers(as_of, market_id)
        ev.blockchain = self._chain_layer(as_of, market_id)
        ev.news, ev.events, ev.official, ev.public_info = \
            self._news_layers(as_of, market_id)
        ev.related_markets = self._related_layer(as_of, market_id)
        ev.history = self._history_layer(as_of, token_id)
        ev.regime = self._regime_layer(ev)
        ev.execution = self._execution_layer(ev)
        ev.risk = self._risk_layer(as_of)
        # model/agent predictions are filled in by their own subsystems and are
        # deliberately empty here — a state built for a backtest must not carry
        # predictions produced by a later model version.
        return ev

    # -- layers -------------------------------------------------------------
    def _market_layer(self, as_of: int, market_id: str) -> Layer:
        L = Layer("market")
        if not market_id:
            L.note = "no market specified"
            return L
        row = self.store.one(
            "SELECT * FROM markets WHERE market_id=? AND capture_ts<=?",
            (market_id, as_of))
        if row:
            L.availability = Availability.OK
            L.as_of = int(row["capture_ts"])
            L.age_secs = _age(as_of, L.as_of)
            L.rows = 1
            L.data = {k: row[k] for k in
                      ("question", "category", "event_id", "condition_id",
                       "close_ts", "status", "created_ts")}
            if L.age_secs > self.st.freshness.max_market_age_secs:
                L.availability = Availability.STALE
                L.note = f"metadata {L.age_secs}s old"
        elif self.source.available:
            meta = self.source.market_meta(market_id, as_of)
            if meta:
                L.availability = Availability.OK
                L.as_of = int(meta.get("last_ts") or 0)
                L.age_secs = _age(as_of, L.as_of)
                L.rows = int(meta.get("n") or 0)
                L.data = {"question": meta.get("q") or "",
                          "category": "", "tokens": meta.get("tokens"),
                          "wallets": meta.get("wallets"),
                          "first_ts": meta.get("first_ts")}
                L.note = "derived from historical tape; no metadata collector row"
        else:
            L.note = "no market metadata source"
        return L

    def _tape_layers(self, as_of: int, token_id: str) -> tuple[Layer, Layer, Layer]:
        price, vol, liq = Layer("price"), Layer("volume"), Layer("liquidity")
        if not (token_id and self.source.available):
            for L in (price, vol, liq):
                L.note = "no tape source"
            return price, vol, liq

        rows = self.source.prints(token_id, as_of, lookback_secs=86_400)
        if not rows:
            for L in (price, vol, liq):
                L.note = "no prints in the 24h before as_of"
            return price, vol, liq

        ts = [r[0] for r in rows]
        px = [r[1] for r in rows]
        nz = [r[2] for r in rows]
        last_ts, last_px = ts[-1], px[-1]

        # velocity / acceleration over the last hour, split into two halves so
        # acceleration is a real second difference rather than a relabelled
        # first difference.
        h1 = [(t, p) for t, p in zip(ts, px) if t > as_of - 3600]
        h0 = [(t, p) for t, p in zip(ts, px) if as_of - 7200 < t <= as_of - 3600]
        v1 = (h1[-1][1] - h1[0][1]) if len(h1) > 1 else 0.0
        v0 = (h0[-1][1] - h0[0][1]) if len(h0) > 1 else 0.0

        price.availability = Availability.OK
        price.as_of = last_ts
        price.age_secs = _age(as_of, last_ts)
        price.rows = len(rows)
        price.history_days = round((ts[-1] - ts[0]) / 86400.0, 3)
        price.data = {
            "last": last_px,
            "mean_1h": statistics.fmean([p for _, p in h1]) if h1 else last_px,
            "velocity_1h": round(v1, 5),
            "acceleration": round(v1 - v0, 5),
            "volatility_1h": round(statistics.pstdev([p for _, p in h1]), 5)
            if len(h1) > 1 else 0.0,
            "range_24h": round(max(px) - min(px), 5),
            "prints_1h": len(h1),
            "gap": round(px[-1] - px[-2], 5) if len(px) > 1 else 0.0,
        }
        if price.age_secs > self.st.freshness.max_market_age_secs:
            price.availability = Availability.STALE
            price.note = f"last print {price.age_secs}s before as_of"

        vol.availability = Availability.OK
        vol.as_of = last_ts
        vol.age_secs = price.age_secs
        vol.rows = len(rows)
        n1 = sum(n for t, n in zip(ts, nz) if t > as_of - 3600)
        n24 = sum(nz)
        vol.data = {"notional_1h": round(n1, 2), "notional_24h": round(n24, 2),
                    "trades_1h": len(h1), "trades_24h": len(rows),
                    "avg_trade": round(n24 / len(rows), 2) if rows else 0.0,
                    "acceleration": round(n1 - (n24 - n1) / 23.0, 2)
                    if len(rows) > 1 else 0.0}

        # Liquidity from the tape is a PROXY, and says so. Real liquidity needs
        # the book, which for history does not exist.
        liq.availability = Availability.OK
        liq.as_of = last_ts
        liq.rows = len(rows)
        gaps = [b - a for a, b in zip(ts, ts[1:])] or [0]

        # A baseline, so "liquidity has disappeared" can be measured as a
        # collapse relative to this market's own normal rather than against an
        # absolute print rate. Most Polymarket markets print a handful of times
        # an hour; an absolute threshold would classify almost all of them as a
        # liquidity vacuum forever and halt trading permanently.
        #
        # The baseline is the MEDIAN of hourly bucket counts, not
        # total/elapsed. Prediction-market flow is bursty — this dataset has
        # tokens whose entire history is 31 prints inside 12 seconds, for which
        # total/elapsed reports 9,300 prints/hour and makes every subsequent
        # hour look like a collapse. The median of buckets is robust to exactly
        # that shape.
        buckets: dict = {}
        for t in ts:
            buckets[t // 3600] = buckets.get(t // 3600, 0) + 1
        counts = sorted(buckets.values())
        baseline_pph = statistics.median(counts) if len(counts) >= 3 else None
        liq.data = {"proxy": True,
                    "median_gap_secs": statistics.median(gaps),
                    "prints_per_hour": round(len(h1), 2),
                    "baseline_prints_per_hour": baseline_pph,
                    "baseline_buckets": len(counts),
                    # None, not 1.0. A market with under three hours of history
                    # has no normal to have departed from, and claiming a ratio
                    # of 1.0 would assert "measured, and healthy".
                    "liquidity_ratio": round(len(h1) / baseline_pph, 4)
                    if baseline_pph else None,
                    "notional_per_hour": round(n1, 2)}
        liq.note = ("tape-derived proxy; true depth requires order-book "
                    "capture, which has no history")
        return price, vol, liq

    def _book_layer(self, as_of: int, token_id: str) -> Layer:
        L = Layer("order_book")
        if not token_id:
            L.note = "no token"
            return L
        row = self.store.one(
            "SELECT * FROM book_snapshots WHERE token_id=? AND capture_ts<=? "
            "ORDER BY capture_ts DESC LIMIT 1", (token_id, as_of))
        span = self.store.history_span_days("book_snapshots")
        if not row:
            total = self.store.count("book_snapshots")
            L.availability = (Availability.UNAVAILABLE if total == 0
                              else Availability.INSUFFICIENT_HISTORY)
            L.history_days = span
            L.note = ("no order-book snapshots exist. This data cannot be "
                      "backfilled — only accumulated from now on. Enable the "
                      "collector with `pqv3 collect --start`.") if total == 0 \
                else "no snapshot for this token before as_of"
            return L
        L.as_of = int(row["capture_ts"])
        L.age_secs = _age(as_of, L.as_of)
        L.rows = 1
        L.history_days = span
        L.availability = (Availability.OK
                          if L.age_secs <= self.st.freshness.max_book_age_secs
                          else Availability.STALE)
        L.data = {k: row[k] for k in ("best_bid", "best_ask", "mid", "spread",
                                      "bid_depth", "ask_depth", "imbalance")}
        try:
            L.data["levels"] = json.loads(row["levels"])
        except Exception:                                     # noqa: BLE001
            L.data["levels"] = []
        if span < self.st.collectors.min_history_days:
            L.note = (f"only {span}d of book history; strategies depending on "
                      f"depth need {self.st.collectors.min_history_days}d")
        return L

    def _wallet_layers(self, as_of: int, market_id: str) -> tuple[Layer, Layer, Layer]:
        w, top, cross = Layer("wallets"), Layer("top_wallet_signals"), \
            Layer("cross_wallet")
        if not (market_id and self.source.available):
            for L in (w, top, cross):
                L.note = "no wallet source"
            return w, top, cross

        rows = self.source.wallets_in_market(market_id, as_of,
                                             lookback_secs=6 * 3600)
        if not rows:
            for L in (w, top, cross):
                L.note = "no wallet activity in the 6h before as_of"
            return w, top, cross

        notional = sum(r["notional"] or 0.0 for r in rows)
        w.availability = Availability.OK
        w.as_of = max(int(r["last_ts"]) for r in rows)
        w.age_secs = _age(as_of, w.as_of)
        w.rows = len(rows)
        w.data = {"active_wallets": len(rows), "notional_6h": round(notional, 2),
                  "top": [{"wallet": r["wallet"], "n": r["n"],
                           "notional": round(r["notional"] or 0.0, 2),
                           "avg_price": round(r["avg_price"] or 0.0, 4)}
                          for r in rows[:20]]}

        # Concentration: one wallet carrying most of the flow is a different
        # situation from twenty wallets agreeing, and the two must not average
        # into the same "wallet signal".
        shares = sorted((r["notional"] or 0.0) / notional for r in rows) \
            if notional > 0 else []
        hhi = sum(s * s for s in shares)
        top.availability = Availability.OK
        top.as_of = w.as_of
        top.age_secs = w.age_secs
        top.rows = len(rows)
        top.data = {"herfindahl": round(hhi, 4),
                    "top_wallet_share": round(shares[-1], 4) if shares else 0.0,
                    "n_wallets": len(rows),
                    "concentrated": hhi > 0.25}

        # Convergence: how tightly the active wallets agree on price. Wide
        # dispersion means they are not trading the same thesis.
        prices = [r["avg_price"] for r in rows if r["avg_price"]]
        cross.availability = Availability.OK
        cross.as_of = w.as_of
        cross.age_secs = w.age_secs
        cross.rows = len(rows)
        cross.data = {
            "price_dispersion": round(statistics.pstdev(prices), 5)
            if len(prices) > 1 else 0.0,
            "consensus_price": round(statistics.fmean(prices), 5) if prices else 0.0,
            "convergence": round(1.0 / (1.0 + 20 * statistics.pstdev(prices)), 4)
            if len(prices) > 1 else 0.0,
        }
        return w, top, cross

    def _chain_layer(self, as_of: int, market_id: str) -> Layer:
        L = Layer("blockchain")
        total = self.store.count("chain_events")
        if total == 0:
            L.availability = (Availability.NOT_CONFIGURED
                              if not self.st.collectors.chain_rpc
                              else Availability.UNAVAILABLE)
            L.note = ("no chain RPC configured (set collectors.chain_rpc)"
                      if not self.st.collectors.chain_rpc
                      else "chain collector configured but has captured nothing")
            return L
        rows = self.store.query(
            "SELECT kind, COUNT(*) n, SUM(amount) amt, MAX(ts) last_ts "
            "  FROM chain_events WHERE capture_ts<=? AND ts>? "
            " GROUP BY kind", (as_of, as_of - 86_400))
        L.history_days = self.store.history_span_days("chain_events")
        if not rows:
            L.availability = Availability.INSUFFICIENT_HISTORY
            L.note = "no chain events in the 24h before as_of"
            return L
        L.as_of = max(int(r["last_ts"]) for r in rows)
        L.age_secs = _age(as_of, L.as_of)
        L.rows = sum(int(r["n"]) for r in rows)
        L.availability = (Availability.OK
                          if L.age_secs <= self.st.freshness.max_chain_age_secs
                          else Availability.STALE)
        L.data = {"by_kind": {r["kind"]: {"n": r["n"], "amount": r["amt"]}
                              for r in rows}}
        return L

    def _news_layers(self, as_of: int, market_id: str) -> tuple[Layer, ...]:
        news, events, official, public = (Layer("news"), Layer("events"),
                                          Layer("official"), Layer("public_info"))
        total = self.store.count("news_items")
        if total == 0:
            for L in (news, events, official, public):
                L.availability = (Availability.NOT_CONFIGURED
                                  if not self.st.collectors.news_feeds
                                  else Availability.UNAVAILABLE)
                L.note = ("no news feeds configured (set collectors.news_feeds)"
                          if not self.st.collectors.news_feeds
                          else "news collector has captured nothing yet")
            return news, events, official, public

        span = self.store.history_span_days("news_items", "capture_ts")
        # capture_ts, NOT ts: publication time is not availability time.
        base = ("FROM news_items n LEFT JOIN news_market_links l "
                "  ON l.news_id = n.id "
                " WHERE n.capture_ts <= ? AND n.capture_ts > ? ")
        params = [as_of, as_of - 6 * 3600]
        if market_id:
            base += " AND (l.market_id = ? OR l.market_id IS NULL) "
            params.append(market_id)
        rows = self.store.query(
            "SELECT n.id, n.title, n.source_class, n.reliability, "
            "       n.confirmation, n.capture_ts, n.event_ts, "
            "       COALESCE(l.relevance,0) relevance, "
            "       COALESCE(l.direction,0) direction, "
            "       COALESCE(l.magnitude,0) magnitude " + base +
            " ORDER BY n.capture_ts DESC LIMIT 50", params)

        for L in (news, events, official, public):
            L.history_days = span
        if not rows:
            for L in (news, events, official, public):
                L.availability = Availability.INSUFFICIENT_HISTORY
                L.note = "no items captured in the 6h before as_of"
            return news, events, official, public

        newest = max(int(r["capture_ts"]) for r in rows)
        for L in (news, events, official, public):
            L.as_of = newest
            L.age_secs = _age(as_of, newest)
            L.rows = len(rows)
            L.availability = (Availability.OK
                              if L.age_secs <= self.st.freshness.max_news_age_secs
                              else Availability.STALE)

        rel = [r for r in rows if r["relevance"] > 0.2]
        # Reliability-weighted direction. An unconfirmed rumour and an official
        # release must not contribute equally.
        wsum = sum(r["reliability"] * r["relevance"] for r in rel) or 1e-9
        news.data = {
            "items": len(rows), "relevant": len(rel),
            "weighted_direction": round(
                sum(r["direction"] * r["reliability"] * r["relevance"]
                    for r in rel) / wsum, 4),
            "max_magnitude": max((r["magnitude"] for r in rel), default=0.0),
            "latest": [{"title": r["title"], "class": r["source_class"],
                        "confirmation": r["confirmation"],
                        "capture_ts": r["capture_ts"],
                        "publication_lag_secs": max(
                            0, int(r["capture_ts"]) - int(r["event_ts"] or 0))
                        if r["event_ts"] else None}
                       for r in rows[:10]],
        }
        official.data = {"items": sum(1 for r in rows
                                      if r["source_class"] == "OFFICIAL")}
        public.data = {"items": sum(1 for r in rows
                                    if r["source_class"] == "SOCIAL")}
        events.data = {"with_event_time": sum(1 for r in rows if r["event_ts"]),
                       "confirmed": sum(1 for r in rows
                                        if r["confirmation"] in
                                        ("OFFICIAL", "MULTI_SOURCE"))}
        return news, events, official, public

    def _related_layer(self, as_of: int, market_id: str) -> Layer:
        L = Layer("related_markets")
        if not market_id:
            return L
        row = self.store.one("SELECT event_id FROM markets WHERE market_id=?",
                             (market_id,))
        event_id = (row or {}).get("event_id") or ""
        if not event_id:
            L.availability = Availability.INSUFFICIENT_HISTORY
            L.note = ("no event grouping known for this market; run "
                      "`pqv3 sync-markets` to populate event ids")
            return L
        sibs = self.store.query(
            "SELECT market_id, question FROM markets WHERE event_id=? "
            "AND market_id != ? LIMIT 40", (event_id, market_id))
        L.availability = Availability.OK if sibs else Availability.INSUFFICIENT_HISTORY
        L.as_of = as_of
        L.age_secs = 0
        L.rows = len(sibs)
        L.data = {"event_id": event_id, "siblings": sibs}
        if not sibs:
            L.note = "market has no siblings under its event"
        return L

    def _history_layer(self, as_of: int, token_id: str) -> Layer:
        L = Layer("history")
        if not (token_id and self.source.available):
            return L
        rows = self.source.prints(token_id, as_of, lookback_secs=90 * 86_400,
                                  limit=4000)
        if not rows:
            L.note = "no historical prints"
            return L
        ts = [r[0] for r in rows]
        L.availability = Availability.OK
        L.as_of = ts[-1]
        L.age_secs = _age(as_of, ts[-1])
        L.rows = len(rows)
        L.history_days = round((ts[-1] - ts[0]) / 86400.0, 2)
        L.data = {"first_ts": ts[0], "prints": len(rows)}
        return L

    def _regime_layer(self, ev: EvidenceState) -> Layer:
        """Filled by regime/detect.py; placed here so the field always exists."""
        from ..regime.detect import classify
        return classify(ev, self.st)

    def _execution_layer(self, ev: EvidenceState) -> Layer:
        """Everything needed to answer 'can this actually be filled'.

        Kept separate from `liquidity` because the two answer different
        questions: liquidity is a property of the market, executability is a
        property of the market *and our capital*.
        """
        L = Layer("execution")
        px = ev.price
        if not px.ok:
            L.availability = Availability.UNAVAILABLE
            L.note = "no usable price; execution cannot be modelled"
            return L
        last = float(px.get("last") or 0.0)
        liq = ev.liquidity
        per_hour = float(liq.get("notional_per_hour") or 0.0)
        book = ev.order_book

        L.availability = Availability.OK
        L.as_of = px.as_of
        L.age_secs = px.age_secs
        # Spread: measured when the book is live, otherwise explicitly None.
        spread = book.get("spread") if book.ok else None
        L.data = {
            "reference_price": last,
            "spread": spread,
            "spread_measured": book.ok,
            "notional_per_hour": per_hour,
            "assumed_slippage_bps": self.st.costs.slippage_bps,
            "latency_ms": self.st.costs.latency_ms,
            "min_order_usdc": self.st.capital.min_order_usdc,
            "uncertainty": [] if book.ok else
            ["depth unmeasured", "spread unmeasured", "queue position unknown"],
        }
        return L

    def _risk_layer(self, as_of: int) -> Layer:
        """Live portfolio state. Present-tense by definition, so it is read
        from the store rather than reconstructed."""
        L = Layer("risk")
        open_rows = self.store.query(
            "SELECT market_id, size_usdc, correlation_key, strategy_id, "
            "       wallet_followed FROM positions "
            " WHERE status='OPEN' AND opened_ts<=?", (as_of,))
        L.availability = Availability.OK
        L.as_of = as_of
        L.age_secs = 0
        L.rows = len(open_rows)
        exposure = sum(float(r["size_usdc"] or 0.0) for r in open_rows)
        by_corr: dict = {}
        for r in open_rows:
            k = r["correlation_key"] or r["market_id"]
            by_corr[k] = by_corr.get(k, 0.0) + float(r["size_usdc"] or 0.0)
        L.data = {"open_positions": len(open_rows),
                  "gross_exposure": round(exposure, 2),
                  "by_correlation": {k: round(v, 2) for k, v in by_corr.items()},
                  "max_correlated": round(max(by_corr.values()), 2)
                  if by_corr else 0.0}
        return L


# Module-level convenience matching the name the brief asks for.
_DEFAULT: StateBuilder | None = None


def get_information_state(timestamp: int, market_id: str = "",
                          token_id: str = "", *, st: Settings | None = None,
                          store: Store | None = None) -> EvidenceState:
    """Reconstruct the exact information state at `timestamp`."""
    global _DEFAULT
    if st is None or store is None:
        if _DEFAULT is None:
            from ..config import load
            s = st or load()
            _DEFAULT = StateBuilder(s, store or Store(s))
        builder = _DEFAULT
    else:
        builder = StateBuilder(st, store)
    return builder.get(int(timestamp), market_id, token_id)
