"""Recording the moment an outcome actually became public.

This is the highest-value single change available to the project, and the
reason is worth stating precisely rather than asserted.

**The problem.** `resolutions.settled_ts` is 0 in all 8,116 rows of V1's
`intel.sqlite3`. The ingester never populated it. So the only settlement clock
available is `resolutions.ts` — when the V1 system *observed* the resolution —
which is later than the trade in 100% of joined rows.

**Why that is safe but useless.** Using observation time can only *delay* the
moment an outcome enters a wallet's statistics, never advance it, so it cannot
leak. But its range spans roughly 7.4 days, which means every trade older than
that appears to settle simultaneously. The measured consequence, from V2's own
feature audit: `pit_evidence_share` is **0.00** for every wallet tested — not
one trade had any settled track record behind it at the moment it was placed.

**What that breaks.** Four search axes become structurally inert:
`min_settled_n`, `min_roll_win_rate`, `max_consec_losses`, `min_edge_t`. The
sweep still *tests* them, so it pays the multiple-testing cost of 5,184
transformations while only ~432 are distinct — making the Benjamini–Hochberg
threshold roughly **12x stricter than the evidence requires**, for no benefit.
Every hypothesis of the form "follow this wallet while it is running hot" is
currently untestable. Not false. Untestable.

**The fix, in three tiers.** V3 cannot write to the V1 file, so true settlement
times live in V3's own `resolution_times` table, and `HistoricalSource` prefers
it. Each row records HOW its timestamp was established:

    VENUE_REPORTED   the venue's own resolution timestamp        confidence 1.00
    CHAIN_EVENT      the on-chain resolution transaction         confidence 0.95
    FIRST_OBSERVED   first time OUR collector saw it resolved    confidence 0.60
    V1_FALLBACK      V1's observation time, carried over         confidence 0.20

These are never averaged. A tier-4 fallback is worse than the tier-1 value it
replaces, and blending them would produce a number with no interpretation.

**Honest limit.** Tiers 1 and 2 can backfill history — the venue and the chain
both remember. Tier 3 cannot: it only starts working from the moment the
collector runs. So this improves history *if* the venue API still serves
resolution times for old markets, and improves the future unconditionally.
`backfill()` reports which tier each row came from, so the improvement is
measured rather than assumed.
"""

from __future__ import annotations

import time

from .base import Collector, CollectorRun, http_json

METHOD_CONFIDENCE = {
    "VENUE_REPORTED": 1.00,
    "CHAIN_EVENT": 0.95,
    "FIRST_OBSERVED": 0.60,
    "V1_FALLBACK": 0.20,
}


class SettlementTimeCollector(Collector):
    """Establishes real settlement timestamps and stores them V3-side."""

    name = "settled_ts"

    # -- live capture -------------------------------------------------------
    def _run(self, run: CollectorRun) -> None:
        """Tier 3: notice newly-resolved markets and stamp the moment we saw it.

        Cheap and continuous. For any market that resolves while this is
        running, FIRST_OBSERVED is accurate to within one poll interval, which
        is a different quality of evidence from V1's multi-day smear.
        """
        from ..core.source import HistoricalSource
        src = HistoricalSource(self.st)
        if not src.available:
            run.status = "ERROR"
            run.error = "no V1 source to reconcile against"
            return

        known = {r["token_id"] for r in self.store.query(
            "SELECT token_id FROM resolution_times")}
        conn = src._conn()
        try:
            rows = conn.execute(
                "SELECT token_id, market_id, price, ts FROM resolutions "
                " WHERE price IN (0.0, 1.0)").fetchall()
        finally:
            conn.close()

        now = int(time.time())
        fresh = [dict(r) for r in rows if r["token_id"] not in known]
        if not fresh:
            run.detail = f"{len(known)} tokens already timestamped; none new"
            return

        # A resolution row V1 wrote very recently is one we can date well. One
        # it wrote long ago we cannot — carrying it over as V1_FALLBACK is
        # honest; stamping it FIRST_OBSERVED now would claim it resolved today.
        recent_cut = now - 3600
        payload = []
        for r in fresh:
            v1_ts = int(r["ts"] or 0)
            if v1_ts >= recent_cut:
                method, settled = "FIRST_OBSERVED", v1_ts
            else:
                method, settled = "V1_FALLBACK", v1_ts
            payload.append({
                "token_id": r["token_id"], "market_id": r["market_id"] or "",
                "outcome_price": float(r["price"]), "settled_ts": settled,
                "method": method, "confidence": METHOD_CONFIDENCE[method],
                "v1_ts": v1_ts, "delta_secs": 0, "ts": settled})
        run.rows = self.store.insert("resolution_times", payload,
                                     source=self.name)
        n_obs = sum(1 for p in payload if p["method"] == "FIRST_OBSERVED")
        run.detail = (f"{run.rows} new tokens: {n_obs} FIRST_OBSERVED, "
                      f"{run.rows - n_obs} V1_FALLBACK")
        run.notes.append(
            "V1_FALLBACK rows carry confidence 0.20 and do not enable "
            "point-in-time track-record features; only backfill or live "
            "capture can improve them")

    # -- backfill -----------------------------------------------------------
    def backfill(self, *, limit: int = 500) -> dict:
        """Tier 1: ask the venue for real resolution timestamps.

        This is the call that can repair history. It is separated from `_run`
        because it is expensive, rate-limited and worth running deliberately
        rather than on every poll.
        """
        from ..core.source import HistoricalSource
        src = HistoricalSource(self.st)
        out = {"attempted": 0, "upgraded": 0, "unchanged": 0, "errors": 0,
               "by_method": {}, "median_delta_secs": None,
               "enabled": self.st.collectors.enabled}
        if not self.st.collectors.enabled:
            out["note"] = ("collectors are disabled; backfill makes network "
                           "calls and will not run. Enable with "
                           "`pqv3 collect --enable`.")
            return out
        if not src.available:
            out["note"] = "no V1 source"
            return out

        targets = self.store.query(
            "SELECT token_id, market_id, v1_ts FROM resolution_times "
            " WHERE method IN ('V1_FALLBACK','FIRST_OBSERVED') LIMIT ?",
            (limit,))
        if not targets:
            out["note"] = ("nothing to upgrade; run the collector once to "
                           "populate resolution_times first")
            return out

        deltas: list[int] = []
        base = self.st.collectors.gamma_base.rstrip("/")
        for t in targets:
            out["attempted"] += 1
            data, err = http_json(
                f"{base}/markets",
                params={"clob_token_ids": t["token_id"]},
                timeout=self.st.collectors.http_timeout_secs)
            if err or not data:
                out["errors"] += 1
                continue
            rec = data[0] if isinstance(data, list) and data else data
            if not isinstance(rec, dict):
                out["errors"] += 1
                continue
            settled = _parse_ts(rec.get("umaResolutionStatus")
                                and rec.get("endDate") or rec.get("closedTime")
                                or rec.get("endDate"))
            if not settled:
                out["unchanged"] += 1
                continue
            v1_ts = int(t["v1_ts"] or 0)
            delta = settled - v1_ts
            deltas.append(delta)
            self.store.insert("resolution_times", [{
                "token_id": t["token_id"], "market_id": t["market_id"],
                "settled_ts": settled, "method": "VENUE_REPORTED",
                "confidence": METHOD_CONFIDENCE["VENUE_REPORTED"],
                "v1_ts": v1_ts, "delta_secs": delta, "ts": settled}],
                source=self.name, replace=True)
            out["upgraded"] += 1

        rows = self.store.query(
            "SELECT method, COUNT(*) n FROM resolution_times GROUP BY method")
        out["by_method"] = {r["method"]: r["n"] for r in rows}
        if deltas:
            deltas.sort()
            out["median_delta_secs"] = deltas[len(deltas) // 2]
            out["note"] = (
                f"venue settlement times differ from V1's observation times by "
                f"a median of {out['median_delta_secs']}s. A large negative "
                f"delta means V1 learned of resolutions long after they "
                f"happened, which is exactly the smear that made point-in-time "
                f"track record untestable.")
        return out


def coverage(store) -> dict:
    """What fraction of settlements now have a usable timestamp.

    "Usable" means confidence >= 0.60 — the tier at which point-in-time
    track-record features stop being inert. Reporting raw row count would show
    100% coverage the moment the fallback tier runs, which would be true and
    completely misleading.
    """
    rows = store.query(
        "SELECT method, COUNT(*) n, AVG(confidence) c "
        "  FROM resolution_times GROUP BY method")
    total = sum(r["n"] for r in rows)
    usable = sum(r["n"] for r in rows if float(r["c"] or 0) >= 0.60)
    return {
        "total": total,
        "usable": usable,
        "usable_share": round(usable / total, 4) if total else 0.0,
        "by_method": {r["method"]: r["n"] for r in rows},
        "pit_features_enabled": usable >= 500,
        "note": ("point-in-time wallet track-record features stay disabled "
                 "until at least 500 settlements carry a confidence >= 0.60 "
                 "timestamp. Below that the four affected search axes are "
                 "inert and inflate the multiple-testing penalty for nothing.")
        if usable < 500 else
        f"{usable} settlements carry usable timestamps; point-in-time "
        f"track-record features are enabled.",
    }


def _parse_ts(value) -> int:
    """Accept epoch seconds, epoch millis, or an ISO-8601 string."""
    if value in (None, "", 0):
        return 0
    if isinstance(value, (int, float)):
        v = int(value)
        return v // 1000 if v > 10 ** 12 else v
    s = str(value).strip().replace("Z", "+00:00")
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:                                          # noqa: BLE001
        return 0
