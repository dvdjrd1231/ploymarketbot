"""RN1 Strategy Model V1: the frozen rule, the clean boundary, the quarantine.

The handoff's hard requirements, expressed as tests rather than as intentions:

* the three thresholds (0.20 / 0.80 / $5) and the 1.40 labelling boundary are
  constants, and changing one fails a test rather than a review;
* the clean prospective boundary is 2026-08-20 16:10 UTC and predictions
  before it cannot enter the forward sample by any path;
* the contaminated ~40.48% run raises on access instead of returning a number;
* a pending prediction is PENDING — not wrong, not right — because counting
  pending as incorrect is how a forward experiment gets called a failure
  before it has run.
"""

from __future__ import annotations

import datetime as dt

import pytest

from pqb.wallet_state_research import registry, states, strategy_v1, structure
from pqb.wallet_state_research.episodes import (AGGRESSIVE, DIRECTIONAL,
                                                LABEL_RATIO_BOUNDARY, PROTECT,
                                                build_episodes)
from pqb.wallet_state_research.events import WalletEvent

BOUNDARY = strategy_v1.PROSPECTIVE_BOUNDARY_TS
BEFORE = BOUNDARY - 86_400
AFTER = BOUNDARY + 3_600
YES, NO = "tokY", "tokN"


def _event(token, side, ts, price=0.5, shares=10.0, usdc=None,
           wallet="0xw", market="m1"):
    return WalletEvent(wallet=wallet, market_id=market, token_id=token,
                       outcome=("Yes" if token == YES else "No"), side=side,
                       ts=ts, price=price, shares=shares,
                       usdc=(price * shares if usdc is None else usdc),
                       question="Will X happen?")


def _episode(price, usdc, ts=AFTER, opposite_shares=0.0, shares=10.0,
             tape_end=None):
    events = [_event(YES, "BUY", ts, price=price, shares=shares, usdc=usdc)]
    if opposite_shares:
        events.append(_event(NO, "BUY", ts + 60, shares=opposite_shares))
    return build_episodes(events,
                          tape_end_ts=tape_end or (ts + 30 * 86_400))[0]


# ===========================================================================
# 1. THE FROZEN CONSTANTS
# ===========================================================================


def test_the_v1_thresholds_are_exactly_the_published_values():
    """§3 and §50. If this test needs editing, the frozen experiment has been
    changed and every number it produced becomes uninterpretable."""
    assert strategy_v1.V1_AGGRESSIVE_PRICE_MAX == 0.20
    assert strategy_v1.V1_AGGRESSIVE_CAPITAL_MIN == 5.00
    assert strategy_v1.V1_DIRECTIONAL_PRICE_MIN == 0.80
    assert strategy_v1.V1_MODEL_VERSION == "RN1_STRATEGY_MODEL_V1"
    assert LABEL_RATIO_BOUNDARY == 1.40


def test_the_prospective_boundary_is_2026_08_20_1610_utc():
    assert strategy_v1.PROSPECTIVE_BOUNDARY_UTC == dt.datetime(
        2026, 8, 20, 16, 10, 0, tzinfo=dt.timezone.utc)
    assert strategy_v1.DEFAULT_FRESHNESS_MINUTES == 15.0


# ===========================================================================
# 2. THE RULE
# ===========================================================================


def test_all_three_arms_of_the_rule():
    model = strategy_v1.RN1StrategyModelV1()
    aggressive = model.predict(_episode(price=0.15, usdc=10.0))
    directional = model.predict(_episode(price=0.85, usdc=10.0))
    protect = model.predict(_episode(price=0.50, usdc=10.0))
    assert aggressive.label == AGGRESSIVE
    assert directional.label == DIRECTIONAL
    assert protect.label == PROTECT


def test_the_aggressive_arm_needs_BOTH_conditions():
    """A cheap price with too little capital is PROTECT, not AGGRESSIVE —
    the AND is load-bearing and is the easiest thing to implement as an OR."""
    model = strategy_v1.RN1StrategyModelV1()
    assert model.predict(_episode(price=0.15, usdc=4.99)).label == PROTECT
    assert model.predict(_episode(price=0.15, usdc=5.00)).label == AGGRESSIVE


def test_the_boundaries_are_inclusive_exactly_as_written():
    """`<= 0.20`, `>= 0.80`, `>= $5`."""
    model = strategy_v1.RN1StrategyModelV1()
    assert model.predict(_episode(0.20, 10.0)).label == AGGRESSIVE
    assert model.predict(_episode(0.2001, 10.0)).label == PROTECT
    assert model.predict(_episode(0.80, 10.0)).label == DIRECTIONAL
    assert model.predict(_episode(0.7999, 10.0)).label == PROTECT


def test_the_cheap_arm_is_checked_before_the_expensive_one():
    """Order matters only if a price could satisfy both; it cannot, but the
    rule is written as if/elif and the test pins the written order."""
    model = strategy_v1.RN1StrategyModelV1()
    assert model.predict(_episode(0.10, 100.0)).label == AGGRESSIVE


def test_initial_capital_sums_the_fills_of_the_first_instant():
    """An order split across several fills in one second is one decision, and
    reading only the first fill would understate it across a $5 threshold."""
    events = [_event(YES, "BUY", AFTER, price=0.10, shares=30.0, usdc=3.0),
              _event(YES, "BUY", AFTER, price=0.10, shares=30.0, usdc=3.0)]
    episode = build_episodes(events, tape_end_ts=AFTER + 30 * 86_400)[0]
    assert strategy_v1.initial_capital(episode) == pytest.approx(6.0)
    assert strategy_v1.RN1StrategyModelV1().predict(
        episode).label == AGGRESSIVE


def test_the_looser_capital_reading_is_diagnostic_only():
    """Later adds inside the freshness window are measured but never fed to
    the rule — changing what the rule reads IS changing the rule."""
    events = [_event(YES, "BUY", AFTER, price=0.10, shares=10.0, usdc=1.0),
              _event(YES, "BUY", AFTER + 300, price=0.10, shares=90.0,
                     usdc=9.0)]
    episode = build_episodes(events, tape_end_ts=AFTER + 30 * 86_400)[0]
    assert strategy_v1.initial_capital(episode) == pytest.approx(1.0)
    assert strategy_v1.capital_within_freshness(episode) == pytest.approx(10.0)
    # The rule uses the strict reading, so this is PROTECT.
    assert strategy_v1.RN1StrategyModelV1().predict(episode).label == PROTECT


# ===========================================================================
# 3. THE CLEAN PROSPECTIVE GATE
# ===========================================================================


def test_a_condition_before_the_boundary_is_not_prospective():
    gate = strategy_v1.eligibility(_episode(0.5, 10.0, ts=BEFORE))
    assert gate.eligible is True          # it can still be studied...
    assert gate.prospective is False      # ...but not as forward evidence
    assert gate.reason == strategy_v1.R_BEFORE_BOUNDARY


def test_a_condition_after_the_boundary_is_prospective():
    gate = strategy_v1.eligibility(_episode(0.5, 10.0, ts=AFTER))
    assert gate.eligible and gate.prospective
    assert gate.information_cutoff_ts == pytest.approx(AFTER + 900.0)


def test_old_conditions_cannot_enter_the_clean_sample_by_any_path():
    """The contamination that produced ~40.48%, prevented structurally."""
    old = [_episode(0.5, 10.0, ts=BEFORE - i * 3_600) for i in range(20)]
    report, cases = strategy_v1.evaluate_v1(old, prospective_only=True)
    assert report.predictions == 0
    assert cases == []
    assert report.rejected[strategy_v1.R_BEFORE_BOUNDARY] == 20


def test_a_redeem_before_the_prediction_disqualifies():
    gate = strategy_v1.eligibility(_episode(0.5, 10.0, ts=AFTER),
                                   has_redeem_before=lambda _e: True)
    assert gate.eligible is False
    assert gate.reason == strategy_v1.R_REDEEM_FIRST


def test_a_pending_prediction_is_unresolved_not_incorrect():
    """Counting pending as wrong is how a forward experiment is declared a
    failure before it has run."""
    pending = _episode(0.5, 10.0, ts=AFTER, tape_end=AFTER + 60)
    assert pending.label_quality == "truncated"
    report, _ = strategy_v1.evaluate_v1([pending], prospective_only=True)
    assert report.predictions == 1
    assert report.unresolved == 1
    assert report.resolved == 0
    assert report.accuracy == 0.0        # no denominator, not a zero score
    payload = report.to_dict()
    assert payload["confidenceInterval95"]["available"] is False


# ===========================================================================
# 4. THREE-CLASS SCORING
# ===========================================================================


def _report_with(pairs):
    report = strategy_v1.MulticlassReport()
    for predicted, actual in pairs:
        report.record(predicted, actual)
    return report


def test_per_class_precision_recall_and_f1():
    report = _report_with([
        (AGGRESSIVE, AGGRESSIVE), (AGGRESSIVE, PROTECT),
        (PROTECT, PROTECT), (PROTECT, PROTECT),
        (DIRECTIONAL, DIRECTIONAL), (DIRECTIONAL, PROTECT)])
    assert report.resolved == 6
    assert report.correct == 4
    assert report.accuracy == pytest.approx(4 / 6)
    assert report.precision(AGGRESSIVE) == pytest.approx(0.5)
    assert report.recall(AGGRESSIVE) == pytest.approx(1.0)
    assert report.precision(PROTECT) == pytest.approx(1.0)
    assert report.recall(PROTECT) == pytest.approx(0.5)
    assert report.f1(DIRECTIONAL) == pytest.approx(2 * 0.5 * 1.0 / 1.5)


def test_balanced_accuracy_averages_only_classes_that_occur():
    """A three-class metric on a two-class sample describes the sample."""
    report = _report_with([(PROTECT, PROTECT), (PROTECT, PROTECT),
                           (AGGRESSIVE, AGGRESSIVE)])
    assert report.balanced_accuracy == pytest.approx(1.0)


def test_the_majority_baseline_is_reported_beside_accuracy():
    report = _report_with([(PROTECT, DIRECTIONAL)] * 9
                          + [(PROTECT, PROTECT)])
    assert report.majority_baseline == pytest.approx(0.9)
    assert report.accuracy == pytest.approx(0.1)
    assert report.to_dict()["beatsMajorityBaseline"] is False


# ===========================================================================
# 5. THE STATE MACHINE
# ===========================================================================


def test_the_trajectory_walks_the_documented_states():
    events = [_event(YES, "BUY", 1_000, shares=100.0),
              _event(YES, "BUY", 1_100, shares=50.0),
              _event(NO, "BUY", 2_000, shares=20.0),
              _event(NO, "BUY", 3_000, shares=400.0)]
    episode = build_episodes(events, tape_end_ts=1_000 + 30 * 86_400)[0]
    path = states.trajectory(episode)
    visited = [t.to_state for t in path.transitions]
    assert visited[0] == states.STATE_1_INITIAL_ONE_SIDED
    assert states.STATE_2_ACCUMULATING_ORIGINAL in visited
    assert states.STATE_4_OPPOSITE_TRANSITION in visited
    assert states.STATE_6_TWO_SIDED_AGGRESSIVE in visited
    assert path.reached_two_sided


def test_state_at_a_time_never_reflects_later_events():
    events = [_event(YES, "BUY", 1_000, shares=100.0),
              _event(NO, "BUY", 5_000, shares=400.0)]
    episode = build_episodes(events, tape_end_ts=1_000 + 30 * 86_400)[0]
    path = states.trajectory(episode)
    assert path.state_at(2_000) == states.STATE_1_INITIAL_ONE_SIDED
    assert path.state_at(6_000) != states.STATE_1_INITIAL_ONE_SIDED


def test_every_transition_carries_its_timestamp_and_inventory():
    events = [_event(YES, "BUY", 1_000, shares=100.0),
              _event(NO, "BUY", 2_000, shares=200.0)]
    episode = build_episodes(events, tape_end_ts=1_000 + 30 * 86_400)[0]
    for transition in states.trajectory(episode).transitions:
        assert transition.ts > 0
        assert transition.trigger
        assert transition.seconds_since_first_buy >= 0


def test_the_transition_study_refuses_a_thin_row():
    study = states.TransitionStudy()
    events = [_event(YES, "BUY", 1_000, shares=100.0),
              _event(NO, "BUY", 2_000, shares=200.0)]
    episode = build_episodes(events, tape_end_ts=1_000 + 30 * 86_400)[0]
    study.add(states.trajectory(episode))
    probabilities = study.probabilities(min_observations=20)
    assert all(not row["sufficient"] for row in probabilities.values())


# ===========================================================================
# 6. THE REGISTRY AND THE QUARANTINE
# ===========================================================================


def test_the_contaminated_run_raises_instead_of_returning_a_number():
    """§52. It must never reach a performance table by any path."""
    record = registry.default_registry().get(
        "RN1_V1_FORWARD_VALIDATOR_CONTAMINATED")
    assert record.quarantined
    with pytest.raises(registry.ContaminatedResultError, match="QUARANTINED"):
        record.metrics()
    payload = record.to_dict()
    assert payload["metrics"] == "WITHHELD — quarantined"
    assert "40" not in str(payload["metrics"])


def test_quarantined_models_are_excluded_from_usable():
    reg = registry.default_registry()
    versions = {r.version for r in reg.usable()}
    assert "RN1_V1_FORWARD_VALIDATOR_CONTAMINATED" not in versions
    assert "RN1_STRATEGY_MODEL_V1" in versions


def test_a_registered_version_is_never_overwritten():
    reg = registry.default_registry()
    with pytest.raises(ValueError, match="never overwritten"):
        reg.register(registry.ModelRecord(version="RN1_STRATEGY_MODEL_V1"))


def test_candidate_a_and_b_are_preserved_verbatim():
    reg = registry.default_registry()
    a = reg.get("CANDIDATE_A1_60_79_SINGLE_BUY_3M").metrics()
    b = reg.get("CANDIDATE_B_60_79_PERSIST_30M").metrics()
    assert a["qualified"] == 25 and a["settled"] == 9
    assert a["settledWinRate"] == pytest.approx(0.7778)
    assert b["settledExecutions"] == 53 and b["record"] == "40-13"
    assert b["winRate"] == pytest.approx(0.7547)
    assert b["simulatedPnlUsd"] == pytest.approx(11.29)


def test_v1_records_its_full_provenance():
    record = registry.default_registry().get("RN1_STRATEGY_MODEL_V1")
    payload = record.to_dict()
    for field in ("version", "status", "features", "thresholds",
                  "labelDefinition", "prospectiveBoundary", "createdTs"):
        assert payload[field], field
    assert payload["thresholds"]["aggressivePriceMax"] == 0.20
    assert payload["thresholds"]["directionalPriceMin"] == 0.80


# ===========================================================================
# 7. STRUCTURE DISCOVERY — the null must be able to win
# ===========================================================================


def test_pure_noise_yields_no_robust_structure():
    """The most important test in the file. A search that cannot return
    NO_ROBUST_STRUCTURE_FOUND is a pattern generator."""
    import random

    rng = random.Random(7)
    rows, labels = [], []
    for index in range(2_000):
        rows.append({"wallet": f"w{index % 40}",
                     "initial_price": rng.random(),
                     "initial_capital": rng.random() * 100,
                     "first_buy_ts": 1_700_000_000 + index * 37,
                     "category": rng.choice(["a", "b", "c"]),
                     "same_side_buys": rng.randint(1, 5),
                     "seconds_to_add": rng.random() * 5_000})
        labels.append(rng.choice([DIRECTIONAL, PROTECT, AGGRESSIVE]))
    report = structure.discover(rows, labels, trials=15)
    assert report.verdict == structure.NOT_FOUND
    assert report.survived == []
    assert report.examined > 5          # it really did look


def test_a_planted_relationship_is_found():
    """...and one that cannot find a real relationship is useless."""
    rows, labels = [], []
    for index in range(2_000):
        buys = 1 if index % 2 else 5
        rows.append({"wallet": "w", "initial_price": 0.5,
                     "initial_capital": 10.0,
                     "first_buy_ts": 1_700_000_000 + index * 37,
                     "category": "a", "same_side_buys": buys,
                     "seconds_to_add": 100.0})
        labels.append(AGGRESSIVE if buys == 5 else DIRECTIONAL)
    report = structure.discover(rows, labels, trials=15)
    assert report.verdict == structure.FOUND
    assert any(c.name == "same_side_buy_count" for c in report.survived)


def test_the_search_scale_is_always_reported():
    report = structure.discover([], [], trials=5)
    payload = report.to_dict()
    assert "candidatesExamined" in payload
    assert payload["verdict"] == structure.NOT_FOUND


def test_a_tiny_sample_is_refused_rather_than_mined():
    candidate = structure.score_candidate(
        "x", "x", ["a", "b"] * 5, [DIRECTIONAL, PROTECT] * 5)
    assert candidate.survived is False
    assert "below the" in candidate.reason


# ===========================================================================
# 8. THE LEAN ADAPTER
# ===========================================================================


def test_the_adapter_returns_nothing_when_disabled():
    from pqb.config import WalletStateResearchConfig
    from pqb.wallet_state_research import lean_adapter

    class _Cfg:
        wallet_state_research = WalletStateResearchConfig()

    features = lean_adapter.get_alpha_features(_Cfg())
    assert features is lean_adapter.NO_ALPHA_FEATURES
    assert features.available is False
    assert lean_adapter.may_influence(features) is False


def test_the_alpha_features_carry_no_trading_decision():
    from pqb.wallet_state_research import lean_adapter

    payload = lean_adapter.WalletStateAlphaFeatures().to_dict()
    for forbidden in ("Side", "Size", "Weight", "Quantity", "Order",
                      "Action", "Direction"):
        assert forbidden not in payload


def test_unknown_values_are_none_not_zero():
    from pqb.wallet_state_research import lean_adapter

    features = lean_adapter.NO_ALPHA_FEATURES
    assert features.aggressive_probability is None
    assert features.data_quality_score is None


def test_a_v1_prediction_translates_with_its_provenance():
    from pqb.wallet_state_research import lean_adapter

    prediction = strategy_v1.RN1StrategyModelV1().predict(
        _episode(0.15, 10.0),
        strategy_v1.eligibility(_episode(0.15, 10.0)))
    features = lean_adapter.from_v1_prediction(prediction, stage="observe")
    assert features.available
    assert features.aggressive_probability == 1.0
    assert features.directional_probability == 0.0
    assert features.model_version == "RN1_STRATEGY_MODEL_V1"
    assert features.information_cutoff_timestamp
    # A rule is certain, not calibrated. Claiming confidence would claim a
    # calibration nothing has measured.
    assert features.wallet_state_confidence is None
    assert lean_adapter.may_influence(features) is False


def test_may_influence_is_true_only_at_the_influence_stage():
    from pqb.wallet_state_research import lean_adapter

    features = lean_adapter.WalletStateAlphaFeatures(
        available=True, stage="influence")
    assert lean_adapter.may_influence(features) is True
    assert lean_adapter.may_influence(
        lean_adapter.WalletStateAlphaFeatures(
            available=True, stage="observe")) is False
