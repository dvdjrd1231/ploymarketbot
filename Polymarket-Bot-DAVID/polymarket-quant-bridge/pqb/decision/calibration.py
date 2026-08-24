"""
Continuous outcome feedback: was the probability we stated actually right?

A probability model that is never checked is an opinion with decimal places.
This records what we predicted, waits for the market to settle, and then asks
the only question that matters: **when we said 70%, did it happen about 70% of
the time?**

That is calibration, and it is a different question from profitability. A model
can be profitable by luck and badly calibrated; one that is well calibrated has
an edge that is likely to persist. Both are reported, because the brief asks
for evidence that the edge is real before any money is put behind it.

Nothing here changes a decision. It measures, and the measurements are what
justify — or refuse — the step to live trading.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS predictions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    token_id     TEXT NOT NULL,
    market_id    TEXT,
    outcome      TEXT,
    probability  REAL NOT NULL,      -- what we said
    market_price REAL NOT NULL,      -- what the crowd said
    ev           REAL NOT NULL,
    confidence   REAL NOT NULL,
    stake        REAL DEFAULT 0,
    acted        INTEGER DEFAULT 0,  -- did we actually trade it
    settled      REAL,               -- 1.0 / 0.0 once known
    settled_ts   REAL,
    evidence     TEXT,
    category     TEXT,               -- market category, for the breakdown
    ttr          TEXT,               -- time-to-resolution bucket
    consensus    INTEGER DEFAULT 0   -- independent wallets behind the estimate
);
CREATE INDEX IF NOT EXISTS idx_pred_token ON predictions(token_id);
CREATE INDEX IF NOT EXISTS idx_pred_open  ON predictions(settled);
"""

# Probability buckets for the calibration table.
BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01))


@dataclass
class BucketStat:
    low: float
    high: float
    n: int = 0
    predicted_sum: float = 0.0
    hits: int = 0

    @property
    def predicted(self) -> float:
        return self.predicted_sum / self.n if self.n else 0.0

    @property
    def actual(self) -> float:
        return self.hits / self.n if self.n else 0.0

    @property
    def error(self) -> float:
        return self.actual - self.predicted

    def to_dict(self) -> dict:
        return {"range": f"{self.low:.0%}-{self.high:.0%}", "n": self.n,
                "predicted": round(self.predicted, 4),
                "actual": round(self.actual, 4), "error": round(self.error, 4)}


class Calibration:
    """Records predictions and scores them once markets settle."""

    def __init__(self, journal):
        self.journal = journal
        self.journal.execute_script(_SCHEMA) if hasattr(
            self.journal, "execute_script") else self._ensure()
        self._migrate()

    def _ensure(self) -> None:
        for statement in _SCHEMA.strip().split(";"):
            if statement.strip():
                self.journal.execute(statement)

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created.

        ``CREATE TABLE IF NOT EXISTS`` never alters an existing table, so a new
        column would otherwise be invisible on an upgraded install and every
        insert naming it would fail. Adding the column above is all that's
        needed, here and for existing databases both.
        """
        try:
            have = {r["name"] for r in
                    self.journal.query("PRAGMA table_info(predictions)")}
        except Exception:                                # noqa: BLE001
            return
        for column, decl in (("category", "TEXT"), ("ttr", "TEXT"),
                             ("consensus", "INTEGER DEFAULT 0")):
            if have and column not in have:
                self.journal.execute(
                    f"ALTER TABLE predictions ADD COLUMN {column} {decl}")

    # -- recording -----------------------------------------------------------

    def record(self, opportunity, acted: bool, stake: float = 0.0,
               category: str = "", ttr: str = "", consensus: int = 0) -> None:
        """Note a prediction. **Rejected candidates are recorded too.**

        Only scoring the trades we took would measure the filter, not the
        model: if we never act below 60% we would never learn whether our 30%
        estimates were any good, and the calibration curve would cover a
        fraction of its own range.

        ``category`` / ``ttr`` / ``consensus`` are stored so calibration can be
        broken down by them later — the model may be sharp on sports and blind
        on politics, and one blended number would hide that.
        """
        estimate = opportunity.estimate
        self.journal.execute(
            """INSERT INTO predictions(ts, token_id, market_id, outcome,
                 probability, market_price, ev, confidence, stake, acted,
                 evidence, category, ttr, consensus)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (time.time(), opportunity.token_id, opportunity.market_id,
             opportunity.outcome, estimate.probability, estimate.market_price,
             opportunity.ev_per_dollar, estimate.confidence, stake,
             1 if acted else 0,
             json.dumps(estimate.to_dict().get("evidence", []), default=str),
             category, ttr, int(consensus)))

    def settle(self, resolutions: dict[str, float]) -> int:
        """Fill in outcomes for predictions whose markets have now resolved."""
        if not resolutions:
            return 0
        open_rows = self.journal.query(
            "SELECT id, token_id FROM predictions WHERE settled IS NULL")
        done = 0
        for row in open_rows:
            value = resolutions.get(str(row["token_id"]))
            if value is None:
                continue
            self.journal.execute(
                "UPDATE predictions SET settled=?, settled_ts=? WHERE id=?",
                (1.0 if value >= 0.99 else 0.0, time.time(), row["id"]))
            done += 1
        return done

    # -- reporting -----------------------------------------------------------

    def report(self, acted_only: bool = False) -> dict:
        """Calibration, edge and Brier score over everything settled so far."""
        sql = ("SELECT probability, market_price, ev, settled, acted, stake "
               "FROM predictions WHERE settled IS NOT NULL")
        if acted_only:
            sql += " AND acted = 1"
        rows = self.journal.query(sql)
        if not rows:
            return {"n": 0, "buckets": [], "note": "nothing has settled yet"}

        buckets = [BucketStat(low, high) for low, high in BUCKETS]
        brier = market_brier = 0.0
        for row in rows:
            p, actual = float(row["probability"]), float(row["settled"])
            brier += (p - actual) ** 2
            market_brier += (float(row["market_price"]) - actual) ** 2
            for bucket in buckets:
                if bucket.low <= p < bucket.high:
                    bucket.n += 1
                    bucket.predicted_sum += p
                    bucket.hits += int(actual >= 0.5)
                    break

        n = len(rows)
        return {
            "n": n,
            # Lower is better. Beating the market's own Brier score is the
            # cleanest evidence that the edge is real rather than luck.
            "brier": round(brier / n, 5),
            "marketBrier": round(market_brier / n, 5),
            "beatsMarket": brier < market_brier,
            "buckets": [b.to_dict() for b in buckets if b.n],
            "hitRate": round(sum(1 for r in rows if r["settled"] >= 0.5) / n, 4),
        }

    def by_bucket(self, column: str) -> list[dict]:
        """Settled performance grouped by price bucket, wallet or market."""
        rows = self.journal.query(
            f"SELECT {column} AS k, COUNT(*) n, AVG(settled) hit, AVG(ev) ev "
            "FROM predictions WHERE settled IS NOT NULL "
            f"GROUP BY {column} ORDER BY n DESC LIMIT 20")
        return [{"key": r["k"], "n": r["n"], "hitRate": round(r["hit"] or 0, 4),
                 "avgEv": round(r["ev"] or 0, 5)} for r in rows]
