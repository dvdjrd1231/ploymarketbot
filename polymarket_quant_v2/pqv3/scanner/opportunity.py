"""The opportunity scanner.

Scans **every eligible market**, not only the ones a tracked wallet has touched.
Restricting the scan to wallet-followed markets is the selection bias that
makes a copy system look better than it is: it only ever sees the markets its
wallets chose, so it can never learn that those wallets choose badly.

Two-stage by necessity. A full decision costs a state build, an ensemble, 25
agents and a gate pass — perhaps 60ms — which over 1,285 markets is a minute
per sweep and unusable as a loop. So:

    stage 1  cheap scoring over every market from bounded aggregate queries
    stage 2  full `DecisionEngine.decide` on the top N only

The cut between them is the only place the scanner can silently lose an
opportunity, so `scan()` records how many markets were dropped at stage 1 and
`learning/missed.py` re-examines them after the fact. A cap that nobody reports
reads as "we covered everything"; this one is reported.
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..core.canon import Opportunity, Signal, SignalClass
from ..core.pit import StateBuilder
from ..core.source import HistoricalSource


@dataclass
class ScanResult:
    as_of: int
    opportunities: list = field(default_factory=list)
    markets_scanned: int = 0
    markets_eligible: int = 0
    markets_dropped_at_stage1: int = 0
    dropped_ids: list = field(default_factory=list)
    elapsed_ms: int = 0
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"as_of": self.as_of,
                "markets_scanned": self.markets_scanned,
                "markets_eligible": self.markets_eligible,
                "markets_dropped_at_stage1": self.markets_dropped_at_stage1,
                "elapsed_ms": self.elapsed_ms,
                "notes": self.notes,
                "opportunities": [o.to_dict() for o in self.opportunities]}


class Scanner:
    def __init__(self, st: Settings, store, *,
                 source: HistoricalSource | None = None,
                 builder: StateBuilder | None = None) -> None:
        self.st = st
        self.store = store
        self.source = source or HistoricalSource(st)
        self.builder = builder or StateBuilder(st, store, self.source)

    def scan(self, *, as_of: int = 0, top_n: int = 25,
             lookback_secs: int = 6 * 3600, wallet_dna: dict | None = None,
             max_markets: int = 600) -> ScanResult:
        t0 = time.perf_counter()
        dna = wallet_dna or {}

        if not self.source.available:
            res = ScanResult(as_of=as_of or int(time.time()))
            res.notes.append("no historical source; nothing to scan")
            return res

        # Anchor to the DATA clock, not the wall clock. This tape's last print
        # can be days old, and a scan looking back six hours from `now` would
        # correctly find nothing and incorrectly look like a broken scanner.
        # Trading is not anchored this way — DATA_VALIDITY still measures
        # staleness against the wall clock and refuses.
        lag = self.source.data_lag_secs()
        if not as_of:
            as_of = min(int(time.time()), self.source.latest_ts()
                        or int(time.time()))
        res = ScanResult(as_of=as_of)
        if lag > self.st.freshness.max_market_age_secs:
            res.notes.append(
                f"the tape's newest print is {lag / 3600:.1f}h old, so the scan "
                f"is anchored to the data clock ({as_of}) rather than now. "
                f"These are RESEARCH results: every one of them would be "
                f"refused by DATA_VALIDITY as stale if offered as a trade.")

        markets = self.source.active_markets(as_of, lookback_secs, max_markets)
        res.markets_scanned = len(markets)
        if len(markets) >= max_markets:
            res.notes.append(
                f"stage-1 candidate list truncated at {max_markets} markets by "
                f"notional; markets below that are not scanned this pass")

        scored: list = []
        for m in markets:
            o = self._stage1(m, as_of, dna)
            if o is not None:
                scored.append(o)
        res.markets_eligible = len(scored)

        scored.sort(key=lambda o: -o.overall_score)
        res.opportunities = scored[:top_n]
        dropped = scored[top_n:]
        res.markets_dropped_at_stage1 = len(dropped)
        res.dropped_ids = [o.market_id for o in dropped[:200]]
        if dropped:
            res.notes.append(
                f"{len(dropped)} eligible markets ranked below the top {top_n} "
                f"and were not taken to a full decision; they are recorded for "
                f"missed-opportunity analysis")

        res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return res

    # -- stage 1 ------------------------------------------------------------
    def _stage1(self, m: dict, as_of: int, dna: dict) -> Opportunity | None:
        """Cheap scoring from one token's tape. Returns None if ineligible."""
        market_id = m["market_id"]
        toks = self.source.tokens_for_market(market_id, as_of)
        if not toks:
            return None
        token_id = toks[0]["token_id"]
        prints = self.source.prints(token_id, as_of, lookback_secs=86_400,
                                    limit=600)
        if len(prints) < 5:
            return None

        px = [p for _, p, _, _ in prints]
        ts = [t for t, _, _, _ in prints]
        last = px[-1]
        if not (self.st.costs.min_price <= last <= self.st.costs.max_price):
            return None

        # Anchor the candidate to its OWN most recent print, not to the sweep's
        # timestamp. This is the event-driven discipline every honest backtest
        # uses: a decision is evaluated at a moment when the data supporting it
        # was actually fresh. Anchoring all candidates to a common `as_of`
        # would make every market whose last print is older than the freshness
        # limit fail DATA_VALIDITY — which is the correct verdict for a live
        # trade and a useless one for research, because it hides what the other
        # eleven gates would have said.
        #
        # This does NOT create look-ahead: `evaluated_at` is a timestamp at or
        # before the sweep anchor, and every layer is still built with `<=`.
        o = Opportunity(market_id=market_id, token_id=token_id,
                        question=m.get("question") or "",
                        market_probability=last, as_of=ts[-1])

        hour = [(t, p) for t, p in zip(ts, px) if t > as_of - 3600]
        vel = (hour[-1][1] - hour[0][1]) if len(hour) > 1 else 0.0
        vol = statistics.pstdev([p for _, p in hour]) if len(hour) > 1 else 0.0
        span_h = max((ts[-1] - ts[0]) / 3600.0, 1e-6)
        pph = len(prints) / span_h

        # --- mispricing against the price-band base rate. The one estimator
        # cheap enough to run over every market.
        base = self._band(last)
        if base and int(base.get("n") or 0) >= 200:
            fair = float(base["hit_rate"])
            o.fair_probability = fair
            o.mispricing_score = min(1.0, abs(fair - last) / 0.15)
            o.statistical_score = min(1.0, math.log10(max(base["n"], 1)) / 4.0)
        else:
            o.fair_probability = last

        # --- microstructure
        o.microstructure_score = min(1.0, abs(vel) / 0.08) * min(1.0, pph / 20.0)
        o.information_shock_score = min(1.0, vol / 0.06)

        # --- wallet signal, alpha-weighted
        wallets = self.source.wallets_in_market(market_id, as_of, 6 * 3600)
        profiled = [w for w in wallets if w["wallet"] in dna]
        if profiled:
            alphas = [float(dna[w["wallet"]].alpha_vs_band
                            if hasattr(dna[w["wallet"]], "alpha_vs_band")
                            else dna[w["wallet"]].get("alpha_vs_band", 0.0))
                      for w in profiled]
            o.wallet_signal_score = min(1.0, abs(statistics.fmean(alphas)) / 0.04)
            o.signals.append(Signal(
                source="wallet_flow", kind="alpha_weighted",
                direction=1.0 if statistics.fmean(alphas) > 0 else -1.0,
                strength=o.wallet_signal_score,
                classification=SignalClass.SIGNAL,
                note=f"{len(profiled)} profiled wallets active, mean alpha "
                     f"{statistics.fmean(alphas):+.4f}"))

        # --- news and events, only if the collector has anything
        news = self.store.one(
            "SELECT COUNT(*) n, MAX(l.magnitude) mag, AVG(l.direction) dir "
            "  FROM news_market_links l JOIN news_items i ON i.id = l.news_id "
            " WHERE l.market_id=? AND i.capture_ts<=? AND i.capture_ts>?",
            (market_id, as_of, as_of - 6 * 3600))
        if news and int(news.get("n") or 0) > 0:
            o.news_score = min(1.0, float(news.get("mag") or 0.0))
            o.event_score = min(1.0, int(news["n"]) / 5.0)
            o.signals.append(Signal(
                source="news", kind="linked_items",
                direction=float(news.get("dir") or 0.0),
                strength=o.news_score, classification=SignalClass.INFORMATION,
                note=f"{news['n']} linked items in 6h"))

        # --- cross-market
        sibs = self.store.query(
            "SELECT market_id FROM markets WHERE event_id = "
            "  (SELECT event_id FROM markets WHERE market_id=? AND event_id!='')"
            " AND market_id != ? LIMIT 20", (market_id, market_id))
        if sibs:
            o.cross_market_score = min(1.0, len(sibs) / 4.0)

        # --- execution feasibility. Multiplies the whole score, so an
        # untradeable market ranks at zero rather than merely lower.
        notional_h = sum(n for t, _, n, _ in prints if t > as_of - 3600)
        takeable = notional_h * self.st.costs.fill_ratio_assumption
        min_order = self.st.capital.min_order_usdc
        o.execution_score = 0.0 if takeable < min_order else \
            min(1.0, math.log1p(takeable / min_order) / math.log1p(50))

        # --- risk: subtracted, so a hazardous market cannot top the list
        o.risk_score = min(1.0, 0.5 * min(1.0, vol / 0.08)
                           + 0.5 * (1.0 if pph < 2 else 0.0))

        # Confidence here is stage-1 only and is deliberately capped: nothing
        # that has not been through the gates may report high confidence.
        o.confidence = min(0.5, o.overall_score)
        return o if o.overall_score > 0 else None

    def _band(self, price: float) -> dict:
        from ..intelligence.wallets import _band
        lo, hi = _band(price)
        key = f"band_baseline_{lo:.2f}_{hi:.2f}"
        cached = self.store.get_meta(key, "")
        if cached:
            import json
            try:
                return json.loads(cached)
            except Exception:                                  # noqa: BLE001
                return {}
        base = self.source.price_band_baseline(lo, hi)
        import json
        self.store.set_meta(key, json.dumps(base))
        return base
