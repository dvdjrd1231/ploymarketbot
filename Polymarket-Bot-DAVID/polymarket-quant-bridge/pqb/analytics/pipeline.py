"""
The analytical pass, in the order the brief specifies.

    broad ingestion -> normalisation -> feature generation
                    -> dynamic ranking / anomaly detection

The adapter has already done the first two steps by the time anything here
runs; this module owns the rest and hands the result to the decision engine
through :class:`~pqb.models.BridgeContext`.

**Two cadences, on purpose.** Rebuilding every wallet's profile and re-ranking
the whole observed population means reading the full lookback window — tens of
thousands of rows once a session has been running for a while. That is a
several-hundred-millisecond job, and doing it inside a 20-second decision cycle
would make the loop's cost grow with its own history. So the heavy pass runs on
its own timer (``refresh_seconds``) while anomaly detection runs every cycle
against the cached profiles and freshly ingested trades. A rank computed four
minutes ago is not meaningfully staler than one computed now; a convergence
cluster four minutes late has already happened.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from ..logs import Log
from ..models import (
    AnomalySignal, MarketFeatures, MarketIntel, WalletIntel, WalletSignal,
)
from .anomalies import AnomalyConfig, detect_all
from .features import build_profiles, market_intel
from .ranking import rank_wallets
from .store import IntelStore

MarkFn = Callable[[str], Optional[float]]


class IntelPipeline:
    """Owns the analytical state between cycles."""

    def __init__(self, store: IntelStore, log: Log,
                 anomaly_config: Optional[AnomalyConfig] = None,
                 pinned: Optional[dict[str, str]] = None,
                 cohort_size: int = 25, lookback_days: float = 30.0,
                 refresh_seconds: int = 300,
                 mark_fn: Optional[MarkFn] = None):
        self.store = store
        self.log = log
        self.cfg = anomaly_config or AnomalyConfig()
        self.pinned = {k.lower(): v for k, v in (pinned or {}).items()}
        self.cohort_size = cohort_size
        self.lookback_days = lookback_days
        self.refresh_seconds = refresh_seconds
        self.mark_fn = mark_fn

        self.intel: dict[str, WalletIntel] = {}
        self.market_intel: dict[str, MarketIntel] = {}
        self.anomalies: list[AnomalySignal] = []
        self._profiles: dict = {}
        self._last_refresh = 0.0
        self._observed = 0
        # (kind, subject, token) -> when it was last recorded. See _is_new.
        self._seen: dict[tuple[str, str, str], float] = {}

        # Load whatever the last session established, so cycle 1 after a
        # restart has ranks rather than an empty board it takes a refresh
        # interval to rebuild.
        try:
            self.intel = self.store.load_scores()
            self._observed = len(self.intel)
        except Exception as exc:                      # pragma: no cover
            self.log.warning("Could not load wallet scores", error=repr(exc))

    # -- resolutions ---------------------------------------------------------

    def record_resolutions(self, markets: dict[str, MarketFeatures],
                           resolved_price: Callable[[str, str],
                                                    Optional[float]]) -> int:
        """Capture how markets settled — the ground truth wallet skill needs.

        Called every cycle with the markets currently in view. Writes are
        ignore-on-conflict, so re-seeing a settled market costs one no-op
        insert rather than rewriting the value historical scores were computed
        against.
        """
        written = 0
        for market_id, market in markets.items():
            if market.status.value == "active":
                continue
            for token_id in market.quotes:
                price = resolved_price(market_id, token_id)
                if price is None:
                    continue
                self.store.record_resolution(token_id, market_id, price)
                written += 1
        return written

    # -- the pass ------------------------------------------------------------

    def refresh(self, force: bool = False,
                now: Optional[float] = None) -> bool:
        """Rebuild profiles and re-rank. Returns True when it actually ran."""
        now = time.time() if now is None else now
        if not force and now - self._last_refresh < self.refresh_seconds:
            return False

        since = int(now - self.lookback_days * 86400)
        rows = self.store.trades_since(since)
        if not rows:
            self._last_refresh = now
            return False

        resolutions = self.store.resolutions()
        self._profiles = build_profiles(rows, resolutions,
                                        mark_fn=self.mark_fn, now=now)
        self.intel = rank_wallets(self._profiles, pinned=self.pinned,
                                  cohort_size=self.cohort_size, now=now)
        self._observed = len(self.intel)
        self.store.save_scores(self.intel.values())
        self.store.rollup(since_ts=int(now - 86400 * 3))
        self._last_refresh = now

        ranked = sum(1 for w in self.intel.values() if w.rank)
        # O(1) stats for the dashboard. The reader used to COUNT(DISTINCT)
        # over the raw trades on the GUI thread — seconds per refresh on a
        # grown store, which is exactly a window going "Not Responding" after
        # hours. These are written here, on the bot's own timer, instead.
        try:
            self.store.set_meta("observed_wallets", float(self._observed))
            self.store.set_meta("ranked_wallets", float(ranked))
            # MAX(id), not COUNT(*): the table is insert-only with an
            # autoincrement id, so the max IS the lifetime count, at O(1) —
            # and it stays O(1) at ten million rows.
            self.store.set_meta(
                "anomaly_count",
                float(self.store.query(
                    "SELECT COALESCE(MAX(id),0) n FROM anomalies")[0]["n"]))
            # The GROUP BY over research_rows lives HERE (every 300s, off the
            # GUI), never on the window's refresh path.
            self.store.set_meta(
                "tokens_ready",
                float(len(self.store.research_tokens(min_rows=200))))
        except Exception:                                # noqa: BLE001
            pass
        top = sorted((w for w in self.intel.values() if w.rank),
                     key=lambda w: w.rank)[:3]
        self.log.event("intel.ranked", observed=self._observed, ranked=ranked,
                       scored=sum(1 for p in self._profiles.values() if p.scores),
                       resolutions=len(resolutions),
                       top=", ".join(f"{w.label}:{w.score:.3f}" for w in top)
                       or None)
        return True

    def detect(self, markets: dict[str, MarketFeatures],
               now: Optional[float] = None) -> list[AnomalySignal]:
        """Run the detectors against what has just been ingested."""
        now = time.time() if now is None else now
        if not self.cfg.enabled:
            self.anomalies = []
            return []

        recent = self.store.trades_since(int(now - self.cfg.recent_seconds))
        history = self.store.trades_since(
            int(now - self.cfg.lookback_days * 86400))
        activity = self.store.market_activity(
            int(now - self.cfg.recent_seconds))

        cohort_wallets = {w.wallet for w in self.intel.values() if w.in_cohort}
        cohort_activity: dict[str, dict] = {}
        for row in recent:
            if str(row.get("wallet") or "").lower() not in cohort_wallets:
                continue
            market_id = str(row.get("market_id") or "")
            if not market_id:
                continue
            bucket = cohort_activity.setdefault(
                market_id, {"wallets": set(), "net_usdc": 0.0})
            bucket["wallets"].add(row["wallet"])
            usdc = float(row.get("usdc") or 0.0)
            bucket["net_usdc"] += (usdc if str(row.get("side")).upper() == "BUY"
                                   else -usdc)
        for bucket in cohort_activity.values():
            bucket["wallets"] = len(bucket["wallets"])

        self.market_intel = market_intel(activity, self.store, cohort_wallets,
                                         cohort_activity, now=now)
        categories = {mid: m.category for mid, m in markets.items()}
        end_times = {mid: m.end_ts for mid, m in markets.items() if m.end_ts}

        self.anomalies = detect_all(
            recent=recent, history=history, profiles=self._profiles,
            intel=self.intel, markets=self.market_intel, categories=categories,
            end_times=end_times, cfg=self.cfg, now=now)

        # A detection stays true for as long as the evidence is inside the
        # recent window, so the same convergence cluster is found again on
        # every cycle for the next hour. The engine must keep *seeing* it — it
        # is still live evidence — but recording it each time would write the
        # same event 180 times and make "how often does this fire?" unanswerable
        # from the table that exists to answer it.
        fresh = [a for a in self.anomalies if self._is_new(a, now)]
        if fresh:
            self.store.record_anomalies(fresh)
            counts: dict[str, int] = {}
            for anomaly in fresh:
                counts[anomaly.kind] = counts.get(anomaly.kind, 0) + 1
            self.log.event("intel.anomalies", new=len(fresh),
                           active=len(self.anomalies),
                           **{k: v for k, v in sorted(counts.items())})
            for anomaly in fresh[:5]:
                self.log.event("anomaly", kind=anomaly.kind,
                               subject=(anomaly.label or anomaly.subject)[:24],
                               token=anomaly.token_id[:12] or None,
                               z=round(anomaly.z, 2),
                               strength=round(anomaly.strength, 3))
        return self.anomalies

    def _is_new(self, anomaly, now: float) -> bool:
        """First sighting of this detection, or the first since it lapsed."""
        key = (anomaly.kind, anomaly.subject, anomaly.token_id)
        last = self._seen.get(key)
        if last is not None and now - last < self.cfg.recent_seconds:
            return False
        self._seen[key] = now
        # Bounded: keys expire once they are older than any window that could
        # re-detect them, so a long session cannot grow this without limit.
        if len(self._seen) > 20_000:
            cutoff = now - self.cfg.recent_seconds
            self._seen = {k: t for k, t in self._seen.items() if t >= cutoff}
        return True

    # -- observed activity as weighted evidence ------------------------------

    def signals_from(self, trades, tokens: set[str],
                     max_signals: int = 400) -> list[WalletSignal]:
        """Turn freshly observed trades into weighted evidence for the engine.

        This is what connects broad ingestion to the decision. Without it the
        analytical layer ranks thousands of wallets and none of them ever
        reaches the engine: ``WalletSignal`` used to be produced only for
        wallets named in the config, so an empty ``wallets:`` list — the
        intended default — meant the wallet term was a structural zero. Blended
        at ``wallet_signal_weight``, that silently caps every score at
        ``1 - weight`` and makes entry arithmetically impossible while the logs
        show a healthy bridge observing and ranking away.

        Two deliberate choices:

        * **Filtered to the tracked universe.** A trade in a market we are not
          watching cannot inform a decision about one we are, and keeping them
          would put thousands of irrelevant signals through every scan.
        * **Not restricted to the ranked cohort.** Any observed wallet with a
          profile contributes; what differs is its *weight*, which is its
          measured score scaled by how much evidence stands behind it. An
          unranked wallet therefore carries almost nothing rather than being
          excluded — which is what keeps a signal from outside the cohort
          discoverable, as the brief requires.
        """
        if not tokens:
            return []
        out: list[WalletSignal] = []
        for trade in trades:
            token = str(getattr(trade, "token_id", "") or "")
            if token not in tokens:
                continue
            intel = self.intel.get(str(getattr(trade, "wallet", "")).lower())
            if intel is None:
                continue
            out.append(WalletSignal(
                wallet=intel.wallet, label=intel.label, weight=intel.weight,
                action="ENTRY" if trade.side == "BUY" else "EXIT",
                token_id=token, market_id=trade.market_id,
                outcome=trade.outcome, price=trade.price, size=trade.size,
                usdc=trade.usdc, timestamp=trade.ts,
            ))
        # Strongest evidence first, then bounded: a burst on the exchange must
        # not turn one cycle's scan into an unbounded scan.
        out.sort(key=lambda s: s.weight, reverse=True)
        return out[:max_signals]

    # -- diagnostics ---------------------------------------------------------

    @property
    def observed_wallets(self) -> int:
        return self._observed

    def cohort(self) -> list[WalletIntel]:
        return sorted((w for w in self.intel.values() if w.in_cohort),
                      key=lambda w: w.rank)

    def status(self) -> dict:
        ranked = [w for w in self.intel.values() if w.rank]
        return {
            "observed": self._observed,
            "ranked": len(ranked),
            "cohort": sum(1 for w in ranked if w.in_cohort),
            "anomalies": len(self.anomalies),
            "lastRefresh": self._last_refresh,
            "store": self.store.stats(),
        }
