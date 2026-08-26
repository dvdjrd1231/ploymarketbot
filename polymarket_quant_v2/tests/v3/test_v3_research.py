"""The discovery -> validation engine.

Most of these tests exist because the corresponding mistake was actually made
during development and caught by running the thing on real data. Each one
pins down a specific way a research pipeline flatters itself.
"""

from __future__ import annotations

import math
import random

import pytest

from pqv3.research import baseline, stats, sweep, validate, walkforward
from pqv3.research.backtest import (Evaluation, capital_test, evaluate,
                                    settlement_clock_quality)
from pqv3.research.hypothesis import (Hypothesis, Rule, generate,
                                      inert_features, live_features)
from pqv3.research.matrix import Matrix


# --------------------------------------------------------------- fixtures
def _matrix(n: int = 4000, *, seed: int = 5, bias: float = 0.10,
            edge_feature: str = "market_price_move") -> Matrix:
    """A synthetic tape with TWO planted structures, deliberately:

    1. a favourite-longshot bias — cheap tokens are overpriced — so a rule that
       merely prefers favourites looks profitable on raw returns;
    2. a genuine edge on `edge_feature` that is INDEPENDENT of price band.

    A correct pipeline must find (2) and reject (1). A pipeline scoring on raw
    returns finds (1) and calls it a discovery, which is exactly what the first
    real run of this system did.
    """
    rng = random.Random(seed)
    from pqv3.research.matrix import FEATURES
    m = Matrix(cols={f: [] for f in FEATURES})
    t0 = 1_700_000_000
    for i in range(n):
        p_true = rng.uniform(0.05, 0.95)
        # Longshots overpriced, favourites underpriced.
        skew = bias if p_true > 0.55 else -bias
        price = min(0.95, max(0.05, p_true - skew + rng.gauss(0, 0.02)))
        move = rng.gauss(0, 0.05)
        # The real edge: a large negative move genuinely raises the hit rate.
        p_eff = min(0.98, p_true + (0.20 if move < -0.06 else 0.0))
        res = 1.0 if rng.random() < p_eff else 0.0
        for f in FEATURES:
            m.cols[f].append(0.0)
        m.cols["price"][-1] = round(price, 4)
        m.cols[edge_feature][-1] = round(move, 4)
        m.cols["notional"][-1] = rng.uniform(20, 400)
        m.cols["secs_to_settle"][-1] = rng.uniform(2, 6) * 86_400
        m.cols["hour_of_day"][-1] = i % 24
        m.resolution.append(res)
        m.ts.append(t0 + i * 900)
        m.wallet.append(f"0xw{i % 60:03d}")
        m.market_id.append(f"mkt{i % 250:04d}")
        m.token_id.append(f"tok{i % 400:04d}")
    return m


@pytest.fixture
def mx():
    return _matrix()


# --------------------------------------------------------------- baseline
def test_matched_baseline_sums_to_zero(mx, st):
    """Leave-one-out excess over a whole window must average ~0 by construction.

    If it does not, the baseline is not a baseline and every 'alpha' measured
    against it is an offset rather than an edge.
    """
    mb = baseline.build(mx, st, 0, len(mx))
    ex = [mb.excess(i) for i in range(len(mx))]
    ex = [e for e in ex if e is not None]
    assert len(ex) > 1000
    assert abs(stats.mean(ex)) < 1e-9


def test_price_preference_scores_no_alpha(mx, st):
    """THE control. A rule that only picks favourites must score ~0 excess.

    On raw returns this same rule looks spectacular, because a winning longshot
    pays +19 and a winning favourite pays +0.11.
    """
    h = Hypothesis("x", "price", (Rule("price", "ge", 0.60),), "favourites")
    ev = evaluate(mx, h, st, lo=0, hi=len(mx), with_stats=False)
    assert ev.n > 200
    assert ev.expectancy > 0, "the planted bias should make this profitable"
    assert abs(ev.alpha_vs_baseline) < 0.05, (
        f"a pure price preference scored {ev.alpha_vs_baseline:+.4f} of "
        f"matched excess; the band control is not working")


def test_planted_edge_is_found(mx, st):
    """The genuine, band-independent edge must survive the same control."""
    h = Hypothesis("y", "micro", (Rule("market_price_move", "le", -0.06),),
                   "dip")
    ev = evaluate(mx, h, st, lo=0, hi=len(mx), with_stats=True)
    assert ev.n > 100
    assert ev.alpha_vs_baseline > 0.05, (
        "the planted band-independent edge was not detected")
    assert ev.p_value < 0.01


def test_thin_buckets_are_not_compared(st):
    mb = baseline.MatchedBaseline(ret={0: 1.0}, bucket={0: ("b", 1)},
                                  sums={("b", 1): 1.0}, counts={("b", 1): 3})
    assert mb.excess(0) is None, "a baseline from 3 observations was used"


# ------------------------------------------------------------------ stats
def test_p_values_are_uniform_under_the_null():
    rng = random.Random(11)
    ps = [stats.p_value_one_sided([rng.gauss(0, 1) for _ in range(50)])
          for _ in range(1500)]
    rate = sum(1 for p in ps if p < 0.05) / len(ps)
    assert 0.03 < rate < 0.07, f"null rejection rate {rate:.3f} is miscalibrated"


def test_p_value_is_floored_not_zero():
    """An overwhelming-but-finite effect must floor, never underflow to 0.0."""
    rng = random.Random(3)
    huge = [1.0 + rng.gauss(0, 0.01) for _ in range(400)]
    p = stats.p_value_one_sided(huge)
    assert p > 0.0, "a p-value of exactly zero is an arithmetic artefact"
    assert p <= stats.P_FLOOR
    assert stats.format_p(p).startswith("<")


def test_format_p_passes_ordinary_values_through():
    assert stats.format_p(0.031) == "0.031"


def test_bh_uses_the_full_denominator():
    """Correcting against survivors instead of the search is the classic lie."""
    small = stats.benjamini_hochberg([0.001], alpha=0.10, n_tests=1)
    large = stats.benjamini_hochberg([0.001], alpha=0.10, n_tests=20_000)
    assert small.threshold > large.threshold
    assert large.n_significant == 0, (
        "p=0.001 cleared a 20,000-test correction")


def test_concentration_detects_one_market_carrying_everything():
    rets = [1.0] + [0.0] * 20
    keys = ["mkt_a"] + [f"mkt_{i}" for i in range(20)]
    assert stats.concentration(rets, keys) == pytest.approx(1.0)


# ------------------------------------------------------------- hypotheses
def test_inert_features_are_excluded(mx):
    """Searching an all-zero axis costs FDR budget and can never pay."""
    inert = inert_features(mx)
    assert "w_win_rate" in inert
    assert "price" in live_features(mx)


def test_search_space_reports_its_true_size(mx):
    sp = generate(mx, depth=2, max_hypotheses=3000)
    assert sp.tested >= sp.distinct > 0
    assert sp.note
    d = sp.to_dict()
    assert d["tested"] >= d["distinct_tested"]


def test_rules_never_see_the_outcome(mx):
    """`admit_mask` touches feature columns only, never `resolution`.

    Checked against the compiled code object rather than the source text, so
    the docstring that EXPLAINS the rule does not trip the test that enforces
    it.
    """
    from pqv3.research import hypothesis as H
    names = set(H.admit_mask.__code__.co_names)
    assert "resolution" not in names, (
        f"admit_mask references {names & {'resolution'}}")
    # And it must actually be blind: flipping every outcome cannot change which
    # rows a rule admits.
    h = Hypothesis("z", "price", (Rule("price", "ge", 0.5),), "p")
    before = H.admit_mask(mx, h, 0, len(mx))
    mx.resolution = [1.0 - r for r in mx.resolution]
    assert H.admit_mask(mx, h, 0, len(mx)) == before


# ------------------------------------------------------------------ screen
def test_screen_requires_profit_AND_alpha(mx, st):
    """Either criterion alone fills the finalist list with rejects.

    On the real tape 2,928 rules beat their price band while still losing
    money, and 393 made money purely by picking the band.
    """
    sp = generate(mx, depth=1)
    sc = sweep.screen(mx, sp.hypotheses, st, lo=0, hi=len(mx), min_n=30)
    for h, n, excess, absolute in sc.kept:
        assert excess > 0 and absolute > 0
    assert sc.evaluated == len(sp.hypotheses)


def test_screen_sample_is_deterministic(mx, st):
    sp = generate(mx, depth=1)
    a = sweep.screen(mx, sp.hypotheses, st, lo=0, hi=len(mx),
                     max_sample_rows=500)
    b = sweep.screen(mx, sp.hypotheses, st, lo=0, hi=len(mx),
                     max_sample_rows=500)
    assert [h.hypothesis_id for h, *_ in a.kept] == \
           [h.hypothesis_id for h, *_ in b.kept]


# ------------------------------------------------------------ walk-forward
def test_walkforward_folds_never_test_before_training(mx, st):
    h = Hypothesis("y", "micro", (Rule("market_price_move", "le", -0.06),),
                   "dip")
    wf = walkforward.run(mx, h, st, folds=4)
    assert wf.n_folds == 4
    for f in wf.folds:
        assert f.train_to <= f.test_from, (
            "a fold trained on data at or after its own test window")
        assert f.train_from < f.train_to


# -------------------------------------------------------- settlement clock
def test_degenerate_settlement_clock_is_detected(mx, st):
    """The defect that made every strategy report the same trade count."""
    for i in range(len(mx)):
        # Everything settles at the same instant, as V1's data actually does.
        mx.cols["secs_to_settle"][i] = (mx.ts[-1] + 86_400) - mx.ts[i]
    q = settlement_clock_quality(mx, 0, len(mx))
    assert not q["usable"]
    assert "settled_ts" in q["reason"] or "degenerate" in q["reason"]


def test_capital_test_labels_modelled_holds(mx, st):
    for i in range(len(mx)):
        mx.cols["secs_to_settle"][i] = (mx.ts[-1] + 86_400) - mx.ts[i]
    h = Hypothesis("y", "micro", (Rule("market_price_move", "le", -0.06),),
                   "dip")
    ct = capital_test(mx, h, st, lo=0, hi=len(mx))
    assert ct.hold_model == "MODELLED"
    assert ct.reliable is False
    assert "MODELLED" in ct.note


def test_capital_test_respects_the_bankroll(mx, st):
    h = Hypothesis("y", "micro", (Rule("market_price_move", "le", -0.06),),
                   "dip")
    ct = capital_test(mx, h, st, lo=0, hi=len(mx))
    assert ct.starting_capital == st.capital.starting_capital
    assert ct.trades <= ct.signals
    # The per-trade cap is a fraction of CURRENT equity, so it rises as the
    # account compounds. The invariant is against peak equity, not against the
    # opening balance — asserting the latter would forbid compounding, which
    # the brief explicitly requires.
    # Peak equity comes from the curve, not from the closing balance: the
    # account can grow past its final value and shrink back, and sizing was
    # done against equity at the moment of the trade.
    peak = max([e for _, e in ct.equity_curve] + [ct.starting_capital,
                                                  ct.ending_capital])
    assert ct.largest_position <= peak * st.capital.max_fraction_per_trade * 1.02, (
        f"largest position ${ct.largest_position:.2f} exceeds "
        f"{st.capital.max_fraction_per_trade:.0%} of peak equity ${peak:.2f}")
    assert ct.largest_position >= st.capital.min_order_usdc


def test_capital_test_reports_why_signals_were_skipped(mx, st):
    h = Hypothesis("y", "micro", (Rule("market_price_move", "le", -0.06),),
                   "dip")
    ct = capital_test(mx, h, st, lo=0, hi=len(mx))
    if ct.trades < ct.signals:
        assert ct.skip_reasons, "signals were skipped with no reason recorded"


# ------------------------------------------------------------------ ladder
def _ev(**kw) -> Evaluation:
    d = dict(n=200, markets=30, wallets=25, expectancy=0.05,
             alpha_vs_baseline=0.04, baseline_expectancy=0.01, win_rate=0.6,
             p_value=1e-6, concentration=0.2, max_drawdown=0.1)
    d.update(kw)
    e = Evaluation()
    for k, v in d.items():
        setattr(e, k, v)
    return e


class _WF:
    def __init__(self, ok=True):
        self.stable, self.positive_share = ok, 0.8 if ok else 0.2
        self.n_evaluable, self.note = 5, ""

    def to_dict(self):
        return {}


class _RB:
    def __init__(self, ok=True):
        self.fragile, self.survival, self.note = not ok, 0.9 if ok else 0.3, ""

    def to_dict(self):
        return {}


class _CT:
    def __init__(self, trades=20, reliable=True):
        self.trades, self.signals, self.total_return = trades, 100, 0.1
        self.fill_rate, self.skip_reasons = 0.2, {}
        self.reliable, self.hold_model = reliable, \
            "DATA" if reliable else "MODELLED"

    def to_dict(self):
        return {}


def test_ladder_validates_a_clean_candidate(st):
    v = validate.assign(st=st, oos=_ev(), is_eval=_ev(), walkforward=_WF(),
                        robustness=_RB(), capital=_CT(),
                        bh_threshold=1e-4, hypotheses_tested=5000)
    assert v.status == "VALIDATED"


def test_ladder_rejects_price_band_effects(st):
    v = validate.assign(st=st, oos=_ev(alpha_vs_baseline=-0.01),
                        is_eval=_ev(), walkforward=_WF(), robustness=_RB(),
                        capital=_CT(), bh_threshold=1e-4,
                        hypotheses_tested=5000)
    assert v.status == "NO_ALPHA"


def test_ladder_reports_the_most_fundamental_failure(st):
    """Negative AND fragile AND unstable should report NEGATIVE."""
    v = validate.assign(st=st, oos=_ev(expectancy=-0.01,
                                       alpha_vs_baseline=-0.01),
                        is_eval=_ev(), walkforward=_WF(False),
                        robustness=_RB(False), capital=_CT(),
                        bh_threshold=1e-4, hypotheses_tested=5000)
    assert v.status == "NEGATIVE"
    assert len(v.failure_modes) >= 3, "only the winning failure was recorded"


def test_perfect_win_rate_demands_more_evidence(st):
    v = validate.assign(st=st, oos=_ev(win_rate=1.0, n=40), is_eval=_ev(),
                        walkforward=_WF(), robustness=_RB(), capital=_CT(),
                        bh_threshold=1e-4, hypotheses_tested=5000)
    assert v.status == "INSUFFICIENT_EVIDENCE"


def test_modelled_capital_cannot_earn_a_strong_rating(st):
    v = validate.assign(st=st, oos=_ev(n=500, markets=60, wallets=40),
                        is_eval=_ev(), walkforward=_WF(), robustness=_RB(),
                        capital=_CT(reliable=False), bh_threshold=1e-4,
                        hypotheses_tested=5000)
    assert v.status == "VALIDATED"
    assert v.evidence_quality != "STRONG"
    assert any("ASSUMED holding period" in c for c in v.caveats)


def test_only_validate_may_assign_a_status():
    """V2's rule, kept: one module owns the vocabulary."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2] / "pqv3"
    offenders = []
    for f in root.rglob("*.py"):
        if f.name in ("validate.py", "api.py", "ui.py", "cli.py"):
            continue
        txt = f.read_text(encoding="utf-8")
        # Reading the vocabulary (counting outcomes, rendering a report) is
        # fine. ASSIGNING it is what must live in one place.
        for pattern in ('status = "VALIDATED"', 'status="VALIDATED"',
                        '.status = "VALIDATED"'):
            if pattern in txt:
                offenders.append(f"{f.relative_to(root)} :: {pattern}")
    assert not offenders, f"these assign VALIDATED outside validate.py: {offenders}"


def test_promotion_to_live_requires_a_human(store):
    store.insert("strategies", [{
        "strategy_id": "s1", "version": 1, "status": "PAPER",
        "params": "{}", "ts": 1}], source="test")
    blocked = validate.promote(store, "s1", to="LIVE", actor="system")
    assert not blocked["ok"] and "human" in blocked["error"]
    ok = validate.promote(store, "s1", to="LIVE", actor="human")
    assert ok["ok"]


# ------------------------------------------------- V2/V3 driver equivalence
def test_v3_driver_matches_v2_stream(tape):
    """The safety property behind reusing V2's state machine with a new driver.

    V3 supplies the loop, the heap and the row source so that repaired
    settlement timestamps actually reach the research pass. That is only reuse
    rather than a fork if, with NO overrides present, it reproduces V2's own
    stream exactly.
    """
    from pqv3.bootstrap import ensure_v2_importable
    if not ensure_v2_importable():
        pytest.skip("pqv2 not importable")
    from pqv2.config import Settings as V2Settings
    from pqv2.substrate.state import stream_observations
    from pqv3.research.matrix import stream_observations_v3

    s2 = V2Settings()
    s2.data_db = tape.data_db
    s2.costs.min_price = tape.costs.min_price
    s2.costs.max_price = tape.costs.max_price

    v2 = list(stream_observations(s2, min_notional=1.0))
    v3 = list(stream_observations_v3(tape, None, min_notional=1.0))
    assert len(v2) == len(v3) and v2, f"{len(v2)} vs {len(v3)} observations"

    from pqv3.research.matrix import FEATURES
    for a, b in zip(v2, v3):
        assert a.trade.ts == b.trade.ts
        assert a.trade.token_id == b.trade.token_id
        for f in FEATURES:
            assert getattr(a, f) == pytest.approx(getattr(b, f)), (
                f"feature {f} diverged at ts={a.trade.ts}")


def test_repaired_settlement_reaches_the_matrix(tape, store):
    """A repair must change what the causal pass produces, or it is cosmetic."""
    from pqv3.research.matrix import stream_observations_v3
    before = list(stream_observations_v3(tape, store, min_notional=1.0))
    assert before

    # Plant a trustworthy settlement time far earlier than V1's fallback.
    earliest = min(o.trade.ts for o in before)
    store.insert("resolution_times", [
        {"token_id": "TOK_A", "settled_ts": earliest + 60,
         "method": "VENUE_REPORTED", "confidence": 1.0}], source="test")

    after = list(stream_observations_v3(tape, store, min_notional=1.0))
    assert len(after) == len(before)
    changed = [(a, b) for a, b in zip(before, after)
               if a.secs_to_settle != b.secs_to_settle]
    assert changed, ("the repaired settlement timestamp did not reach the "
                     "observation stream")


def test_matrix_cache_is_invalidated_by_a_repair(tape, store):
    """Otherwise `collect --backfill-settled` improves data `discover` ignores."""
    from pqv3.research.matrix import build, data_fingerprint
    m1 = build(tape, store)
    fp1 = data_fingerprint(tape, store)
    assert m1.fingerprint == fp1

    store.insert("resolution_times", [
        {"token_id": "TOK_B", "settled_ts": 1_700_000_500,
         "method": "VENUE_REPORTED", "confidence": 1.0}], source="test")
    fp2 = data_fingerprint(tape, store)
    assert fp2 != fp1, "a repair did not move the fingerprint"

    m2 = build(tape, store)
    assert m2.fingerprint == fp2, "the stale cache was returned after a repair"
