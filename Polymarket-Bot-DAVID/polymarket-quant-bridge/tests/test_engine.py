"""
Decision-engine behaviour (prompt section 4), with 4.1 — exit management — as
the bulk of it.

These pin the *structure* the real Quant Bridge engine must also honour, not the
reference engine's particular thresholds: every position adjudicated every
cycle, exits ranked worst-case first, and a target wallet treated as weighted
evidence that quantitative conviction can override.
"""

from __future__ import annotations

import time

import pytest

from pqb.bridge.baseline_engine import BaselineDecisionEngine
from pqb.config import EngineConfig
from pqb.models import (
    AccountState, Action, BridgeContext, MarketFeatures, MarketStatus,
    OutcomeQuote, PositionView, WalletSignal,
)

NOW = time.time()


def market(market_id="m1", *, bid=0.50, ask=0.51, end_in=86_400,
           liquidity=50_000.0, status=MarketStatus.ACTIVE,
           token_id="tok1", second_token=None) -> MarketFeatures:
    quotes = {token_id: OutcomeQuote(
        token_id=token_id, outcome="Yes", bid=bid, ask=ask,
        mid=(bid + ask) / 2, spread=round(ask - bid, 4), source="stream",
        bid_depth=1_000.0, ask_depth=1_000.0, updated_ts=NOW)}
    if second_token:
        quotes[second_token] = OutcomeQuote(
            token_id=second_token, outcome="No", bid=round(1 - ask, 2),
            ask=round(1 - bid, 2), mid=0.5, spread=round(ask - bid, 4),
            source="stream", bid_depth=1_000.0, ask_depth=1_000.0,
            updated_ts=NOW)
    return MarketFeatures(
        market_id=market_id, question="Will it?", category="Test",
        status=status, end_ts=int(NOW + end_in) if end_in else None,
        quotes=quotes, liquidity=liquidity, volume_24h=30_000.0,
        volume_total=100_000.0)


def position(*, entry=0.50, mark=0.50, peak=0.0, size=100.0, market_id="m1",
             token_id="tok1", end_in=86_400, reduced=0) -> PositionView:
    view = PositionView(
        token_id=token_id, market_id=market_id, outcome="Yes",
        question="Will it?", size=size, avg_price=entry, cur_price=mark,
        opened_ts=int(NOW - 3600), end_ts=int(NOW + end_in) if end_in else None,
        peak_price=peak or max(entry, mark), lifecycle_id=1)
    setattr(view, "reduced_count", reduced)
    return view


def context(*, positions=None, markets=None, signals=None, balance=100.0,
            flattening=False, flatten_reason="", drawdown=0.0, wallets=0,
            min_trade=0.19) -> BridgeContext:
    return BridgeContext(
        cycle_id="c1", ts=NOW,
        account=AccountState(balance=balance, position_value=0.0),
        markets={m.market_id: m for m in (markets or [])},
        positions=positions or [], wallet_signals=signals or [],
        min_trade_size=min_trade, flattening=flattening,
        flatten_reason=flatten_reason,
        portfolio_drawdown=drawdown, tracked_wallets=wallets)


def engine(**overrides) -> BaselineDecisionEngine:
    cfg = EngineConfig()
    for key, value in overrides.items():        # e.g. exits__stop_loss_pct
        section, _, field = key.partition("__")
        setattr(getattr(cfg, section), field, value)
    return BaselineDecisionEngine(cfg)


def decision_for(decisions, token_id):
    return next(d for d in decisions if d.token_id == token_id)


# --- the contract -----------------------------------------------------------

def test_every_open_position_is_adjudicated_every_cycle():
    positions = [position(token_id="a", market_id="m1"),
                 position(token_id="b", market_id="m2")]
    markets = [market("m1", token_id="a"), market("m2", token_id="b")]
    decisions = engine().evaluate(context(positions=positions, markets=markets))

    for token in ("a", "b"):
        assert decision_for(decisions, token).action in (
            Action.HOLD, Action.EXIT, Action.REDUCE)


def test_a_cycle_with_no_candidates_still_says_so():
    """Silence and "nothing qualified" must not look the same in the journal."""
    decisions = engine(entry__min_score=0.99).evaluate(
        context(markets=[market(liquidity=6_000.0)]))
    assert len(decisions) == 1
    assert decisions[0].action is Action.NOTHING
    assert decisions[0].rationale["gate"] == "no_candidates"
    # The counts are the point: they distinguish "everything was stale" from
    # "everything scored just under the bar".
    assert decisions[0].rationale["scanned"] == 1
    assert decisions[0].rationale["belowScore"] == 1
    assert 0.0 < decisions[0].rationale["bestScore"] < 0.99


# --- exit management --------------------------------------------------------

def test_stop_loss_exits():
    decisions = engine(exits__stop_loss_pct=0.25).evaluate(
        context(positions=[position(entry=0.50, mark=0.30)],
                markets=[market(bid=0.30, ask=0.31)]))
    assert decisions[0].action is Action.EXIT
    assert decisions[0].exit_style == "stop"


def test_take_profit_exits():
    decisions = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.70)],
                markets=[market(bid=0.70, ask=0.71)]))
    assert decisions[0].action is Action.EXIT
    assert decisions[0].exit_style == "take_profit"


def test_trailing_stop_fires_only_after_it_is_armed():
    # +4% peak then a giveback: below trailing_arm_pct, so it must not fire.
    unarmed = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.50, peak=0.52)],
                markets=[market(bid=0.50, ask=0.51)]))
    assert unarmed[0].action is not Action.EXIT

    # +40% peak, then 25% given back: armed, and past the threshold.
    armed = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.525, peak=0.70)],
                markets=[market(bid=0.52, ask=0.53)]))
    assert armed[0].action is Action.EXIT
    assert armed[0].exit_style == "trailing"


def test_stop_loss_outranks_profit_taking():
    """Worst case first: a position that is both stopped out and (per a stale
    peak) trailing must exit on the stop, and the journal must say so."""
    decisions = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.20, peak=0.90)],
                markets=[market(bid=0.20, ask=0.21)]))
    assert decisions[0].exit_style == "stop"


def test_reduce_at_the_partial_threshold():
    decisions = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.61, peak=0.61)],
                markets=[market(bid=0.61, ask=0.62)]))
    assert decisions[0].action is Action.REDUCE
    assert decisions[0].exit_style == "reduce"


def test_reduce_fires_only_once_per_position():
    decisions = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.61, peak=0.61,
                                    reduced=1)],
                markets=[market(bid=0.61, ask=0.62)]))
    assert decisions[0].action is not Action.REDUCE


def test_a_resolved_market_is_exited_regardless_of_pnl():
    decisions = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.51)],
                markets=[market(status=MarketStatus.RESOLVED)]))
    assert decisions[0].action is Action.EXIT
    assert decisions[0].exit_style == "resolution"


def test_flattening_exits_everything():
    decisions = engine().evaluate(
        context(positions=[position()], markets=[market()], flattening=True,
                flatten_reason="doubling"))
    assert decisions[0].action is Action.EXIT
    assert decisions[0].exit_style == "doubling"


def test_a_kill_switch_flatten_is_not_journalled_as_a_doubling_exit():
    """Both close the whole book, but they are different events — and the
    performance report groups by exit style, so conflating them would
    misattribute every kill-switch close."""
    decisions = engine().evaluate(
        context(positions=[position()], markets=[market()], flattening=True,
                flatten_reason="kill_switch"))
    assert decisions[0].action is Action.EXIT
    assert decisions[0].exit_style == "kill_switch"
    assert "kill switch" in decisions[0].reason.lower()


def test_near_resolution_policy_is_configurable():
    ctx = context(positions=[position(entry=0.50, mark=0.52, end_in=600)],
                  markets=[market(bid=0.52, ask=0.53, end_in=600)])
    assert engine(exits__near_resolution_action="hold").evaluate(ctx)[0].action \
        is Action.HOLD
    exited = engine(exits__near_resolution_action="exit").evaluate(ctx)[0]
    assert exited.action is Action.EXIT
    assert exited.exit_style == "time_decay"


def test_max_hold_time_exits():
    decisions = engine(exits__max_hold_seconds=60).evaluate(
        context(positions=[position()], markets=[market()]))
    assert decisions[0].action is Action.EXIT
    assert decisions[0].exit_style == "time"


def test_every_exit_carries_its_reasoning():
    decisions = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.30)],
                markets=[market(bid=0.30, ask=0.31)]))
    rationale = decisions[0].rationale
    for key in ("returnPct", "conviction", "thresholds", "markPrice"):
        assert key in rationale


# --- wallets are a signal, not a command ------------------------------------

def exit_signal(token_id="tok1", weight=1.0) -> WalletSignal:
    return WalletSignal(wallet="0xabc", label="alpha", weight=weight,
                        action="EXIT", token_id=token_id, market_id="m1",
                        price=0.55, size=10.0, usdc=5.5, timestamp=int(NOW))


def test_a_wallet_exit_is_followed_when_conviction_is_low():
    # Slightly under water and drifting: no risk rule fires, and conviction is
    # below the override bar, so the wallet's exit is the deciding input.
    decisions = engine(exits__wallet_exit_override_score=0.65).evaluate(
        context(positions=[position(entry=0.50, mark=0.49, peak=0.52)],
                markets=[market(bid=0.49, ask=0.50)],
                signals=[exit_signal()], wallets=1))
    assert decisions[0].action is Action.EXIT
    assert decisions[0].exit_style == "wallet"
    assert decisions[0].wallet_influence == "alpha"
    assert decisions[0].rationale["override"] is False


def test_a_wallet_exit_is_overridden_when_conviction_is_high():
    decisions = engine(exits__wallet_exit_override_score=0.30).evaluate(
        context(positions=[position(entry=0.50, mark=0.58, peak=0.58)],
                markets=[market(bid=0.58, ask=0.59)],
                signals=[exit_signal()], wallets=1))
    assert decisions[0].action is Action.HOLD
    assert decisions[0].exit_style == "wallet_override"
    assert decisions[0].rationale["override"] is True


def test_wallet_following_can_be_switched_off_entirely():
    decisions = engine(exits__follow_wallet_exit=False).evaluate(
        context(positions=[position(entry=0.50, mark=0.49, peak=0.52)],
                markets=[market(bid=0.49, ask=0.50)],
                signals=[exit_signal()], wallets=1))
    assert decisions[0].action is not Action.EXIT


def test_stale_wallet_signals_are_ignored():
    stale = exit_signal()
    stale.timestamp = int(NOW - 10_000)
    decisions = engine(entry__signal_ttl_seconds=1_800).evaluate(
        context(positions=[position(entry=0.50, mark=0.49, peak=0.52)],
                markets=[market(bid=0.49, ask=0.50)],
                signals=[stale], wallets=1))
    assert decisions[0].exit_style != "wallet"


# --- entries ----------------------------------------------------------------

def test_entries_are_blocked_while_flattening():
    decisions = engine().evaluate(context(markets=[market()], flattening=True))
    assert all(d.action is Action.NOTHING for d in decisions)
    assert "flatten" in decisions[0].reason.lower()


def test_entries_are_blocked_past_the_drawdown_limit():
    decisions = engine(portfolio__max_drawdown_pct=0.30).evaluate(
        context(markets=[market()], drawdown=0.35))
    assert decisions[0].action is Action.NOTHING
    assert "drawdown" in decisions[0].reason.lower()


def test_entries_are_blocked_without_enough_cash():
    decisions = engine().evaluate(context(markets=[market()], balance=0.10))
    assert decisions[0].action is Action.NOTHING
    assert "cash" in decisions[0].reason.lower()


def test_only_one_outcome_per_market_is_funded():
    """Both sides of a binary market sum to ~1.00: holding both locks in a loss."""
    decisions = engine(entry__min_score=0.1).evaluate(
        context(markets=[market(token_id="yes", second_token="no")],
                balance=1_000.0))
    buys = [d for d in decisions if d.action is Action.BUY]
    assert len(buys) == 1


def test_the_batch_never_commits_more_cash_than_exists():
    markets = [market(f"m{i}", token_id=f"tok{i}") for i in range(8)]
    decisions = engine(entry__min_score=0.1,
                       portfolio__max_open_positions=8).evaluate(
        context(markets=markets, balance=100.0))
    committed = sum(d.size_usdc for d in decisions if d.action is Action.BUY)
    assert committed <= 100.0 * (1 - EngineConfig().portfolio.reserve_cash_fraction) + 1e-9


def test_a_stale_quote_is_not_traded_on():
    stale = market()
    stale.quotes["tok1"].source = "none"
    decisions = engine(entry__min_score=0.1).evaluate(
        context(markets=[stale], balance=100.0))
    assert not [d for d in decisions if d.action is Action.BUY]


def test_wide_spreads_are_skipped():
    decisions = engine(entry__min_score=0.1, entry__max_spread=0.02).evaluate(
        context(markets=[market(bid=0.40, ask=0.60)], balance=100.0))
    assert not [d for d in decisions if d.action is Action.BUY]


def test_tail_prices_are_skipped():
    decisions = engine(entry__min_score=0.1).evaluate(
        context(markets=[market(bid=0.97, ask=0.98)], balance=100.0))
    assert not [d for d in decisions if d.action is Action.BUY]


def test_no_configured_wallets_does_not_veto_every_entry():
    """The wallet term is undefined with no wallets tracked, not zero.

    Blending in a structural zero would cap every score at (1 - weight) and
    silently disable entries in a config that looks armed.
    """
    ctx = context(markets=[market(liquidity=200_000.0)], balance=1_000.0,
                  wallets=0)
    decisions = engine(entry__wallet_signal_weight=0.9,
                       entry__min_score=0.55).evaluate(ctx)
    assert [d for d in decisions if d.action is Action.BUY]


def test_require_wallet_signal_suppresses_unsupported_entries():
    decisions = engine(entry__require_wallet_signal=True,
                       entry__min_score=0.1).evaluate(
        context(markets=[market()], balance=1_000.0, wallets=1))
    assert not [d for d in decisions if d.action is Action.BUY]


def test_position_limit_marks_the_surplus_as_no_action():
    markets = [market(f"m{i}", token_id=f"tok{i}") for i in range(5)]
    decisions = engine(entry__min_score=0.1,
                       portfolio__max_open_positions=2).evaluate(
        context(markets=markets, balance=1_000.0))
    assert len([d for d in decisions if d.action is Action.BUY]) == 2
    surplus = [d for d in decisions
               if d.action is Action.NOTHING and d.token_id]
    assert surplus and "limit is full" in surplus[0].reason


# --- marking a position while its market closes -----------------------------

def test_a_closing_market_is_not_marked_from_the_book():
    """A stale book near resolution once marked 191 shares bought at $0.13 at
    $1.00 — a +669% paper gain on a token that settled at zero minutes later,
    inflating the account by $167. Portfolio value drives the doubling rule, so
    a false spike past 2x baseline would flatten everything for nothing."""
    import inspect
    from pqb.runner import Runner
    src = inspect.getsource(Runner._mark_for)
    assert "info.resolved or info.closed" in src
    assert "resolved_price" in src
    assert "_last_trade_price" in src
    # And the book remains the source for markets that are still trading.
    assert "self.data.prices.mark" in src


# --- the stagnation exit (toggle) --------------------------------------------

def test_stagnation_is_off_by_default():
    """A flat, hours-old position holds unless the operator flips the toggle."""
    decisions = engine().evaluate(
        context(positions=[position(entry=0.50, mark=0.50, end_in=1800)],
                markets=[market(end_in=1800)]))
    assert decision_for(decisions, "tok1").action is Action.HOLD


def test_a_stagnant_position_in_a_fast_market_is_pruned():
    """Held 1h flat in a market resolving within the hour: window is minutes."""
    decisions = engine(exits__stagnation_enabled=True).evaluate(
        context(positions=[position(entry=0.50, mark=0.50, end_in=1800)],
                markets=[market(end_in=1800)]))
    verdict = decision_for(decisions, "tok1")
    assert verdict.action is Action.EXIT
    assert verdict.exit_style == "stagnant"


def test_a_moving_position_is_not_stagnant():
    """+4% is momentum; the window does not apply."""
    decisions = engine(exits__stagnation_enabled=True).evaluate(
        context(positions=[position(entry=0.50, mark=0.52, end_in=1800)],
                markets=[market(end_in=1800, bid=0.52, ask=0.53)]))
    assert decision_for(decisions, "tok1").action is Action.HOLD


def test_long_horizon_markets_get_the_long_window():
    """Held 1h in a week-long market: the 1.5h event window has not closed."""
    decisions = engine(exits__stagnation_enabled=True).evaluate(
        context(positions=[position(entry=0.50, mark=0.50, end_in=7 * 86_400)],
                markets=[market(end_in=7 * 86_400)]))
    assert decision_for(decisions, "tok1").action is Action.HOLD


def test_stop_loss_outranks_stagnation():
    """A real stop is a stop, not a stagnation prune."""
    decisions = engine(exits__stagnation_enabled=True).evaluate(
        context(positions=[position(entry=0.50, mark=0.35, end_in=1800)],
                markets=[market(end_in=1800, bid=0.35, ask=0.36)]))
    verdict = decision_for(decisions, "tok1")
    assert verdict.action is Action.EXIT
    assert verdict.exit_style == "stop"
