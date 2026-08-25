"""THE OPPORTUNITY LEDGER — no signal disappears without a named reason.

The master prompt's requirement: *"There must never be an unexplained gap
between DATA and TRADE."* This module is the mechanism that makes that
checkable rather than aspirational.

Every candidate opportunity gets one :class:`Opportunity` record that travels
the whole pipeline and accumulates the exact verdict of every layer that
looked at it. A signal cannot leave the pipeline without terminating in a
state, and it cannot be rejected without naming the gate that rejected it — so
"where did the trades go" becomes a `GROUP BY` rather than an investigation.

**Why this is the first thing built and not the last.** The measured build had
40,820 consecutive DO_NOTHING decisions and no lifecycle rows at all. The
journal recorded the reason, but only as free text on a decision row, so the
question "which layer is costing us the most opportunities" needed a script
and a guess. With this ledger it is one number, per gate, per route, per day.

**What it deliberately does not do.** It does not veto anything, it does not
adjust a threshold, and it does not know what a good trade is. It observes.
A rejection recorded here is still a rejection; the point is that it is now a
*counted* one, attributed to an *owner* (see :mod:`pqv2.gatemap`), so that the
decision to relax a rule is taken against evidence about that specific rule
rather than by lowering everything and hoping.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable, Optional

from .gatemap import (GATES_BY_KEY, GLOBAL_SAFETY, PORTFOLIO, ROUTE_A, ROUTE_B,
                      STRATEGY_A, STRATEGY_B, classify)

# -- the states a signal can be in (the prompt's list, verbatim) -------------

SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
BEHAVIOR_MATCHED = "BEHAVIOR_MATCHED"
STRATEGY_ACCEPTED = "STRATEGY_ACCEPTED"
STRATEGY_REJECTED = "STRATEGY_REJECTED"
RISK_PASSED = "RISK_PASSED"
RISK_REJECTED = "RISK_REJECTED"
PORTFOLIO_APPROVED = "PORTFOLIO_APPROVED"
PORTFOLIO_REJECTED = "PORTFOLIO_REJECTED"
EXECUTION_ATTEMPTED = "EXECUTION_ATTEMPTED"
EXECUTION_SUCCESSFUL = "EXECUTION_SUCCESSFUL"
EXECUTION_FAILED = "EXECUTION_FAILED"

STATES = (SIGNAL_RECEIVED, BEHAVIOR_MATCHED, STRATEGY_ACCEPTED,
          STRATEGY_REJECTED, RISK_PASSED, RISK_REJECTED, PORTFOLIO_APPROVED,
          PORTFOLIO_REJECTED, EXECUTION_ATTEMPTED, EXECUTION_SUCCESSFUL,
          EXECUTION_FAILED)

TERMINAL = (STRATEGY_REJECTED, RISK_REJECTED, PORTFOLIO_REJECTED,
            EXECUTION_SUCCESSFUL, EXECUTION_FAILED)

# The order the funnel is reported in. A signal should only ever move down
# this list; `Opportunity.advance` refuses to move it back up, because a
# pipeline that can revisit an earlier stage cannot be summed into a funnel.
_ORDER = {state: i for i, state in enumerate((
    SIGNAL_RECEIVED, BEHAVIOR_MATCHED, STRATEGY_ACCEPTED, RISK_PASSED,
    PORTFOLIO_APPROVED, EXECUTION_ATTEMPTED, EXECUTION_SUCCESSFUL))}


@dataclass
class Opportunity:
    """One candidate trade, with everything that happened to it.

    The field list is the master prompt's, in its order. Fields the build
    cannot populate stay ``None`` — never 0.0 — because a spread of zero and an
    unmeasured spread lead to opposite conclusions and must not collapse into
    the same value on the way into the ledger.
    """

    ts: float = 0.0
    route: str = ROUTE_B
    market: str = ""
    token: str = ""
    wallet: str = ""
    strategy_id: str = ""

    signal_strength: Optional[float] = None
    behavior_match: Optional[float] = None
    market_state: Optional[float] = None
    entry_price: Optional[float] = None
    spread: Optional[float] = None
    liquidity: Optional[float] = None
    depth: Optional[float] = None
    seconds_to_resolution: Optional[float] = None
    recent_move: Optional[float] = None
    wallet_history_n: Optional[int] = None

    # Per-layer verdicts. "" = that layer never looked at this signal, which
    # is different from "looked and approved".
    strategy_a_result: str = ""
    strategy_b_result: str = ""
    risk_result: str = ""
    portfolio_result: str = ""
    execution_result: str = ""

    state: str = SIGNAL_RECEIVED
    rejected_by: str = ""          # gate key from gatemap
    rejected_owner: str = ""       # STRATEGY_A | GLOBAL_SAFETY | ...
    reason: str = ""               # the exact sentence
    stake: Optional[float] = None
    trail: list = field(default_factory=list)

    def advance(self, state: str, note: str = "") -> "Opportunity":
        """Move to a later stage. Backwards moves are a programming error."""
        if state in _ORDER and self.state in _ORDER \
                and _ORDER[state] < _ORDER[self.state]:
            raise ValueError(
                f"funnel cannot move backwards: {self.state} -> {state}")
        self.state = state
        self.trail.append({"ts": time.time(), "state": state, "note": note})
        return self

    def reject(self, state: str, gate_key: str, reason: str) -> "Opportunity":
        """Terminate the signal, naming the gate. Never call without one.

        `gate_key` is required and is looked up in the map, so a rejection
        emitted by a layer nobody has classified lands in the audit as
        ``unclassified`` and stays visible until somebody classifies it.
        """
        gate = GATES_BY_KEY.get(gate_key)
        self.state = state
        self.rejected_by = gate_key or "unclassified"
        self.rejected_owner = gate.owner if gate else "UNCLASSIFIED"
        self.reason = reason
        self.trail.append({"ts": time.time(), "state": state,
                           "gate": self.rejected_by, "note": reason})
        return self

    @property
    def reached_execution(self) -> bool:
        return self.state in (EXECUTION_ATTEMPTED, EXECUTION_SUCCESSFUL,
                              EXECUTION_FAILED)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["trail"] = list(self.trail)
        return out


# ---------------------------------------------------------------------------
# the ledger
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    route          TEXT NOT NULL,
    market         TEXT,
    token          TEXT,
    wallet         TEXT,
    strategy_id    TEXT,
    signal_strength REAL,
    behavior_match REAL,
    market_state   REAL,
    entry_price    REAL,
    spread         REAL,
    liquidity      REAL,
    depth          REAL,
    seconds_to_resolution REAL,
    recent_move    REAL,
    wallet_history_n INTEGER,
    strategy_a_result TEXT,
    strategy_b_result TEXT,
    risk_result    TEXT,
    portfolio_result TEXT,
    execution_result TEXT,
    state          TEXT NOT NULL,
    rejected_by    TEXT,
    rejected_owner TEXT,
    reason         TEXT,
    stake          REAL,
    trail          TEXT
);
CREATE INDEX IF NOT EXISTS idx_opp_route_ts ON opportunities(route, ts);
CREATE INDEX IF NOT EXISTS idx_opp_gate ON opportunities(rejected_by);
"""


class OpportunityLedger:
    """Persisted, append-only, and separate from the bot's own journal.

    Separate on purpose. V2 must not write into the original installation's
    database (non-negotiable rule 1), and an observability store that shares a
    file with the trading journal is one bad migration away from taking the
    trading loop down with it.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def record(self, opp: Opportunity) -> int:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO opportunities(
                     ts, route, market, token, wallet, strategy_id,
                     signal_strength, behavior_match, market_state,
                     entry_price, spread, liquidity, depth,
                     seconds_to_resolution, recent_move, wallet_history_n,
                     strategy_a_result, strategy_b_result, risk_result,
                     portfolio_result, execution_result, state, rejected_by,
                     rejected_owner, reason, stake, trail)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (opp.ts or time.time(), opp.route, opp.market, opp.token,
                 opp.wallet, opp.strategy_id, opp.signal_strength,
                 opp.behavior_match, opp.market_state, opp.entry_price,
                 opp.spread, opp.liquidity, opp.depth,
                 opp.seconds_to_resolution, opp.recent_move,
                 opp.wallet_history_n, opp.strategy_a_result,
                 opp.strategy_b_result, opp.risk_result, opp.portfolio_result,
                 opp.execution_result, opp.state, opp.rejected_by,
                 opp.rejected_owner, opp.reason, opp.stake,
                 json.dumps(opp.trail)))
            self._conn.commit()
            return cur.lastrowid

    def rows(self, route: Optional[str] = None,
             since: float = 0.0) -> list[dict]:
        sql = "SELECT * FROM opportunities WHERE ts >= ?"
        params: list = [since]
        if route:
            sql += " AND route = ?"
            params.append(route)
        with self._lock:
            return [dict(r) for r in
                    self._conn.execute(sql + " ORDER BY ts", tuple(params))]


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def funnel(rows: Iterable[dict], route: Optional[str] = None) -> dict:
    """The opportunity -> execution funnel, per the prompt's list.

    Counts are CUMULATIVE reach-counts, not terminal-state counts: a signal
    that executed also reached "strategy accepted". Reporting terminal states
    instead would show 1 accepted and 1 executed and hide the 300 that got to
    the same stage and died later, which is the shape of the question being
    asked.
    """
    rows = [r for r in rows if route is None or r.get("route") == route]
    if not rows:
        return {"route": route or "ALL", "signals": 0, "stages": [],
                "note": "no opportunities recorded yet"}

    def reached(state: str) -> int:
        floor = _ORDER.get(state)
        if floor is None:
            return sum(1 for r in rows if r.get("state") == state)
        count = 0
        for r in rows:
            here = _ORDER.get(r.get("state"))
            if here is not None and here >= floor:
                count += 1
            elif r.get("state") in TERMINAL:
                # A terminal rejection still reached every stage before the
                # one that rejected it.
                owner = r.get("rejected_owner")
                if state == SIGNAL_RECEIVED:
                    count += 1
                elif state == BEHAVIOR_MATCHED and r.get("behavior_match"):
                    count += 1
                elif state == STRATEGY_ACCEPTED and owner in (
                        GLOBAL_SAFETY, PORTFOLIO, "EXECUTION"):
                    count += 1
                elif state == RISK_PASSED and owner in (PORTFOLIO, "EXECUTION"):
                    count += 1
                elif state == PORTFOLIO_APPROVED and owner == "EXECUTION":
                    count += 1
        return count

    stages = [{"state": s, "count": reached(s)} for s in (
        SIGNAL_RECEIVED, BEHAVIOR_MATCHED, STRATEGY_ACCEPTED, RISK_PASSED,
        PORTFOLIO_APPROVED, EXECUTION_ATTEMPTED, EXECUTION_SUCCESSFUL)]

    # Where they died, by gate and by owner.
    by_gate: dict = {}
    by_owner: dict = {}
    for r in rows:
        if r.get("state") not in TERMINAL or r.get("state") == \
                EXECUTION_SUCCESSFUL:
            continue
        gate = r.get("rejected_by") or "unclassified"
        owner = r.get("rejected_owner") or "UNCLASSIFIED"
        by_gate[gate] = by_gate.get(gate, 0) + 1
        by_owner[owner] = by_owner.get(owner, 0) + 1

    executed = sum(1 for r in rows if r.get("state") == EXECUTION_SUCCESSFUL)
    return {
        "route": route or "ALL",
        "signals": len(rows),
        "executed": executed,
        "conversion": round(executed / len(rows), 4) if rows else 0.0,
        "stages": stages,
        "rejectedByGate": dict(sorted(by_gate.items(),
                                      key=lambda kv: -kv[1])),
        "rejectedByOwner": dict(sorted(by_owner.items(),
                                       key=lambda kv: -kv[1])),
        "unclassified": by_gate.get("unclassified", 0),
    }


def suppression_ranking(rows: Iterable[dict], route: str = ROUTE_B) -> list:
    """Which rules cost this route the most opportunities (prompt Q19-Q21).

    Sorted by volume, annotated with whether the rule is even entitled to
    block this route. A Strategy A gate appearing here with a large count on
    route B is not a tuning observation — it is a wiring bug, and it is
    labelled as one.
    """
    counts: dict = {}
    for r in rows:
        if r.get("route") != route:
            continue
        if r.get("state") not in TERMINAL or \
                r.get("state") == EXECUTION_SUCCESSFUL:
            continue
        key = r.get("rejected_by") or "unclassified"
        counts[key] = counts.get(key, 0) + 1

    out = []
    for key, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        gate = GATES_BY_KEY.get(key)
        entitled = gate.blocks(route) if gate else True
        out.append({
            "gate": key,
            "owner": gate.owner if gate else "UNCLASSIFIED",
            "count": n,
            "entitledToBlockThisRoute": entitled,
            "verdict": ("justified — this layer owns the route"
                        if entitled else
                        "INHERITED FROM THE OTHER ROUTE — this gate has no "
                        "authority here and is suppressing valid signals"),
            "summary": gate.summary if gate else
                       "no classification; add it to gatemap.GATES",
        })
    return out


def counters(rows: Iterable[dict]) -> dict:
    """Separate live counters per route, as the prompt requires."""
    out: dict = {}
    for route in (ROUTE_A, ROUTE_B):
        sub = [r for r in rows if r.get("route") == route]
        out[route] = {
            "signals": len(sub),
            "behaviorMatches": sum(1 for r in sub if r.get("behavior_match")),
            "accepted": sum(1 for r in sub if r.get("strategy_b_result") ==
                            "accepted" or r.get("strategy_a_result") ==
                            "accepted"),
            "strategyRejected": sum(1 for r in sub
                                    if r.get("state") == STRATEGY_REJECTED),
            "riskRejected": sum(1 for r in sub
                                if r.get("state") == RISK_REJECTED),
            "portfolioRejected": sum(1 for r in sub
                                     if r.get("state") == PORTFOLIO_REJECTED),
            "executionAttempted": sum(1 for r in sub if r.get("state") in (
                EXECUTION_ATTEMPTED, EXECUTION_SUCCESSFUL, EXECUTION_FAILED)),
            "executionSuccessful": sum(
                1 for r in sub if r.get("state") == EXECUTION_SUCCESSFUL),
            "executionFailed": sum(1 for r in sub
                                   if r.get("state") == EXECUTION_FAILED),
        }
    return out
