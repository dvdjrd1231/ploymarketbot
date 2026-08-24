"""Phase-0 safety controls: idempotency, the single-order cap, and entry-block.

These are the §3 non-negotiables that were gaps: a client-generated order id so
an identical order cannot be sent twice, a hard per-order size cap, and
exposure / daily-loss limits that stop NEW ENTRIES while still letting exits run.
"""

from __future__ import annotations

import asyncio
import types

from pqb.adapters.execution_adapter import PolymarketExecutionAdapter
from pqb.config import Config
from pqb.logs import Log
from pqb.models import (
    Action, Decision, MarketFeatures, MarketStatus, OutcomeQuote, PositionView,
)
from pqb.runner import Runner
from pqb.adapters.sizing import SizedOrder


def run(coro):
    return asyncio.run(coro)


def market(*, bid=0.49, ask=0.50, token_id="tok1", depth=10_000.0):
    return MarketFeatures(
        market_id="m1", question="Will it?", status=MarketStatus.ACTIVE,
        quotes={token_id: OutcomeQuote(
            token_id=token_id, outcome="Yes", bid=bid, ask=ask,
            spread=round(ask - bid, 4), source="stream",
            bid_depth=depth, ask_depth=depth, tick_size=0.01)},
        liquidity=50_000.0)


def buy(usdc=20.0):
    return Decision(action=Action.BUY, token_id="tok1", market_id="m1",
                    outcome="Yes", size_usdc=usdc)


def adapter(journal):
    return PolymarketExecutionAdapter(Config(), Log(), journal)


# -- idempotent client order id ---------------------------------------------

def test_client_order_id_is_deterministic(journal):
    a = adapter(journal)
    d = buy()
    sized = SizedOrder(True, price=0.50, shares=40.0, usdc=20.0)
    assert a._client_order_id(d, sized) == a._client_order_id(d, sized)


def test_client_order_id_changes_with_size_or_price(journal):
    a = adapter(journal)
    d = buy()
    base = a._client_order_id(d, SizedOrder(True, price=0.50, shares=40.0, usdc=20.0))
    diff_size = a._client_order_id(d, SizedOrder(True, price=0.50, shares=60.0, usdc=30.0))
    diff_price = a._client_order_id(d, SizedOrder(True, price=0.55, shares=40.0, usdc=22.0))
    assert base != diff_size
    assert base != diff_price


def test_idempotency_ledger_expires(journal):
    a = adapter(journal)
    a.config.risk.idempotency_ttl_seconds = 120
    a._record_order("pqb-abc")
    assert a._is_duplicate("pqb-abc")
    # Age the record past the window; it should no longer count as a duplicate.
    a._recent_orders["pqb-abc"] = 0.0
    assert not a._is_duplicate("pqb-abc")


def test_ttl_zero_disables_dedup(journal):
    a = adapter(journal)
    a.config.risk.idempotency_ttl_seconds = 0
    a._record_order("pqb-abc")
    assert not a._is_duplicate("pqb-abc")


# -- single-order cap --------------------------------------------------------

def test_a_buy_over_the_single_order_cap_is_rejected(journal):
    a = adapter(journal)
    a.config.risk.max_single_order_usdc = 5.0
    report = run(a.execute(buy(usdc=20.0), market(), None,
                           available_cash=1000.0, min_trade_size=1.0))
    assert report.status == "REJECTED_RISK"
    assert not report.submitted


def test_a_buy_under_the_cap_is_allowed(journal):
    a = adapter(journal)
    a.config.risk.max_single_order_usdc = 100.0
    report = run(a.execute(buy(usdc=20.0), market(), None,
                           available_cash=1000.0, min_trade_size=1.0))
    assert report.status != "REJECTED_RISK"


# -- entry-block (exposure / daily loss) -------------------------------------

def _runner(tmp_path) -> Runner:
    cfg = Config()
    cfg.root = tmp_path
    return Runner(cfg, Log())


def _account(value):
    return types.SimpleNamespace(portfolio_value=value, balance=value)


def test_open_exposure_limit_blocks_entries(tmp_path):
    r = _runner(tmp_path)
    r.config.risk.max_open_exposure_usdc = 50.0
    positions = [PositionView(token_id="t", market_id="m", size=200.0,
                              avg_price=0.50)]   # exposure = $100 > $50
    reason = r._entry_block(_account(100.0), positions)
    assert "exposure" in reason


def test_daily_loss_limit_blocks_entries(tmp_path):
    r = _runner(tmp_path)
    r.config.risk.max_daily_loss_usdc = 10.0
    r._entry_block(_account(100.0), [])       # anchors the day at 100
    reason = r._entry_block(_account(85.0), [])  # down $15 > $10 limit
    assert "daily loss" in reason


def test_no_block_when_limits_are_zero(tmp_path):
    r = _runner(tmp_path)
    positions = [PositionView(token_id="t", market_id="m", size=1_000.0,
                              avg_price=0.9)]
    assert r._entry_block(_account(10.0), positions) == ""
