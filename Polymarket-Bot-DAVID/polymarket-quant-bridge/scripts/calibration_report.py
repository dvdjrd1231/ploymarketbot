"""
The evidence report: is the edge real, before any money is put behind it.

The brief is explicit — run the decision model in paper mode first and report
calibration, EV, drawdown, win rate and performance by wallet, market and price
bucket *before* allowing live execution. This is that report.

The question it exists to answer is not "did we make money". A model can make
money by luck. It is: **when we said 70%, did it happen about 70% of the time,
and did we say it better than the market did?** A model that beats the market's
own Brier score has an edge likely to persist; one that does not is guessing
with extra steps, however profitable it looked for a week.

    python -m pqb.cli calibration
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _rows(conn, sql, params=()):
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    except sqlite3.OperationalError:
        return []


def report(journal_path: Path, min_count: int = 3) -> int:
    conn = sqlite3.connect(str(journal_path))
    try:
        return _report(conn, min_count)
    finally:
        conn.close()


def _report(conn, min_count: int) -> int:
    print("=" * 78)
    print("  DECISION MODEL — EVIDENCE REPORT")
    print("=" * 78)

    total = _rows(conn, "SELECT COUNT(*) n FROM predictions")
    if not total or not total[0]["n"]:
        print("\n  No predictions recorded yet.")
        print("  Run the bot with the EV engine for a while, then try again.")
        return 0
    n_all = total[0]["n"]
    acted = _rows(conn, "SELECT COUNT(*) n FROM predictions WHERE acted=1")[0]["n"]
    settled = _rows(conn,
                    "SELECT COUNT(*) n FROM predictions WHERE settled IS NOT NULL"
                    )[0]["n"]

    print(f"\n  predictions made      : {n_all:,}")
    print(f"  passed the EV gate    : {acted:,}"
          f"   ({acted / n_all:.1%} of everything it looked at)")
    print(f"  settled so far        : {settled:,}")

    if not settled:
        print("\n  NOTHING HAS SETTLED YET, so calibration cannot be measured.")
        print("  Markets have to resolve before we can score what we predicted.")
        print("  Until then the numbers below are what the model BELIEVES, not")
        print("  what it has been proven right about.")
        _edges(conn)
        return 0

    # --- calibration --------------------------------------------------------
    print("\n" + "-" * 78)
    print("  CALIBRATION — when we said X%, how often did it happen?")
    print("-" * 78)
    buckets = [(0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)]
    print(f"  {'we said':<12} {'n':>6} {'predicted':>10} {'actual':>8} {'error':>8}")
    for low, high in buckets:
        rows = _rows(conn,
                     "SELECT probability, settled FROM predictions "
                     "WHERE settled IS NOT NULL AND probability >= ? "
                     "AND probability < ?", (low, high))
        if len(rows) < min_count:
            continue
        predicted = sum(r["probability"] for r in rows) / len(rows)
        actual = sum(r["settled"] for r in rows) / len(rows)
        flag = "  <-- off" if abs(actual - predicted) > 0.15 else ""
        print(f"  {low:.0%}-{high:.0%}{'':<5} {len(rows):>6} {predicted:>9.1%} "
              f"{actual:>7.1%} {actual - predicted:>+7.1%}{flag}")

    # --- against the market -------------------------------------------------
    rows = _rows(conn, "SELECT probability, market_price, settled FROM "
                       "predictions WHERE settled IS NOT NULL")
    ours = sum((r["probability"] - r["settled"]) ** 2 for r in rows) / len(rows)
    theirs = sum((r["market_price"] - r["settled"]) ** 2 for r in rows) / len(rows)
    print("\n" + "-" * 78)
    print("  ARE WE BETTER THAN THE MARKET?  (Brier score, lower is better)")
    print("-" * 78)
    print(f"  our estimates    : {ours:.4f}")
    print(f"  the market price : {theirs:.4f}")
    if ours < theirs:
        print(f"  -> we are BETTER by {theirs - ours:.4f}. This is the evidence")
        print("     that the edge is real rather than luck.")
    else:
        print(f"  -> we are WORSE by {ours - theirs:.4f}. On this sample the")
        print("     market prices these outcomes better than we do, and there")
        print("     is no case for trading against it yet.")

    # --- where the edge is, and is not --------------------------------------
    for column, title in (("market_price", "PRICE BUCKET"),):
        print("\n" + "-" * 78)
        print(f"  PERFORMANCE BY {title}")
        print("-" * 78)
        print(f"  {'bought around':<16} {'n':>5} {'hit rate':>9} {'avg EV':>9}")
        for low, high in ((0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)):
            rows = _rows(conn,
                         "SELECT settled, ev FROM predictions WHERE settled "
                         "IS NOT NULL AND market_price >= ? AND market_price < ?",
                         (low, high))
            if len(rows) < min_count:
                continue
            hit = sum(r["settled"] for r in rows) / len(rows)
            ev = sum(r["ev"] for r in rows) / len(rows)
            print(f"  {low:.0%}-{high:.0%}{'':<9} {len(rows):>5} {hit:>8.1%} "
                  f"{ev:>+8.2%}")

    # --- broken down by the dimensions the model may differ across -----------
    _breakdown(conn, "category", "MARKET CATEGORY", min_count)
    _breakdown(conn, "ttr", "TIME TO RESOLUTION", min_count)
    _consensus_breakdown(conn, min_count)

    # --- the overfitting guard ----------------------------------------------
    _holdout(conn, min_count)

    _edges(conn)
    _traded(conn)
    return 0


def _breakdown(conn, column, title, min_count) -> None:
    """Calibration and edge within each value of a categorical column.

    A model can be sharp on sports and blind on politics; a single blended
    Brier score hides that. This shows, per group, how often our calls landed
    and whether we still beat the market there — because an edge that only
    exists in one category should only be traded in that category.
    """
    try:
        groups = _rows(conn, f"SELECT DISTINCT {column} AS g FROM predictions "
                             f"WHERE settled IS NOT NULL AND {column} IS NOT NULL "
                             f"AND {column} != ''")
    except Exception:                                    # noqa: BLE001
        return
    if not groups:
        return
    print("\n" + "-" * 78)
    print(f"  PERFORMANCE BY {title}")
    print("-" * 78)
    print(f"  {'group':<18} {'n':>5} {'predicted':>10} {'actual':>8} "
          f"{'ours':>7} {'mkt':>7} {'edge?':>6}")
    for row in sorted(groups, key=lambda r: str(r["g"])):
        g = row["g"]
        rows = _rows(conn, f"SELECT probability, market_price, settled FROM "
                           f"predictions WHERE settled IS NOT NULL AND {column}=?",
                     (g,))
        if len(rows) < min_count:
            continue
        n = len(rows)
        predicted = sum(r["probability"] for r in rows) / n
        actual = sum(r["settled"] for r in rows) / n
        ours = sum((r["probability"] - r["settled"]) ** 2 for r in rows) / n
        mkt = sum((r["market_price"] - r["settled"]) ** 2 for r in rows) / n
        edge = "yes" if ours < mkt else "no"
        print(f"  {str(g)[:18]:<18} {n:>5} {predicted:>9.1%} {actual:>7.1%} "
              f"{ours:>7.3f} {mkt:>7.3f} {edge:>6}")


def _consensus_breakdown(conn, min_count) -> None:
    """Does agreement among independent wallets actually predict better?"""
    rows = _rows(conn, "SELECT consensus, probability, market_price, settled "
                       "FROM predictions WHERE settled IS NOT NULL")
    if not rows:
        return
    bands = [("0 wallets", 0, 1), ("1 wallet", 1, 2),
             ("2 wallets", 2, 3), ("3+ wallets", 3, 10 ** 9)]
    printed = False
    for label, lo, hi in bands:
        sub = [r for r in rows if lo <= int(r["consensus"] or 0) < hi]
        if len(sub) < min_count:
            continue
        if not printed:
            print("\n" + "-" * 78)
            print("  PERFORMANCE BY WALLET CONSENSUS")
            print("-" * 78)
            print(f"  {'consensus':<14} {'n':>5} {'actual':>8} {'ours':>7} {'mkt':>7}")
            printed = True
        n = len(sub)
        actual = sum(r["settled"] for r in sub) / n
        ours = sum((r["probability"] - r["settled"]) ** 2 for r in sub) / n
        mkt = sum((r["market_price"] - r["settled"]) ** 2 for r in sub) / n
        print(f"  {label:<14} {n:>5} {actual:>7.1%} {ours:>7.3f} {mkt:>7.3f}")


def _holdout(conn, min_count) -> None:
    """Out-of-sample check: does the edge survive on data it never 'saw'?

    An edge measured on the same data used to build the model is the classic way
    to fool yourself. This splits the settled predictions in time — the earlier
    70% as the reference, the most recent 30% held out — and reports whether the
    model still beats the market on the held-out slice. If it only wins in-sample
    that is reported plainly, because trading it would be trading noise.
    """
    rows = _rows(conn, "SELECT probability, market_price, settled FROM "
                       "predictions WHERE settled IS NOT NULL ORDER BY ts")
    print("\n" + "-" * 78)
    print("  OUT-OF-SAMPLE CHECK (overfitting guard)")
    print("-" * 78)
    if len(rows) < max(2 * min_count, 20):
        print(f"  Not enough settled history yet ({len(rows)}). Need at least "
              f"{max(2 * min_count, 20)} before a hold-out means anything.")
        return
    cut = int(len(rows) * 0.7)
    holdout = rows[cut:]
    n = len(holdout)
    ours = sum((r["probability"] - r["settled"]) ** 2 for r in holdout) / n
    mkt = sum((r["market_price"] - r["settled"]) ** 2 for r in holdout) / n
    print(f"  held-out sample : {n} most-recent settled predictions")
    print(f"  our Brier       : {ours:.4f}")
    print(f"  market Brier    : {mkt:.4f}")
    if ours < mkt:
        print("  -> the edge SURVIVES out of sample. This is the honest signal")
        print("     that it is real and not fitted to its own history.")
    else:
        print("  -> NO EDGE out of sample. On data it did not train on, the")
        print("     market prices these better. Do not trade this as an edge.")


def _edges(conn) -> None:
    print("\n" + "-" * 78)
    print("  WHERE IT THINKS THE EDGE IS")
    print("-" * 78)
    rows = _rows(conn, "SELECT outcome, probability, market_price, ev, "
                       "confidence, acted FROM predictions "
                       "ORDER BY ev DESC LIMIT 8")
    if not rows:
        return
    print(f"  {'outcome':<22} {'ours':>6} {'market':>7} {'EV':>8} {'conf':>6} act")
    for r in rows:
        print(f"  {(r['outcome'] or '')[:22]:<22} {r['probability']:>6.3f} "
              f"{r['market_price']:>7.3f} {r['ev']:>+7.2%} "
              f"{r['confidence']:>6.2f} {'yes' if r['acted'] else '-'}")


def _traded(conn) -> None:
    rows = _rows(conn,
                 "SELECT realized_pnl, return_pct FROM lifecycles "
                 "WHERE status='CLOSED'")
    print("\n" + "-" * 78)
    print("  WHAT ACTUALLY HAPPENED TO THE MONEY")
    print("-" * 78)
    if not rows:
        print("  No trades have closed yet.")
        return
    wins = [r for r in rows if r["realized_pnl"] > 0]
    total = sum(r["realized_pnl"] for r in rows)
    # Worst peak-to-trough on the realised sequence: the drawdown the brief
    # asks to see alongside the win rate, because a good average can hide it.
    equity, peak, worst = 0.0, 0.0, 0.0
    for r in rows:
        equity += r["realized_pnl"]
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    print(f"  closed trades : {len(rows)}")
    print(f"  win rate      : {len(wins) / len(rows):.1%}")
    print(f"  total P&L     : ${total:,.2f}")
    print(f"  worst drawdown: ${worst:,.2f}")
    print("\n  A high win rate with a large drawdown is not a good system.")
    print("  Both numbers matter, which is why both are here.")
