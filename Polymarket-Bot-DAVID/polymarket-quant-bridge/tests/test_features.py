"""The feature vector — the contract shared by research and live decisions."""

from __future__ import annotations

import math
import time

from pqb.analytics.anomalies import KINDS
from pqb.features import FEATURE_NAMES, position_features, token_features
from pqb.models import (
    AccountState, AnomalySignal, BridgeContext, MarketFeatures, MarketIntel,
    MarketStatus, OutcomeQuote, PositionView, WalletIntel, WalletSignal,
)


def quote(**kwargs) -> OutcomeQuote:
    base = dict(token_id="T1", outcome="Yes", bid=0.44, ask=0.46, mid=0.45,
                last=0.45, spread=0.02, bid_depth=800.0, ask_depth=400.0,
                tick_size=0.01, source="stream", updated_ts=time.time())
    base.update(kwargs)
    return OutcomeQuote(**base)


def market(quote_override: OutcomeQuote | None = None,
           **kwargs) -> MarketFeatures:
    base = dict(market_id="M1", question="Will it?", category="Politics",
                status=MarketStatus.ACTIVE, end_ts=int(time.time()) + 86400,
                liquidity=40_000.0, volume_total=500_000.0, volume_24h=50_000.0)
    base.update(kwargs)
    m = MarketFeatures(**base)
    m.quotes = {"T1": quote_override or quote()}
    return m


def context(**kwargs) -> BridgeContext:
    base = dict(cycle_id="c1", ts=time.time(), account=AccountState(balance=100.0))
    base.update(kwargs)
    return BridgeContext(**base)


# --- the contract -----------------------------------------------------------

def test_every_declared_column_is_always_present():
    row = token_features(market(), quote())
    assert set(row) == set(FEATURE_NAMES)


def test_column_order_is_stable():
    assert len(FEATURE_NAMES) == len(set(FEATURE_NAMES))
    assert FEATURE_NAMES[0] == "price"


def test_values_are_always_finite():
    # A rule comparing against NaN evaluates false for every row, which looks
    # like a rule that never triggers rather than one whose input is missing.
    hostile = market(quote_override=quote(bid=None, ask=None, mid=None,
                                          last=None, spread=None,
                                          bid_depth=0.0, ask_depth=0.0,
                                          source="none", updated_ts=0.0),
                     liquidity=0.0, volume_total=0.0, volume_24h=0.0,
                     end_ts=None)
    row = token_features(hostile, hostile.quotes["T1"])
    for name, value in row.items():
        assert isinstance(value, float), name
        assert not math.isnan(value), name
        assert not math.isinf(value), name


def test_no_context_is_survivable():
    row = token_features(market(), quote(), context=None)
    assert row["wallet_entries"] == 0.0
    assert row["anomaly_max"] == 0.0


# --- derived columns --------------------------------------------------------

def test_relative_spread_not_just_absolute():
    # 2c is nothing at 0.80 and prohibitive at 0.04.
    cheap = token_features(market(), quote(ask=0.04, spread=0.02))
    rich = token_features(market(), quote(ask=0.80, spread=0.02))
    assert cheap["spread_rel"] > rich["spread_rel"] * 10


def test_depth_imbalance_is_signed():
    bid_heavy = token_features(market(), quote(bid_depth=900.0, ask_depth=100.0))
    ask_heavy = token_features(market(), quote(bid_depth=100.0, ask_depth=900.0))
    assert bid_heavy["depth_imbalance"] > 0
    assert ask_heavy["depth_imbalance"] < 0


def test_turnover_surfaces_a_small_market_waking_up():
    sleepy = token_features(market(volume_total=1_000_000.0, volume_24h=1_000.0),
                            quote())
    waking = token_features(market(volume_total=20_000.0, volume_24h=15_000.0),
                            quote())
    assert waking["volume_turnover"] > sleepy["volume_turnover"]


def test_tape_features_only_count_this_token():
    m = market()
    m.recent_trades = [
        {"tokenId": "T1", "side": "BUY", "price": 0.40, "size": 100, "ts": 1},
        {"tokenId": "T1", "side": "SELL", "price": 0.44, "size": 100, "ts": 2},
        {"tokenId": "OTHER", "side": "BUY", "price": 0.90, "size": 999, "ts": 3},
    ]
    row = token_features(m, quote())
    assert row["tape_trades"] == 2.0
    assert row["tape_buy_ratio"] == 0.5
    assert round(row["tape_price_drift"], 4) == 0.1


# --- wallet evidence uses earned weight -------------------------------------

def test_wallet_influence_comes_from_the_derived_rank():
    strong = WalletIntel(wallet="0xstrong", score=0.9, sample=100, confidence=0.8)
    weak = WalletIntel(wallet="0xweak", score=0.2, sample=100, confidence=0.8)
    signals = [WalletSignal(wallet="0xstrong", action="ENTRY", token_id="T1",
                            usdc=500.0, weight=1.0),
               WalletSignal(wallet="0xweak", action="ENTRY", token_id="T1",
                            usdc=500.0, weight=1.0)]

    both = token_features(market(), quote(), context(
        wallet_signals=signals,
        wallet_intel={"0xstrong": strong, "0xweak": weak}))
    only_weak = token_features(market(), quote(), context(
        wallet_signals=signals[1:], wallet_intel={"0xweak": weak}))

    assert both["wallet_entries"] == 2.0
    # Both configured at weight 1.0; the derived scores are what separate them.
    assert both["wallet_weighted"] > only_weak["wallet_weighted"] * 1.5
    assert both["wallet_best_score"] == 0.9


def test_exits_subtract_from_weighted_flow():
    intel = {"0xa": WalletIntel(wallet="0xa", score=0.8, sample=100,
                                confidence=0.9)}
    entry = token_features(market(), quote(), context(
        wallet_signals=[WalletSignal(wallet="0xa", action="ENTRY",
                                     token_id="T1", usdc=100.0)],
        wallet_intel=intel))
    exit_ = token_features(market(), quote(), context(
        wallet_signals=[WalletSignal(wallet="0xa", action="EXIT",
                                     token_id="T1", usdc=100.0)],
        wallet_intel=intel))
    assert entry["wallet_weighted"] > 0 > exit_["wallet_weighted"]
    assert entry["wallet_net_usdc"] == -exit_["wallet_net_usdc"]


def test_best_rank_is_inverted_so_bigger_is_stronger():
    row = token_features(market(), quote(), context(
        wallet_signals=[WalletSignal(wallet="0xa", action="ENTRY",
                                     token_id="T1")],
        wallet_intel={"0xa": WalletIntel(wallet="0xa", rank=4)}))
    assert row["wallet_best_rank"] == 0.25


# --- anomalies get one column each ------------------------------------------

def test_each_anomaly_kind_has_its_own_column():
    for kind in KINDS:
        assert f"anomaly_{kind}" in FEATURE_NAMES


def test_anomaly_strength_lands_in_its_own_column():
    ctx = context(anomalies=[
        AnomalySignal(kind="convergence", token_id="T1", strength=0.7),
        AnomalySignal(kind="market_flow", market_id="M1", strength=0.4),
    ])
    row = token_features(market(), quote(), ctx)
    assert row["anomaly_convergence"] == 0.7
    # A market-level anomaly is evidence about every outcome in that market.
    assert row["anomaly_market_flow"] == 0.4
    assert row["anomaly_max"] == 0.7
    assert row["anomaly_count"] == 2.0
    assert row["anomaly_size_spike"] == 0.0


def test_anomalies_for_another_token_do_not_leak():
    ctx = context(anomalies=[
        AnomalySignal(kind="convergence", token_id="OTHER", strength=0.9)])
    row = token_features(market(), quote(), ctx)
    assert row["anomaly_convergence"] == 0.0


# --- market flow ------------------------------------------------------------

def test_market_intel_flows_through():
    row = token_features(market(), quote(), context(),
                         market_intel=MarketIntel(market_id="M1", flow_z=4.2,
                                                  net_usdc=1_234.0,
                                                  wallets_active=9,
                                                  cohort_net_usdc=800.0))
    assert row["flow_z"] == 4.2
    assert row["cohort_net_usdc"] == 800.0


# --- position features ------------------------------------------------------

def test_position_features_extend_rather_than_replace():
    position = PositionView(token_id="T1", market_id="M1", size=100.0,
                            avg_price=0.40, cur_price=0.50, peak_price=0.55,
                            opened_ts=int(time.time()) - 7200)
    row = position_features(position, market(), quote(), context())
    # Every market-side column survives: an exit needs the market's state too.
    assert set(FEATURE_NAMES).issubset(row)
    assert round(row["pos_return"], 4) == 0.25
    assert round(row["pos_drawdown_from_peak"], 4) == round(0.05 / 0.55, 4)
    assert 1.9 < row["pos_hold_hours"] < 2.1


def test_position_features_survive_a_missing_market():
    position = PositionView(token_id="T1", size=10.0, avg_price=0.4,
                            cur_price=0.4)
    row = position_features(position, None, None, context())
    assert set(FEATURE_NAMES).issubset(row)
    assert row["price"] == 0.0
