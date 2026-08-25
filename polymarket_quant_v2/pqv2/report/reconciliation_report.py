"""Section 12: the before/after reconciliation diagnostic.

Two jobs:

  1. BEFORE — read whatever reconciliation history the existing installation
     actually has, read-only. If there is none, say so. The patch's own
     instruction is to establish whether these exits were really happening
     before claiming anything was fixed.

  2. AFTER — replay the recorded events through the corrected guard and report
     what would have changed.

The replay is a COUNTERFACTUAL and is labelled as one. It cannot say what the
P&L would have been, because a position that is not closed goes on to have a
different future that this data does not contain. Anyone reporting a P&L delta
from a replay like this is inventing it.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Settings
from ..reconciliation import (Diagnostics, ExitReason, PositionEvidence,
                              ReconciliationGuard, Resolution, training_filter)


def _ro(path: Path):
    p = Path(path)
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


@dataclass
class BeforeState:
    available: bool = False
    reconciliation_rows: int = 0
    kinds: list = field(default_factory=list)
    lifecycles_total: int = 0
    lifecycles_closed: int = 0
    reconciled_exits: int = 0
    exit_styles: list = field(default_factory=list)
    reconciled_pnl: float = 0.0
    total_pnl: float = 0.0
    reconciled_share_of_closed: float = 0.0
    training_contaminated: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def measure_before(st: Settings) -> BeforeState:
    """Read the existing installation's reconciliation history. Read-only."""
    out = BeforeState()
    conn = _ro(st.journal_db)
    if conn is None:
        out.note = "no journal database found"
        return out
    try:
        out.available = True
        out.reconciliation_rows = conn.execute(
            "SELECT COUNT(*) FROM reconciliations").fetchone()[0]
        out.kinds = [(r[0], r[1]) for r in conn.execute(
            "SELECT kind, COUNT(*) FROM reconciliations GROUP BY 1 "
            "ORDER BY 2 DESC")]
        out.lifecycles_total = conn.execute(
            "SELECT COUNT(*) FROM lifecycles").fetchone()[0]
        out.lifecycles_closed = conn.execute(
            "SELECT COUNT(*) FROM lifecycles WHERE status='CLOSED'").fetchone()[0]
        out.exit_styles = [(r[0] or "", r[1]) for r in conn.execute(
            "SELECT exit_style, COUNT(*) FROM lifecycles "
            "WHERE status='CLOSED' GROUP BY 1 ORDER BY 2 DESC")]
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(realized_pnl),0) FROM lifecycles "
            "WHERE status='CLOSED' AND exit_style='reconciled'").fetchone()
        out.reconciled_exits, out.reconciled_pnl = int(row[0]), float(row[1])
        out.total_pnl = float(conn.execute(
            "SELECT COALESCE(SUM(realized_pnl),0) FROM lifecycles "
            "WHERE status='CLOSED'").fetchone()[0])
        if out.lifecycles_closed:
            out.reconciled_share_of_closed = \
                out.reconciled_exits / out.lifecycles_closed
        # How many of those would have fed the empirical gate as if they were
        # decided exits? high_confidence._load_setups reads every CLOSED
        # lifecycle and scores realized_pnl > 0 as a win.
        out.training_contaminated = out.reconciled_exits
    except sqlite3.Error as exc:
        out.note = f"journal query failed: {exc}"
    finally:
        conn.close()

    if out.reconciliation_rows == 0 and out.lifecycles_total == 0:
        out.note = (
            "The existing installation has recorded 0 reconciliation events "
            "and 0 lifecycles: it has never opened a position, so the "
            "reconciliation exit path has never executed in production. The "
            "defect is LATENT, not observed. It is real -- it is visible in "
            "reconcile.py:121-153 and reproduced by the regression tests -- "
            "but no historical trade was harmed by it, and no before/after "
            "P&L comparison is possible.")
    return out


def replay(events: list, *, guard: ReconciliationGuard | None = None) -> dict:
    """Counterfactual: run recorded events through the corrected guard.

    `events` is a list of PositionEvidence. Returns what the patch would have
    done, and explicitly refuses to estimate a P&L delta.
    """
    guard = guard or ReconciliationGuard()
    outcomes = {"closed": 0, "held": 0, "uncertain": 0, "settled": 0,
                "abandoned": 0}
    for i, ev in enumerate(events):
        result = guard.observe(ev, now=float(i) * 3600.0)
        r = result.resolution
        if r == Resolution.CONFIRMED_POSITION_CLOSED.value:
            outcomes["closed"] += 1
        elif r == Resolution.MARKET_SETTLED.value:
            outcomes["settled"] += 1
        elif r == Resolution.POSITION_STILL_OPEN.value:
            outcomes["held"] += 1
        elif r == Resolution.ABANDONED.value:
            outcomes["abandoned"] += 1
        else:
            outcomes["uncertain"] += 1
    return {
        "events_replayed": len(events),
        "outcomes": outcomes,
        "diagnostics": guard.diag.to_dict(),
        "would_have_exited_before": len(events),
        "exits_prevented": len(events) - outcomes["closed"] - outcomes["settled"],
        "exits_confirmed": outcomes["closed"] + outcomes["settled"],
        "pnl_delta": None,
        "pnl_note": (
            "NOT ESTIMATED, deliberately. A position that is no longer closed "
            "goes on to have a different future, and that future is not in "
            "this data. Any P&L delta quoted from a replay like this would be "
            "invented. Measure it forward in shadow mode instead."),
    }


def render(before: BeforeState, after: dict | None = None,
           quarantine: dict | None = None) -> str:
    L: list = []
    L.append("=" * 74)
    L.append("RECONCILIATION EXIT SAFETY - BEFORE / AFTER")
    L.append("=" * 74)

    L.append("\nBEFORE  (existing installation, read-only)")
    L.append("-" * 42)
    if not before.available:
        L.append(f"  {before.note}")
    else:
        L.append(f"  reconciliation events recorded   {before.reconciliation_rows:>10,}")
        L.append(f"  lifecycles total                 {before.lifecycles_total:>10,}")
        L.append(f"  lifecycles closed                {before.lifecycles_closed:>10,}")
        L.append(f"  closed with exit_style=reconciled{before.reconciled_exits:>10,}")
        if before.lifecycles_closed:
            L.append(f"  reconciled share of all closes   "
                     f"{before.reconciled_share_of_closed:>9.1%}")
            L.append(f"  P&L attributed to reconciliation {before.reconciled_pnl:>10,.2f}")
            L.append(f"  total realised P&L               {before.total_pnl:>10,.2f}")
            L.append(f"  training records contaminated    "
                     f"{before.training_contaminated:>10,}")
        if before.kinds:
            L.append(f"  kinds: {before.kinds}")
        if before.exit_styles:
            L.append(f"  exit styles: {before.exit_styles}")
    if before.note and before.available:
        L.append("")
        for line in _wrap(before.note, 70):
            L.append(f"  {line}")

    L.append("\nAFTER  (corrected guard)")
    L.append("-" * 42)
    if not after:
        L.append("  no events to replay - nothing recorded to replay against.")
        L.append("  The corrected behaviour is demonstrated instead by the")
        L.append("  regression suite: tests/test_reconciliation.py, 32 tests.")
    else:
        o = after["outcomes"]
        L.append(f"  events replayed                  {after['events_replayed']:>10,}")
        L.append(f"  would have exited (old path)     "
                 f"{after['would_have_exited_before']:>10,}")
        L.append(f"  exits PREVENTED                  {after['exits_prevented']:>10,}")
        L.append(f"  exits CONFIRMED genuine          {after['exits_confirmed']:>10,}")
        L.append(f"    of which settlement            {o['settled']:>10,}")
        L.append(f"  position found still open        {o['held']:>10,}")
        L.append(f"  still uncertain                  {o['uncertain']:>10,}")
        L.append(f"  abandoned to monitoring          {o['abandoned']:>10,}")
        L.append("\n  diagnostic counters:")
        for k, v in after["diagnostics"].items():
            L.append(f"    {k:<44}{v:>8,}")
        L.append("")
        for line in _wrap(after["pnl_note"], 70):
            L.append(f"  {line}")

    if quarantine:
        L.append("\nTRAINING-DATA PROTECTION")
        L.append("-" * 42)
        L.append(f"  records examined                 {quarantine['examined']:>10,}")
        L.append(f"  eligible to teach a model        {quarantine['eligible']:>10,}")
        L.append(f"  QUARANTINED (unverified recon)   {quarantine['quarantined']:>10,}")

    L.append("\nREMAINING ANOMALIES")
    L.append("-" * 42)
    for a in anomalies(before, after):
        for line in _wrap(a, 70):
            L.append(f"  {line}")
        L.append("")
    L.append("=" * 74)
    return "\n".join(L)


def anomalies(before: BeforeState, after: dict | None) -> list:
    out: list = []
    if before.available and before.lifecycles_total == 0:
        out.append(
            "The reconciliation exit path has never run in production (0 "
            "lifecycles, 0 reconciliation rows). The defect is LATENT: real in "
            "the code, unobserved in the data. The patch is therefore "
            "preventive, and no claim is made that it recovered any lost P&L.")
    if before.reconciled_exits:
        out.append(
            f"{before.reconciled_exits:,} historical trades carry "
            "exit_style='reconciled'. These were written by the unpatched path "
            "with no verification, so they must be treated as "
            "RECONCILIATION_UNVERIFIED and quarantined from training until "
            "each is re-verified. training_filter() does this.")
    out.append(
        "The upstream cause is unaddressed by this patch and remains: "
        "reconcile.py calls exchange_positions() and treats an empty result as "
        "'every position is gone'. The guard now refuses to act on that, but "
        "the engine will still log a mass mismatch on every API blip. A "
        "source-health check at the adapter is the complete fix.")
    out.append(
        "This patch is implemented in V2 and is NOT applied to the original "
        "installation, per the standing rule never to modify it. The exact "
        "minimal change for reconcile.py is supplied as a reviewable diff in "
        "patches/v1_reconcile_guard.patch - apply it deliberately, not "
        "automatically.")
    return out


def _wrap(text: str, width: int) -> list:
    words, line, out = text.split(), "", []
    for w in words:
        if len(line) + len(w) + 1 > width:
            out.append(line)
            line = w
        else:
            line = f"{line} {w}".strip()
    if line:
        out.append(line)
    return out


def build(st: Settings) -> dict:
    """Full before/after, plus a training-quarantine count from real records."""
    before = measure_before(st)
    quarantine = None
    conn = _ro(st.journal_db)
    if conn is not None:
        try:
            rows = [{"exit_reason": (r["exit_style"] or "")}
                    for r in conn.execute(
                        "SELECT exit_style FROM lifecycles WHERE status='CLOSED'")]
            eligible, quarantined = training_filter(rows)
            quarantine = {"examined": len(rows), "eligible": len(eligible),
                          "quarantined": len(quarantined)}
        except sqlite3.Error:
            pass
        finally:
            conn.close()
    return {"before": before.to_dict(), "after": None,
            "quarantine": quarantine,
            "rendered": render(before, None, quarantine)}
