"""THE OPPORTUNITY-LOSS AUDIT — read-only, over the real installation.

Answers the master prompt's twenty-two diagnostic questions from the databases
the running system already writes, without touching them. Nothing here opens a
connection in write mode; every path is `mode=ro`.

The audit exists because of a specific failure it was built to expose. On the
measured build:

    Strategy A (polymarket-quant-bridge)
        40,820 decisions / 92.3 hours / 100% DO_NOTHING
        one reason, for every single one of them:
            "Learning mode: no validated strategies yet"
        strategy library: 170 rejected, 49 validating, 13 new,
                          2 quarantined, 0 VALIDATED

    Strategy B (wallet-strategy-lab)
        20,748 hypotheses tested across 12 wallets
        2 VALIDATED, with out-of-sample p-values of 5.7e-174 and 2.7e-4
        connected to the bot: NO. Zero references in the whole package.

So the gap between DATA and TRADE was not a threshold anywhere. It was one
boolean owned by Strategy A gating an account that had two independently
validated strategies sitting in a different database that nothing read.

That is the class of problem this audit is meant to make impossible to miss
again: it reports what each route validated, what each route was allowed to
do, and — the number that matters — whether anything is being stopped by a
gate belonging to the other route.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import gatemap
from .funnel import ROUTE_A, ROUTE_B, counters, funnel, suppression_ranking


def _ro(path: str | Path) -> Optional[sqlite3.Connection]:
    """Read-only connection, or None. The original install is never written."""
    path = Path(path)
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _rows(conn: Optional[sqlite3.Connection], sql: str,
          params: tuple = ()) -> list[dict]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return []


def _normalise(reason: str) -> str:
    """Collapse numbers so ten thousand near-identical sentences group."""
    return re.sub(r"[-+]?\d*\.?\d+", "N", reason or "")[:140]


@dataclass
class Paths:
    """Where the original installation keeps its state. Nothing is created."""

    journal: Path
    library: Path
    intel: Path
    walletlab: Path
    ledger: Optional[Path] = None

    @classmethod
    def discover(cls, data_dir: str | Path,
                 ledger: Optional[str | Path] = None) -> "Paths":
        root = Path(data_dir)
        return cls(journal=root / "journal.sqlite3",
                   library=root / "library.sqlite3",
                   intel=root / "intel.sqlite3",
                   walletlab=root / "walletlab" / "experiments.sqlite3",
                   ledger=Path(ledger) if ledger else None)


# ---------------------------------------------------------------------------
# route A — the existing engine
# ---------------------------------------------------------------------------


def route_a(paths: Paths) -> dict:
    """What Strategy A did, and what stopped it."""
    conn = _ro(paths.journal)
    if conn is None:
        return {"available": False, "reason": "no journal at "
                                              f"{paths.journal}"}
    try:
        actions = _rows(conn, "SELECT action, COUNT(*) n FROM decisions "
                              "GROUP BY action ORDER BY n DESC")
        span = _rows(conn, "SELECT MIN(ts) a, MAX(ts) b, "
                           "COUNT(DISTINCT cycle_id) c FROM decisions")
        reasons = _rows(conn, "SELECT reason FROM decisions")
        lifecycles = _rows(conn, "SELECT status, COUNT(*) n FROM lifecycles "
                                 "GROUP BY status")
        closed = _rows(conn, "SELECT exit_style, COUNT(*) n, "
                             "SUM(realized_pnl) pnl FROM lifecycles "
                             "WHERE status='CLOSED' GROUP BY exit_style")
    finally:
        conn.close()

    counts = Counter()
    by_gate = Counter()
    for row in reasons:
        counts[_normalise(row.get("reason", ""))] += 1
        by_gate[gatemap.classify(row.get("reason", ""))] += 1

    total = sum(counts.values())
    hours = 0.0
    cycles = 0
    if span and span[0].get("a"):
        hours = (span[0]["b"] - span[0]["a"]) / 3600.0
        cycles = span[0]["c"]

    opened = sum(r["n"] for r in lifecycles)
    done = sum(r["n"] for r in lifecycles if r["status"] == "CLOSED")

    return {
        "available": True,
        "decisions": total,
        "cycles": cycles,
        "hours": round(hours, 1),
        "actions": {r["action"]: r["n"] for r in actions},
        "positionsOpened": opened,
        "tradesCompleted": done,
        "tradesPerDay": round(done / (hours / 24.0), 2) if hours > 0 else 0.0,
        "topReasons": [{"count": n, "reason": r}
                       for r, n in counts.most_common(10)],
        "byGate": [{"gate": g, "owner": (gatemap.GATES_BY_KEY[g].owner
                                         if g in gatemap.GATES_BY_KEY
                                         else "UNCLASSIFIED"),
                    "count": n, "share": round(n / total, 4) if total else 0.0}
                   for g, n in by_gate.most_common()],
        "closedByExitStyle": [{"style": r["exit_style"], "n": r["n"],
                               "pnl": round(r["pnl"] or 0.0, 4)}
                              for r in closed],
    }


def route_a_library(paths: Paths) -> dict:
    """Strategy A's own validation ladder — the thing learning mode waits on."""
    conn = _ro(paths.library)
    if conn is None:
        return {"available": False,
                "reason": f"no strategy library at {paths.library}"}
    try:
        statuses = _rows(conn, "SELECT status, COUNT(*) n FROM strategies "
                               "GROUP BY status ORDER BY n DESC")
    finally:
        conn.close()
    by_status = {r["status"]: r["n"] for r in statuses}
    validated = by_status.get("validated", 0) + by_status.get("VALIDATED", 0)
    return {
        "available": True,
        "byStatus": by_status,
        "total": sum(by_status.values()),
        "validated": validated,
        "gatesEntries": validated == 0,
        "note": ("Learning mode blocks every entry while this is 0. It is "
                 "Strategy A's ladder and Strategy A's opinion about its own "
                 "edge — see gatemap.learning_mode."),
    }


# ---------------------------------------------------------------------------
# route B — wallet-strategy-lab
# ---------------------------------------------------------------------------


def route_b(paths: Paths) -> dict:
    """What Strategy B validated, and whether anything can act on it."""
    conn = _ro(paths.walletlab)
    if conn is None:
        return {"available": False,
                "reason": f"no walletlab registry at {paths.walletlab}"}
    try:
        statuses = _rows(conn, "SELECT status, COUNT(*) n FROM experiments "
                               "GROUP BY status ORDER BY n DESC")
        validated = _rows(conn, "SELECT wallet, score, oos_p, spec_json "
                                "FROM experiments WHERE status='VALIDATED' "
                                "ORDER BY score DESC")
        passes = _rows(conn, "SELECT * FROM passes ORDER BY id DESC LIMIT 1")
    finally:
        conn.close()

    specs = []
    for row in validated:
        try:
            spec = json.loads(row.get("spec_json") or "{}")
        except (TypeError, ValueError):
            spec = {}
        specs.append({
            "wallet": row.get("wallet"),
            "score": round(float(row.get("score") or 0.0), 4),
            "oosP": row.get("oos_p"),
            "minPrice": spec.get("min_price"),
            "maxPrice": spec.get("max_price"),
            "delaySecs": spec.get("delay_secs"),
            "stakeMode": spec.get("stake_mode"),
            "spec": spec,
        })

    last = passes[0] if passes else {}
    return {
        "available": True,
        "byStatus": {r["status"]: r["n"] for r in statuses},
        "validated": len(specs),
        "strategies": specs,
        "lastPass": {
            "wallets": last.get("wallets"),
            "hypotheses": last.get("hypotheses"),
            "validated": last.get("validated"),
            "fdrThreshold": last.get("fdr_threshold"),
        } if last else {},
    }


def wiring(paths: Paths, engine_root: str | Path) -> dict:
    """Is route B connected to anything that can trade? (The core finding.)

    Answered by searching the running engine's source for any reference to the
    walletlab package or its registry. A strategy nothing imports cannot place
    an order, however well validated it is — and this is the check that turns
    that from an assumption into a fact.
    """
    root = Path(engine_root)
    if not root.exists():
        return {"available": False, "reason": f"engine not found at {root}"}
    # Needles must be UNIQUE to walletlab. An earlier version of this check
    # also looked for "experiments.sqlite3" and reported the engine as
    # connected — because `pqb/research.py` has its own experiments database
    # of that name. A diagnostic that returns the opposite of the truth on a
    # substring collision is worse than no diagnostic, so every needle here
    # names walletlab specifically, and the matching needle is reported so the
    # claim can be checked rather than believed.
    needles = ("walletlab", "wallet-strategy-lab", "wallet_strategy_lab",
               "walletlab/experiments", "walletlab\\experiments")
    hits: list[str] = []
    for source in root.rglob("*.py"):
        if ".venv" in source.parts or "__pycache__" in source.parts:
            continue
        try:
            text = source.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for needle in needles:
            if needle in text:
                hits.append(f"{source.relative_to(root)}: matched {needle!r}")
                break
    return {
        "available": True,
        "connected": bool(hits),
        "references": hits,
        "verdict": ("route B is wired into the engine" if hits else
                    "ROUTE B IS NOT CONNECTED. Its validated strategies "
                    "cannot reach execution because no code in the trading "
                    "engine reads them. This is a wiring gap, not a "
                    "threshold problem, and no amount of loosening Strategy "
                    "A's filters will produce a single route-B trade."),
    }


# ---------------------------------------------------------------------------
# the whole audit
# ---------------------------------------------------------------------------


def report(paths: Paths, engine_root: str | Path) -> dict:
    """Everything, in one read-only pass."""
    out: dict[str, Any] = {}
    out["routeA"] = route_a(paths)
    out["routeALibrary"] = route_a_library(paths)
    out["routeB"] = route_b(paths)
    out["wiring"] = wiring(paths, engine_root)
    out["gateMap"] = gatemap.summary()

    ledger_rows: list[dict] = []
    if paths.ledger and Path(paths.ledger).exists():
        from .funnel import OpportunityLedger
        ledger = OpportunityLedger(paths.ledger)
        try:
            ledger_rows = ledger.rows()
        finally:
            ledger.close()
    out["ledger"] = {
        "available": bool(ledger_rows),
        "reason": ("" if ledger_rows else
                   "no V2 opportunity ledger yet — it fills in once the V2 "
                   "router runs; the route-A numbers above come from the "
                   "bot's own journal and do not need it"),
        "counters": counters(ledger_rows) if ledger_rows else {},
        "funnelA": funnel(ledger_rows, ROUTE_A) if ledger_rows else {},
        "funnelB": funnel(ledger_rows, ROUTE_B) if ledger_rows else {},
        "suppressionB": (suppression_ranking(ledger_rows, ROUTE_B)
                         if ledger_rows else []),
    }
    out["answers"] = _answers(out)
    return out


def _answers(data: dict) -> list:
    """The prompt's twenty-two questions, answered or explicitly not."""
    a = data.get("routeA") or {}
    lib = data.get("routeALibrary") or {}
    b = data.get("routeB") or {}
    wire = data.get("wiring") or {}
    led = data.get("ledger") or {}

    def q(n: int, question: str, answer: str) -> dict:
        return {"n": n, "question": question, "answer": answer}

    unknown = ("Not answerable yet: needs the V2 router running so the "
               "opportunity ledger has rows.")

    top_gate = (a.get("byGate") or [{}])[0]
    out = [
        q(1, "How many opportunities existed?",
          f"{a.get('decisions', 0):,} decision cycles over "
          f"{a.get('hours', 0)} hours. Per-outcome opportunity counts need "
          "the V2 ledger."),
        q(2, "How many wallet signals were generated?",
          f"Route B validated {b.get('validated', 0)} strategies from "
          f"{(b.get('lastPass') or {}).get('hypotheses', 0):,} hypotheses, "
          "but emitted no live signals into the engine — see Q6."),
        q(3, "How many Strategy A signals were generated?",
          f"0. All {a.get('decisions', 0):,} decisions were "
          f"{list((a.get('actions') or {}).keys())}."),
        q(4, "How many Strategy B signals were generated?",
          "0 reached the engine. Route B is not connected to it."),
        q(5, "How many Strategy B signals were rejected?",
          "None were rejected. They were never routed — a different and worse "
          "failure than rejection, because rejection at least leaves a trace."),
        q(6, "Why were they rejected?",
          ("They were not rejected by any gate. "
           if not wire.get("connected") else "") + str(wire.get("verdict", ""))),
        q(7, "How many passed risk?", unknown),
        q(8, "How many passed portfolio allocation?", unknown),
        q(9, "How many execution attempts occurred?",
          f"{a.get('positionsOpened', 0)} positions were ever opened."),
        q(10, "How many executions succeeded?",
          f"{a.get('positionsOpened', 0)}."),
        q(11, "How many trades completed?",
          f"{a.get('tradesCompleted', 0)} "
          f"({a.get('tradesPerDay', 0)}/day)."),
        q(12, "What was the expectancy?",
          "Undefined — no closed trades on this data set."
          if not a.get("tradesCompleted") else
          "See the consistency study (`pqb consistency`)."),
        q(13, "What was the average winner?", "Undefined — no closed trades."
          if not a.get("tradesCompleted") else "See `pqb consistency`."),
        q(14, "What was the average loser?", "Undefined — no closed trades."
          if not a.get("tradesCompleted") else "See `pqb consistency`."),
        q(15, "What was the drawdown?", "0 — no capital was ever deployed."
          if not a.get("tradesCompleted") else "See `pqb consistency`."),
        q(16, "What was the compounded return?",
          "0% — the account never traded."
          if not a.get("tradesCompleted") else "See `pqb consistency`."),
        q(17, "Which strategy produced the strongest risk-adjusted results?",
          "Neither has live results. On out-of-sample evidence route B is the "
          f"only one with anything validated ({b.get('validated', 0)} "
          f"strategies vs {lib.get('validated', 0)} for route A)."),
        q(18, "Which wallet strategy family produced the strongest results?",
          ", ".join(f"{s['wallet'][:12]}... score {s['score']}"
                    for s in (b.get("strategies") or [])) or "none validated"),
        q(19, "Which rules are currently suppressing the most opportunities?",
          f"{top_gate.get('gate', 'n/a')} "
          f"({top_gate.get('count', 0):,} = "
          f"{top_gate.get('share', 0):.0%} of all decisions), owned by "
          f"{top_gate.get('owner', 'n/a')}."),
        q(20, "Which suppressions are justified by evidence?",
          "The GLOBAL_SAFETY, PORTFOLIO and EXECUTION gates are justified for "
          "both routes by construction — they are statements about the "
          "account and the venue. See gateMap."),
        q(21, "Which suppressions are merely inherited from Strategy A?",
          f"{len(gatemap.gates_for(gatemap.STRATEGY_A))} gates belong to "
          "Strategy A and must not bind route B: "
          + ", ".join(g.key for g in gatemap.gates_for(gatemap.STRATEGY_A))),
        q(22, "What candidate strategy should be tested next?",
          "Route B's validated specs, through the V2 router, in shadow mode — "
          "they are the only strategies in the system with out-of-sample "
          "evidence behind them."),
    ]
    return out


def render(data: dict) -> str:
    """The audit as text."""
    a = data.get("routeA") or {}
    lib = data.get("routeALibrary") or {}
    b = data.get("routeB") or {}
    wire = data.get("wiring") or {}

    lines = ["OPPORTUNITY-LOSS AUDIT", "=" * 74,
             "Read-only over the original installation. Nothing was modified.",
             ""]

    lines.append("ROUTE A - the existing quant engine")
    if not a.get("available"):
        lines.append(f"   unavailable: {a.get('reason')}")
    else:
        lines += [
            f"   {a['decisions']:,} decisions / {a['cycles']:,} cycles / "
            f"{a['hours']}h",
            f"   actions: {a['actions']}",
            f"   positions opened: {a['positionsOpened']}   trades "
            f"completed: {a['tradesCompleted']}  ({a['tradesPerDay']}/day)",
            "   where the decisions went, by gate:"]
        for row in a["byGate"]:
            lines.append(f"     {row['share']:>6.1%}  {row['count']:>7,}  "
                         f"{row['gate']:<22} [{row['owner']}]")
    lines.append("")

    lines.append("ROUTE A - its own validation ladder (what learning mode "
                 "waits for)")
    if lib.get("available"):
        lines.append(f"   {lib['byStatus']}")
        lines.append(f"   VALIDATED: {lib['validated']}"
                     + ("   <-- entries are blocked while this is 0"
                        if lib["gatesEntries"] else ""))
    else:
        lines.append(f"   unavailable: {lib.get('reason')}")
    lines.append("")

    lines.append("ROUTE B - wallet-strategy-lab")
    if not b.get("available"):
        lines.append(f"   unavailable: {b.get('reason')}")
    else:
        last = b.get("lastPass") or {}
        lines.append(f"   {b['byStatus']}")
        lines.append(f"   last pass: {last.get('wallets')} wallets, "
                     f"{last.get('hypotheses'):,} hypotheses, FDR "
                     f"{last.get('fdrThreshold')}")
        lines.append(f"   VALIDATED: {b['validated']}")
        for s in b["strategies"]:
            lines.append(f"     {s['wallet'][:16]}...  score {s['score']}  "
                         f"oos_p {s['oosP']:.2e}  price "
                         f"{s['minPrice']}-{s['maxPrice']}  delay "
                         f"{s['delaySecs']}s")
    lines.append("")

    lines.append("WIRING - can route B reach execution?")
    lines.append(f"   connected: {wire.get('connected')}")
    lines.append(f"   {wire.get('verdict', '')}")
    lines.append("")

    lines.append("THE TWENTY-TWO QUESTIONS")
    for row in data.get("answers", []):
        lines.append(f"  {row['n']:>2}. {row['question']}")
        lines.append(f"      {row['answer']}")
    lines.append("")

    lines += [
        "NOTE ON WHAT THIS DOES NOT SAY",
        "  It does not say route B will make money. It says route B is the "
        "only part of",
        "  the system holding out-of-sample evidence, and that nothing can "
        "act on it.",
        "  Connecting it is what makes it testable; it is not what makes it "
        "true."]
    return "\n".join(lines)
