"""The operator's LEAN research dataset: schema, identical construction,
market-level leakage prevention."""

from __future__ import annotations

import types

from pqb.analytics.history_series import _series_for
from pqb.research import DiscoveredStrategy, aggregate, signature_of


def _tape(n=200, start=1_700_000_000, price0=0.40):
    """A synthetic settled-market tape: drifting price, mixed wallets."""
    rows = []
    price = price0
    for i in range(n):
        price = min(0.95, price + 0.001)
        rows.append({
            "ts": start + i * 120, "price": price, "size": 10.0 + (i % 7),
            "usdc": price * (10.0 + (i % 7)),
            "side": "BUY" if i % 3 else "SELL",
            "wallet": f"0xw{i % 9}",
        })
    # One whale print for concentration/large-ratio signal.
    rows[100]["size"] = 500.0
    rows[100]["usdc"] = rows[100]["price"] * 500.0
    return rows


def test_historical_rows_carry_the_operator_schema():
    tape = _tape()
    # Lifecycle and countdown are measured against the PUBLISHED settlement
    # time, never the end of the tape (see test_leakage).
    series = _series_for("tokH", tape, scores={},
                         settled_ts=tape[-1]["ts"] + 3600)
    assert len(series) > 50
    mid = series[len(series) // 2]
    # Lifecycle normalisation: monotonic 0..1 across the market's life.
    assert 0.0 < mid["lifecycle_pct"] < 1.0
    assert series[-1]["lifecycle_pct"] > series[0]["lifecycle_pct"]
    assert mid["market_age_hours"] > 0
    # The tape family, same names as live construction.
    for column in ("tape_buy_volume", "tape_sell_volume", "tape_large_ratio",
                   "tape_velocity", "tape_trade_rate",
                   "wallet_count_tape", "wallet_concentration"):
        assert column in mid, column
    # Concentration is a share, and the whale bucket shows it.
    whale_rows = [r for r in series if r["wallet_concentration"] > 0.5]
    assert whale_rows, "the whale print never showed up in concentration"


def test_historical_rows_carry_market_state_scores():
    """Identical construction, literally: the live MarketStateTracker replayed
    over the historical tape fills the ms_ columns."""
    series = _series_for("tokH", _tape(), scores={})
    active = [r for r in series if r.get("ms_data_quality", 0) > 0]
    assert active, "market-state replay produced nothing"
    assert any(r.get("ms_trade_rate", 0) > 0 for r in active)
    assert any(r.get("ms_state", -1) >= 0 for r in active)


def _report(feature: str, accepted=True):
    strategy = types.SimpleNamespace(
        to_dict=lambda: {"direction": "LONG", "entry_feature": feature,
                         "entry_op": ">", "filter_feature": "",
                         "filter_op": ""},
        describe=lambda: f"LONG when {feature} > x")
    return types.SimpleNamespace(accepted=accepted, strategy=strategy,
                                 full={"rank_score": 0.5})


def test_market_split_requires_both_halves():
    """The no-leakage rule: a rule confirmed only within one half of the
    market universe is rejected however many tokens agreed."""
    # Tokens t1/t2 belong to market MA, t3 to MB. Hash halves differ or not —
    # find two markets that land in different halves deterministically.
    import hashlib

    def half(market):
        return int(hashlib.sha1(market.encode()).hexdigest(), 16) % 2

    markets = [f"M{i}" for i in range(20)]
    half0 = next(m for m in markets if half(m) == 0)
    half1 = next(m for m in markets if half(m) == 1)

    per_token = [("t1", [_report("f")]), ("t2", [_report("f")]),
                 ("t3", [_report("f")])]

    # All three tokens in ONE half: rejected despite 3 acceptances.
    one_half = {"t1": half0, "t2": half0, "t3": half0}
    assert aggregate(per_token, min_tokens=2, market_of=one_half) == []

    # Spread across both halves: kept.
    both = {"t1": half0, "t2": half1, "t3": half0}
    kept = aggregate(per_token, min_tokens=2, market_of=both)
    assert len(kept) == 1
    assert kept[0].accepted_on == 3


def test_split_disabled_keeps_old_behaviour():
    per_token = [("t1", [_report("f")]), ("t2", [_report("f")])]
    kept = aggregate(per_token, min_tokens=2, market_of=None)
    assert len(kept) == 1
