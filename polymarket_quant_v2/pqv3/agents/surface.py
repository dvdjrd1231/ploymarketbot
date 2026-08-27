"""§40 — continuous operational intelligence.

    "When appropriate, surface important discoveries to the user through the
     chat interface. Do not overwhelm the user with trivial events. Prioritize
     information according to IMPORTANCE x EXPECTED ECONOMIC IMPACT x URGENCY."

Two halves, and the second is the harder one. Detecting that something changed
is easy; deciding it is worth interrupting somebody over is the actual problem,
and a monitor that fails at it gets muted, after which it detects nothing that
matters to anyone.

So priority is a product of three factors, each bounded 0..1 and each carrying
its own justification string. A product, not a sum: a finding that is urgent
and important but has no plausible economic consequence scores near zero and
stays quiet, which is the correct treatment of an alarming-looking reading that
cannot cost anything. `PRIORITY_FLOOR` then decides what surfaces at all; the
rest is still recorded, so "you never told me" has an answer.

These three numbers are ESTIMATES and are labelled as such everywhere they are
rendered. They rank a queue. They are not measurements, they never enter a
decision, and no sizing or gate reads them — which is exactly why the estimate
is permissible here and would not be one line further down.

DEDUPLICATION is by `key` plus the measured value. The same condition,
unchanged, surfaces once. When the value moves it surfaces again, because "the
drawdown is 8%" and "the drawdown is 19%" are different facts wearing the same
name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

PRIORITY_FLOOR = 0.18       # below this: recorded, not surfaced
MAX_SURFACED = 5            # per read. §40's "do not overwhelm", as a number


@dataclass
class Discovery:
    key: str
    kind: str
    headline: str
    measured: str
    importance: float
    impact: float
    urgency: float
    why: str = ""
    action: str = ""
    ts: int = 0

    @property
    def priority(self) -> float:
        return round(self.importance * self.impact * self.urgency, 4)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["priority"] = self.priority
        d["basis"] = ("importance x expected economic impact x urgency, each "
                      "an ESTIMATE bounded 0..1. This ranks a queue; it is not "
                      "a measurement and nothing downstream reads it")
        return d


class Surfacer:
    """Reads the store, ranks what changed, remembers what it already said."""

    def __init__(self, st, store, engine=None) -> None:
        self.st = st
        self.store = store
        self.engine = engine
        self.last_delivery: dict = {}

    # ------------------------------------------------------------- detectors
    def detect(self) -> list[Discovery]:
        out: list[Discovery] = []
        for fn in (self._collectors, self._drawdown, self._crash,
                   self._gates_costing_money, self._degraded_strategies,
                   self._new_validated, self._data_staleness,
                   self._retire_signals, self._settlement_unlocked,
                   self._liquidity_withdrawal, self._spread_dislocation,
                   self._stale_probability, self._volume_shock):
            try:
                out.extend(fn() or [])
            except Exception as e:                            # noqa: BLE001
                out.append(Discovery(
                    key=f"detector_error:{fn.__name__}", kind="SYSTEM",
                    headline=f"a monitor raised while running",
                    measured=f"{fn.__name__}: {type(e).__name__}: {e}",
                    importance=0.6, impact=0.3, urgency=0.5,
                    why="a monitor that fails silently is worse than no "
                        "monitor: it reports calm it never checked"))
        now = int(time.time())
        for d in out:
            d.ts = d.ts or now
        out.sort(key=lambda d: -d.priority)
        return out

    def _collectors(self) -> list:
        out = []
        for h in self.store.health():
            if h["status"] != "ERROR":
                continue
            out.append(Discovery(
                key=f"collector_error:{h['collector']}", kind="DATA",
                headline=f"{h['collector']} collector is failing",
                measured=(h.get("error") or "")[:160],
                importance=0.8, impact=0.5, urgency=0.9,
                why="capture that stops is history that cannot be recovered "
                    "later; order-book and news gaps are permanent",
                action="pqv3 collect --enable"))
        return out

    def _drawdown(self) -> list:
        from ..portfolio.capital import account_from_store
        mode = self.st.mode.value
        acct = account_from_store(self.store, self.st, mode)
        stop = self.st.capital.hard_stop_drawdown
        if acct.drawdown <= 0 or stop <= 0:
            return []
        frac = acct.drawdown / stop
        if frac < 0.5:
            return []
        return [Discovery(
            key="drawdown", kind="RISK",
            headline=("drawdown has reached the hard stop; trading is halted"
                      if frac >= 1 else
                      "drawdown is approaching the hard stop"),
            measured=f"{acct.drawdown:.1%} of a {stop:.0%} stop ({mode})",
            importance=1.0, impact=1.0, urgency=min(1.0, frac),
            why="the hard stop halts everything and requires a human to "
                "resume. Reaching it unnoticed wastes the pause",
            action="pqv3 chat 'explain the losses'")]

    def _crash(self) -> list:
        c = getattr(self.engine, "last_crash", None) if self.engine else None
        if not c or getattr(c, "level", "NORMAL") == "NORMAL":
            return []
        return [Discovery(
            key=f"crash:{c.level}", kind="RISK",
            headline=f"crash meter reads {c.level}",
            measured=f"score {getattr(c, 'score', 0):.3f}, confidence "
                     f"{getattr(c, 'confidence', 0):.2f}",
            importance=0.9, impact=0.8,
            urgency=1.0 if c.level == "CRASH" else 0.6,
            why="the meter reports the strongest single input as the level, "
                "so this is one alarming reading rather than an average")]

    def _gates_costing_money(self) -> list:
        """§20/§26: a gate that forgoes more than it avoids is a live cost."""
        from ..learning.forensics import Forensics
        f = Forensics(self.st, self.store)
        out = []
        for row in f.gate_cost_report():
            net = row.get("net")
            if net is None or net >= 0 or int(row.get("n") or 0) < 10:
                continue
            out.append(Discovery(
                key=f"gate_cost:{row['gate']}", kind="RESEARCH",
                headline=f"{row['gate']} has forgone more than it avoided",
                measured=f"net {net:+.3f} over n={row['n']} "
                         f"(avoided {row.get('avoided')}, "
                         f"forgone {row.get('forgone')})",
                importance=0.7, impact=0.7, urgency=0.3,
                why="a gate is only worth keeping if it avoids more than it "
                    "costs. This is a research finding, NOT an instruction to "
                    "unblock it — it becomes a change by going through "
                    "`pqv3 invert` and then discovery, like anything else",
                action="pqv3 invert --gates-only"))
        return out

    def _degraded_strategies(self) -> list:
        n = self.store.count("strategies", "status='DEGRADED'")
        if not n:
            return []
        return [Discovery(
            key=f"degraded:{n}", kind="RESEARCH",
            headline=f"{n} strategy/strategies marked DEGRADED",
            measured=f"{n} on the lifecycle ladder",
            importance=0.7, impact=0.6, urgency=0.4,
            why="§19: the market is nonstationary and an edge that worked "
                "before may have stopped. Degradation detected late is "
                "capital already lost",
            action="pqv3 strategies")]

    def _new_validated(self) -> list:
        n = self.store.count("strategies", "status IN ('APPROVED','LIVE')")
        if not n:
            return []
        return [Discovery(
            key=f"validated:{n}", kind="RESEARCH",
            headline=f"{n} strategy/strategies stand APPROVED or LIVE",
            measured=f"{n} past the validation ladder",
            importance=0.6, impact=0.8, urgency=0.2,
            why="approved work that nothing consumes is the most expensive "
                "kind of research",
            action="pqv3 signals")]

    def _data_staleness(self) -> list:
        if not self.st.collectors.enabled:
            return []           # not stale, just off — reported by the audit
        out = []
        for table, col, limit, label in (
                ("book_snapshots", "capture_ts",
                 self.st.freshness.max_book_age_secs, "order book"),
                ("news_items", "capture_ts",
                 self.st.freshness.max_news_age_secs, "news"),
                ("chain_events", "ts",
                 self.st.freshness.max_chain_age_secs, "chain")):
            last = self.store.scalar(f"SELECT MAX({col}) FROM {table}",
                                     default=0) or 0
            if not last:
                continue
            age = int(time.time()) - int(last)
            if age <= limit * 3:
                continue
            out.append(Discovery(
                key=f"stale:{table}", kind="DATA",
                headline=f"{label} data has stopped arriving",
                measured=f"newest row is {age // 60} min old; the freshness "
                         f"limit is {limit // 60} min",
                importance=0.8, impact=0.6, urgency=0.8,
                why="stale data must stop a trade rather than slow one down. "
                    "Every gate reading this layer will now refuse"))
        return out

    def _retire_signals(self) -> list:
        n = self.store.count("loss_forensics", "remedy='retire'")
        if not n:
            return []
        return [Discovery(
            key=f"retire:{n}", kind="LEARNING",
            headline=f"{n} loss(es) carry a 'retire' remedy",
            measured=f"{n} forensic record(s)",
            importance=0.7, impact=0.7, urgency=0.4,
            why="§22: the point of forensics is not repeating a known failure",
            action="pqv3 forensics")]

    def _settlement_unlocked(self) -> list:
        """Good news is news too — §40 does not say 'surface only problems'."""
        from ..ingest.settled_ts import coverage
        cov = coverage(self.store)
        if not cov.get("pit_features_enabled"):
            return []
        return [Discovery(
            key="settlement_unlocked", kind="DATA",
            headline="settlement timestamps now support point-in-time features",
            measured=f"{cov.get('usable')}/{cov.get('total')} usable",
            importance=0.6, impact=0.8, urgency=0.3,
            why="four search axes that were inert become live, which changes "
                "the denominator. The next pass is not comparable to the last",
            action="pqv3 discover --rebuild")]

    # ------------------------------------------------- §20 market anomalies
    #
    # These four read `book_snapshots`, which is empty on a fresh install and
    # cannot be backfilled. They are written anyway, and each returns nothing
    # rather than raising when the history is thin, so the capability exists
    # the moment collection has run long enough rather than being a thing
    # somebody has to remember to add later. `_microstructure_ready` reports
    # which of the two it is, and the audit reads that instead of guessing.

    MIN_SNAPSHOTS = 200

    def _microstructure_ready(self) -> tuple[bool, str]:
        n = self.store.count("book_snapshots")
        span = self.store.history_span_days("book_snapshots")
        if n < self.MIN_SNAPSHOTS:
            return False, (f"{n} order-book snapshots, under the "
                           f"{self.MIN_SNAPSHOTS} needed for a baseline. "
                           f"Depth history accumulates only while collectors "
                           f"run and cannot be backfilled")
        return True, f"{n} snapshots over {span:.1f} d"

    def _book_baseline(self, col: str) -> list:
        """Per-token recent-vs-prior comparison on one book column."""
        return self.store.query(
            f"SELECT token_id,"
            f"       AVG(CASE WHEN capture_ts >= ? THEN {col} END) recent,"
            f"       AVG(CASE WHEN capture_ts <  ? THEN {col} END) prior,"
            f"       COUNT(*) n"
            f"  FROM book_snapshots GROUP BY token_id HAVING n >= 40",
            (int(time.time()) - 3600, int(time.time()) - 3600))

    def _liquidity_withdrawal(self) -> list:
        ok, _ = self._microstructure_ready()
        if not ok:
            return []
        out = []
        for r in self._book_baseline("(bid_depth + ask_depth)"):
            recent, prior = r["recent"], r["prior"]
            if not recent or not prior or prior <= 0:
                continue
            drop = 1.0 - (recent / prior)
            if drop < 0.5:
                continue
            out.append(Discovery(
                key=f"liquidity_withdrawal:{r['token_id']}", kind="MARKET",
                headline="depth has been withdrawn from a market",
                measured=f"{r['token_id'][:20]}: mean depth {prior:.1f} -> "
                         f"{recent:.1f} ({drop:.0%} gone)",
                importance=0.8, impact=0.7, urgency=0.8,
                why="§20: liquidity leaving ahead of a move is either "
                    "information arriving or a maker stepping away. Either "
                    "way the execution model's fill assumption no longer "
                    "holds, so a size that was feasible an hour ago may not be"))
        return out

    def _spread_dislocation(self) -> list:
        ok, _ = self._microstructure_ready()
        if not ok:
            return []
        out = []
        for r in self._book_baseline("spread"):
            recent, prior = r["recent"], r["prior"]
            if not recent or not prior or prior <= 0:
                continue
            if recent < prior * 3.0:
                continue
            out.append(Discovery(
                key=f"spread_blowout:{r['token_id']}", kind="MARKET",
                headline="spread has widened sharply",
                measured=f"{r['token_id'][:20]}: {prior:.4f} -> {recent:.4f}",
                importance=0.7, impact=0.8, urgency=0.7,
                why="a spread that triples is the cost of crossing tripling. "
                    "An edge measured against the old spread may not survive "
                    "the new one"))
        return out

    def _stale_probability(self) -> list:
        """A book that has not moved while others have. §20's stale price."""
        ok, _ = self._microstructure_ready()
        if not ok:
            return []
        rows = self.store.query(
            "SELECT token_id, COUNT(DISTINCT mid) distinct_mid, COUNT(*) n,"
            "       MAX(capture_ts) last_ts FROM book_snapshots"
            " WHERE capture_ts >= ? GROUP BY token_id HAVING n >= 60",
            (int(time.time()) - 6 * 3600,))
        out = []
        for r in rows:
            if r["distinct_mid"] > 1:
                continue
            out.append(Discovery(
                key=f"stale_price:{r['token_id']}", kind="MARKET",
                headline="a quoted mid has not moved at all",
                measured=f"{r['token_id'][:20]}: one distinct mid across "
                         f"{r['n']} snapshots in six hours",
                importance=0.6, impact=0.6, urgency=0.5,
                why="§20: a price that never moves is either a market nobody "
                    "is pricing or a feed that has frozen. Both matter and "
                    "they are not the same problem — check collector health "
                    "before reading it as an opportunity"))
        return out

    def _volume_shock(self) -> list:
        """Unusual trade flow, from decisions rather than from the book.

        This one needs no order-book history, so it works on a fresh install:
        the scanner records how many markets it saw, and a step change in that
        is worth knowing about whether or not depth is being captured.
        """
        rows = self.store.query(
            "SELECT market_id, COUNT(*) n FROM decisions"
            " WHERE ts >= ? GROUP BY market_id ORDER BY n DESC LIMIT 5",
            (int(time.time()) - 3600,))
        if not rows or rows[0]["n"] < 20:
            return []
        return [Discovery(
            key=f"decision_burst:{rows[0]['market_id']}", kind="MARKET",
            headline="one market is absorbing most of the decision traffic",
            measured=f"{rows[0]['market_id'][:24]}: {rows[0]['n']} decisions "
                     f"in an hour",
            importance=0.5, impact=0.5, urgency=0.4,
            why="repeated evaluation of one market usually means it keeps "
                "reaching the gates and keeps failing at the same one. Worth "
                "reading the blocking gate rather than the count")]

    def microstructure_status(self) -> dict:
        """What the §20 market detectors can currently see."""
        ready, detail = self._microstructure_ready()
        return {"ready": ready, "detail": detail,
                "detectors": ["liquidity_withdrawal", "spread_dislocation",
                              "stale_probability", "volume_shock"],
                "note": ("all four are implemented. The three that read "
                         "order-book depth stay silent until enough history "
                         "exists, because depth cannot be backfilled — that "
                         "is a data limit, not a missing capability"
                         if not ready else
                         "order-book history is sufficient; all four are "
                         "active")}

    # ------------------------------------------------------------ persistence
    def _already_said(self, d: Discovery) -> bool:
        row = self.store.one(
            "SELECT measured FROM discoveries WHERE key=? "
            " ORDER BY id DESC LIMIT 1", (d.key,))
        return bool(row) and row["measured"] == d.measured

    def run(self) -> list[dict]:
        """Detect, record what is new, return what is worth interrupting for."""
        fresh = []
        for d in self.detect():
            if self._already_said(d):
                continue
            try:
                self.store.insert("discoveries", [{
                    "key": d.key, "kind": d.kind, "headline": d.headline,
                    "measured": d.measured, "importance": d.importance,
                    "impact": d.impact, "urgency": d.urgency,
                    "priority": d.priority, "why": d.why, "action": d.action,
                    "surfaced": int(d.priority >= PRIORITY_FLOOR),
                }], source="surfacer")
            except Exception:                                 # noqa: BLE001
                pass
            fresh.append(d)

        surfaced = [d.to_dict() for d in fresh
                    if d.priority >= PRIORITY_FLOOR][:MAX_SURFACED]
        if surfaced:
            # §40's outbound half. Failure here must not take the loop with it:
            # a monitor that dies on an unreachable endpoint has become the
            # outage it was installed to report.
            try:
                from .notify import send
                self.last_delivery = send(self.st, surfaced)
            except Exception as e:                            # noqa: BLE001
                self.last_delivery = {"error": f"{type(e).__name__}: {e}"}
        return surfaced

    def pending(self, limit: int = 20) -> list[dict]:
        return self.store.query(
            "SELECT * FROM discoveries WHERE acked=0 AND surfaced=1 "
            " ORDER BY priority DESC, id DESC LIMIT ?", (limit,))

    def ack(self, key: str = "") -> int:
        c = self.store.conn()
        if key:
            cur = c.execute("UPDATE discoveries SET acked=1 WHERE key=? "
                            "AND acked=0", (key,))
        else:
            cur = c.execute("UPDATE discoveries SET acked=1 WHERE acked=0")
        c.commit()
        return cur.rowcount
