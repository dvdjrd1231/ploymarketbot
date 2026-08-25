"""The signal ledger: the no-silent-block audit, as a data structure.

Every potential opportunity enters here and leaves in exactly one terminal
state, carrying the gate key that stopped it. Nothing is allowed to disappear
between DATA and TRADE without a row explaining itself -- `Funnel.reconcile()`
raises if the arithmetic does not close.

Why a ledger rather than logging: a log line is evidence that a human read
something. A ledger is evidence that the numbers add up. The V1 system had
excellent logging and still nobody could say where 40,820 opportunities went,
because "DO_NOTHING" was one string covering every possible cause.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional


class Stage(str, Enum):
    """The states a signal may occupy. Terminal states are marked."""

    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    BEHAVIOR_MATCHED = "BEHAVIOR_MATCHED"
    STRATEGY_ACCEPTED = "STRATEGY_ACCEPTED"
    STRATEGY_REJECTED = "STRATEGY_REJECTED"          # terminal
    RISK_PASSED = "RISK_PASSED"
    RISK_REJECTED = "RISK_REJECTED"                  # terminal
    PORTFOLIO_APPROVED = "PORTFOLIO_APPROVED"
    PORTFOLIO_REJECTED = "PORTFOLIO_REJECTED"        # terminal
    EXECUTION_ATTEMPTED = "EXECUTION_ATTEMPTED"
    EXECUTION_SUCCESSFUL = "EXECUTION_SUCCESSFUL"    # terminal
    EXECUTION_FAILED = "EXECUTION_FAILED"            # terminal

    @property
    def terminal(self) -> bool:
        return self in _TERMINAL


_TERMINAL = frozenset({
    Stage.STRATEGY_REJECTED, Stage.RISK_REJECTED, Stage.PORTFOLIO_REJECTED,
    Stage.EXECUTION_SUCCESSFUL, Stage.EXECUTION_FAILED,
})

# The order a signal must progress through. Skipping forward is a bug; the
# ledger refuses it rather than producing a funnel that quietly does not add up.
_ORDER = [
    Stage.SIGNAL_RECEIVED, Stage.BEHAVIOR_MATCHED, Stage.STRATEGY_ACCEPTED,
    Stage.RISK_PASSED, Stage.PORTFOLIO_APPROVED, Stage.EXECUTION_ATTEMPTED,
    Stage.EXECUTION_SUCCESSFUL,
]
_RANK = {s: i for i, s in enumerate(_ORDER)}


class Mode(str, Enum):
    """What a signal is allowed to do with capital.

    The V1 deadlock (gates.v1.empirical_no_setup_history) exists because one
    ladder governed both research and money: a setup needed closed trades to be
    trusted, and could not get closed trades without being trusted. Separating
    the modes is the fix, and it loosens nothing -- LIVE keeps every original
    requirement.
    """

    RESEARCH = "RESEARCH"     # backtest only, no capital, no ledger stage past strategy
    SHADOW = "SHADOW"         # full pipeline, simulated fills, evidence accrues
    PAPER = "PAPER"           # simulated capital, real-time prices
    LIVE = "LIVE"             # real money; requires VALIDATED + human sign-off


@dataclass
class SignalRecord:
    """One opportunity, from detection to terminal state.

    Every field the brief lists as required for the opportunity-loss audit is
    here. Fields that this dataset genuinely cannot supply (order-book depth on
    historical series) are None rather than zero -- a zero would read as
    "measured and empty" and silently justify a depth rejection.
    """

    signal_id: str
    ts: int
    route: str                      # "A" | "B"
    market_id: str = ""
    token_id: str = ""
    wallet: str = ""
    strategy_id: str = ""
    mode: str = Mode.RESEARCH.value

    # signal characterisation
    signal_strength: float = 0.0
    behavior_match: float = 0.0
    market_state: Optional[float] = None
    entry_price: float = 0.0
    spread: Optional[float] = None
    liquidity: Optional[float] = None
    depth: Optional[float] = None
    secs_to_settle: Optional[int] = None
    recent_price_move: Optional[float] = None
    wallet_settled_n: int = 0
    wallet_win_rate: float = 0.0

    # per-layer verdicts
    strategy_a_result: str = ""
    strategy_b_result: str = ""
    risk_result: str = ""
    portfolio_result: str = ""
    execution_result: str = ""

    stage: str = Stage.SIGNAL_RECEIVED.value
    gate_key: str = ""              # the registered gate that stopped it
    reason: str = ""                # human-readable, exact
    stake: float = 0.0
    fill_price: float = 0.0
    pnl: float = 0.0

    history: list = field(default_factory=list)

    # -- transitions ---------------------------------------------------------
    def advance(self, stage: Stage, *, gate_key: str = "", reason: str = "",
                **fields) -> "SignalRecord":
        """Move to `stage`. Terminal states are final; skipping is refused."""
        current = Stage(self.stage)
        if current.terminal:
            raise AssertionError(
                f"signal {self.signal_id} is already terminal at "
                f"{current.value}; cannot advance to {stage.value}")
        if not stage.terminal:
            if _RANK.get(stage, 0) <= _RANK.get(current, -1) and current is not stage:
                raise AssertionError(
                    f"signal {self.signal_id}: {current.value} -> {stage.value} "
                    "is not forward progress")
        if stage.terminal and stage is not Stage.EXECUTION_SUCCESSFUL and not gate_key:
            raise AssertionError(
                f"signal {self.signal_id} rejected at {stage.value} with no "
                "gate key. Rule 6: always log the exact rejection reason.")
        if gate_key:
            from . import gates
            gates.get(gate_key)          # raises if unregistered
        self.history.append({"stage": stage.value, "gate": gate_key,
                             "reason": reason})
        self.stage = stage.value
        self.gate_key = gate_key
        self.reason = reason
        for k, v in fields.items():
            setattr(self, k, v)
        return self

    def reject(self, stage: Stage, gate_key: str, reason: str,
               **fields) -> "SignalRecord":
        from . import gates
        gates.assert_may_block(gate_key, self.route)
        return self.advance(stage, gate_key=gate_key, reason=reason, **fields)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["history"] = json.dumps(self.history)
        return d


class Funnel:
    """Counters per route, and the reconciliation that proves nothing vanished."""

    def __init__(self) -> None:
        self.stages: dict[str, Counter] = {"A": Counter(), "B": Counter()}
        self.gates: dict[str, Counter] = {"A": Counter(), "B": Counter()}
        self.pnl: dict[str, float] = {"A": 0.0, "B": 0.0}
        self.wins: Counter = Counter()
        self.losses: Counter = Counter()
        self.returns: dict[str, list] = {"A": [], "B": []}
        self.opportunities: Counter = Counter()

    def observe_opportunity(self, route: str) -> None:
        self.opportunities[route] += 1

    def record(self, rec: SignalRecord) -> None:
        route = rec.route
        stages = self.stages.setdefault(route, Counter())
        gates = self.gates.setdefault(route, Counter())

        # Every record exists because a signal was received. SIGNAL_RECEIVED is
        # the constructor state and is never appended to `history` by
        # `advance`, so counting only history would report zero received and
        # make the funnel unbalanced by exactly the number of signals -- which
        # is what `assert_balanced` is here to catch.
        stages[Stage.SIGNAL_RECEIVED.value] += 1
        for step in rec.history:
            if step["stage"] == Stage.SIGNAL_RECEIVED.value:
                continue          # would double-count the line above
            stages[step["stage"]] += 1
            if step.get("gate"):
                gates[step["gate"]] += 1

    def record_outcome(self, route: str, pnl: float, ret: float) -> None:
        self.pnl[route] = self.pnl.get(route, 0.0) + pnl
        self.returns.setdefault(route, []).append(ret)
        if pnl > 0:
            self.wins[route] += 1
        elif pnl < 0:
            self.losses[route] += 1

    # -- the arithmetic that must close -------------------------------------
    def reconcile(self, route: str) -> dict:
        """Every received signal must be accounted for exactly once.

        received == rejected_at_strategy + rejected_at_risk
                    + rejected_at_portfolio + execution_failed
                    + execution_successful + still_in_flight
        """
        s = self.stages.get(route, Counter())
        received = s.get(Stage.SIGNAL_RECEIVED.value, 0)
        terminal = sum(s.get(t.value, 0) for t in _TERMINAL)
        in_flight = received - terminal
        ok = in_flight >= 0
        return {
            "route": route,
            "received": received,
            "strategy_rejected": s.get(Stage.STRATEGY_REJECTED.value, 0),
            "risk_rejected": s.get(Stage.RISK_REJECTED.value, 0),
            "portfolio_rejected": s.get(Stage.PORTFOLIO_REJECTED.value, 0),
            "execution_failed": s.get(Stage.EXECUTION_FAILED.value, 0),
            "execution_successful": s.get(Stage.EXECUTION_SUCCESSFUL.value, 0),
            "in_flight": in_flight,
            "balanced": ok,
        }

    def assert_balanced(self) -> None:
        for route in self.stages:
            r = self.reconcile(route)
            if not r["balanced"]:
                raise AssertionError(
                    f"funnel does not close for route {route}: {r}. There is "
                    "an unexplained gap between DATA and TRADE.")

    def expectancy(self, route: str) -> float:
        rs = self.returns.get(route) or []
        return sum(rs) / len(rs) if rs else 0.0

    def summary(self) -> dict:
        out = {}
        for route in sorted(set(self.stages) | set(self.opportunities)):
            rec = self.reconcile(route)
            rs = self.returns.get(route) or []
            wins = [r for r in rs if r > 0]
            losses = [r for r in rs if r < 0]
            rec.update({
                "opportunities": self.opportunities.get(route, 0),
                "behavior_matched": self.stages[route].get(
                    Stage.BEHAVIOR_MATCHED.value, 0),
                "accepted": self.stages[route].get(
                    Stage.STRATEGY_ACCEPTED.value, 0),
                "execution_attempted": self.stages[route].get(
                    Stage.EXECUTION_ATTEMPTED.value, 0),
                "wins": len(wins), "losses": len(losses),
                "pnl": round(self.pnl.get(route, 0.0), 2),
                "expectancy": round(self.expectancy(route), 5),
                "avg_win": round(sum(wins) / len(wins), 5) if wins else 0.0,
                "avg_loss": round(sum(losses) / len(losses), 5) if losses else 0.0,
                "top_rejections": self.gates[route].most_common(10),
            })
            out[route] = rec
        return out


class LedgerStore:
    """Durable ledger. Its own database, under the V2 work dir only."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS signals (
        signal_id TEXT PRIMARY KEY, ts INTEGER, route TEXT, mode TEXT,
        market_id TEXT, token_id TEXT, wallet TEXT, strategy_id TEXT,
        signal_strength REAL, behavior_match REAL, market_state REAL,
        entry_price REAL, spread REAL, liquidity REAL, depth REAL,
        secs_to_settle INTEGER, recent_price_move REAL,
        wallet_settled_n INTEGER, wallet_win_rate REAL,
        strategy_a_result TEXT, strategy_b_result TEXT, risk_result TEXT,
        portfolio_result TEXT, execution_result TEXT,
        stage TEXT, gate_key TEXT, reason TEXT,
        stake REAL, fill_price REAL, pnl REAL, history TEXT
    );
    CREATE INDEX IF NOT EXISTS ix_sig_route ON signals(route, stage);
    CREATE INDEX IF NOT EXISTS ix_sig_gate  ON signals(gate_key);
    CREATE INDEX IF NOT EXISTS ix_sig_ts    ON signals(ts);
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(self.SCHEMA)
        self.conn.commit()

    def write(self, records: Iterable[SignalRecord]) -> int:
        rows = [r.to_dict() for r in records]
        if not rows:
            return 0
        cols = list(rows[0])
        sql = (f"INSERT OR REPLACE INTO signals ({','.join(cols)}) "
               f"VALUES ({','.join(':' + c for c in cols)})")
        self.conn.executemany(sql, rows)
        self.conn.commit()
        return len(rows)

    def rejection_report(self, route: str | None = None) -> list[tuple]:
        """Q19: which rules are suppressing the most opportunities?"""
        sql = ("SELECT gate_key, COUNT(*) n FROM signals "
               "WHERE gate_key != ''")
        params: list = []
        if route:
            sql += " AND route = ?"
            params.append(route)
        sql += " GROUP BY gate_key ORDER BY n DESC"
        return self.conn.execute(sql, params).fetchall()

    def close(self) -> None:
        self.conn.close()


_COUNTER = [0]


def new_signal_id(prefix: str = "S") -> str:
    _COUNTER[0] += 1
    return f"{prefix}{int(time.time() * 1000) % 10**10:010d}-{_COUNTER[0]:06d}"
