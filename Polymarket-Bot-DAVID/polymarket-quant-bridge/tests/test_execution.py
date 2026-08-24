"""
Execution adapter behaviour (prompt section 6).

The theme is: an order is only ever sent at a price someone is actually
quoting. Every test here exists because the alternative — inventing a price
when the book is unavailable — produces an order that cannot fill while the
journal records a clean fill that never happened.
"""

from __future__ import annotations

import asyncio

import pytest

from pqb.adapters.execution_adapter import PaperBook, PolymarketExecutionAdapter
from pqb.config import Config
from pqb.logs import Log
from pqb.models import (
    Action, Decision, MarketFeatures, MarketStatus, OutcomeQuote, PositionView,
)


def run(coro):
    return asyncio.run(coro)


def market(*, bid=None, ask=None, token_id="tok1", source="stream",
           status=MarketStatus.ACTIVE, depth=1_000.0) -> MarketFeatures:
    spread = None if (bid is None or ask is None) else round(ask - bid, 4)
    return MarketFeatures(
        market_id="m1", question="Will it?", status=status,
        quotes={token_id: OutcomeQuote(
            token_id=token_id, outcome="Yes", bid=bid, ask=ask, spread=spread,
            source=source, bid_depth=depth, ask_depth=depth, tick_size=0.01)},
        liquidity=50_000.0)


def empty_book_market(token_id="tok1") -> MarketFeatures:
    """A market present in the feature set whose book has not arrived yet.

    Real, and the case that matters: the first cycle after a restart, or a
    market just pinned because we hold a position in it.
    """
    return market(bid=None, ask=None, token_id=token_id, source="none")


def position(size=100.0, entry=0.50, mark=0.50) -> PositionView:
    return PositionView(token_id="tok1", market_id="m1", outcome="Yes",
                        size=size, avg_price=entry, cur_price=mark,
                        lifecycle_id=1)


def adapter(journal, price_fn=None, paper=None) -> PolymarketExecutionAdapter:
    return PolymarketExecutionAdapter(Config(), Log(), journal, paper=paper,
                                      price_fn=price_fn)


def buy(usdc=20.0) -> Decision:
    return Decision(action=Action.BUY, token_id="tok1", market_id="m1",
                    outcome="Yes", size_usdc=usdc)


def exit_decision(style="stop") -> Decision:
    return Decision(action=Action.EXIT, token_id="tok1", market_id="m1",
                    outcome="Yes", exit_style=style)


# --- the entry-price fallback must not exist --------------------------------

def test_an_exit_is_refused_rather_than_priced_from_the_entry_price(journal):
    """With no bid and no way to fetch one, do not invent a price.

    Pricing the exit at the position's own entry produces a sell limit above
    the market: a Fill-And-Kill order is killed, nothing exits, and the journal
    would otherwise record a tidy zero-P&L close that never occurred.
    """
    ex = adapter(journal, price_fn=None)
    report = run(ex.execute(exit_decision(), empty_book_market(), position(),
                            available_cash=0.0, min_trade_size=0.19))

    assert not report.submitted
    assert report.status == "INVALID"
    assert "entry price" in report.error.lower()


def test_an_exit_refetches_the_bid_when_the_book_is_cold(journal):
    """A ~100ms REST round trip beats guessing what an order will fill at."""
    calls = []

    async def price_fn(token_id, side):
        calls.append((token_id, side.value))
        return 0.42

    ex = adapter(journal, price_fn=price_fn)
    report = run(ex.execute(exit_decision(), empty_book_market(), position(),
                            available_cash=0.0, min_trade_size=0.19))

    assert calls == [("tok1", "SELL")]
    assert report.ok
    assert report.avg_price == 0.42          # the refetched bid, not the entry
    assert report.filled_size == 100.0


def test_a_refetch_that_finds_nothing_still_refuses(journal):
    async def price_fn(_token, _side):
        return None

    ex = adapter(journal, price_fn=price_fn)
    report = run(ex.execute(exit_decision(), empty_book_market(), position(),
                            available_cash=0.0, min_trade_size=0.19))
    assert not report.submitted
    assert report.status == "INVALID"


def test_a_failing_refetch_is_not_fatal(journal):
    async def price_fn(_token, _side):
        raise RuntimeError("network down")

    ex = adapter(journal, price_fn=price_fn)
    report = run(ex.execute(exit_decision(), empty_book_market(), position(),
                            available_cash=0.0, min_trade_size=0.19))
    assert not report.submitted        # refused, not crashed


def test_a_buy_is_refused_when_there_is_no_ask(journal):
    ex = adapter(journal, price_fn=None)
    report = run(ex.execute(buy(), empty_book_market(), None,
                            available_cash=100.0, min_trade_size=0.19))
    assert not report.submitted
    assert "ask" in report.error.lower()


def test_the_live_book_is_preferred_over_a_refetch(journal):
    """No round trip when the streamed book already has the touch."""
    called = False

    async def price_fn(_token, _side):
        nonlocal called
        called = True
        return 0.99

    ex = adapter(journal, price_fn=price_fn)
    report = run(ex.execute(exit_decision(), market(bid=0.44, ask=0.45),
                            position(), available_cash=0.0,
                            min_trade_size=0.19))
    assert not called
    assert report.avg_price == 0.44


# --- the spread must actually cost something --------------------------------

def test_a_round_trip_across_the_spread_loses_the_spread(journal):
    """Buy at the ask, sell at the bid: a flat market is a small loss, never
    a clean zero. A zero here means a price was invented somewhere."""
    paper = PaperBook(journal, starting_cash=100.0, reset=True)
    ex = adapter(journal, paper=paper)
    book = market(bid=0.44, ask=0.45)

    entry = run(ex.execute(buy(usdc=45.0), book, None, available_cash=100.0,
                           min_trade_size=0.19))
    assert entry.ok and entry.avg_price == 0.45

    held = position(size=entry.filled_size, entry=entry.avg_price, mark=0.44)
    held.lifecycle_id = 1
    out = run(ex.execute(exit_decision(), book, held, available_cash=0.0,
                         min_trade_size=0.19))
    assert out.ok and out.avg_price == 0.44
    pnl = (out.avg_price - entry.avg_price) * out.filled_size
    assert pnl < 0


# --- duplicate protection ---------------------------------------------------

def test_a_second_order_for_the_same_token_is_refused(journal):
    """The in-flight guard, exercised through two concurrent executions."""
    started = asyncio.Event()

    async def slow_price(_token, _side):
        started.set()
        await asyncio.sleep(0.05)
        return 0.44

    async def scenario():
        ex = adapter(journal, price_fn=slow_price)
        book = empty_book_market()
        first = asyncio.create_task(
            ex.execute(exit_decision(), book, position(), 0.0, 0.19))
        await started.wait()
        await asyncio.sleep(0)          # let the first claim the token
        second = await ex.execute(exit_decision(), book, position(), 0.0, 0.19)
        return await first, second

    first, second = run(scenario())
    assert first.ok
    assert second.status == "SKIPPED_IN_FLIGHT"


def test_a_non_actionable_decision_does_nothing(journal):
    ex = adapter(journal)
    report = run(ex.execute(Decision(action=Action.HOLD, token_id="tok1"),
                            market(bid=0.44, ask=0.45), position(), 0.0, 0.19))
    assert report.status == "NO_ACTION"
    assert not report.submitted


def test_a_closed_market_cannot_be_bought_into(journal):
    ex = adapter(journal)
    report = run(ex.execute(buy(), market(bid=0.44, ask=0.45,
                                          status=MarketStatus.CLOSED),
                            None, available_cash=100.0, min_trade_size=0.19))
    assert not report.submitted
    assert "tradable" in report.error.lower()


# --- the simulator respects visible depth -----------------------------------

def test_a_simulated_fill_is_capped_by_visible_depth(journal):
    """Filling the full size regardless of depth would make dry-run flattest
    on exactly the thin markets where slippage matters most."""
    paper = PaperBook(journal, starting_cash=100.0, reset=True)
    ex = adapter(journal, paper=paper)
    report = run(ex.execute(buy(usdc=45.0), market(bid=0.44, ask=0.45, depth=10.0),
                            None, available_cash=100.0, min_trade_size=0.19))
    assert report.filled_size == 10.0
    assert report.status == "SIMULATED_PARTIAL"


# --- fees are charged on both sides and recorded ---------------------------

def _fee_book(tmp_path, fee=0.01, cash=100.0):
    from pqb.adapters.execution_adapter import PaperBook
    from conftest import MemoryStore
    return PaperBook(MemoryStore(), starting_cash=cash, fee_per_trade=fee)


def test_buy_charges_a_fee(tmp_path):
    book = _fee_book(tmp_path)
    book.buy(buy(), price=0.50, shares=10.0)
    # 10 shares at 0.50 = $5.00, plus the 1c fee.
    assert round(book.cash, 4) == round(100.0 - 5.0 - 0.01, 4)
    assert book.fees_paid == 0.01


def test_sell_charges_a_fee_and_it_lands_in_the_pnl(tmp_path):
    book = _fee_book(tmp_path)
    book.buy(buy(), price=0.50, shares=10.0)
    pnl = book.sell("tok1", price=0.50, shares=10.0)
    # Flat round trip: the only thing that changed hands is two fees, and the
    # reported P&L must show the exit fee rather than a tidy zero.
    assert pnl == -0.01
    assert round(book.fees_paid, 4) == 0.02
    assert round(book.cash, 4) == round(100.0 - 0.02, 4)


def test_fees_survive_a_restart(tmp_path):
    from pqb.adapters.execution_adapter import PaperBook
    from conftest import MemoryStore
    store = MemoryStore()
    first = PaperBook(store, starting_cash=100.0, fee_per_trade=0.01)
    first.buy(buy(), price=0.50, shares=10.0)
    again = PaperBook(store, starting_cash=100.0, fee_per_trade=0.01)
    assert again.fees_paid == 0.01
    assert round(again.cash, 4) == round(first.cash, 4)


# --- the fee fund ------------------------------------------------------------

def test_fee_fund_replenishes_at_ten_percent(journal):
    """The operator's rule: fee money refills when it drops to 10% of start."""
    paper = PaperBook(journal, starting_cash=100.0, reset=True,
                      fee_per_trade=0.10, fee_fund=1.00)
    d = Decision(action=Action.BUY, token_id="tokF", market_id="m1",
                 outcome="Yes")
    # 9 fills x $0.10 = fund at $0.10 = the 10% floor -> refills to $1.00.
    for _ in range(9):
        paper.buy(d, price=0.50, shares=1.0)
    assert paper.fee_fund_topups == 1
    assert paper.fee_fund == 1.00
    assert round(paper.fees_paid, 2) == 0.90


def test_fee_fund_state_survives_restart(journal):
    paper = PaperBook(journal, starting_cash=100.0, reset=True,
                      fee_per_trade=0.10, fee_fund=1.00)
    d = Decision(action=Action.BUY, token_id="tokF", market_id="m1",
                 outcome="Yes")
    paper.buy(d, price=0.50, shares=1.0)
    reloaded = PaperBook(journal, starting_cash=100.0, reset=False,
                         fee_per_trade=0.10, fee_fund=1.00)
    assert round(reloaded.fee_fund, 2) == 0.90


def test_zero_fund_disables_the_mechanism(journal):
    paper = PaperBook(journal, starting_cash=100.0, reset=True,
                      fee_per_trade=0.10, fee_fund=0.0)
    d = Decision(action=Action.BUY, token_id="tokF", market_id="m1",
                 outcome="Yes")
    paper.buy(d, price=0.50, shares=1.0)
    assert paper.fee_fund_topups == 0
