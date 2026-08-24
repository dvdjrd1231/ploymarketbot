"""
Reconciliation mismatch handling (prompt section 6).

The rule under test is: on any divergence, record it, adopt the exchange as
canonical, and alert. These tests drive the reconciler with a stub exchange so
each class of mismatch can be produced deliberately.

The coroutines are driven with ``asyncio.run`` rather than pytest-asyncio —
one fewer plugin to install, and the reconciler is called exactly once per
scenario, so there is nothing an event-loop fixture would buy.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from pqb.config import Config
from pqb.logs import Log
from pqb.models import AccountState, Action, Decision, ExecutionReport, PositionView
from pqb.reconcile import Reconciler


def run(coro):
    return asyncio.run(coro)


class FakeExchange:
    """Stands in for the data adapter's read-only exchange surface."""

    def __init__(self, positions=None, orders=None, balance=100.0):
        self._positions = positions or []
        self._orders = orders or []
        self.balance = balance

    async def exchange_positions(self):
        return list(self._positions)

    async def open_orders(self):
        return list(self._orders)

    async def account(self):
        return AccountState(address="0xtest", balance=self.balance)


def live_config() -> Config:
    cfg = Config()
    cfg.mode.dry_run = False
    cfg.mode.allow_live = True
    return cfg


def add_open_position(journal, token_id: str, size: float, price: float,
                      age_seconds: float = 3600.0) -> int:
    """Open a lifecycle, aged past the settle grace period by default.

    Age matters: a just-opened position is deliberately exempt from the
    "missing on the exchange" check, because the Data API lags a fill by a few
    seconds and flagging that would halt on every entry.
    """
    decision = Decision(action=Action.BUY, token_id=token_id, market_id="m1",
                        outcome="Yes", question="Will it?")
    journal.record_decision(decision, "cycle-1")
    report = ExecutionReport(decision=decision, submitted=True, status="FILLED",
                             requested_size=size, filled_size=size,
                             avg_price=price)
    lifecycle_id = journal.open_lifecycle(decision, report)
    if age_seconds:
        journal.execute("UPDATE lifecycles SET entry_ts = ? WHERE id = ?",
                        (time.time() - age_seconds, lifecycle_id))
    return lifecycle_id


def no_mark(_token_id):
    return None


def reconciler(journal, exchange, cfg=None, paper=None) -> Reconciler:
    return Reconciler(cfg or live_config(), Log(), journal, exchange,
                      paper=paper)


# --- the happy path ---------------------------------------------------------

def test_agreement_is_clean(journal):
    add_open_position(journal, "tok1", 10.0, 0.50)
    exchange = FakeExchange(
        [PositionView(token_id="tok1", size=10.0, avg_price=0.50,
                      cur_price=0.55)])

    result = run(reconciler(journal, exchange).run(no_mark))

    assert result.clean
    assert result.checked == 1
    assert (result.adopted, result.closed, result.resized) == (0, 0, 0)


# --- mismatches -------------------------------------------------------------

def test_position_gone_from_the_exchange_closes_the_lifecycle(journal):
    lifecycle_id = add_open_position(journal, "tok1", 10.0, 0.50)
    exchange = FakeExchange([])          # the exchange holds nothing

    result = run(reconciler(journal, exchange).run(lambda _t: 0.70))

    assert not result.clean
    assert result.closed == 1
    assert result.mismatches[0].kind == "position.missing"
    row = journal.query("SELECT * FROM lifecycles WHERE id = ?",
                        (lifecycle_id,))[0]
    assert row["status"] == "CLOSED"
    assert row["exit_style"] == "reconciled"
    # Closed at the last known mark: (0.70 - 0.50) * 10
    assert row["realized_pnl"] == pytest.approx(2.0)
    assert journal.query("SELECT COUNT(*) n FROM reconciliations")[0]["n"] == 1


def test_unknown_exchange_position_is_adopted(journal):
    exchange = FakeExchange(
        [PositionView(token_id="tok9", market_id="m9", outcome="No",
                      size=40.0, avg_price=0.25, cur_price=0.30)])

    result = run(reconciler(journal, exchange).run(no_mark))

    assert result.adopted == 1
    assert result.mismatches[0].kind == "position.unknown"
    rows = journal.open_lifecycles()
    assert len(rows) == 1
    assert rows[0]["token_id"] == "tok9"
    assert rows[0]["entry_size"] == pytest.approx(40.0)


def test_size_drift_adopts_the_exchange_figure(journal):
    lifecycle_id = add_open_position(journal, "tok1", 10.0, 0.50)
    exchange = FakeExchange(
        [PositionView(token_id="tok1", size=6.0, avg_price=0.50,
                      cur_price=0.50)])

    result = run(reconciler(journal, exchange).run(no_mark))

    assert result.resized == 1
    assert result.mismatches[0].kind == "position.size"
    row = journal.query("SELECT * FROM lifecycles WHERE id = ?",
                        (lifecycle_id,))[0]
    assert row["entry_size"] == pytest.approx(6.0)
    assert row["status"] == "OPEN"      # a resize is not a close


def test_tiny_drift_is_within_tolerance(journal):
    add_open_position(journal, "tok1", 100.0, 0.50)
    exchange = FakeExchange(
        [PositionView(token_id="tok1", size=100.002, avg_price=0.50,
                      cur_price=0.50)])

    assert run(reconciler(journal, exchange).run(no_mark)).clean


def test_resting_orders_are_reported(journal):
    exchange = FakeExchange([], orders=[{"id": "order-1"}, {"id": "order-2"}])

    result = run(reconciler(journal, exchange).run(no_mark))

    assert result.orphan_orders == 2
    kinds = [r["kind"] for r in journal.query("SELECT kind FROM reconciliations")]
    assert "orders.resting" in kinds


# --- balance ----------------------------------------------------------------

def test_unexplained_balance_drop_is_flagged(journal):
    journal.set_state("account.balance", 100.0)
    exchange = FakeExchange([], balance=60.0)

    result = run(reconciler(journal, exchange).run(no_mark))

    assert any(m.kind == "balance.drop" for m in result.mismatches)
    assert journal.get_state("account.balance") == 60.0


def test_balance_increase_is_not_a_mismatch(journal):
    # Deposits and settlement credits arrive from outside the bridge.
    journal.set_state("account.balance", 100.0)
    exchange = FakeExchange([], balance=140.0)

    result = run(reconciler(journal, exchange).run(no_mark))
    assert not any(m.kind == "balance.drop" for m in result.mismatches)


# --- dry-run ----------------------------------------------------------------

def test_dry_run_reconciles_against_the_simulated_book(journal):
    """Dry-run has no exchange truth, so the paper book is the comparison."""
    from pqb.adapters.execution_adapter import PaperBook

    paper = PaperBook(journal, starting_cash=100.0, reset=True)
    paper.buy(Decision(action=Action.BUY, token_id="tokP", market_id="m1",
                       outcome="Yes"), price=0.40, shares=25.0)
    cfg = Config()                       # dry-run by default

    result = run(reconciler(journal, FakeExchange([]), cfg, paper).run(no_mark))

    # The book holds a position the journal never opened a lifecycle for.
    assert result.adopted == 1
    assert journal.open_lifecycles()[0]["token_id"] == "tokP"

    # ...and after adoption the next pass is clean.
    second = run(reconciler(journal, FakeExchange([]), cfg, paper).run(no_mark))
    assert second.clean


# --- bookkeeping ------------------------------------------------------------

def test_the_run_is_persisted_for_status_reporting(journal):
    run(reconciler(journal, FakeExchange([])).run(no_mark))
    assert journal.get_state("reconcile.last") is not None


def test_reconciliation_runs_every_loop_by_default(journal):
    """The safety rule is to never trade on state we have not just verified."""
    r = reconciler(journal, FakeExchange([]))
    assert r.config.reconciliation.every_loop is True
    r.last_run = 1e12
    assert r.due(1e12 + 1) is True


def test_the_interval_applies_only_when_every_loop_is_off(journal):
    cfg = live_config()
    cfg.reconciliation.every_loop = False
    cfg.reconciliation.interval_seconds = 300
    r = reconciler(journal, FakeExchange([]), cfg)

    assert r.due() is True                # never run
    r.last_run = 1e12                     # far in the future
    assert r.due(1e12 + 10) is False
    assert r.due(1e12 + 400) is True


# --- halt classification ----------------------------------------------------

def test_a_freshly_opened_position_is_not_flagged_as_missing(journal):
    """The Data API lags a fill by seconds. Without the grace period,
    halt-on-mismatch would fire on every single entry."""
    add_open_position(journal, "tok1", 10.0, 0.50, age_seconds=0)

    result = run(reconciler(journal, FakeExchange([])).run(no_mark))

    assert result.clean
    assert result.skipped_young == 1
    assert not result.should_halt


def test_an_aged_missing_position_halts(journal):
    add_open_position(journal, "tok1", 10.0, 0.50, age_seconds=3600)

    result = run(reconciler(journal, FakeExchange([])).run(no_mark))

    assert result.should_halt
    assert result.critical[0].kind == "position.missing"


def test_an_unknown_position_halts(journal):
    exchange = FakeExchange(
        [PositionView(token_id="tokX", size=5.0, avg_price=0.30)])
    result = run(reconciler(journal, exchange).run(no_mark))
    assert result.should_halt


def test_size_drift_halts(journal):
    add_open_position(journal, "tok1", 10.0, 0.50)
    exchange = FakeExchange(
        [PositionView(token_id="tok1", size=2.0, avg_price=0.50)])
    result = run(reconciler(journal, exchange).run(no_mark))
    assert result.should_halt


def test_resting_orders_alone_do_not_halt(journal):
    """Informational: odd for a Fill-And-Kill-only engine, and worth an alert,
    but it does not mean our belief about what we hold is wrong."""
    exchange = FakeExchange([], orders=[{"id": "o1"}])
    result = run(reconciler(journal, exchange).run(no_mark))

    assert not result.clean          # it IS reported
    assert not result.should_halt    # ...but it is not a halt condition
    assert result.critical == []


def test_a_clean_pass_never_halts(journal):
    add_open_position(journal, "tok1", 10.0, 0.50)
    exchange = FakeExchange(
        [PositionView(token_id="tok1", size=10.0, avg_price=0.50)])
    result = run(reconciler(journal, exchange).run(no_mark))
    assert result.clean and not result.should_halt
    assert result.summary() == "clean"


def test_paper_mode_mismatches_never_halt(journal):
    """A simulation has no exchange to disagree with. Its mismatches are
    self-healed and journalled, but halting paper trading over its own
    bookkeeping protects nothing — and reads as an exchange problem to an
    operator who is not on an exchange (seen in the field)."""
    from pqb.adapters.execution_adapter import PaperBook

    paper = PaperBook(journal, starting_cash=100.0, reset=True)
    paper.buy(Decision(action=Action.BUY, token_id="tokQ", market_id="m1",
                       outcome="Yes"), price=0.40, shares=25.0)
    cfg = Config()                       # dry-run by default

    result = run(reconciler(journal, FakeExchange([]), cfg, paper).run(no_mark))
    assert result.mismatches               # the mismatch is still visible...
    assert not result.should_halt          # ...but simulation never halts
