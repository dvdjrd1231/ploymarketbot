"""The code-enforced live gate (§3, acceptance 11.7): live stays blocked until
the paper record proves a calibrated, positive, controlled-drawdown edge."""

from __future__ import annotations

import sqlite3

from pqb.config import LiveGateConfig
from pqb.decision.live_gate import evaluate


def _journal(tmp_path, predictions, lifecycles):
    path = tmp_path / "journal.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE predictions (probability REAL, market_price REAL, "
                 "settled REAL)")
    conn.execute("CREATE TABLE lifecycles (realized_pnl REAL, status TEXT)")
    conn.executemany("INSERT INTO predictions VALUES (?,?,?)", predictions)
    conn.executemany("INSERT INTO lifecycles VALUES (?,?)", lifecycles)
    conn.commit()
    conn.close()
    return path


def _good_predictions(n=60):
    # Well-calibrated and better than the market: we said 0.8 and it happened,
    # the market only said 0.6. Half win, half lose, matching our estimate.
    rows = []
    for i in range(n):
        won = 1.0 if i % 5 < 4 else 0.0     # 80% actually happen
        rows.append((0.8, 0.6, won))
    return rows


def cfg(**kw):
    base = dict(require=True, min_settled=50, min_total_pnl_usdc=0.0,
                max_drawdown_usdc=25.0, require_beat_market=True)
    base.update(kw)
    return LiveGateConfig(**base)


def test_missing_journal_fails_closed(tmp_path):
    result = evaluate(tmp_path / "nope.sqlite3", cfg())
    assert not result.passed


def test_too_few_settled_blocks(tmp_path):
    path = _journal(tmp_path, _good_predictions(10),
                    [(5.0, "CLOSED")] * 5)
    result = evaluate(path, cfg())
    assert not result.passed
    assert any("settled" in r for r in result.reasons)


def test_a_proven_edge_passes(tmp_path):
    path = _journal(tmp_path, _good_predictions(60),
                    [(2.0, "CLOSED")] * 20)   # +$40, no drawdown
    result = evaluate(path, cfg())
    assert result.passed, result.reasons


def test_losing_money_blocks_even_if_calibrated(tmp_path):
    path = _journal(tmp_path, _good_predictions(60),
                    [(-2.0, "CLOSED")] * 20)  # net negative
    result = evaluate(path, cfg())
    assert not result.passed
    assert any("P&L" in r for r in result.reasons)


def test_deep_drawdown_blocks(tmp_path):
    # Ends net positive (+$10) but dug a $50 hole first — deeper than the $25 cap.
    lifecycles = [(-50.0, "CLOSED"), (60.0, "CLOSED")]
    path = _journal(tmp_path, _good_predictions(60), lifecycles)
    result = evaluate(path, cfg())
    assert not result.passed
    assert any("drawdown" in r for r in result.reasons)


def test_worse_than_market_blocks(tmp_path):
    # We said 0.5 on everything (Brier 0.25); market said the truth (Brier 0).
    preds = [(0.5, 1.0 if i % 2 else 0.0, 1.0 if i % 2 else 0.0)
             for i in range(60)]
    path = _journal(tmp_path, preds, [(2.0, "CLOSED")] * 20)
    result = evaluate(path, cfg())
    assert not result.passed
    assert any("Brier" in r for r in result.reasons)
