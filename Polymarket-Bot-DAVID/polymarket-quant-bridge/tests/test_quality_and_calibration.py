"""§30 data quality and §18 probabilistic metrics.

Both exist to stop the same failure: a number that looks like the same kind of
number as another one when it is not. A prediction from a two-event condition
with no book and no settlement is not the same object as one from a deep tape
with a resolved market behind it, and a rule that asserts 100% and is wrong a
third of the time is not "67% accurate" in any sense a trader should act on.
"""

from __future__ import annotations

import pytest

from pqb.wallet_state_research import quality
from pqb.wallet_state_research.episodes import (AGGRESSIVE, DIRECTIONAL,
                                                PROTECT, build_episodes)
from pqb.wallet_state_research.events import WalletEvent
from pqb.wallet_state_research.strategy_v1 import MulticlassReport

T0 = 1_787_300_000.0
YES, NO = "tokY", "tokN"


def _event(token, side, ts, price=0.5, shares=10.0):
    return WalletEvent(wallet="0xw", market_id="m1", token_id=token,
                       outcome="Yes" if token == YES else "No", side=side,
                       ts=ts, price=price, shares=shares, usdc=price * shares,
                       question="Will X happen?")


class _Quote:
    def __init__(self, source, depth=None):
        self.source, self.depth = source, depth
        self.available = source != "unavailable"


# -- §30 ---------------------------------------------------------------------


def _rich_episode():
    events = [_event(YES, "BUY", T0 + i * 60, shares=10.0) for i in range(4)]
    events.append(_event(NO, "BUY", T0 + 400, shares=20.0))
    return build_episodes(events, tape_end_ts=T0 + 30 * 86_400,
                          settled_markets={"m1"})[0]


def _thin_episode():
    events = [_event(YES, "BUY", T0), _event(NO, "BUY", T0)]
    return build_episodes(events, tape_end_ts=T0 + 60)[0]


def test_a_rich_reconstruction_outscores_a_thin_one():
    rich = _rich_episode()
    thin = _thin_episode()
    good = quality.score(rich, rich.snapshot(3.0), quote=_Quote("book", 500.0),
                         settled=True, wallet_prior_conditions=30,
                         category_is_heuristic=False)
    bad = quality.score(thin, thin.snapshot(3.0), quote=None,
                        settled=False, wallet_prior_conditions=0)
    assert good.score > bad.score
    assert good.tier == quality.HIGH
    assert bad.tier == quality.LOW


def test_every_component_the_spec_names_is_scored():
    episode = _rich_episode()
    scored = quality.score(episode, episode.snapshot(3.0))
    assert set(scored.components) == set(quality.COMPONENTS)
    for name in quality.COMPONENTS:
        assert scored.reasons[name], name


def test_the_score_is_multiplicative_so_one_zero_sinks_it():
    """An average lets seven good components hide one fatal one."""
    episode = _rich_episode()
    with_book = quality.score(episode, episode.snapshot(3.0),
                              quote=_Quote("book", 500.0), settled=True,
                              wallet_prior_conditions=30)
    without = quality.score(episode, episode.snapshot(3.0),
                            quote=_Quote("unavailable"), settled=True,
                            wallet_prior_conditions=30)
    assert without.score < with_book.score * 0.7
    assert without.weakest == "order_book_completeness"


def test_a_shared_timestamp_is_penalised():
    """All events in one second means ordering rests on insertion order, not
    on time — which is exactly where 'which side first' gets decided."""
    events = [_event(YES, "BUY", T0), _event(YES, "BUY", T0),
              _event(NO, "BUY", T0)]
    episode = build_episodes(events, tape_end_ts=T0 + 30 * 86_400)[0]
    scored = quality.score(episode, episode.snapshot(3.0))
    assert scored.components["timestamp_quality"] <= 0.2
    assert "one second" in scored.reasons["timestamp_quality"]


def test_settlement_completeness_ranks_the_four_label_qualities():
    events = [_event(YES, "BUY", T0), _event(NO, "BUY", T0 + 60)]
    truncated = build_episodes(events, tape_end_ts=T0 + 120)[0]
    quiet = build_episodes(events, tape_end_ts=T0 + 30 * 86_400)[0]
    redeemed = build_episodes(events, tape_end_ts=T0 + 120,
                              redemptions={("0xw", "m1"): T0 + 100})[0]
    resolved = build_episodes(events, tape_end_ts=T0 + 120,
                              settled_markets={"m1"})[0]
    values = [quality.score(e, e.snapshot(3.0)).components[
        "settlement_completeness"]
        for e in (truncated, quiet, redeemed, resolved)]
    assert values == sorted(values), values


def test_a_missing_snapshot_does_not_raise():
    """A quality score that cannot be computed for a poor input would defeat
    its own purpose."""
    episode = _thin_episode()
    scored = quality.score(episode, None, quote=None)
    assert 0.0 < scored.score <= 1.0
    assert scored.tier in (quality.LOW, quality.MEDIUM, quality.HIGH)


def test_the_summary_reports_the_weakest_link():
    episode = _rich_episode()
    scores = [quality.score(episode, episode.snapshot(3.0),
                            quote=_Quote("unavailable")) for _ in range(5)]
    summary = quality.summarise(scores)
    assert summary["available"]
    assert summary["predictions"] == 5
    assert "order_book_completeness" in summary["weakestComponentCounts"]
    assert set(summary["componentMeans"]) == set(quality.COMPONENTS)


def test_an_empty_population_says_so():
    assert quality.summarise([])["available"] is False


# -- §18 ---------------------------------------------------------------------


def _report_with(pairs, probabilities=None):
    report = MulticlassReport()
    for predicted, actual in pairs:
        report.record(predicted, actual,
                      probabilities or {DIRECTIONAL: 0.0, PROTECT: 0.0,
                                        AGGRESSIVE: 0.0, predicted: 1.0})
    return report


def test_a_confident_and_wrong_rule_is_punished_by_log_loss():
    """The whole point of §18: accuracy alone cannot see overconfidence."""
    right = _report_with([(PROTECT, PROTECT)] * 20)
    wrong = _report_with([(PROTECT, AGGRESSIVE)] * 20)
    a = right.probabilistic()
    b = wrong.probabilistic()
    assert a["logLoss"] < b["logLoss"]
    assert a["brierScore"] < b["brierScore"]


def test_log_loss_is_finite_even_for_a_one_hot_miss():
    """An unclipped one-hot vector produces infinity the moment it is wrong,
    which is a number nobody can compare against anything."""
    report = _report_with([(PROTECT, AGGRESSIVE)] * 5)
    metrics = report.probabilistic()
    assert metrics["logLoss"] < float("inf")
    assert metrics["clipEpsilon"] > 0


def test_calibration_bins_carry_their_counts():
    report = _report_with([(PROTECT, PROTECT)] * 10
                          + [(PROTECT, AGGRESSIVE)] * 10)
    calibration = report.probabilistic()["calibration"]
    assert calibration["available"]
    for row in calibration["bins"]:
        assert row["n"] > 0
        assert "meanPredicted" in row and "observedFrequency" in row
    assert 0.0 <= calibration["expectedCalibrationError"] <= 1.0


def test_a_perfectly_calibrated_rule_has_near_zero_error():
    report = _report_with([(PROTECT, PROTECT)] * 30)
    calibration = report.probabilistic()["calibration"]
    assert calibration["expectedCalibrationError"] == pytest.approx(0.0,
                                                                   abs=1e-9)


def test_probabilistic_metrics_are_absent_when_no_vectors_recorded():
    report = MulticlassReport()
    report.record(PROTECT, PROTECT)          # no probabilities supplied
    assert report.probabilistic()["available"] is False


def test_the_metrics_ride_on_the_report_payload():
    report = _report_with([(PROTECT, PROTECT)] * 12)
    payload = report.to_dict()
    assert payload["probabilistic"]["available"] is True
    assert "logLoss" in payload["probabilistic"]
    assert "calibration" in payload["probabilistic"]
