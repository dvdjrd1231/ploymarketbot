"""Sequential discovery: order and timing as a hypothesis, not a shortcut.

The operator's spec, pinned: chains are observed n-grams (2-4, never more),
timing-bounded; kept only with enough sample across enough markets AND
incremental net value over their own best component; direction discovered
from the response; frozen replay on unseen series only; and — critically —
a validated chain still cannot vote in the live engine (§17).
"""

from __future__ import annotations

import pytest

from pqb.analytics.sequences import (describe, extract_events, frozen_replay,
                                     mine, rows_from_csv)


def _row(price=0.5, **kwargs):
    base = {"price": price, "spread_rel": 0.02, "depth_imbalance": 0.0,
            "log_liquidity": 10.0, "tape_velocity_z": 0.0,
            "wallet_concentration": 0.1, "ms_anomaly": 0.0, "ms_state": 0.0,
            "liq_imbalance": 0.0, "liq_long_usd_60s": 0.0,
            "liq_short_usd_60s": 0.0, "_ts": 0.0}
    base.update(kwargs)
    return base


def _flat(n, price=0.5):
    return [_row(price + 0.0001 * (i % 3)) for i in range(n)]


# -- event extraction --------------------------------------------------------

def test_impulses_and_anomalies_are_named():
    rows = _flat(50)
    rows[30] = _row(0.60)               # a big jump against a flat series
    rows[40] = _row(0.60, ms_anomaly=90.0)
    kinds = {e.kind for e in extract_events(rows)}
    assert "price_up_impulse" in kinds
    assert "anomaly_high" in kinds


def test_state_transitions_are_events_not_states():
    rows = _flat(60)
    for i in range(35, 60):
        rows[i]["ms_state"] = 2.0       # enters IMPULSE once, stays there
    events = [e for e in extract_events(rows) if e.kind == "state_impulse"]
    assert len(events) == 1             # the CHANGE fires, the state does not


def test_cluster_burst_when_many_kinds_land_together():
    rows = _flat(50)
    rows[25] = _row(0.60, ms_anomaly=90.0, depth_imbalance=0.5,
                    wallet_concentration=0.9)
    kinds = {e.kind for e in extract_events(rows)}
    assert "cluster_burst" in kinds


def test_thin_series_yields_nothing():
    assert extract_events(_flat(10)) == []


# -- mining discipline -------------------------------------------------------

def _planted_series(reps=12, follow=0.05):
    """anomaly -> book_flips_bid, then price rises by `follow`.

    Every second block also plants a STANDALONE book_flips_bid with no
    payoff — the component alone must be measurably weaker than the chain,
    or the incremental gate (correctly) refuses the added complexity.
    """
    rows = _flat(30)
    for rep in range(reps):
        base = rows[-1]["price"]
        block = _flat(6, base)
        block[1] = _row(base, ms_anomaly=90.0)
        block[3] = _row(base, depth_imbalance=0.5)
        rows.extend(block)
        rows.extend(_row(base + follow * (i + 1) / 6) for i in range(6))
        rows.extend(_flat(8, base + follow))
        if rep % 2 == 0:               # the flip WITHOUT the anomaly: no move
            lone = _flat(6, rows[-1]["price"])
            lone[2] = _row(rows[-1]["price"], depth_imbalance=0.5)
            rows.extend(lone)
            rows.extend(_flat(10, rows[-1]["price"]))
    return rows


def test_a_recurring_paying_chain_is_discovered():
    series = [("M1", _planted_series()), ("M2", _planted_series())]
    result = mine(series, min_occurrences=6, cost=0.005, hold_bars=8)
    kept = result["candidates"]
    assert kept, f"nothing kept: {result['funnel']}"
    chains = [tuple(c["chain"]) for c in kept]
    assert any("anomaly_high" in c and "book_flips_bid" in c for c in chains)
    best = kept[0]
    assert best["direction"] == "up"          # discovered from the response
    assert best["netExpectancy"] > 0
    assert best["markets"] >= 2


def test_funnel_names_every_stage_and_reason():
    result = mine([("M1", _flat(100))], min_occurrences=6)
    funnel = result["funnel"]
    for key in ("eventsObserved", "eventTypes", "chainsGenerated",
                "sufficientSample", "grossPositive", "netPositive",
                "incremental", "kept", "rejectReasons"):
        assert key in funnel


def test_costs_kill_chains_that_cannot_pay():
    series = [("M1", _planted_series(follow=0.002)),
              ("M2", _planted_series(follow=0.002))]
    result = mine(series, min_occurrences=6, cost=0.05, hold_bars=8)
    assert result["candidates"] == []
    assert result["funnel"]["rejectReasons"].get("cannot clear costs", 0) > 0


def test_longer_chains_need_proportionally_more_evidence():
    """A 4-chain must not ride on a 2-chain's sample floor."""
    series = [("M1", _planted_series(reps=7)), ("M2", _planted_series(reps=7))]
    result = mine(series, min_occurrences=6, cost=0.005, hold_bars=8,
                  max_len=4)
    for candidate in result["candidates"]:
        floor = 6 * (len(candidate["chain"]) - 1)
        assert candidate["occurrences"] >= floor


def test_single_market_chains_are_rejected():
    result = mine([("M1", _planted_series())], min_occurrences=6,
                  min_markets=2, cost=0.005, hold_bars=8)
    assert result["candidates"] == []


# -- the frozen replay (OOS discipline) --------------------------------------

def test_frozen_replay_scores_unseen_series():
    rule = {"type": "sequence", "chain": ["anomaly_high", "book_flips_bid"],
            "direction": "up", "gap_bars": 15, "hold_bars": 8}
    stats = frozen_replay(_planted_series(), rule, cost=0.005)
    assert stats["trades"] > 0
    assert stats["expectancy"] > 0


def test_frozen_replay_charges_the_wrong_direction():
    rule = {"type": "sequence", "chain": ["anomaly_high", "book_flips_bid"],
            "direction": "down", "gap_bars": 15, "hold_bars": 8}
    stats = frozen_replay(_planted_series(), rule, cost=0.005)
    assert stats["trades"] > 0
    assert stats["expectancy"] < 0            # the rise punishes a DOWN call


def test_frozen_replay_never_overlaps_one_move():
    rule = {"type": "sequence", "chain": ["anomaly_high", "book_flips_bid"],
            "direction": "up", "gap_bars": 15, "hold_bars": 8}
    series = _planted_series(reps=5)
    stats = frozen_replay(series, rule, cost=0.0)
    assert stats["trades"] <= 5               # one replay per planted move


# -- identity, library, and the §17 execution bar ----------------------------

def test_sequence_signature_is_chain_and_direction():
    from pqb.research import signature_of

    rule = {"type": "sequence", "chain": ["a", "b"], "direction": "up",
            "gap_bars": 15, "hold_bars": 8}
    assert signature_of(rule) == "seq|a|b|up"
    retimed = dict(rule, gap_bars=5)
    assert signature_of(retimed) == signature_of(rule)   # same family


def test_validated_sequences_still_cannot_vote(tmp_path):
    """§17: discovery is never execution. Even a VALIDATED chain carries
    zero weight in the live engine until an execution path exists."""
    from pqb.bridge.lean_engine import LeanDecisionEngine
    from pqb.config import Config
    from pqb.research import DiscoveredStrategy

    cfg = Config()
    cfg.root = tmp_path
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    chain = DiscoveredStrategy(
        rule={"type": "sequence", "chain": ["a", "b"], "direction": "up"},
        signature="seq|a|b|up", describe="SEQ")
    chain.status = "validated"
    chain.confidence = 0.9
    ordinary = DiscoveredStrategy(rule={"entry_feature": "price_z"},
                                  signature="s", describe="rule")
    ordinary.status = "validated"
    engine.strategies = [chain, ordinary]
    trading = engine.trading_strategies
    assert len(trading) == 1
    assert trading[0].signature == "s"


def test_describe_is_human_readable():
    text = describe({"type": "sequence", "chain": ["a", "b"],
                     "direction": "up", "gap_bars": 15, "hold_bars": 8})
    assert "a -> b" in text and "UP" in text


def test_rows_from_csv_roundtrip(tmp_path):
    path = tmp_path / "features.csv"
    path.write_text("timestamp,price,spread_rel\n"
                    "2026-01-01 00:00:00,0.5,0.02\n"
                    "2026-01-01 00:01:00,0.6,0.03\n", encoding="utf-8")
    rows = rows_from_csv(path)
    assert len(rows) == 2
    assert rows[1]["price"] == 0.6
    assert rows[1]["_ts"] > rows[0]["_ts"]
