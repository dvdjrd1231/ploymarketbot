"""Military-attack longshot effect: a reported 52% is a hypothesis.

Pinned: the classifier is literal (Counter-Strike esports is NOT war);
observations use entry-time information only and each market contributes
exactly its longshot side; correlated contracts on one crisis are one
EVENT; the category must beat the same-priced control; and a validated
rule still cannot vote in the live engine.
"""

from __future__ import annotations

import pytest

from pqb.analytics.longshot import (classify_military, cluster_key, describe,
                                    frozen_replay, observations_from_tape,
                                    study)


# -- the classifier ----------------------------------------------------------

def test_military_questions_match():
    assert classify_military("Will Israel strike Iran by March?")
    assert classify_military("Russia x Ukraine ceasefire in 2026?")
    assert classify_military("Will the US attack Houthi positions?")


def test_counter_strike_is_not_war():
    """The dataset's own first lesson."""
    assert not classify_military(
        "Counter-Strike: G2 vs M80 (BO1) - Esports World Cup Group A")
    assert not classify_military("Warriors vs Lakers - who wins?")
    assert not classify_military("Will Bitcoin go up or down?")


def test_one_crisis_is_one_event():
    a = cluster_key("Will Israel strike Iran by March?", 1_780_000_000)
    b = cluster_key("Israel military action against Iran this month?",
                    1_780_100_000)
    assert a == b                      # same actors, same month -> one event


# -- entry-time honesty ------------------------------------------------------

def _tape(prices, usd_each=100.0):
    return [{"ts": 1_000 + i * 60, "price": p, "usdc": usd_each,
             "market_id": "M1", "question": "q"}
            for i, p in enumerate(prices)]


def test_observations_use_only_the_tape_prefix():
    # Price sits at 0.20 through the entry points, then jumps to 0.95
    # at the very end. Entry-time implied must be ~0.20, never 0.95.
    trades = _tape([0.20] * 30 + [0.95])
    obs = observations_from_tape(trades, payout=1.0, market_id="M1",
                                 question="q")
    assert obs
    assert all(o["implied"] == pytest.approx(0.20) for o in obs)
    assert all(o["tradedUsd"] <= 100.0 * 31 for o in obs)


def test_high_priced_side_is_inverted_to_the_longshot():
    """A 0.80 token IS a 0.20 longshot on the other side — one observation,
    complement-priced, complement-paid."""
    obs = observations_from_tape(_tape([0.80] * 30), payout=1.0,
                                 market_id="M1", question="q")
    assert obs
    assert all(o["implied"] == pytest.approx(0.20) for o in obs)
    assert all(o["payout"] == 0.0 for o in obs)      # complement lost


# -- the study: controls decide ----------------------------------------------

class FakeStore:
    def __init__(self, tokens):
        # tokens: list of (token, question, prices, payout)
        self._tokens = tokens

    def resolutions(self):
        return {t: (1.0 if payout else 0.0)
                for t, _q, _p, payout in self._tokens}

    def query(self, sql, params=()):
        rows = []
        for token, question, prices, _payout in self._tokens:
            for i, price in enumerate(prices):
                rows.append({"token_id": token, "market_id": f"mkt-{token}",
                             "ts": 1_000 + i * 60, "price": price,
                             "usdc": 100.0, "question": question})
        return rows


def _fleet(n, question, price, payout_fn, prefix):
    return [(f"{prefix}{i}", question.format(i=i), [price] * 30,
             payout_fn(i)) for i in range(n)]


def test_underpriced_military_longshots_are_discovered():
    # Military 0.20 longshots resolve YES 60% of the time (mispriced);
    # control 0.20 longshots resolve at their fair 20%.
    military = _fleet(20, "Will country-{i} strike country-X?", 0.20,
                      lambda i: i % 5 < 3, "war")
    control = _fleet(20, "Will team {i} win the cup?", 0.20,
                     lambda i: i % 5 == 0, "ctl")
    result = study(FakeStore(military + control), cost=0.02, min_events=10)
    kept = result["candidates"]
    assert kept, f"nothing kept: {result['funnel']}"
    best = kept[0]
    assert best["prob_lo"] <= 0.20 < best["prob_hi"]
    assert best["realized"] > best["implied"]
    assert best["netExpectancy"] > best["controlNet"]


def test_fairly_priced_military_markets_yield_nothing():
    military = _fleet(20, "Will country-{i} strike country-X?", 0.20,
                      lambda i: i % 5 == 0, "war")     # fair 20%
    result = study(FakeStore(military), cost=0.02, min_events=10)
    assert result["candidates"] == []
    assert result["funnel"]["rejectReasons"]


def test_insufficient_events_is_named_not_promoted():
    military = _fleet(4, "Will country-{i} strike country-X?", 0.20,
                      lambda i: True, "war")
    result = study(FakeStore(military), cost=0.02, min_events=10)
    assert result["candidates"] == []
    assert result["funnel"]["rejectReasons"].get(
        "insufficient sample (events)", 0) > 0


def test_one_dominating_crisis_is_fragile():
    """Twenty contracts on ONE event cluster must not validate the effect."""
    military = _fleet(20, "Will Israel strike Iran scenario {i}?", 0.20,
                      lambda i: True, "war")           # all same actors
    result = study(FakeStore(military), cost=0.02, min_events=10)
    assert result["candidates"] == []
    reasons = result["funnel"]["rejectReasons"]
    assert reasons.get("insufficient sample (events)", 0) \
        + reasons.get("fragile: one event cluster dominates", 0) > 0


def test_calibration_curve_is_reported_for_both_groups():
    military = _fleet(20, "Will country-{i} strike country-X?", 0.20,
                      lambda i: i % 2 == 0, "war")
    control = _fleet(20, "Will team {i} win?", 0.20,
                     lambda i: i % 5 == 0, "ctl")
    funnel = study(FakeStore(military + control), min_events=10)["funnel"]
    assert funnel["calibrationMilitary"]
    assert funnel["calibrationControl"]
    cell = list(funnel["calibrationMilitary"].values())[0]
    for key in ("implied", "realized", "edge", "netPerShare", "events",
                "topClusterShare"):
        assert key in cell


# -- frozen OOS replay -------------------------------------------------------

_RULE = {"type": "longshot", "category": "military", "prob_lo": 0.15,
         "prob_hi": 0.25, "side": "low", "min_traded_usd": 0.0}


def test_frozen_replay_one_observation_per_market():
    stats = frozen_replay(_tape([0.20] * 40), _RULE, payout=1.0, cost=0.02)
    assert stats["trades"] == 1
    assert stats["pnl"] == pytest.approx(1.0 - 0.20 - 0.02)


def test_frozen_replay_inverts_high_priced_series():
    stats = frozen_replay(_tape([0.80] * 40), _RULE, payout=1.0, cost=0.02)
    assert stats["trades"] == 1
    assert stats["pnl"] == pytest.approx(0.0 - 0.20 - 0.02)


def test_frozen_replay_respects_the_band():
    stats = frozen_replay(_tape([0.45] * 40), _RULE, payout=1.0, cost=0.02)
    assert stats["trades"] == 0


# -- identity and the execution bar ------------------------------------------

def test_signature_and_family():
    from pqb.research import family_of, signature_of

    assert signature_of(_RULE) == "longshot|military|0.15|0.25|low"
    assert family_of(_RULE) == "longshot-calibration"
    refloored = dict(_RULE, min_traded_usd=2500.0)
    assert signature_of(refloored) == signature_of(_RULE)   # same family


def test_validated_longshots_cannot_vote(tmp_path):
    from pqb.bridge.lean_engine import LeanDecisionEngine
    from pqb.config import Config
    from pqb.research import DiscoveredStrategy

    cfg = Config()
    cfg.root = tmp_path
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    shot = DiscoveredStrategy(rule=dict(_RULE), signature="longshot|x",
                              describe="LONGSHOT")
    shot.status = "validated"
    engine.strategies = [shot]
    assert engine.trading_strategies == []


def test_describe_reads_naturally():
    assert "15%-25%" in describe(_RULE)
