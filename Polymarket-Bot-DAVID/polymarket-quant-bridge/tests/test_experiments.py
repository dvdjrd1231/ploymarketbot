"""A failed hypothesis must leave information behind, and that information
must not be allowed to close the search.

The two halves are in tension and both are load-bearing. Remembering nothing
means rediscovering the same dead idea forever; remembering too confidently
means a family that failed three times for a reason that was never about the
idea — too few markets, a replay that crashed — gets quietly locked out and
the search narrows itself. The tests below pin exactly where that line sits.
"""

from __future__ import annotations

from pqb.adversarial import FAILED, INVERSE_WON, SURVIVED, AdversarialReport
from pqb.config import ResearchConfig
from pqb.experiments import (ADVERSARIAL_FAILURE, DATA_QUALITY_FAILURE,
                             DEAD_END_STRIKES, EXCESSIVE_COSTS,
                             INSUFFICIENT_MARKET_BREADTH, NEGATIVE_EXPECTANCY,
                             NOT_DIRECTIONAL, PARAMETER_SENSITIVITY,
                             R_FAILED, R_INCONCLUSIVE, R_PROMISING,
                             R_VALIDATED, RULE_NEVER_FIRED,
                             SINGLE_MARKET_DEPENDENCE, TEMPORAL_FAILURE,
                             INDISTINGUISHABLE_FROM_RANDOM,
                             LIQUIDITY_DEPENDENCE,
                             Experiment, ExperimentStore, classify,
                             from_candidate, next_question)


CFG = ResearchConfig()


def _cum(trades=0, markets=0, expectancy=0.0, top_share=0.0):
    return {"trades": trades, "markets": markets, "expectancy": expectancy,
            "top_share": top_share, "drawdown": 0.0, "forward_markets": 0,
            "markets_by_temporal_class": {}}


def _report(**results) -> AdversarialReport:
    return AdversarialReport(candidate_id="c", results=dict(results),
                             verdict="BROKEN" if FAILED in results.values()
                             else "SURVIVED")


def _store(tmp_path) -> ExperimentStore:
    return ExperimentStore(tmp_path / "experiments.sqlite3")


# -- classification -----------------------------------------------------------

def test_a_rule_that_never_fired_is_not_a_rule_that_lost():
    """The most important distinction in the taxonomy. One is a
    non-observation, the other is a refutation, and collapsing them makes an
    untested idea look tried."""
    result, reason = classify("validating", _cum(), CFG,
                              attempts={"zeroTrades": 5})
    assert (result, reason) == (R_INCONCLUSIVE, RULE_NEVER_FIRED)

    crashed, why = classify("validating", _cum(), CFG,
                            attempts={"errors": 3})
    assert (crashed, why) == (R_FAILED, DATA_QUALITY_FAILURE)


def test_an_attack_that_landed_outranks_the_summary_statistics():
    """The statistics are what the attack was attacking, so letting them
    have the last word would make the battery decorative."""
    healthy = _cum(trades=80, markets=6, expectancy=0.05)
    result, reason = classify("validating", healthy, CFG,
                              _report(temporal_split=FAILED))
    assert (result, reason) == (R_FAILED, TEMPORAL_FAILURE)

    sensitive, why = classify("validating", healthy, CFG,
                              _report(neighbour_thresholds=FAILED))
    assert why == PARAMETER_SENSITIVITY

    costly, why_costly = classify("validating", healthy, CFG,
                                  _report(cost_stress=FAILED))
    assert why_costly == EXCESSIVE_COSTS


def test_concentration_outranks_the_failures_it_causes():
    """A record that is one market's story fails the temporal split and the
    subset split too — but as a CONSEQUENCE. Reporting 'decaying' would send
    the search after a regime that was never there, when the real finding is
    that the idea has not been tested yet."""
    result, reason = classify(
        "validating", _cum(trades=80, markets=5, expectancy=0.15,
                           top_share=0.99), CFG,
        _report(leave_one_market_out=FAILED, temporal_split=FAILED,
                market_subsets=FAILED))
    assert (result, reason) == (R_INCONCLUSIVE, SINGLE_MARKET_DEPENDENCE)
    assert next_question(reason)[0] == "NEEDS_BREADTH"


def test_both_directions_paying_is_recorded_as_not_directional():
    result, reason = classify("validating", _cum(trades=60, markets=5,
                                                 expectancy=0.03), CFG,
                              _report(inverse=FAILED))
    assert (result, reason) == (R_FAILED, NOT_DIRECTIONAL)


def test_the_inverse_winning_is_a_finding_not_a_failure():
    result, reason = classify("validating", _cum(trades=60, markets=5,
                                                 expectancy=-0.01), CFG,
                              _report(inverse=INVERSE_WON))
    assert result == R_INCONCLUSIVE
    assert reason == NOT_DIRECTIONAL


def test_concentration_reads_as_untested_rather_than_refuted():
    """One market's story cannot refute an idea any more than it can prove
    one, and the directive it produces says so."""
    result, reason = classify("validating",
                              _cum(trades=60, markets=3, expectancy=0.04,
                                   top_share=0.9), CFG)
    assert (result, reason) == (R_INCONCLUSIVE, SINGLE_MARKET_DEPENDENCE)
    directive, _why = next_question(reason)
    assert directive == "NEEDS_BREADTH"


def test_successes_are_classified_too():
    """A memory holding only failures cannot answer what kinds of thing have
    worked, which is half of what a research record is for."""
    assert classify("validated", _cum(trades=100, markets=8,
                                      expectancy=0.05), CFG)[0] == R_VALIDATED
    assert classify("validating", _cum(trades=60, markets=5,
                                       expectancy=0.04), CFG)[0] == R_PROMISING


def test_every_reason_that_can_be_produced_has_a_next_question():
    """A classification with no follow-up is a label, not a lesson."""
    reachable = [NEGATIVE_EXPECTANCY, INSUFFICIENT_MARKET_BREADTH,
                 SINGLE_MARKET_DEPENDENCE, EXCESSIVE_COSTS,
                 PARAMETER_SENSITIVITY, TEMPORAL_FAILURE, RULE_NEVER_FIRED,
                 DATA_QUALITY_FAILURE, ADVERSARIAL_FAILURE, NOT_DIRECTIONAL]
    for reason in reachable:
        directive, why = next_question(reason)
        assert directive and why, reason


# -- the memory ---------------------------------------------------------------

def test_an_unchanged_verdict_is_not_written_again(tmp_path):
    """Two thousand candidates re-examined hourly would otherwise write two
    thousand identical rows an hour and bury the moments something moved."""
    store = _store(tmp_path)
    try:
        exp = Experiment(candidate_id="c#v1", family="F",
                         result=R_FAILED, failure_reason=NEGATIVE_EXPECTANCY,
                         status="rejected", trades=40, markets=4)
        assert store.record(exp) is True
        assert store.record(exp) is False
        assert len(store.history("c#v1")) == 1

        exp.trades = 90                     # the evidence genuinely moved
        assert store.record(exp) is True
        assert len(store.history("c#v1")) == 2
    finally:
        store.close()


def test_a_family_is_throttled_only_after_repeated_identical_failures(tmp_path):
    store = _store(tmp_path)
    try:
        for i in range(DEAD_END_STRIKES - 1):
            store.record(Experiment(candidate_id=f"c{i}", family="F",
                                    result=R_FAILED,
                                    failure_reason=PARAMETER_SENSITIVITY))
        assert store.is_dead_end("F") == (False, "")

        store.record(Experiment(candidate_id="cLast", family="F",
                                result=R_FAILED,
                                failure_reason=PARAMETER_SENSITIVITY))
        dead, why = store.is_dead_end("F")
        assert dead and PARAMETER_SENSITIVITY in why
    finally:
        store.close()


def test_one_candidate_reclassified_twice_is_one_strike(tmp_path):
    """Otherwise a single stubborn candidate could close its own family by
    being looked at three times."""
    store = _store(tmp_path)
    try:
        for trades in (20, 40, 60):
            store.record(Experiment(candidate_id="same", family="F",
                                    trades=trades, result=R_FAILED,
                                    failure_reason=PARAMETER_SENSITIVITY))
        assert store.is_dead_end("F")[0] is False
    finally:
        store.close()


def test_failures_that_are_about_the_data_never_accumulate(tmp_path):
    """Suppressing a family because it has not been given enough markets is
    the starvation loop arriving by a different route."""
    store = _store(tmp_path)
    try:
        for reason in (INSUFFICIENT_MARKET_BREADTH, SINGLE_MARKET_DEPENDENCE,
                       RULE_NEVER_FIRED, DATA_QUALITY_FAILURE):
            for i in range(DEAD_END_STRIKES + 2):
                store.record(Experiment(candidate_id=f"{reason}{i}",
                                        family="F", result=R_FAILED,
                                        failure_reason=reason))
        assert store.is_dead_end("F") == (False, "")
    finally:
        store.close()


def test_any_success_in_a_family_reopens_it(tmp_path):
    """The memory exists to stop repetition, not to close a search that has
    just shown it was worth running."""
    store = _store(tmp_path)
    try:
        for i in range(DEAD_END_STRIKES):
            store.record(Experiment(candidate_id=f"c{i}", family="F",
                                    result=R_FAILED,
                                    failure_reason=PARAMETER_SENSITIVITY))
        assert store.is_dead_end("F")[0] is True

        store.record(Experiment(candidate_id="winner", family="F",
                                result=R_PROMISING))
        assert store.is_dead_end("F")[0] is False
    finally:
        store.close()


def test_only_the_latest_directive_per_candidate_is_open(tmp_path):
    """Showing a candidate's whole history as open questions would present
    resolved ones as outstanding."""
    store = _store(tmp_path)
    try:
        store.record(Experiment(candidate_id="c", family="F", trades=10,
                                result=R_FAILED,
                                failure_reason=EXCESSIVE_COSTS,
                                directive="LENGTHEN_HOLD"))
        store.record(Experiment(candidate_id="c", family="F", trades=90,
                                result=R_FAILED,
                                failure_reason=TEMPORAL_FAILURE,
                                directive="ADD_REGIME_CONDITION"))
        assert store.latest_directives() == {"c": "ADD_REGIME_CONDITION"}
        assert len(store.directives()) == 1
    finally:
        store.close()


def test_from_candidate_builds_the_record_without_touching_the_library(tmp_path):
    entry = {"id": "c#v1", "signature": "sig", "family": "F",
             "source": "QUANT", "status": "validating",
             "rule": {"type": "threshold", "entry_feature": "flow_z"},
             "in_win": 0.8}
    experiment = from_candidate(
        entry, _cum(trades=60, markets=5, expectancy=0.04), CFG,
        adversarial=_report(market_subsets=SURVIVED), maturity="NEAR_MISS")
    assert experiment.result == R_PROMISING
    assert experiment.features == ["flow_z"]
    assert experiment.adversarial["results"]["market_subsets"] == SURVIVED


def test_the_summary_counts_subjects_not_rows(tmp_path):
    """Row counts grow with how often the pass runs; subject counts describe
    the research."""
    store = _store(tmp_path)
    try:
        for trades in (10, 20, 30):
            store.record(Experiment(candidate_id="c", family="F",
                                    trades=trades, result=R_FAILED,
                                    failure_reason=NEGATIVE_EXPECTANCY))
        out = store.summary()
        assert out["experimentsRecorded"] == 3
        assert out["experimentSubjects"] == 1
        assert out["experimentFailureReasons"][NEGATIVE_EXPECTANCY] == 1
    finally:
        store.close()


# -- the two probe-fed failure classes ----------------------------------------

def test_a_failed_placebo_outranks_every_other_diagnosis():
    """Ordering matters more than the label. If the condition does nothing,
    then "decayed", "did not replicate" and "too concentrated" are all
    descriptions of noise, and each one sends the search after a structure
    that was never there. Asked first, deliberately."""
    healthy = _cum(trades=60, markets=6, expectancy=0.02)
    result, reason = classify(
        "validating", healthy, CFG,
        _report(placebo=FAILED, temporal_split=FAILED,
                market_subsets=FAILED, drawdown_stress=FAILED))
    assert result == R_FAILED
    assert reason == INDISTINGUISHABLE_FROM_RANDOM


def test_a_positive_record_can_still_be_indistinguishable_from_random():
    """The case no other test can reach: everything about the record is
    healthy, and the control says the hold did it."""
    healthy = _cum(trades=60, markets=6, expectancy=0.03)
    result, reason = classify("validating", healthy, CFG,
                              _report(placebo=FAILED))
    assert (result, reason) == (R_FAILED, INDISTINGUISHABLE_FROM_RANDOM)


def test_an_edge_only_in_thin_books_is_a_tradability_failure_not_a_cost_one():
    """EXCESSIVE_COSTS says "lengthen the hold". That advice is actively
    wrong here — the problem is not margin, it is that the fills are not
    there — so the two must not share a reason code."""
    healthy = _cum(trades=60, markets=6, expectancy=0.02)
    result, reason = classify("validating", healthy, CFG,
                              _report(liquidity_stress=FAILED))
    assert result == R_FAILED
    assert reason == LIQUIDITY_DEPENDENCE
    assert reason != EXCESSIVE_COSTS


def test_both_new_reasons_route_to_an_actionable_next_question():
    """A classification with no follow-up is a label, not research."""
    for reason in (INDISTINGUISHABLE_FROM_RANDOM, LIQUIDITY_DEPENDENCE):
        directive, why = next_question(reason)
        assert directive and why
