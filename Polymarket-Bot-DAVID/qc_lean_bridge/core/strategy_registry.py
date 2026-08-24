"""Strategy history - what OpenClaw remembers between research cycles.

Without this the platform would rediscover the same strategies forever and have
no way to answer "is the new research actually better than what is already
live?". The registry is the persistent memory:

    strategies        every candidate ever ACCEPTED, with its validated metrics,
                      its rule, and the dataset template it was trained on
    live_performance  realized live results per strategy, sampled over time
    cycles            one row per OpenClaw research cycle (audit trail)

Status lifecycle:

    validated ──promote──► deployed ──degradation──► retired
         ▲                     │
         └──── replaced ◄──────┘   (a better strategy takes its slot)

Stored in the same SQLite file as the event store by default, so a single file
is the whole platform's state.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .logging_setup import get_logger

log = get_logger("registry")

VALIDATED = "validated"
DEPLOYED = "deployed"
RETIRED = "retired"


@dataclass
class StrategyRecord:
    strategy_id: str
    created: str
    status: str
    rule: str
    score: float
    sharpe: float
    sortino: float
    profit_factor: float
    win_rate: float
    max_dd_pct: float
    expectancy: float
    trades: int
    net_profit: float
    strategy_json: str          # backtester.Strategy fields
    validation_json: str        # the full validation report
    dataset_json: str           # column/meta template -> lets live rebuild features
    cycle: int = 0
    notes: str = ""

    def strategy_dict(self) -> dict:
        return json.loads(self.strategy_json or "{}")

    def dataset_template(self) -> dict:
        return json.loads(self.dataset_json or "{}")

    def as_dict(self) -> dict:
        d = asdict(self)
        d["strategy"] = self.strategy_dict()
        for k in ("strategy_json", "validation_json", "dataset_json"):
            d.pop(k, None)
        return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StrategyRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._schema()

    def _schema(self) -> None:
        c = self.conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS strategies (
                strategy_id TEXT PRIMARY KEY,
                created TEXT, status TEXT, rule TEXT,
                score REAL, sharpe REAL, sortino REAL, profit_factor REAL,
                win_rate REAL, max_dd_pct REAL, expectancy REAL,
                trades INTEGER, net_profit REAL,
                strategy_json TEXT, validation_json TEXT, dataset_json TEXT,
                cycle INTEGER DEFAULT 0, notes TEXT DEFAULT ''
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS live_performance (
                strategy_id TEXT, ts TEXT,
                trades INTEGER, wins INTEGER, win_rate REAL,
                net_pnl REAL, expectancy REAL, max_dd REAL,
                degradation REAL, healthy INTEGER
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS cycles (
                cycle INTEGER PRIMARY KEY,
                started TEXT, finished TEXT, trigger TEXT,
                ticks INTEGER, rows INTEGER, discovered INTEGER,
                accepted INTEGER, deployed INTEGER, notes TEXT
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS ix_live_sid ON live_performance(strategy_id)")
        c.execute("CREATE INDEX IF NOT EXISTS ix_strat_status ON strategies(status)")
        self.conn.commit()

    # ------------------------------------------------------------ strategies
    def record_accepted(self, report, dataset_template: dict, cycle: int) -> StrategyRecord:
        """Persist an accepted strategy from a Phase-4/5 validation report."""
        s = report.strategy
        f = report.full
        # Cycle-qualify the id: S0007 from cycle 3 must never collide with S0007
        # from cycle 4 - they are different strategies trained on different data.
        sid = f"C{cycle:03d}_{s.id}"
        rec = StrategyRecord(
            strategy_id=sid,
            created=_now(),
            status=VALIDATED,
            rule=s.describe(),
            score=float(f.get("rank_score", 0.0)),
            sharpe=float(f.get("sharpe", 0.0)),
            sortino=float(f.get("sortino", 0.0)),
            profit_factor=float(f.get("profit_factor", 0.0)),
            win_rate=float(f.get("win_rate", 0.0)),
            max_dd_pct=float(f.get("max_drawdown_pct", 0.0)),
            expectancy=float(f.get("expectancy", 0.0)),
            trades=int(f.get("trades", 0)),
            net_profit=float(f.get("net_profit", 0.0)),
            strategy_json=json.dumps(s.to_dict()),
            validation_json=json.dumps(_report_summary(report), default=str),
            dataset_json=json.dumps(dataset_template),
            cycle=cycle,
        )
        self.conn.execute("""
            INSERT OR REPLACE INTO strategies
            (strategy_id,created,status,rule,score,sharpe,sortino,profit_factor,
             win_rate,max_dd_pct,expectancy,trades,net_profit,strategy_json,
             validation_json,dataset_json,cycle,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.strategy_id, rec.created, rec.status, rec.rule, rec.score,
             rec.sharpe, rec.sortino, rec.profit_factor, rec.win_rate,
             rec.max_dd_pct, rec.expectancy, rec.trades, rec.net_profit,
             rec.strategy_json, rec.validation_json, rec.dataset_json,
             rec.cycle, rec.notes))
        self.conn.commit()
        return rec

    def set_status(self, strategy_id: str, status: str, notes: str = "") -> None:
        self.conn.execute(
            "UPDATE strategies SET status=?, notes=? WHERE strategy_id=?",
            (status, notes, strategy_id))
        self.conn.commit()
        log.info("strategy status", extra={"strategy": strategy_id,
                                           "status": status, "notes": notes})

    def get(self, strategy_id: str) -> StrategyRecord | None:
        row = self.conn.execute(
            "SELECT * FROM strategies WHERE strategy_id=?", (strategy_id,)).fetchone()
        return _to_record(row) if row else None

    def by_status(self, status: str) -> list[StrategyRecord]:
        rows = self.conn.execute(
            "SELECT * FROM strategies WHERE status=? ORDER BY score DESC",
            (status,)).fetchall()
        return [_to_record(r) for r in rows]

    def all(self, limit: int = 200) -> list[StrategyRecord]:
        rows = self.conn.execute(
            "SELECT * FROM strategies ORDER BY cycle DESC, score DESC LIMIT ?",
            (limit,)).fetchall()
        return [_to_record(r) for r in rows]

    def deployed(self) -> list[StrategyRecord]:
        return self.by_status(DEPLOYED)

    def best_candidate(self, exclude: set[str] | None = None) -> StrategyRecord | None:
        """Highest-scoring validated strategy not already live / retired."""
        exclude = exclude or set()
        for rec in self.by_status(VALIDATED):
            if rec.strategy_id not in exclude:
                return rec
        return None

    # ------------------------------------------------- new-vs-previous compare
    def compare_to_live(self, candidates: list[StrategyRecord]) -> dict:
        """Is the new research actually better than what is already deployed?

        OpenClaw uses this to decide whether a cycle earned a redeploy. Comparing
        on the composite rank score (not raw profit) is deliberate - the whole
        point of the validation stack is that reliability beats the fattest
        backtest.
        """
        live = self.deployed()
        best_live = max((r.score for r in live), default=0.0)
        best_new = max((r.score for r in candidates), default=0.0)
        improvement = best_new - best_live
        return {
            "live_count": len(live),
            "best_live_score": round(best_live, 4),
            "best_new_score": round(best_new, 4),
            "improvement": round(improvement, 4),
            "better": bool(candidates) and (not live or improvement > 0),
            "best_live_id": max(live, key=lambda r: r.score).strategy_id if live else "",
            "best_new_id": max(candidates, key=lambda r: r.score).strategy_id
                           if candidates else "",
        }

    # ------------------------------------------------------ live performance
    def record_live(self, strategy_id: str, perf: dict) -> None:
        self.conn.execute("""
            INSERT INTO live_performance
            (strategy_id,ts,trades,wins,win_rate,net_pnl,expectancy,max_dd,
             degradation,healthy) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (strategy_id, _now(), int(perf.get("trades", 0)),
             int(perf.get("wins", 0)), float(perf.get("win_rate", 0.0)),
             float(perf.get("net_pnl", 0.0)), float(perf.get("expectancy", 0.0)),
             float(perf.get("max_dd", 0.0)), float(perf.get("degradation", 0.0)),
             int(bool(perf.get("healthy", True)))))
        self.conn.commit()

    def live_history(self, strategy_id: str, limit: int = 100) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM live_performance WHERE strategy_id=? "
            "ORDER BY ts DESC LIMIT ?", (strategy_id, limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------- cycles
    def start_cycle(self, trigger: str) -> int:
        row = self.conn.execute("SELECT COALESCE(MAX(cycle),0) FROM cycles").fetchone()
        cycle = int(row[0]) + 1
        self.conn.execute(
            "INSERT INTO cycles (cycle,started,trigger) VALUES (?,?,?)",
            (cycle, _now(), trigger))
        self.conn.commit()
        return cycle

    def finish_cycle(self, cycle: int, **kw: Any) -> None:
        self.conn.execute("""
            UPDATE cycles SET finished=?, ticks=?, rows=?, discovered=?,
            accepted=?, deployed=?, notes=? WHERE cycle=?""",
            (_now(), int(kw.get("ticks", 0)), int(kw.get("rows", 0)),
             int(kw.get("discovered", 0)), int(kw.get("accepted", 0)),
             int(kw.get("deployed", 0)), str(kw.get("notes", "")), cycle))
        self.conn.commit()

    def cycles(self, limit: int = 50) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM cycles ORDER BY cycle DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.conn.close()


def _to_record(row: sqlite3.Row) -> StrategyRecord:
    return StrategyRecord(**{k: row[k] for k in row.keys()})


def _report_summary(report) -> dict:
    return {
        "accepted": report.accepted,
        "reasons": report.reasons,
        "in_sample": report.in_sample,
        "out_sample": report.out_sample,
        "walk_forward": report.walk_forward,
        "monte_carlo": report.monte_carlo,
        "robustness": report.robustness,
        "overfitting": report.overfitting,
        "full": report.full,
    }
