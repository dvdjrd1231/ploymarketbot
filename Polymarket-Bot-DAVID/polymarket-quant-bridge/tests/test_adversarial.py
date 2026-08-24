"""The battery must be able to say NO, and must refuse to say anything on
evidence too thin to support a verdict.

Both halves matter equally. A battery that never fails anything is decoration;
a battery that passes things on three markets manufactures exactly the
confidence it was built to withhold. Most of these tests are about the second
failure mode, because it is the one that looks like success.
"""

from __future__ import annotations

from pqb.adversarial import (FAILED, INCONCLUSIVE, INVERSE_WON, NOT_RUN,
                             SURVIVED, V_BROKEN, V_NOT_ATTACKED, V_SURVIVED,
                             AdversarialReport, Sibling, alternative_windows,
                             attack, concentration, cost_stress,
                             drawdown_stress, edge_vs_dispersion,
                             inverse_check, leave_one_market_out,
                             market_subsets, neighbour_thresholds,
                             Probe, sample_depth, siblings_of, summary,
                             temporal_split, worth_attacking)
from pqb.config import ResearchConfig


def _row(market: str, trades: int, wins: int, pnl: float, ts: float = 0.0,
         drawdown: float = 0.0) -> dict:
    return {"market_id": market, "trades": trades, "wins": wins, "pnl": pnl,
            "ts": ts, "drawdown": drawdown}


def _cumulative(ledger: list[dict], top_share: float = 0.0,
                drawdown: float = 0.0) -> dict:
    trades = sum(r["trades"] for r in ledger)
    pnl = sum(r["pnl"] for r in ledger)
    return {"trades": trades, "wins": sum(r["wins"] for r in ledger),
            "pnl": pnl, "markets": len(ledger), "drawdown": drawdown,
            "expectancy": (pnl / trades) if trades else 0.0,
            "top_share": top_share, "markets_by_temporal_class": {}}


# -- refusing to answer -------------------------------------------------------

def test_thin_evidence_produces_no_verdict_rather_than_a_pass():
    """The whole point. Two markets cannot support a leave-one-out or a
    subset split, and reporting SURVIVED there would be the manufactured
    confidence the layer exists to prevent."""
    thin = [_row("M1", 8, 5, 1.0), _row("M2", 6, 4, 0.8)]
    assert leave_one_market_out(thin)[0] == NOT_RUN
    assert market_subsets(thin)[0] == NOT_RUN
    assert temporal_split(thin)[0] == NOT_RUN


def test_coverage_counts_unrunnable_tests_against_the_battery():
    """A placebo control needs a fresh replay and is not run here. Shrinking
    the denominator to hide that would let a partial attack read as a
    thorough one."""
    ledger = [_row(f"M{i}", 12, 8, 0.5, ts=float(i)) for i in range(6)]
    entry = {"id": "c#v1", "rule": {"type": "sequence"}, "signature": "sig"}
    report = attack(entry, _cumulative(ledger), ledger, ResearchConfig())
    assert report.coverage < 1.0
    assert report.results["placebo"] == NOT_RUN
    assert "placebo" not in report.to_dict()["results"]


def test_an_unattacked_candidate_is_not_a_survivor():
    report = attack({"id": "x", "rule": {}}, _cumulative([]), [],
                    ResearchConfig())
    assert report.verdict == V_NOT_ATTACKED
    assert report.robustness == 0.0


def test_a_thin_but_clean_sheet_is_weakened_not_survived():
    """Fewer than four runnable tests is a thin attack, not a clean one.
    Otherwise a candidate nobody could question would outrank one that was
    questioned hard and answered."""
    ledger = [_row("M1", 30, 20, 3.0)]
    report = attack({"id": "x", "rule": {"type": "threshold"}, "signature": ""},
                    _cumulative(ledger), ledger, ResearchConfig())
    assert report.verdict != V_SURVIVED


# -- saying no ----------------------------------------------------------------

def test_an_edge_that_is_one_market_fails_leave_one_out():
    ledger = [_row("BIG", 20, 18, 10.0), _row("M2", 15, 6, -1.0),
              _row("M3", 12, 5, -0.8)]
    result, detail = leave_one_market_out(ledger)
    assert result == FAILED
    assert "the edge IS that market" in detail


def test_an_edge_halved_by_its_best_market_is_inconclusive():
    ledger = [_row("BIG", 20, 15, 6.0), _row("M2", 20, 12, 1.0),
              _row("M3", 20, 12, 1.0)]
    assert leave_one_market_out(ledger)[0] == INCONCLUSIVE


def test_subsets_that_disagree_are_not_a_pass():
    ledger = [_row(f"M{i}", 15, 9, 2.0 if i % 2 == 0 else -1.9)
              for i in range(8)]
    result, _detail = market_subsets(ledger)
    assert result in (INCONCLUSIVE, FAILED)


def test_a_decaying_edge_fails_the_temporal_split():
    ledger = [_row("A", 15, 12, 3.0, ts=1.0), _row("B", 15, 11, 2.0, ts=2.0),
              _row("C", 15, 4, -2.0, ts=3.0), _row("D", 15, 3, -2.5, ts=4.0)]
    result, detail = temporal_split(ledger)
    assert result == FAILED
    assert "decaying" in detail


def test_concentration_matches_the_library_promotion_ceiling():
    """Two layers disagreeing about what 'concentrated' means is worse than
    either being wrong."""
    assert concentration({"markets": 4, "top_share": 0.85})[0] == FAILED
    assert concentration({"markets": 4, "top_share": 0.60})[0] == INCONCLUSIVE
    assert concentration({"markets": 4, "top_share": 0.20})[0] == SURVIVED
    assert concentration({"markets": 1, "top_share": 1.0})[0] == NOT_RUN


def test_sample_depth_can_fail_a_candidate_the_ladder_is_happy_with():
    """Different questions: the ladder asks whether to promote, this asks
    whether the other verdicts should be believed."""
    cfg = ResearchConfig()
    assert sample_depth({"trades": 4, "markets": 2}, cfg)[0] == FAILED
    assert sample_depth({"trades": 15, "markets": 2}, cfg)[0] == INCONCLUSIVE
    assert sample_depth({"trades": 60, "markets": 5}, cfg)[0] == SURVIVED


# -- costs --------------------------------------------------------------------

def test_cost_stress_declines_to_compare_incompatible_units():
    """Expectancy is per position for a bridge rule and the spread is per
    share. A number produced by subtracting one from the other would be
    meaningless in whichever direction it fell."""
    cfg = ResearchConfig()
    result, detail = cost_stress({"trades": 50, "expectancy": 0.5},
                                 {"type": "threshold"}, cfg)
    assert result == NOT_RUN
    assert "per share" in detail


def test_cost_stress_runs_where_the_units_match():
    cfg = ResearchConfig()          # assumed_spread 0.010, stress x0.5
    cfg.assumed_spread = 0.010
    survives = cost_stress({"trades": 50, "expectancy": 0.02},
                           {"type": "sequence"}, cfg)
    breaks = cost_stress({"trades": 50, "expectancy": 0.001},
                         {"type": "sequence"}, cfg)
    assert survives[0] == SURVIVED
    assert breaks[0] == FAILED


def test_edge_vs_dispersion_asks_the_one_question_every_rule_type_can_answer():
    """It measures dispersion and says so. An earlier version charged a fixed
    share of mean absolute P&L as a proxy for costs, failed 179 of 179 real
    candidates, and was measuring cross-market cancellation the whole time."""
    consistent = [_row(m, 20, 13, 1.0) for m in "ABCDEF"]
    scattered = [_row("A", 20, 18, 8.0), _row("B", 20, 4, -6.0),
                 _row("C", 20, 15, 5.0), _row("D", 20, 5, -6.5)]
    assert edge_vs_dispersion(consistent)[0] == SURVIVED
    assert edge_vs_dispersion(scattered)[0] == FAILED
    assert edge_vs_dispersion(scattered[:2])[0] == NOT_RUN


def test_drawdown_larger_than_the_whole_profit_fails():
    assert drawdown_stress({"pnl": 10.0, "drawdown": 20.0})[0] == FAILED
    assert drawdown_stress({"pnl": 10.0, "drawdown": 2.0})[0] == SURVIVED


# -- siblings -----------------------------------------------------------------

def test_siblings_that_disagree_on_the_sign_mean_a_threshold_not_a_rule():
    siblings = [Sibling("a#v1", "threshold", trades=20, expectancy=0.05,
                        markets=3),
                Sibling("a#v2", "threshold", trades=20, expectancy=-0.04,
                        markets=3)]
    result, detail = neighbour_thresholds(siblings)
    assert result == FAILED
    assert "disagree on the sign" in detail


def test_a_sibling_without_evidence_is_not_heard_from():
    """One trade in one market is not a parameter-sensitivity study."""
    assert neighbour_thresholds(
        [Sibling("a#v2", "threshold", trades=1, expectancy=-5.0,
                 markets=1)])[0] == NOT_RUN


def test_both_directions_paying_is_a_failure_and_the_inverse_winning_is_not():
    """The most misread test in the battery, pinned. A relationship whose
    inverse also pays is not directional; a relationship whose inverse pays
    BETTER may be real and backwards, which is a finding."""
    both_pay = inverse_check(
        {"expectancy": 0.04},
        [Sibling("inv", "inverse", trades=30, expectancy=0.03, markets=4)])
    assert both_pay[0] == FAILED

    backwards = inverse_check(
        {"expectancy": -0.02},
        [Sibling("inv", "inverse", trades=30, expectancy=0.05, markets=4)])
    assert backwards[0] == INVERSE_WON


def test_alternative_windows_reads_the_hold_variants_the_pass_registers():
    good = [Sibling("h1", "window", trades=20, expectancy=0.03, markets=3),
            Sibling("h2", "window", trades=20, expectancy=0.02, markets=3)]
    mixed = [Sibling("h1", "window", trades=20, expectancy=0.03, markets=3),
             Sibling("h2", "window", trades=20, expectancy=-0.02, markets=3)]
    assert alternative_windows(good)[0] == SURVIVED
    assert alternative_windows(mixed)[0] == INCONCLUSIVE


def test_siblings_are_matched_by_provenance_not_by_shape():
    """Two candidates sharing a rule shape by coincidence are not a
    controlled perturbation, and counting them as one fabricates a test."""
    entry = {"id": "p#v1", "signature": "sigA"}
    rows = [
        {"id": "p#v2", "signature": "sigA", "rule": {}},                # ver
        {"id": "inv#v1", "signature": "sigB",
         "rule": {"variant": "inverse", "variant_of": "p#v1"}},         # child
        {"id": "stranger#v1", "signature": "sigC", "rule": {}},         # no
    ]
    found = siblings_of(entry, rows,
                        lambda _i: {"trades": 20, "expectancy": 0.01,
                                    "markets": 3})
    assert {s.id for s in found} == {"p#v2", "inv#v1"}
    assert {s.relation for s in found} == {"threshold", "inverse"}


# -- the fold -----------------------------------------------------------------

def test_one_failure_breaks_the_verdict_however_many_tests_passed():
    ledger = [_row("BIG", 30, 28, 20.0, ts=1.0),
              _row("M2", 20, 8, -1.0, ts=2.0),
              _row("M3", 20, 8, -1.0, ts=3.0),
              _row("M4", 20, 8, -1.0, ts=4.0)]
    report = attack({"id": "c", "rule": {"type": "sequence"},
                     "signature": "s"},
                    _cumulative(ledger, top_share=0.95), ledger,
                    ResearchConfig())
    assert report.verdict == V_BROKEN
    assert "leave_one_market_out" in report.failed_tests
    # Broken, not erased: still scoreable, so it can still be re-tested.
    assert report.robustness > 0.0


def test_robustness_is_a_geometric_mean_so_most_passed_is_not_all_failed():
    ledger = [_row(f"M{i}", 20, 13, 1.0, ts=float(i)) for i in range(8)]
    report = attack({"id": "c", "rule": {"type": "sequence"},
                     "signature": "s"},
                    _cumulative(ledger, top_share=0.15), ledger,
                    ResearchConfig())
    assert report.verdict in (V_SURVIVED, "WEAKENED")
    assert report.robustness > 0.5


def test_only_promising_candidates_are_attacked():
    """§4 says every PROMISING strategy, and the word is load-bearing. The
    first version of this attacked everything with evidence and returned
    BROKEN for all 179 real candidates — because on a losing record every
    test fails trivially. Leave out the best market and a loser is still a
    loser. None of that is a finding."""
    assert not worth_attacking({"trades": 0, "expectancy": 0.1}, "validating")
    assert not worth_attacking({"trades": 100, "expectancy": 0.1}, "retired")
    assert not worth_attacking({"trades": 100, "expectancy": 0.1},
                               "quarantined")
    assert not worth_attacking({"trades": 100, "expectancy": -0.02},
                               "validating")
    assert worth_attacking({"trades": 40, "expectancy": 0.03}, "validating")


def test_attacking_a_losing_record_produces_no_verdict_at_all():
    """Enforced at the entry point too, so a caller that skips the filter
    cannot produce a page of meaningless BROKEN verdicts."""
    losing = [_row(f"M{i}", 20, 6, -1.0, ts=float(i)) for i in range(6)]
    report = attack({"id": "x", "rule": {"type": "sequence"},
                     "signature": "s"},
                    _cumulative(losing), losing, ResearchConfig())
    assert report.verdict == V_NOT_ATTACKED
    assert report.failed_tests == []
    assert "no apparent edge to disprove" in report.details["not_attacked"]


def test_summary_reports_what_was_attacked_not_what_exists():
    reports = [AdversarialReport(candidate_id="a", verdict=V_BROKEN,
                                 results={"market_subsets": FAILED},
                                 robustness=0.2, coverage=0.5),
               AdversarialReport(candidate_id="b", verdict=V_NOT_ATTACKED)]
    out = summary(reports)
    assert out["adversarialCandidatesAttacked"] == 1
    assert out["adversarialTestsFailed"] == 1
    assert out["adversarialFailuresByTest"] == {"market_subsets": 1}


# -- the boundary -------------------------------------------------------------

def test_the_battery_writes_nothing_and_cannot_reach_the_ladder():
    """§17 as a structural assertion, checked on the parsed module rather
    than on its text so a mention in a docstring is not mistaken for a call.

    If this layer ever calls the status machine or writes an evidence row,
    the separation that makes an opinionated research layer safe has been
    lost — and that is the failure that would be hardest to notice, because
    everything would keep working and only the meaning of 'validated' would
    have changed."""
    import ast
    import inspect

    import pqb.adversarial as module

    forbidden = {"next_status", "set_status", "record_validation",
                 "record_attempt", "upsert_candidate", "record_pass"}
    called = {node.func.attr for node in ast.walk(
        ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    called |= {node.func.id for node in ast.walk(
        ast.parse(inspect.getsource(module)))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert not (called & forbidden), sorted(called & forbidden)

    # ...and it never asks the library for anything, because it is never
    # given one: the whole battery runs on plain dicts.
    assert "library" not in inspect.signature(attack).parameters


# -- the probe contract -------------------------------------------------------

class _Probe(Probe):
    """A probe with scripted answers, so the wiring can be tested without a
    tape. `research._ReplayProbe` is the real one; what matters here is what
    `attack` does with whatever it gets back."""

    def __init__(self, placebo_result=None, liquidity_result=None,
                 raises=False):
        self._placebo = placebo_result
        self._liquidity = liquidity_result
        self._raises = raises

    def placebo(self, entry, cumulative, ledger):
        if self._raises:
            raise RuntimeError("tape unreadable")
        return self._placebo or (NOT_RUN, "declined")

    def liquidity_stress(self, entry, cumulative, ledger):
        if self._raises:
            raise RuntimeError("no depth data")
        return self._liquidity or (NOT_RUN, "declined")


def _clean_sheet():
    ledger = [_row(f"M{i}", 12, 8, 0.5, ts=float(i * 86400 * 40))
              for i in range(6)]
    entry = {"id": "c#v1", "rule": {"type": "sequence"}, "signature": "sig"}
    return entry, _cumulative(ledger), ledger


def test_a_probe_that_raises_is_missing_coverage_not_a_pass():
    """THE safety property of the whole probe design. The probe reaches a
    tape, and tapes fail. If an exception could be swallowed into SURVIVED,
    the easiest way for this layer to manufacture confidence would be to
    break its own data source — the precise reward-hack §5 forbids."""
    entry, cumulative, ledger = _clean_sheet()
    report = attack(entry, cumulative, ledger, ResearchConfig(),
                    probe=_Probe(raises=True))
    assert report.results["placebo"] == NOT_RUN
    assert report.results["liquidity_stress"] == NOT_RUN
    assert "probe failed: RuntimeError" in report.details["placebo"]
    # And it costs us coverage rather than quietly leaving the denominator.
    assert report.coverage < 1.0


def test_a_failed_placebo_breaks_a_candidate_that_passes_everything_else():
    """A record can be positive, broad, stable, replicated and cheap, and
    still be the hold rather than the signal. Nothing else in the battery
    can see that, which is why the control had to exist."""
    entry, cumulative, ledger = _clean_sheet()
    clean = attack(entry, cumulative, ledger, ResearchConfig())
    assert clean.verdict != V_BROKEN

    report = attack(entry, cumulative, ledger, ResearchConfig(),
                    probe=_Probe(placebo_result=(FAILED, "p=0.4")))
    assert "placebo" in report.failed_tests
    assert report.verdict == V_BROKEN
    assert report.robustness < clean.robustness


def test_a_surviving_probe_raises_coverage_without_inventing_a_pass():
    entry, cumulative, ledger = _clean_sheet()
    without = attack(entry, cumulative, ledger, ResearchConfig())
    with_probe = attack(entry, cumulative, ledger, ResearchConfig(),
                        probe=_Probe(placebo_result=(SURVIVED, "p=0.005"),
                                     liquidity_result=(SURVIVED, "both")))
    assert with_probe.coverage > without.coverage
    assert with_probe.results["placebo"] == SURVIVED


def test_the_default_probe_declines_everything():
    """The base class is not a stub that passes. Anyone who subclasses it and
    forgets a method gets NOT RUN, not a free survival."""
    entry, cumulative, ledger = _clean_sheet()
    report = attack(entry, cumulative, ledger, ResearchConfig(), probe=Probe())
    assert report.results["placebo"] == NOT_RUN
    assert report.results["liquidity_stress"] == NOT_RUN
