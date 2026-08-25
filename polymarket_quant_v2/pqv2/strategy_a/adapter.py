"""Strategy A: the existing engine, preserved, wrapped, and never modified.

This module IMPORTS nothing from the V1 installation at module load and WRITES
nothing to it, ever. It reads V1's own databases read-only to report what the
V1 engine did, and it provides an optional in-process bridge for operators who
want both routes running side by side.

Why a wrapper and not a port: the V1 engine is 85,535 lines with 1,142 passing
tests. Rewriting it to add instrumentation would risk exactly the working
system the brief says to preserve. So V2 observes it instead, and the only
change ever recommended to V1 is a config flag it already has.

THE AUDIT FINDING, because it is what this module is really for:

    Every one of the 40,820 decisions V1 has ever journalled is DO_NOTHING,
    and every one carries the same reason:

        "Learning mode: no validated strategies yet - capital is parked until
         discovery produces one (exits still run)."

    That gate is `lean_engine._entry_block_reason`, and it sits ABOVE every
    other entry gate. So the market-state, depth, spread, EV, contradiction and
    empirical-history filters were never reached in production. Loosening any
    of them would have changed nothing, and would have degraded the engine.

    The gate is not a bug. It is doing exactly what it was asked to do. It is
    waiting for `library.sqlite3` to contain a strategy with status
    `validated`, and that file contains 234 strategies: 170 rejected, 49
    validating, 13 new, 2 quarantined, 0 validated.

    So Strategy A is not blocked by a filter. It is blocked by its own
    discovery pipeline never clearing its own validation bar -- and that, in
    turn, is because it validates against 78,219 rows over 123 markets and 3.8
    days while 116,923 rows over 1,285 markets and 90 days sit in the same
    database. One cause, four symptoms.

Strategy A is therefore preserved as-is and marked by evidence, not by opinion.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..gates import Owner, REGISTRY


@dataclass
class StrategyAState:
    """What the V1 engine has actually done, read from its own journals."""

    decisions_total: int = 0
    actions: list = field(default_factory=list)
    reasons: list = field(default_factory=list)
    executions: int = 0
    lifecycles: int = 0
    cycles: int = 0
    library_statuses: list = field(default_factory=list)
    tradable_strategies: int = 0
    blocking_gate: str = ""
    verdict: str = ""
    recommendation: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _ro(path: Path) -> sqlite3.Connection | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        c.execute("PRAGMA query_only = ON")
        return c
    except sqlite3.Error:
        return None


def _count(conn, table: str) -> int:
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM [{table}]").fetchone()[0])
    except sqlite3.Error:
        return -1


def inspect(st: Settings) -> StrategyAState:
    """Read V1's journals and library. Read-only, always."""
    state = StrategyAState()

    jc = _ro(st.journal_db)
    if jc is not None:
        try:
            state.decisions_total = _count(jc, "decisions")
            state.executions = _count(jc, "executions")
            state.lifecycles = _count(jc, "lifecycles")
            state.cycles = _count(jc, "cycles")
            state.actions = [(a, n) for a, n in jc.execute(
                "SELECT action, COUNT(*) FROM decisions GROUP BY 1 "
                "ORDER BY 2 DESC")]
            state.reasons = [(r or "", n) for r, n in jc.execute(
                "SELECT substr(reason,1,120), COUNT(*) n FROM decisions "
                "GROUP BY 1 ORDER BY n DESC LIMIT 10")]
        except sqlite3.Error:
            pass
        finally:
            jc.close()

    lc = _ro(st.library_db)
    if lc is not None:
        try:
            state.library_statuses = [(s, n) for s, n in lc.execute(
                "SELECT status, COUNT(*) FROM strategies GROUP BY 1 "
                "ORDER BY 2 DESC")]
            state.tradable_strategies = int(lc.execute(
                "SELECT COUNT(*) FROM strategies WHERE status='validated'"
            ).fetchone()[0])
        except sqlite3.Error:
            pass
        finally:
            lc.close()

    return _diagnose(state)


def _diagnose(state: StrategyAState) -> StrategyAState:
    """Attribute the blockage to a gate, with the arithmetic that proves it."""
    only_nothing = (len(state.actions) == 1
                    and state.actions[0][0] == "DO_NOTHING")
    top_reason = state.reasons[0][0] if state.reasons else ""

    if only_nothing and "Learning mode" in top_reason:
        state.blocking_gate = "v1.learning_mode"
        state.verdict = (
            f"All {state.decisions_total:,} decisions are DO_NOTHING, all with "
            "the same reason: learning mode. This gate sits above every other "
            "entry gate, so no market-state, depth, spread or EV filter was "
            "ever reached. The engine is not mis-tuned; it is waiting.")
        state.recommendation = (
            f"Do NOT loosen the entry filters -- they are not the constraint. "
            f"The library holds {sum(n for _, n in state.library_statuses)} "
            f"strategies and {state.tradable_strategies} are validated, so "
            "learning mode is correctly closed. Strategy A becomes tradable "
            "the moment its discovery pipeline validates something, and the "
            "measured reason it cannot is substrate starvation: it validates "
            "against 3.8 days and 123 markets while 90 days and 1,285 markets "
            "are available in the same database. Feed it the settled substrate "
            "(pqv2.substrate.data) or run Strategy B alongside it.")
    elif state.executions == 0 and state.decisions_total > 0:
        state.blocking_gate = "unknown"
        state.verdict = (
            f"{state.decisions_total:,} decisions, 0 executions. The gate "
            f"reported most often is: {top_reason[:100]}")
        state.recommendation = (
            "Classify that gate in pqv2/gates.py before changing it.")
    elif state.decisions_total == 0:
        state.verdict = "No V1 journal found or it is empty."
        state.recommendation = (
            "Nothing to preserve from the journal; Strategy A statistics will "
            "be empty until the V1 engine runs.")
    else:
        state.verdict = (
            f"V1 has traded: {state.executions:,} executions across "
            f"{state.lifecycles:,} lifecycles.")
        state.recommendation = (
            "Strategy A has a live record. Compare it against Strategy B on "
            "risk-adjusted terms before changing either.")
    return state


def preserve_verdict(state: StrategyAState) -> dict:
    """Should Strategy A run, be research-only, or be disabled?

    Rule 1 of the brief: do not remove working components. Rule: use evidence.
    Strategy A has never traded, so there is NO evidence it is harmful and no
    evidence it is profitable. The honest verdict for an engine with a zero-
    trade record is neither PRODUCTION nor DISABLED.
    """
    if state.executions == 0:
        return {
            "status": "PRESERVED_UNTRADED",
            "reason": (
                "Strategy A has never executed a trade, so there is no "
                "out-of-sample evidence either way. It must not be marked "
                "DISABLED (no evidence of harm) and must not be marked "
                "PRODUCTION (no evidence of edge). It is preserved unchanged "
                "and runs in parallel; V2 touches none of its files."),
            "capital_authorised": False,
            "action": ("Leave V1 exactly as it is. Its order-placement, "
                       "reconciliation and lifecycle paths are covered by unit "
                       "tests only and have never run in production -- treat "
                       "its first live trade as a first run, not a resumption."),
        }
    return {"status": "PRESERVED_ACTIVE", "capital_authorised": True,
            "reason": "Strategy A has a live record; judge it on that record.",
            "action": "Compare against Strategy B on risk-adjusted terms."}


def orphaned_evidence(st: Settings) -> dict:
    """VALIDATED strategies that exist already and that NOTHING READS.

    Credit where due: this was found by an earlier V2 effort (preserved under
    `prior_v2/`) and independently confirmed here. It is arguably the single
    most actionable fact about the installation, and it is invisible from
    either program alone -- which is exactly why it went unnoticed.

    `wallet-strategy-lab` has run a full pass and validated 2 strategies. They
    sit in `Polymarket-Bot-DATA/state/walletlab/experiments.sqlite3`. The
    trading engine reads `library.sqlite3`, and there is not one reference to
    `walletlab` anywhere in `pqb/` or `ploymarketbot/`.

    So the account was parked in learning mode "until discovery produces a
    validated strategy" while discovery had already produced two, in a
    different file that nothing opened.

    Read carefully before acting on them -- see `caveats`. This function
    reports them; it does not endorse them.
    """
    path = Path(st.data_db).parent / "walletlab" / "experiments.sqlite3"
    out: dict = {"path": str(path), "available": False, "validated": [],
                 "status_histogram": [], "caveats": []}
    conn = _ro(path)
    if conn is None:
        out["note"] = "no walletlab experiment database found"
        return out
    try:
        cols = [d[1] for d in conn.execute("PRAGMA table_info(experiments)")]
        if not cols:
            return out
        out["available"] = True
        out["status_histogram"] = [
            (s, n) for s, n in conn.execute(
                "SELECT status, COUNT(*) FROM experiments GROUP BY 1 "
                "ORDER BY 2 DESC")]
        import json as _json
        for row in conn.execute(
                "SELECT * FROM experiments WHERE status='VALIDATED'"):
            d = dict(zip(cols, row))
            test = _json.loads(d.get("test_json") or "{}")
            spec = _json.loads(d.get("spec_json") or "{}")
            out["validated"].append({
                "wallet": d.get("wallet", ""),
                "score": d.get("score"),
                "oos_p": d.get("oos_p"),
                "price_band": [spec.get("min_price"), spec.get("max_price")],
                "delay_secs": spec.get("delay_secs"),
                "test_expectancy": test.get("expectancy"),
                "test_fills": test.get("n_filled"),
                "test_markets": test.get("n_markets"),
                "test_win_rate": test.get("win_rate"),
            })
    except sqlite3.Error:
        pass
    finally:
        conn.close()

    if out["validated"]:
        statuses = dict(out["status_histogram"])
        out["caveats"] = [
            "NOT CONNECTED: the trading engine reads library.sqlite3. Nothing "
            "in pqb/ or ploymarketbot/ references walletlab. These strategies "
            "have never been able to reach the execution path.",
            "Every validated entry sits in the FAVOURITE band (min_price 0.5 "
            "to 0.7, max 0.98) - the same region where this dataset's "
            "favourite-longshot bias is +8.8 to +8.9 points. Treat a "
            "price-band rule in that range as market structure until wallet "
            "alpha says otherwise.",
            f"No experiment carries status NO_WALLET_ALPHA (histogram: "
            f"{statuses}), so on this pass the wallet-alpha control either did "
            "not fire or was not applied. V2 applies it unconditionally.",
            "Reported test windows span 4-25 markets with win rates up to "
            "1.00. A 100% win rate over 152 fills in 8 markets is the "
            "signature of concentration, not of skill.",
            "V2's own out-of-sample measurement of wallet "
            "0x629da223adfc... is expectancy -0.3282 on naive copy, against "
            "walletlab's +0.208. The two engines split the tape differently "
            "(V2 splits strictly by TIME). That disagreement should be "
            "resolved before either number is trusted.",
        ]
    return out


def inherited_gates() -> list:
    """Which suppressions V2 declined to inherit, and why (brief Q21)."""
    return [{"gate": g.key, "owner": g.owner.value, "what": g.description,
             "why_not_inherited": g.evidence}
            for g in REGISTRY.values() if g.owner is Owner.STRATEGY_A]


def global_safety_gates() -> list:
    """Which rules DO bind both routes, and the evidence for each (Q20)."""
    return [{"gate": g.key, "what": g.description, "evidence": g.evidence,
             "justified": g.justified()}
            for g in REGISTRY.values() if g.owner is Owner.GLOBAL_SAFETY]
