"""
Decision-journal analysis (prompt section 5, feedback loop).

Answers one question in several cuts: **which decisions actually made money?**

    python -m pqb.cli report
    python scripts/analyze_journal.py state/journal.sqlite3

Every grouping below is a tag written at decision time, so nothing here has to
re-derive context after the fact — which is the point of tagging on the way in.
Groups with too few records are shown but flagged: a 100% win rate over two
trades is noise, and presenting it beside a 60% rate over two hundred without
comment would be actively misleading.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

MIN_MEANINGFUL = 10


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise SystemExit(f"No journal at {path}. Run a session first.")
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []


def _money(value: float) -> str:
    return f"{value:>10,.2f}"


def _table(title: str, rows: list[sqlite3.Row], label_key: str,
           min_count: int) -> None:
    print(f"\n{title}")
    print("-" * 78)
    if not rows:
        print("  (no closed positions yet)")
        return
    print(f"  {'group':<22} {'n':>4} {'wins':>5} {'win%':>6} "
          f"{'total P/L':>11} {'avg P/L':>9} {'avg ret%':>9}")
    for row in rows:
        n = row["n"] or 0
        if n < min_count:
            continue
        wins = row["wins"] or 0
        flag = "  *" if n < MIN_MEANINGFUL else ""
        label = str(row[label_key] or "(none)")[:22]
        print(f"  {label:<22} {n:>4} {wins:>5} "
              f"{(wins / n * 100 if n else 0):>5.1f}% "
              f"{_money(row['pnl'] or 0.0)} "
              f"{(row['pnl'] or 0.0) / n if n else 0:>9.2f} "
              f"{(row['ret'] or 0.0) * 100:>8.1f}%{flag}")


_AGG = """
    SELECT {key} AS k,
           COUNT(*) AS n,
           SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
           SUM(realized_pnl) AS pnl,
           AVG(return_pct) AS ret
      FROM lifecycles
     WHERE status = 'CLOSED'
     GROUP BY k
     ORDER BY pnl DESC
"""


def report(journal_path: str | Path, min_count: int = 1) -> None:
    path = Path(journal_path)
    conn = _connect(path)
    try:
        _headline(conn, path)
        for title, key in (
            ("BY EXIT STYLE — which way of getting out has paid", "exit_style"),
            ("BY TIME TO RESOLUTION (at entry)", "ttr_bucket"),
            ("BY LIQUIDITY REGIME", "liquidity_bucket"),
            ("BY MARKET CATEGORY", "category"),
            ("BY WALLET INFLUENCE — did following a wallet help?",
             "wallet_influence"),
        ):
            _table(title, _rows(conn, _AGG.format(key=key)), "k", min_count)

        _decisions(conn, min_count)
        _wallet_override(conn)
        _reconciliation(conn)
        _integrity(conn)
        print(f"\n  * fewer than {MIN_MEANINGFUL} records — not yet meaningful.\n")
    finally:
        conn.close()


def _headline(conn, path: Path) -> None:
    row = _rows(conn, """
        SELECT COUNT(*) n,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) wins,
               COALESCE(SUM(realized_pnl), 0) pnl,
               COALESCE(AVG(hold_seconds), 0) hold
          FROM lifecycles WHERE status = 'CLOSED'""")
    openrow = _rows(conn, "SELECT COUNT(*) n FROM lifecycles WHERE status='OPEN'")
    cycles = _rows(conn, "SELECT COUNT(*) n, COALESCE(SUM(errors),0) e FROM cycles")
    decisions = _rows(conn, "SELECT COUNT(*) n FROM decisions")

    n = row[0]["n"] if row else 0
    wins = (row[0]["wins"] or 0) if row else 0
    print("=" * 78)
    print(f"  DECISION JOURNAL — {path}")
    print("=" * 78)
    print(f"  cycles          : {cycles[0]['n'] if cycles else 0} "
          f"(errors {cycles[0]['e'] if cycles else 0})")
    print(f"  decisions       : {decisions[0]['n'] if decisions else 0}")
    print(f"  positions closed: {n}   still open: "
          f"{openrow[0]['n'] if openrow else 0}")
    if n:
        print(f"  win rate        : {wins / n * 100:.1f}%  ({wins}/{n})")
        print(f"  realised P/L    : {row[0]['pnl']:,.2f}")
        print(f"  average hold    : {(row[0]['hold'] or 0) / 3600:.1f} h")


def _decisions(conn, min_count: int) -> None:
    """Decision mix, including the ones that led to nothing.

    A journal read that ignores HOLD and DO_NOTHING cannot tell an engine that
    is being selective from one that is barely firing.
    """
    rows = _rows(conn, """
        SELECT action AS k, COUNT(*) AS n, AVG(score) AS avg_score
          FROM decisions GROUP BY action ORDER BY n DESC""")
    print("\nDECISION MIX")
    print("-" * 78)
    total = sum(r["n"] for r in rows) or 1
    for row in rows:
        print(f"  {str(row['k']):<14} {row['n']:>6}  "
              f"{row['n'] / total * 100:>5.1f}%   "
              f"avg score {row['avg_score'] or 0:.3f}")


def _wallet_override(conn) -> None:
    """How the two halves of the wallet decision have worked out."""
    rows = _rows(conn, """
        SELECT exit_style AS k, COUNT(*) AS n,
               SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) AS wins,
               COALESCE(SUM(realized_pnl), 0) AS pnl
          FROM lifecycles
         WHERE status='CLOSED' AND exit_style IN ('wallet','wallet_override')
         GROUP BY k""")
    holds = _rows(conn, """
        SELECT COUNT(*) n FROM decisions WHERE exit_style = 'wallet_override'""")
    print("\nWALLET SIGNAL: FOLLOWED vs OVERRIDDEN")
    print("-" * 78)
    if not rows and not (holds and holds[0]["n"]):
        print("  (no wallet-driven exits yet)")
        return
    for row in rows:
        n = row["n"] or 1
        print(f"  {str(row['k']):<20} n={row['n']:<4} "
              f"win {row['wins'] / n * 100:5.1f}%  P/L {row['pnl']:>10,.2f}")
    if holds:
        print(f"  override decisions taken: {holds[0]['n']}")


def _reconciliation(conn) -> None:
    rows = _rows(conn, """
        SELECT kind, COUNT(*) n FROM reconciliations GROUP BY kind
        ORDER BY n DESC""")
    print("\nRECONCILIATION EVENTS")
    print("-" * 78)
    if not rows:
        print("  (none — the bridge and the exchange never diverged)")
        return
    for row in rows:
        print(f"  {row['kind']:<26} {row['n']:>5}")


def _integrity(conn) -> None:
    """Journal integrity — what the multi-hour dry-run session report checks."""
    problems = []
    orphan = _rows(conn, """
        SELECT COUNT(*) n FROM lifecycles
         WHERE status='CLOSED' AND (exit_ts IS NULL OR exit_price = 0)""")
    if orphan and orphan[0]["n"]:
        problems.append(f"{orphan[0]['n']} closed lifecycle(s) with no exit price")
    unlinked = _rows(conn, """
        SELECT COUNT(*) n FROM executions
         WHERE decision_id IS NULL AND status NOT IN ('ADOPTED')""")
    if unlinked and unlinked[0]["n"]:
        problems.append(f"{unlinked[0]['n']} execution(s) with no decision")
    negative = _rows(conn, "SELECT COUNT(*) n FROM lifecycles WHERE entry_size < 0")
    if negative and negative[0]["n"]:
        problems.append(f"{negative[0]['n']} lifecycle(s) with negative size")

    print("\nJOURNAL INTEGRITY")
    print("-" * 78)
    if not problems:
        print("  ok — every execution links to a decision and every closed "
              "lifecycle has an exit.")
    for problem in problems:
        print(f"  PROBLEM: {problem}")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "state/journal.sqlite3"
    report(target)
