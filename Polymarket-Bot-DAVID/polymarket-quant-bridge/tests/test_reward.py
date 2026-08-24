"""The reward decides what to research next, and must be incapable of
deciding anything else.

The failure this guards against is specific: a research system rewarded for
producing validated strategies discovers that the cheapest route to the reward
is to lower what validation means. So the reward is deliberately not "how
close is this to validating" — it is "how much would testing this again teach
us", and the tests below pin the places where those two come apart.
"""

from __future__ import annotations

import pytest

from pqb.adversarial import (FAILED, SURVIVED, V_BROKEN, V_SURVIVED,
                             AdversarialReport)
from pqb.config import ResearchConfig
from pqb.reward import (CONVERGENCE_MAX_BONUS, DEAD_END_MULTIPLIER,
                        SCORE_MAX, score, summary)

CFG = ResearchConfig()


def _cum(trades=0, markets=0, expectancy=0.0, top_share=0.0, drawdown=0.0):
    return {"trades": trades, "markets": markets, "expectancy": expectancy,
            "top_share": top_share, "drawdown": drawdown,
            "wins": int(trades * 0.6), "pnl": expectancy * trades,
            "forward_markets": 0, "markets_by_temporal_class": {}}


def _entry(cid="c#v1", status="validating", in_win=0.0):
    return {"id": cid, "status": status, "in_win": in_win,
            "family": "F", "rule": {"type": "sequence"}}


def _attacked(verdict, robustness, coverage=0.8, failed=()):
    results = {name: FAILED for name in failed}
    results.setdefault("market_subsets", SURVIVED)
    return AdversarialReport(candidate_id="c#v1", results=results,
                             robustness=robustness, coverage=coverage,
                             verdict=verdict)


# -- what it rewards ----------------------------------------------------------

def test_an_untested_candidate_is_informative_rather_than_bad():
    """Scoring the unknown as low is the circularity that left 153 of 231
    candidates never evaluated: no evidence means low priority means no
    allocation means no evidence."""
    out = score(_entry(), _cum(), CFG, attempts={})
    assert out.score > 0
    assert any("never evaluated" in r for r in out.rewards)


def test_a_near_miss_outranks_a_well_covered_equivalent():
    """Positive but underpowered is the cheapest possible source of a real
    strategy, because most of the evidence already exists."""
    near = score(_entry("near"), _cum(trades=12, markets=3, expectancy=0.04),
                 CFG, attempts={"evidence": 3})
    covered = score(_entry("covered"),
                    _cum(trades=200, markets=12, expectancy=0.04), CFG,
                    attempts={"evidence": 12},
                    diversity={"categories": 5, "eras": 4, "bands": 3,
                               "temporal_classes": 3})
    assert near.score > 0
    assert any("near miss" in r for r in near.rewards)
    assert not any("near miss" in r for r in covered.rewards)


def test_a_narrow_record_earns_attention_for_being_narrow():
    """§7: a candidate tested in one category over one month has not been
    shown to generalise, and the next market that could show it is worth
    more than another market of the same kind."""
    narrow = score(_entry("narrow"),
                   _cum(trades=60, markets=5, expectancy=0.03), CFG,
                   diversity={"categories": 1, "eras": 1, "bands": 1,
                              "temporal_classes": 1},
                   attempts={"evidence": 5})
    broad = score(_entry("broad"),
                  _cum(trades=60, markets=5, expectancy=0.03), CFG,
                  diversity={"categories": 4, "eras": 3, "bands": 3,
                             "temporal_classes": 2},
                  attempts={"evidence": 5})
    assert narrow.score > broad.score
    assert any("untested outside one" in r for r in narrow.rewards)


def test_surviving_attack_is_rewarded_and_not_merely_unpunished():
    """The gap between un-attacked and survived is what makes the battery
    worth running."""
    record = _cum(trades=80, markets=6, expectancy=0.04)
    unattacked = score(_entry(), record, CFG, attempts={"evidence": 6})
    survived = score(_entry(), record, CFG, attempts={"evidence": 6},
                     adversarial=_attacked(V_SURVIVED, 1.0))
    broken = score(_entry(), record, CFG, attempts={"evidence": 6},
                   adversarial=_attacked(V_BROKEN, 0.25,
                                         failed=("temporal_split",)))
    assert broken.score < unattacked.score < survived.score


def test_a_thin_attack_is_discounted_toward_the_unattacked_baseline():
    """A candidate that could only be asked three questions must not
    outrank one that was asked ten and answered them."""
    record = _cum(trades=80, markets=6, expectancy=0.04)
    thin = score(_entry(), record, CFG, attempts={"evidence": 6},
                 adversarial=_attacked(V_SURVIVED, 1.0, coverage=0.2))
    thorough = score(_entry(), record, CFG, attempts={"evidence": 6},
                     adversarial=_attacked(V_SURVIVED, 1.0, coverage=0.9))
    assert thin.score < thorough.score


# -- what it refuses to reward ------------------------------------------------

def test_a_flattering_in_sample_number_earns_nothing_on_its_own():
    """The reward is about what testing would TEACH, not about how good the
    candidate looks."""
    flattering = score(_entry(in_win=0.95), _cum(trades=40, markets=3,
                                                 expectancy=-0.02),
                       CFG, attempts={"evidence": 3})
    assert flattering.score < 0.5
    assert any("negative unseen expectancy" in p
               for p in flattering.penalties)


def test_one_flattering_dimension_cannot_carry_the_quality_term():
    """Multiplicative, for the same reason `evidence_score` is: a zero
    anywhere cannot be compensated for elsewhere."""
    concentrated = score(_entry(),
                         _cum(trades=300, markets=8, expectancy=0.2,
                              top_share=0.95), CFG,
                         attempts={"evidence": 8})
    assert concentrated.components["diversification"] < 0.1
    assert any("concentrated" in p for p in concentrated.penalties)


def test_convergence_can_reorder_a_queue_and_never_reach_a_gate():
    """§3: convergence determines what deserves more research, never what is
    considered correct. The ceiling is what enforces that in code."""
    record = _cum(trades=60, markets=5, expectancy=0.03)
    without = score(_entry(), record, CFG, attempts={"evidence": 5})
    with_max = score(_entry(), record, CFG, attempts={"evidence": 5},
                     convergence=1.0)
    assert with_max.score > without.score
    assert with_max.components["convergence"] <= CONVERGENCE_MAX_BONUS


def test_a_dead_end_family_is_throttled_and_still_queued():
    """§18: closing a branch outright is the one thing later evidence cannot
    undo, so the memory throttles and never bans."""
    record = _cum(trades=60, markets=5, expectancy=0.03)
    live = score(_entry(), record, CFG, attempts={"evidence": 5})
    throttled = score(_entry(), record, CFG, attempts={"evidence": 5},
                      dead_end="PARAMETER_SENSITIVITY")
    assert 0 < throttled.score < live.score
    assert throttled.score == pytest.approx(live.score * DEAD_END_MULTIPLIER,
                                            rel=0.02)
    assert throttled.why_stopped


def test_terminal_states_score_zero_and_say_so():
    for status in ("retired", "quarantined"):
        out = score(_entry(status=status), _cum(trades=90, markets=6,
                                                expectancy=0.1), CFG)
        assert out.score == 0.0
        assert status in out.why_stopped


def test_a_candidate_with_nowhere_left_to_test_is_stopped_explicitly():
    out = score(_entry(), _cum(trades=60, markets=5, expectancy=0.03), CFG,
                attempts={"evidence": 5}, eligible_markets=0)
    assert out.components["information"] == 0.0
    assert "nothing left to test it on" in out.why_stopped


def test_the_score_is_bounded_so_no_dimension_can_take_the_slate():
    out = score(_entry(), _cum(trades=5000, markets=200, expectancy=50.0),
                CFG, attempts={}, convergence=1.0, structure_weight=1.6,
                family_weight=1.2,
                adversarial=_attacked(V_SURVIVED, 1.0, coverage=1.0))
    assert out.score <= SCORE_MAX


# -- the sentences ------------------------------------------------------------

def test_every_scored_candidate_can_explain_its_place_in_the_queue():
    """§13 asks for these in words, and deriving them at display time would
    let the explanation drift from the arithmetic that produced the order."""
    out = score(_entry(), _cum(trades=60, markets=5, expectancy=0.03), CFG,
                attempts={"evidence": 5})
    assert out.why_more
    assert "because" in out.why_more

    stopped = score(_entry(status="retired"), _cum(), CFG)
    assert stopped.why_stopped


def test_the_explanation_names_the_penalty_it_overcame():
    out = score(_entry(), _cum(trades=60, markets=5, expectancy=0.03,
                               top_share=0.8), CFG,
                attempts={"evidence": 5})
    assert "despite" in out.why_more


def test_summary_reports_the_top_reason_not_just_the_top_score():
    rows = [score(_entry(f"c{i}"), _cum(trades=20 * i, markets=i,
                                        expectancy=0.01 * i), CFG,
                  attempts={"evidence": i})
            for i in range(1, 5)]
    out = summary(rows)
    assert out["rewardScored"] == 4
    assert out["rewardTopReason"]


# -- the boundary -------------------------------------------------------------

def test_the_reward_never_calls_the_status_machine():
    """§17 as a structural assertion: research priority and promotion are
    separate systems, and the moment one calls the other they are not."""
    import ast
    import inspect

    import pqb.reward as module
    tree = ast.parse(inspect.getsource(module))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    called |= {n.func.id for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not (called & {"next_status", "set_status", "record_validation",
                          "maturity_of", "blocking_of"})
