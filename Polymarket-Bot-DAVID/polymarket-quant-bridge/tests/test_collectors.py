"""The two backfill collectors: settlements, and non-trade activity.

Both exist because a research result was being capped by a collection gap
rather than by the data's nature — settlement coverage capped every graded
prediction and every realised P&L, and the absence of REDEEM made the
lifecycle gate vacuous. So the tests here are mostly about the failure modes
that would make a collector *look* like it worked:

* a drain whose batches shrink until it reports itself finished after 1% (a
  real bug this file now pins);
* a drain that hammers the same unresolvable batch forever;
* an activity collector that re-imports the trade tape through a second route
  and double-counts it.
"""

from __future__ import annotations

import asyncio

import pytest

from pqb.analytics import activity, settlements
from pqb.analytics.store import IntelStore


class _FakeStore:
    """A store that knows which markets are unresolved, like the real one."""

    def __init__(self, markets, resolvable):
        self.markets = list(markets)
        self.resolvable = set(resolvable)
        self.recorded: dict = {}
        self.queries: list = []

    def markets_without_resolution(self, limit=60):
        self.queries.append(limit)
        pending = [m for m in self.markets if m not in self.recorded]
        return pending[:limit]

    def record_resolution(self, token_id, market_id, price):
        self.recorded[market_id] = price


def _settlements_for(resolvable):
    async def _call(market_ids):
        return {f"{m}-tok": (m, 1.0) for m in market_ids if m in resolvable}
    return _call


# -- settlements -------------------------------------------------------------


def test_the_drain_resolves_what_it_can_and_stops_when_empty():
    store = _FakeStore([f"m{i}" for i in range(10)],
                       resolvable=[f"m{i}" for i in range(10)])
    result = asyncio.run(settlements.drain(
        store, _settlements_for(store.resolvable), batch=4,
        pause_seconds=0.0))
    assert result.settled_markets == 10
    assert result.exhausted is True
    assert len(store.recorded) == 10


def test_batches_do_not_shrink_as_markets_are_parked():
    """The bug that made the first drain report itself finished after 1%.

    Parked markets must not eat the batch limit: the store cannot know what
    this run gave up on, so the drain has to ask for extra room.
    """
    store = _FakeStore([f"m{i}" for i in range(200)], resolvable=[])
    asyncio.run(settlements.drain(
        store, _settlements_for(set()), batch=20, pause_seconds=0.0,
        patience=5))
    # Every request must have asked for at least the batch size, growing to
    # cover what had already been parked.
    assert store.queries[0] == 20
    assert store.queries[-1] > 20, store.queries


def test_a_persistently_barren_backlog_stops_rather_than_looping():
    store = _FakeStore([f"m{i}" for i in range(5_000)], resolvable=[])
    result = asyncio.run(settlements.drain(
        store, _settlements_for(set()), batch=50, pause_seconds=0.0,
        patience=3))
    assert result.batches <= 4
    assert result.settled_markets == 0
    assert result.exhausted is False      # honest: it did NOT finish the job


def test_a_partially_resolvable_backlog_keeps_going_past_empty_batches():
    """Unresolved markets cluster, so one empty batch is not the end."""
    resolvable = {f"m{i}" for i in range(100) if i >= 60}
    store = _FakeStore([f"m{i}" for i in range(100)], resolvable=resolvable)
    result = asyncio.run(settlements.drain(
        store, _settlements_for(resolvable), batch=20, pause_seconds=0.0,
        patience=5))
    assert result.settled_markets == 40


def test_an_erroring_source_gives_up_rather_than_spinning():
    async def _boom(_market_ids):
        raise RuntimeError("rate limited")

    store = _FakeStore([f"m{i}" for i in range(500)], resolvable=[])
    result = asyncio.run(settlements.drain(store, _boom, batch=10,
                                           pause_seconds=0.0))
    assert result.errors >= 5
    assert result.error_samples


# -- activity ----------------------------------------------------------------


def _row(kind, wallet="0xw", market="m1", ts=1_787_000_000, size=10.0):
    return {"proxyWallet": wallet, "timestamp": ts, "conditionId": market,
            "type": kind, "size": size, "usdcSize": size, "price": 0,
            "asset": "", "side": "", "outcome": "Yes",
            "title": "Will it?", "transactionHash": "0xabc"}


def test_trades_are_dropped_so_the_tape_is_never_double_imported():
    """The activity feed carries trades too. Importing them through this
    second route would put two sources in a race for one natural key."""
    rows = [_row("TRADE"), _row("REDEEM"), _row("MERGE"), _row("TRADE")]
    records = activity.to_records(rows, "0xw")
    assert len(records) == 2
    assert {r[-1] for r in records} == {"REDEEM", "MERGE"}


def test_a_redemption_carries_no_side_and_no_price():
    """The feed's own shape, not missing data: a redemption pays out across
    whatever was held, so there is no token and no execution price. Inventing
    one would put a fabricated number in the same column as real ones."""
    record = activity.to_records([_row("REDEEM")], "0xw")[0]
    wallet, ts, market, token, outcome, side, price = record[:7]
    assert wallet == "0xw"
    assert market == "m1"
    assert side == ""
    assert price == 0.0
    assert token == ""


def test_every_non_trade_type_is_captured():
    rows = [_row(k) for k in activity.NON_TRADE_TYPES]
    assert len(activity.to_records(rows, "0xw")) == len(
        activity.NON_TRADE_TYPES)


def test_records_round_trip_through_the_real_store(tmp_path):
    store = IntelStore(tmp_path / "intel.sqlite3")
    rows = [_row("REDEEM", market="mA", ts=1_787_000_100),
            _row("REDEEM", market="mA", ts=1_787_000_500),
            _row("MERGE", market="mB"),
            _row("TRADE", market="mC")]
    stored = store.record_activity(activity.to_records(rows, "0xw"))
    assert stored == 3

    census = store.activity_census()
    assert census.get("REDEEM") == 2
    assert census.get("MERGE") == 1
    assert "TRADE" not in census          # the trade was dropped upstream

    # The lifecycle layer asks for the EARLIEST redemption per condition.
    redemptions = store.redemptions()
    assert redemptions[("0xw", "mA")] == 1_787_000_100
    store.close()


def test_re_running_the_collector_stores_nothing_new(tmp_path):
    store = IntelStore(tmp_path / "intel.sqlite3")
    records = activity.to_records([_row("REDEEM")], "0xw")
    assert store.record_activity(records) == 1
    assert store.record_activity(records) == 0
    store.close()


def test_non_trade_events_never_reach_the_episode_builder(tmp_path):
    """A redemption is an event ABOUT a condition, not a leg of it. If one
    leaked into `load_events` it would be read as a trade with side '' and
    silently corrupt the inventory reconstruction."""
    from pqb.wallet_state_research.events import load_events

    store = IntelStore(tmp_path / "intel.sqlite3")
    store.record_activity(activity.to_records(
        [_row("REDEEM", market="mA"), _row("MERGE", market="mA")], "0xw"))
    import sqlite3

    conn = sqlite3.connect(tmp_path / "intel.sqlite3")
    conn.execute(
        "INSERT INTO wallet_trades(wallet, ts, market_id, token_id, outcome, "
        "side, price, size, usdc, question, tx, source, event_type) "
        "VALUES('0xw', 1787000000, 'mA', 'tokA', 'Yes', 'BUY', 0.5, 10, 5, "
        "'q', '0x1', 'test', 'TRADE')")
    conn.commit()
    conn.close()
    store.close()

    events = load_events(tmp_path / "intel.sqlite3")
    assert len(events) == 1
    assert events[0].side == "BUY"


def test_a_redeemed_condition_is_labelled_finished(tmp_path):
    """The point of the whole backfill: a redemption beats the quiet-period
    heuristic, because it is a fact rather than an inference."""
    from pqb.wallet_state_research.episodes import build_episodes
    from pqb.wallet_state_research.events import WalletEvent

    T0 = 1_787_000_000.0
    events = [
        WalletEvent(wallet="0xw", market_id="mA", token_id="y", outcome="Yes",
                    side="BUY", ts=T0, price=0.5, shares=10.0, usdc=5.0),
        WalletEvent(wallet="0xw", market_id="mA", token_id="n", outcome="No",
                    side="BUY", ts=T0 + 60, price=0.5, shares=20.0, usdc=10.0),
    ]
    # Tape ends one minute later: the quiet rule would call this truncated.
    truncated = build_episodes(events, tape_end_ts=T0 + 120)[0]
    assert truncated.label_quality == "truncated"
    assert truncated.labelled is False

    redeemed = build_episodes(events, tape_end_ts=T0 + 120,
                              redemptions={("0xw", "mA"): T0 + 90})[0]
    assert redeemed.label_quality == "redeemed"
    assert redeemed.labelled is True
    assert redeemed.redeemed_ts == T0 + 90


def test_the_v1_redeem_gate_binds_once_redemptions_exist(tmp_path):
    """§13's 'no REDEEM before the prediction', with real data behind it."""
    from pqb.wallet_state_research.episodes import build_episodes
    from pqb.wallet_state_research.events import WalletEvent
    from pqb.wallet_state_research.strategy_v1 import (R_REDEEM_FIRST,
                                                       eligibility)

    T0 = 1_787_300_000.0
    events = [WalletEvent(wallet="0xw", market_id="mA", token_id="y",
                          outcome="Yes", side="BUY", ts=T0, price=0.5,
                          shares=10.0, usdc=6.0)]
    episode = build_episodes(events, tape_end_ts=T0 + 30 * 86_400,
                             redemptions={("0xw", "mA"): T0 - 60})[0]

    def _redeemed_before(e):
        return bool(e.redeemed_ts) and e.redeemed_ts < e.first_buy_ts

    gate = eligibility(episode, has_redeem_before=_redeemed_before)
    assert gate.eligible is False
    assert gate.reason == R_REDEEM_FIRST
