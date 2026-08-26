"""The §11 / §17 / §31 methods, tested against data whose answer is known.

Every estimator here is biased away from zero under the null, which means each
one can produce a confident discovery from pure noise. So the suite is built
around three synthetic processes with known answers, and the tests that matter
most are the ones asserting that nothing is found:

    noise      i.i.d. uniform. Nothing may be found in it, ever.
    AR(1)      real temporal structure, no regimes and no cycle.
    switching  a genuine two-state process with persistent regimes.

A method that cannot tell these three apart is not measuring what its name
says. Two of the estimators in this batch failed exactly that test during
development — the HMM called AR(1) a regime, and its first null preserved the
very structure it was meant to destroy — which is why the discrimination is
pinned here rather than assumed.
"""

from __future__ import annotations

import math

import pytest

from pqv3.regime import hidden as H
from pqv3.research import dependence as D
from pqv3.research import montecarlo as MC
from pqv3.research import spectral as S
from pqv3.research import surrogate as SU
from pqv3.research.surrogate import Rng


# ---------------------------------------------------------------- processes
def noise(n: int, seed: int = 1) -> list:
    r = Rng(seed)
    return [r.random() for _ in range(n)]


def ar1(n: int, phi: float = 0.7, seed: int = 2) -> list:
    r, x, out = Rng(seed), 0.0, []
    for _ in range(n):
        x = phi * x + (r.random() - 0.5)
        out.append(x)
    return out


def switching(n: int, seed: int = 3) -> list:
    r, st, out = Rng(seed), 0, []
    for _ in range(n):
        if r.random() < (0.02 if st == 0 else 0.05):
            st = 1 - st
        out.append((r.random() - 0.5) * (0.2 if st == 0 else 2.0))
    return out


# ---------------------------------------------------------------- surrogate
def test_rng_is_deterministic_and_independent_of_global_state():
    import random
    a = [Rng(42).random() for _ in range(3)]
    random.random()                       # perturb the global generator
    b = [Rng(42).random() for _ in range(3)]
    assert a == b
    assert Rng(42).random() != Rng(43).random()


def test_randrange_is_unbiased_at_the_low_end():
    """`% n` biases low residues, and 2000 draws make that visible in a tail."""
    r = Rng(9)
    counts = [0] * 7
    for _ in range(14_000):
        counts[r.randrange(7)] += 1
    assert max(counts) - min(counts) < 700, counts


def test_cyclic_shift_preserves_the_series_exactly():
    xs = list(range(50))
    out = SU.cyclic_shift(xs, Rng(5))
    assert sorted(out) == sorted(xs)
    assert out != xs
    # It IS the same series read from another start: one seam, no other break.
    seams = sum(1 for i in range(len(out) - 1) if out[i + 1] != out[i] + 1)
    assert seams == 1


def test_shuffle_and_block_preserve_length_and_values():
    xs = ar1(60)
    assert sorted(SU.shuffle(xs, Rng(1))) == sorted(xs)
    b = SU.block(xs, Rng(1))
    assert len(b) == len(xs)
    assert set(b) <= set(xs)


def test_p_value_can_never_be_zero():
    """A finite experiment cannot support p=0."""
    t = SU.test(1e9, [0.0] * 200, null="shuffle", n=100)
    assert t.p_value == pytest.approx(1 / 201, abs=1e-6)
    assert t.significant


def test_an_underpowered_test_refuses_rather_than_reporting_nothing():
    """15 surrogates floor p at 0.0625, so alpha=0.05 could never reject.

    Silently returning 'not significant' for every effect size is a broken
    experiment that reads exactly like a negative result.
    """
    t = SU.test(1e9, [0.0] * 15, null="shuffle", n=100, alpha=0.05)
    assert not t.significant
    assert "UNDERPOWERED" in t.note
    assert "at least 19" in t.note


# --------------------------------------------------------------- dependence
def test_quantise_is_equal_frequency_and_keeps_ties_together():
    b = D.quantise([5, 1, 3, 2, 4], 5)
    assert sorted(b) == [0, 1, 2, 3, 4]
    assert b[1] == 0 and b[0] == 4
    tied = D.quantise([1, 1, 1, 2, 3, 4], 3)
    assert len({tied[0], tied[1], tied[2]}) == 1, "a value in two bins"


def test_independent_random_walks_are_not_a_discovery():
    """The headline trap: raw MI here is large, and means nothing."""
    a, b = [], []
    r = Rng(7)
    xa = xb = 0.0
    for _ in range(600):
        xa += r.random() - 0.5
        xb += r.random() - 0.5
        a.append(xa)
        b.append(xb)
    res = D.mutual_information(a, b, draws=200)
    assert res.verdict == "NO_STRUCTURE_FOUND"
    assert res.nats > 0.1, "raw MI is large — that is exactly the point"
    assert res.surrogate["null_mean"] > 0.1, "and so is the null"


def test_mutual_information_sees_what_correlation_cannot():
    r = Rng(11)
    x = [r.random() * 2 - 1 for _ in range(600)]
    y = [xi * xi + (r.random() - 0.5) * 0.15 for xi in x]
    assert abs(D._pearson(x, y)) < 0.15, "a U-shape is invisible to Pearson"
    res = D.mutual_information(x, y, draws=200, null="shuffle")
    assert res.verdict == "STRUCTURE_PRESENT"
    assert res.stable


def test_transfer_entropy_has_a_direction():
    r = Rng(13)
    n = 600
    a = [r.random() for _ in range(n)]
    b = [0.5, 0.5] + [0.75 * a[i - 2] + 0.25 * r.random()
                      for i in range(2, n)]
    both = D.transfer_entropy_both_ways(a, b, lag=2, draws=200)
    assert both["a_to_b"]["verdict"] == "STRUCTURE_PRESENT"
    assert both["b_to_a"]["verdict"] != "STRUCTURE_PRESENT"
    assert both["reading"] == "A leads B"


def test_degenerate_series_is_not_a_null_result():
    """Real tape has tokens with thousands of prints at one price."""
    res = D.mutual_information([0.002] * 400, noise(400), draws=50)
    assert res.verdict == "DEGENERATE_SERIES"
    assert "nothing to measure" in res.note


def test_lead_lag_recovers_a_known_lag_and_prices_the_search():
    r = Rng(17)
    n = 500
    a = [r.random() for _ in range(n)]
    b = [0.5] * 3 + [a[i - 3] for i in range(3, n)]
    ll = D.lead_lag(a, b, max_lag=10, draws=300)
    assert ll.verdict == "A_LEADS_B" and ll.best_lag == 3
    # The null is the max over the whole lag scan, so it is far above zero.
    assert ll.surrogate["null_mean"] > 0.1, (
        "a null centred on zero would call every lag scan significant")


# ----------------------------------------------------------------- spectral
def irregular_times(days: int = 30, seed: int = 4) -> list:
    """Hourly prints with a nightly gap — the shape real tape has."""
    r, out = Rng(seed), []
    for d in range(days):
        for k in range(24):
            if 2 <= k < 10:
                continue
            out.append(d * 86400 + k * 3600 + r.random() * 900)
    return out


def test_frequency_grid_refuses_periods_it_cannot_resolve():
    t = irregular_times()
    span = max(t) - min(t)
    periods = [1 / f for f in S.frequency_grid(t)]
    assert max(periods) <= span / 2 + 1, "a 'cycle' seen once is a trend"
    gaps = sorted(t[i + 1] - t[i] for i in range(len(t) - 1))
    assert min(periods) >= 2 * gaps[len(gaps) // 2] - 1, "below Nyquist"


def test_recovers_an_embedded_cycle_from_unevenly_sampled_data():
    """A real 12-hour cycle is found — and reported as ALIASED, correctly.

    Sampling sixteen hours a day folds the true 12 h against the 24 h arrival
    rhythm to 8 h, which then outranks the truth. The peak IS significant and
    it IS at the wrong period, and no surrogate null can fix that because the
    alias lives in the data rather than in the null. What the report must not
    do is name 8 h as the period without saying so.
    """
    t = irregular_times()
    r = Rng(21)
    v = [math.sin(2 * math.pi * tt / 43200) * 0.6 + (r.random() - 0.5)
         for tt in t]
    rep = S.analyse(t, v, draws=120, top=10)
    assert rep.verdict == "PERIODICITY_ALIASED"
    assert any(11.0 < p["period_hours"] < 13.0 for p in rep.peaks), (
        "the true period must still be visible among the peaks")
    assert any("alias" in w for w in rep.warnings)
    assert "ALIAS" in rep.note


def test_a_clean_cycle_on_regular_sampling_is_not_flagged_as_aliased():
    """The alias warning must not fire on everything."""
    t = [i * 3600.0 for i in range(600)]
    r = Rng(29)
    v = [math.sin(2 * math.pi * tt / 43200) + (r.random() - 0.5) * 0.4
         for tt in t]
    rep = S.analyse(t, v, draws=120)
    assert rep.verdict == "PERIODICITY_FOUND"
    assert 11.0 < rep.best_period_hours < 13.0, rep.best_period_hours


def test_the_sampling_pattern_does_not_become_a_finding():
    """The null keeps the timestamps, so a nightly gap cancels out."""
    t = irregular_times()
    rep = S.analyse(t, noise(len(t), seed=17), draws=120, seed=17)
    assert rep.verdict == "NO_PERIODICITY_FOUND"
    assert rep.surrogate["null_mean"] > 1.0, (
        "the null's own tallest peak is large — that is the multiplicity "
        "being priced in")


def test_constant_series_reports_degenerate_not_negative():
    t = irregular_times()
    rep = S.analyse(t, [0.002] * len(t), draws=20)
    assert rep.verdict == "DEGENERATE_SERIES"
    assert "never possible" in rep.note


# ------------------------------------------------------------------- hidden
def test_forward_pass_does_not_underflow_on_a_long_series():
    obs = D.quantise(ar1(1500), 4)
    m, _ = H.fit(obs, 2, 4, restarts=1, max_iter=20)
    assert m.loglik == m.loglik and m.loglik != float("-inf")
    assert m.loglik < 0


def test_expected_duration_and_stationary_are_right_on_a_known_chain():
    m = H.HMM(2, 2, start=[0.5, 0.5],
              trans=[[0.9, 0.1], [0.2, 0.8]], emit=[[0.9, 0.1], [0.1, 0.9]])
    assert m.expected_durations() == [10.0, 5.0]
    s = m.stationary()
    assert s[0] == pytest.approx(2 / 3, abs=1e-4)


def test_viterbi_recovers_a_strongly_separated_path():
    m = H.HMM(2, 2, start=[0.5, 0.5],
              trans=[[0.98, 0.02], [0.02, 0.98]],
              emit=[[0.95, 0.05], [0.05, 0.95]])
    true = [0] * 40 + [1] * 40
    obs = list(true)
    path = H.viterbi(m, obs)
    assert sum(1 for a, b in zip(path, true) if a == b) >= 78


def test_markov1_is_a_real_reference_model():
    """It must fit a lag-1 dependent sequence better than an i.i.d. one."""
    r = Rng(5)
    sticky = [0]
    for _ in range(600):
        sticky.append(sticky[-1] if r.random() < 0.9 else 1 - sticky[-1])
    flat = [1 if r.random() < 0.5 else 0 for _ in range(601)]
    assert H.markov1_bic(sticky, 2)[1] < H.markov1_bic(flat, 2)[1]


@pytest.mark.parametrize("seed", [31, 131])
def test_noise_never_yields_hidden_states(seed):
    """Hurdle 1, and it is the reliable one."""
    rep = H.analyse(noise(400, seed=seed), states_range=(2,), restarts=2,
                    surrogates=24)
    assert rep.verdict == "NO_HIDDEN_STRUCTURE", rep.note
    assert "not established" in rep.note, (
        "a conservative test must not report absence as proof of absence")


def test_the_persistence_ratio_never_gates_the_verdict():
    """A discriminator that inverts across seeds must not decide anything.

    Measured: AR(1) scored 1.75-3.07 over five seeds, switching 2.62-7.23.
    Seed 33's switching series (2.62) scores BELOW seed 32's AR(1) (3.07), so
    any threshold between them is wrong in both directions depending on the
    draw. The ratio is still worth reporting; it is not worth deciding on, and
    this pins that it decides nothing.
    """
    seen = []
    for series in (ar1(400, seed=32), switching(900, seed=33)):
        rep = H.analyse(series, states_range=(2,), restarts=2, surrogates=24)
        p = rep.persistence
        seen.append((p["persistence_ratio"], rep.verdict))
        assert "DIAGNOSTIC ONLY" in p["reliability"]
        assert "does NOT gate" in p["reliability"]
        assert "leans_switching" in p, "reported as a lean, never as a verdict"
        assert rep.verdict != "SMOOTH_LATENT_NOT_SWITCHING", (
            "that verdict was withdrawn when the ratio proved unstable")
    # The inversion itself, pinned so a future 'improvement' cannot quietly
    # reintroduce a threshold that this data does not support.
    assert seen[1][0] < seen[0][0], seen


def test_the_report_states_what_it_cannot_separate():
    rep = H.analyse(switching(900, seed=33), states_range=(2,), restarts=2,
                    surrogates=24)
    assert "smooth latent process" in         rep.persistence["what_this_method_cannot_do"]


def test_a_short_series_is_refused_rather_than_fitted():
    rep = H.analyse(noise(50), min_n=200)
    assert rep.verdict == "INSUFFICIENT_EVIDENCE"
    assert rep.warnings


# -------------------------------------------------------------- monte carlo
def test_identical_expectancy_different_survival():
    """The whole reason this module exists.

    Nineteen +6% trades and one -60% trade. Every ordering has the same
    terminal wealth, the same expectancy, the same win rate and the same
    profit factor. A backtest cannot tell them apart. They differ in whether
    the account was still trading at the end.
    """
    returns = [0.06] * 19 + [-0.60]
    st = MC.simulate(returns, starting_capital=100.0, fraction=1.0,
                     paths=2000, hard_stop_drawdown=0.25)
    assert st.prob_hard_stop > 0.15, (
        "ordering must matter — if it does not, the stop is not being applied")
    assert st.p05_final < st.median_final
    assert 0.0 <= st.observed_percentile <= 1.0


def test_a_halted_account_does_not_receive_later_trades():
    """The most common way a Monte Carlo understates ruin.

    Tested on the walk directly rather than through the distribution, because
    the distribution cannot show it: a path that runs up tenfold and then draws
    down 25% both halts AND finishes rich, so a high stop rate and a high
    median are perfectly consistent. Only the single walk pins the semantics.
    """
    final, dd, ruined, stopped = MC._walk(
        [-0.30, -0.10, 10.0], start=100.0, fraction=1.0,
        ruin_at=20.0, stop_dd=0.25)
    assert stopped and not ruined
    assert final == pytest.approx(70.0), (
        "the walk must end at the trade that breached the stop — if the 10.0 "
        "were still applied, an account that had already halted would be "
        "credited with the recovery that followed it")
    assert dd == pytest.approx(0.30)


def test_a_walk_that_never_breaches_takes_every_trade():
    final, _dd, ruined, stopped = MC._walk(
        [0.05] * 4, start=100.0, fraction=1.0, ruin_at=20.0, stop_dd=0.25)
    assert not stopped and not ruined
    assert final == pytest.approx(100.0 * 1.05 ** 4)


def test_block_resampling_can_be_worse_and_the_gap_is_reported():
    r = Rng(23)
    returns = [(0.04 if r.random() < 0.6 else -0.05) for _ in range(120)]
    cmp_ = MC.compare_resamplers(returns, starting_capital=100.0,
                                 fraction=0.5, paths=600)
    assert "iid" in cmp_ and "block" in cmp_
    assert cmp_["block"]["p95_max_drawdown"] >= 0
    assert "clustering_cost" in cmp_ and cmp_["reading"]


def test_too_few_trades_is_refused():
    st = MC.simulate([0.01] * 5)
    assert "INSUFFICIENT_EVIDENCE" in st.note
    assert st.median_final == 0.0


def test_percentiles_are_ordered():
    st = MC.simulate(ar1(200), starting_capital=100.0, fraction=0.1,
                     paths=500)
    assert (st.p05_final <= st.p25_final <= st.median_final
            <= st.p75_final <= st.p95_final)
