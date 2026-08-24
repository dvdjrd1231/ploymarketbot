"""Profit attribution: evidence about the EXISTING system, never a gate.

The operator's spec: identify where profit is gained or leaked so
improvements are evidence-based. These pin that the report reads the real
journal schema, buckets honestly (sample sizes beside expectancies), and
collapses two thousand differently-numbered skip reasons into countable ones.
"""

from __future__ import annotations

import sqlite3

import pytest

from pqb.analytics.attribution import collapse_reason, report, write_report


@pytest.fixture()
def journal(tmp_path):
    """A real journal file, via the real Journal class so the schema can
    never drift from what attribution reads."""
    from pqb.journal import Journal

    path = tmp_path / "journal.sqlite3"
    j = Journal(path)
    j.close()
    return path


def _add_trade(path, entry_price, pnl, hold_seconds, ttr="short",
               exit_reason="target"):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO lifecycles(token_id, status, entry_price, realized_pnl, "
        "hold_seconds, ttr_bucket, exit_reason) VALUES(?,?,?,?,?,?,?)",
        ("T1", "CLOSED", entry_price, pnl, hold_seconds, ttr, exit_reason))
    conn.commit()
    conn.close()


def _add_fill(path, limit_price, avg_price, side="BUY", requested=10.0,
              filled=10.0, fee=0.01):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO executions(ts, side, requested_size, limit_price, "
        "filled_size, avg_price, fee, status) VALUES(?,?,?,?,?,?,?,?)",
        (1.0, side, requested, limit_price, filled, avg_price, fee, "FILLED"))
    conn.commit()
    conn.close()


def _add_skip(path, reason):
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO decisions(ts, action, reason) VALUES(?,?,?)",
        (1.0, "DO_NOTHING", reason))
    conn.commit()
    conn.close()


def test_no_journal_is_reported_not_crashed(tmp_path):
    data = report(tmp_path / "missing.sqlite3")
    assert data["available"] is False


def test_price_buckets_carry_sample_size_and_expectancy(journal):
    _add_trade(journal, 0.55, +2.0, 120)
    _add_trade(journal, 0.57, -1.0, 300)
    _add_trade(journal, 0.85, +5.0, 60)
    data = report(journal)
    buckets = data["byPriceBucket"]
    assert buckets["50-59c"]["trades"] == 2
    assert buckets["50-59c"]["netPnl"] == pytest.approx(1.0)
    assert buckets["50-59c"]["expectancy"] == pytest.approx(0.5)
    assert buckets["80-89c"]["winRate"] == 1.0


def test_holding_time_buckets(journal):
    _add_trade(journal, 0.60, 1.0, 30)          # under a minute
    _add_trade(journal, 0.60, 1.0, 3_600)       # 30m-2h
    data = report(journal)
    assert data["byHoldTime"]["under-1m"]["trades"] == 1
    assert data["byHoldTime"]["30m-2h"]["trades"] == 1


def test_execution_slippage_is_side_aware(journal):
    # A BUY filled ABOVE the intended price leaks money...
    _add_fill(journal, limit_price=0.50, avg_price=0.52, side="BUY")
    # ...a SELL filled ABOVE the intended price does not.
    _add_fill(journal, limit_price=0.50, avg_price=0.52, side="SELL")
    data = report(journal)
    execution = data["execution"]
    assert execution["fillsWithSlippage"] == 1
    assert execution["slippagePaid"] == pytest.approx(0.02 * 10.0)
    assert execution["feesPaid"] == pytest.approx(0.02)


def test_unfilled_orders_are_counted_not_hidden(journal):
    _add_fill(journal, 0.50, 0.0, filled=0.0)
    data = report(journal)
    assert data["execution"]["unfilled"] == 1


def test_skip_reasons_collapse_numbers(journal):
    _add_skip(journal, "Score 0.31 < 0.55 at ask 0.62")
    _add_skip(journal, "Score 0.44 < 0.55 at ask 0.71")
    _add_skip(journal, "spread too wide")
    data = report(journal)
    reasons = data["skipReasons"]
    assert reasons["Score N < N at ask N"] == 2
    assert reasons["spread too wide"] == 1


def test_collapse_reason_is_stable():
    assert collapse_reason("edge 1,234.5 gone") == "edge N gone"
    assert collapse_reason("") == ""


def test_write_report_lands_where_the_ui_reads(journal, tmp_path):
    _add_trade(journal, 0.62, 1.5, 90)
    out = tmp_path / "attribution.json"
    data = write_report(journal, out)
    assert out.exists()
    assert data["closedTrades"] == 1
