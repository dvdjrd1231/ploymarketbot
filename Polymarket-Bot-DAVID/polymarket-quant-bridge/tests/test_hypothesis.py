"""The hypothesis layer proposes, ranks and connects. It never validates.

The layer's whole value rests on one distinction: same-source repetition is
not independent-source convergence. Three quant rules over one series are one
observation wearing three hats. Counting them as three confirmations is
precisely how a research system talks itself into a phantom edge, so most of
these tests are about refusing to do that.
"""

from __future__ import annotations

import pytest

from pqb.convergence import (ADVERSARIAL_TESTS, adversarial_verdict,
                             beats_baseline, convergence_groups,
                             failure_states, hypotheses_from_candidates,
                             inverse_question, staged_combinations, synthesize)
from pqb.hypothesis import (ADVERSARIAL_TEST, PROMOTED_TO_CANDIDATE, REJECTED,
                            SUPPORTED, WEAKENING, Hypothesis, HypothesisStore,
                            Observation, convergence_priority,
                            independent_sources, normalize)
from pqb.library import StrategyLibrary
from pqb.sources import (SOURCE_QUANT, SOURCE_SEQUENCE_STATE,
                         SOURCE_WALLET_BEHAVIOR)


def _store(tmp_path):
    return HypothesisStore(tmp_path / "hypotheses.sqlite3")


# -- normalisation ------------------------------------------------------------

def test_different_engines_describing_one_phenomenon_normalise_together():
    """§5. The engines write in different terms; the layer compares claims
    about the market, not spellings."""
    quant = normalize({"type": "threshold", "entry_feature": "price",
                       "entry_op": "<", "direction": "long",
                       "prob_lo": 0.55, "prob_hi": 0.65})
    sequence = normalize({"type": "sequence",
                          "chain": ["price_down_impulse", "stabilisation"],
                          "direction": "up", "prob_lo": 0.55,
                          "prob_hi": 0.65})

    # Both say: a decline in the mid band favours going long.
    assert quant.direction == sequence.direction == "long"
    assert quant.probability_region == sequence.probability_region == "mid"
    assert quant.price_movement == "decline"
    assert sequence.price_movement == "price_down_impulse"


def test_similarity_ignores_fields_neither_observation_populates():
    """Two nearly-empty observations must not read as a perfect match."""
    sparse_a = Observation(direction="long")
    sparse_b = Observation(direction="short")
    assert sparse_a.similarity(sparse_b) == 0.0

    same = Observation(direction="long", probability_region="mid")
    assert same.similarity(same) == 1.0


# -- correlation control ------------------------------------------------------

def test_same_source_repetition_is_not_independent_confirmation():
    """§16, and the reason the layer exists at all."""
    same_engine = [{"source": SOURCE_QUANT, "markets": {"A", "B"}},
                   {"source": SOURCE_QUANT, "markets": {"C", "D"}},
                   {"source": SOURCE_QUANT, "markets": {"E", "F"}}]
    assert independent_sources(same_engine) == 1

    distinct = [{"source": SOURCE_QUANT, "markets": {"A", "B"}},
                {"source": SOURCE_SEQUENCE_STATE, "markets": {"C", "D"}},
                {"source": SOURCE_WALLET_BEHAVIOR, "markets": {"E", "F"}}]
    assert independent_sources(distinct) == 3


def test_different_engines_over_the_same_markets_are_not_independent():
    """Data independence, not just engine independence. A wallet behaviour
    mined from the five markets a sequence chain was found in is not a second
    opinion about those markets."""
    overlapping = [{"source": SOURCE_QUANT, "markets": {"A", "B", "C"}},
                   {"source": SOURCE_SEQUENCE_STATE,
                    "markets": {"A", "B", "C"}}]
    assert independent_sources(overlapping) == 1

    separate = [{"source": SOURCE_QUANT, "markets": {"A", "B", "C"}},
                {"source": SOURCE_SEQUENCE_STATE, "markets": {"X", "Y", "Z"}}]
    assert independent_sources(separate) == 2


def test_convergence_raises_priority_and_can_never_be_evidence():
    """§4 and §12: 'investigate this harder', never 'this works'."""
    hypothesis = Hypothesis(signature="s", supporting=20, contradicting=0,
                            markets={f"M{i}" for i in range(15)},
                            periods={"2025-01", "2025-02", "2025-03",
                                     "2025-04"},
                            regimes={"calm", "volatile", "thin"},
                            adversarial={"inverse": "survived"},
                            failure_states=["thin liquidity"])

    alone = convergence_priority(hypothesis, independent=1)
    converged = convergence_priority(hypothesis, independent=3)

    assert converged > alone > 0
    # It is a research-priority number and touches nothing else.
    assert hypothesis.status != SUPPORTED
    assert not hypothesis.is_supported()


def test_a_supported_hypothesis_is_not_a_validated_strategy():
    """The most important line in the layer."""
    hypothesis = Hypothesis(signature="s", status=SUPPORTED)
    assert hypothesis.is_supported()
    # SUPPORTED lives in a different vocabulary from the ladder's statuses.
    assert hypothesis.status not in ("validated", "high_confidence",
                                     "tradable")


# -- grouping -----------------------------------------------------------------

def test_convergence_groups_link_without_merging(tmp_path):
    """§5: create a relationship, preserve the originals independently."""
    rows = [
        {"id": "q#v1", "source": SOURCE_QUANT,
         "rule": {"type": "threshold", "entry_feature": "price",
                  "entry_op": "<", "direction": "long", "prob_lo": 0.55,
                  "prob_hi": 0.65},
         "markets": ["A", "B"], "oos_trades": 30, "oos_expectancy": 0.02},
        {"id": "w#v1", "source": SOURCE_WALLET_BEHAVIOR,
         "rule": {"type": "wallet_behavior", "direction": "long",
                  "band": "mid", "trigger": "price_decline"},
         "markets": ["X", "Y"], "oos_trades": 30, "oos_expectancy": 0.03},
    ]
    hypotheses = hypotheses_from_candidates(rows)

    # Two distinct hypotheses, not one merged record.
    assert len(hypotheses) == 2
    assert all(len(h.candidates) == 1 for h in hypotheses)

    groups = convergence_groups(hypotheses, threshold=0.4)
    assert len(groups) == 1
    assert groups[0]["members_total"] == 2
    assert groups[0]["independent"] == 2       # different engines, different markets


def test_a_candidate_with_no_evidence_votes_neither_way():
    """Silence is not agreement."""
    rows = [{"id": "a#v1", "source": SOURCE_QUANT,
             "rule": {"type": "threshold", "entry_feature": "price",
                      "entry_op": "<", "direction": "long"},
             "oos_trades": 0, "oos_expectancy": 0.0}]
    hypothesis = hypotheses_from_candidates(rows)[0]
    assert hypothesis.supporting == 0
    assert hypothesis.contradicting == 0


# -- inverse and adversarial --------------------------------------------------

def test_the_inverse_is_a_question_not_an_assumption():
    """§6. A relationship whose inverse pays equally well is not an edge."""
    hypothesis = Hypothesis(signature="s",
                            pattern=Observation(direction="long",
                                                probability_region="mid"))
    inverse = inverse_question(hypothesis)
    assert inverse.direction == "short"
    assert inverse.probability_region == "mid"    # only direction flips

    # No direction, no inverse to ask about.
    assert inverse_question(Hypothesis(signature="s")) is None


def test_one_decisive_failure_rejects_but_surviving_only_earns_supported():
    """The asymmetry is the point of an adversarial stage."""
    survived = {name: "survived" for name, _m in ADVERSARIAL_TESTS}
    status, reason = adversarial_verdict(survived)
    assert status == SUPPORTED and "survived" in reason

    broken = dict(survived, cost_stress="failed")
    status, reason = adversarial_verdict(broken)
    assert status == REJECTED and "cost_stress" in reason


def test_an_inverse_that_wins_weakens_rather_than_deletes():
    """§13: record failures instead of deleting them. A relationship that is
    real and backwards is a finding."""
    results = {name: "survived" for name, _m in ADVERSARIAL_TESTS}
    results["inverse"] = "inverse_won"
    status, reason = adversarial_verdict(results)
    assert status == WEAKENING
    assert "backwards" in reason


def test_an_unattacked_hypothesis_is_not_thereby_promising():
    status, reason = adversarial_verdict({})
    assert status == ADVERSARIAL_TEST and "not yet attacked" in reason

    status, _reason = adversarial_verdict({"inverse": "survived"})
    assert status == ADVERSARIAL_TEST          # one test is not a battery


def test_failure_states_record_where_the_edge_disappears():
    """§7. A failure state that survives testing becomes a usable filter,
    which is why it is stored rather than merely mentioned."""
    hypothesis = Hypothesis(signature="s")
    states = failure_states(hypothesis, {"thin_liquidity": -0.03,
                                         "high_volatility": 0.02,
                                         "near_resolution": -0.01})
    assert states == ["near_resolution (expectancy -0.0100)",
                      "thin_liquidity (expectancy -0.0300)"]


# -- synthesis and complexity -------------------------------------------------

def test_synthesis_is_staged_not_combinatorial():
    """§8. Four components would give fifteen subsets; a combinatorial
    explosion is not a search, it is a guarantee of finding something."""
    combos = staged_combinations(["A", "B", "C", "D"], max_size=2)
    assert all(len(c) <= 2 for c in combos)
    assert ("A",) in combos and ("A", "B") in combos
    assert ("A", "B", "C") not in combos


def test_complexity_must_be_paid_for():
    """§9 and §14: a complicated strategy must beat its simpler baseline by
    a margin that grows with the conditions it adds."""
    # Marginally better, three extra conditions: not enough.
    assert not beats_baseline(candidate_expectancy=0.021,
                              baseline_expectancy=0.020,
                              candidate_complexity=8, baseline_complexity=5)
    # Materially better: earns its complexity.
    assert beats_baseline(candidate_expectancy=0.045,
                          baseline_expectancy=0.020,
                          candidate_complexity=8, baseline_complexity=5)
    # Same complexity, any improvement counts.
    assert beats_baseline(0.021, 0.020, 5, 5)


def test_synthesised_candidates_start_from_zero_and_exclude_their_source(
        tmp_path):
    """§17 and §22: the new layer must never skip stages. A synthesised
    candidate is an ordinary candidate — no inherited evidence, and the
    markets that suggested it can never testify for it."""
    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    hypothesis = Hypothesis(signature="sig", status=SUPPORTED,
                            relationship="decline then recovery",
                            markets={"SRC1", "SRC2"},
                            sources=[SOURCE_QUANT, SOURCE_WALLET_BEHAVIOR])

    specs = synthesize(hypothesis,
                       {"type": "threshold", "entry_feature": "price"},
                       {"vol": {"volatility": "high"},
                        "liq": {"liquidity": "deep"}}, max_size=2)

    assert specs, "synthesis produced nothing"
    # Simplest first: the cheap explanation is tested before the elaborate one.
    assert len(specs[0].rule.get("components")) == 1

    for spec in specs:
        sid = spec.register(lib)
        assert lib.cumulative(sid)["markets"] == 0      # zero inherited
        assert {"SRC1", "SRC2"} <= lib.excluded_markets(sid)
        row = next(r for r in lib.all_strategies() if r["id"] == sid)
        assert row["status"] == "new"                   # queues like anything else


def test_an_unsupported_hypothesis_synthesises_nothing():
    """No stage may be skipped, including this one."""
    assert synthesize(Hypothesis(signature="s", status=ADVERSARIAL_TEST),
                      {"type": "threshold"}, {"a": {"x": 1}}) == []


# -- persistence and auditability ---------------------------------------------

def test_hypothesis_states_are_appended_never_overwritten(tmp_path):
    """§20."""
    store = _store(tmp_path)
    hid = store.upsert(Hypothesis(signature="",
                                  pattern=Observation(direction="long",
                                                      probability_region="mid"),
                                  relationship="r"))
    store.set_status(hid, ADVERSARIAL_TEST, "enough observations")
    store.set_status(hid, SUPPORTED, "survived the battery")

    history = store.history(hid)
    assert [h["to_status"] for h in history] == ["NEW", ADVERSARIAL_TEST,
                                                 SUPPORTED]
    assert history[-1]["from_status"] == ADVERSARIAL_TEST
    assert store.get(hid).status == SUPPORTED


def test_a_materially_changed_hypothesis_becomes_a_new_version(tmp_path):
    """A hypothesis must not be quietly redefined into one that fits the
    evidence it has already collected."""
    store = _store(tmp_path)
    first = Hypothesis(pattern=Observation(direction="long",
                                           probability_region="mid"),
                       relationship="r")
    store.upsert(first)
    second = Hypothesis(pattern=Observation(direction="long",
                                            probability_region="favourite"),
                        relationship="r2")
    store.upsert(second)

    assert first.id != second.id
    assert len(store.all()) == 2


def test_adversarial_results_are_persisted_including_failures(tmp_path):
    store = _store(tmp_path)
    hid = store.upsert(Hypothesis(pattern=Observation(direction="long"),
                                  relationship="r"))
    store.record_adversarial(hid, "cost_stress", "failed",
                             "edge gone at 4% round trip")

    stored = store.get(hid).adversarial["cost_stress"]
    assert stored["result"] == "failed"
    assert "4%" in stored["detail"]


def test_an_unknown_status_is_refused(tmp_path):
    store = _store(tmp_path)
    hid = store.upsert(Hypothesis(pattern=Observation(direction="long")))
    with pytest.raises(ValueError, match="unknown hypothesis status"):
        store.set_status(hid, "validated", "nice try")


def test_the_dashboard_block_is_separate_from_validation_metrics(tmp_path):
    """§21: do not mix these metrics with validation metrics."""
    store = _store(tmp_path)
    hid = store.upsert(Hypothesis(pattern=Observation(direction="long")))
    store.set_status(hid, SUPPORTED, "survived")

    summary = store.summary()
    assert summary["hypothesesTotal"] == 1
    assert summary["hypothesesByStatus"][SUPPORTED] == 1
    # Nothing here claims anything about validation, evidence or tradability.
    forbidden = {"validated", "tradable", "evidence", "oosTrades",
                 "oosMarkets", "confidence"}
    assert not forbidden & set(summary)
