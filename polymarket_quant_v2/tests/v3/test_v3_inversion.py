"""The Signal Inversion Lab.

The danger with an inversion feature is that it becomes a machine for
relabelling losses as wins. These tests pin the three properties that stop it:
outcomes are never edited, the complement is priced as a real instrument, and
costs are charged on both sides.
"""

from __future__ import annotations

import random

import pytest

from pqv3.research import inversion, stats
from pqv3.research.baseline import band_index, build as build_matched
from pqv3.research.hypothesis import Hypothesis, Rule
from pqv3.research.matrix import FEATURES, Matrix


def _matrix(n=4000, *, seed=11, edge_side="YES", edge_strength=0.25,
            price_lo=0.15, price_hi=0.85):
    """A tape with a planted one-sided edge on `market_price_move <= -0.06`.

    `edge_side="NO"` plants the edge on the COMPLEMENT instead, so the lab must
    report PREDICTIVE_INVERTED rather than BLOCK_TOO_STRICT.
    """
    rng = random.Random(seed)
    m = Matrix(cols={f: [] for f in FEATURES})
    t0 = 1_700_000_000
    for i in range(n):
        p_true = rng.uniform(price_lo, price_hi)
        price = min(0.94, max(0.06, p_true + rng.gauss(0, 0.02)))
        move = rng.gauss(0, 0.05)
        fires = move < -0.06
        p_eff = p_true
        if fires:
            p_eff = (min(0.97, p_true + edge_strength) if edge_side == "YES"
                     else max(0.03, p_true - edge_strength))
        res = 1.0 if rng.random() < p_eff else 0.0
        for f in FEATURES:
            m.cols[f].append(0.0)
        m.cols["price"][-1] = round(price, 4)
        m.cols["market_price_move"][-1] = round(move, 4)
        m.cols["notional"][-1] = 100.0
        m.cols["secs_to_settle"][-1] = 3 * 86_400
        m.resolution.append(res)
        m.ts.append(t0 + i * 900)
        m.wallet.append(f"0xw{i % 50:03d}")
        m.market_id.append(f"mkt{i % 200:04d}")
        m.token_id.append(f"tok{i % 300:04d}")
    return m


DIP = Hypothesis("dip", "gate", (Rule("market_price_move", "le", -0.06),),
                 "dip fires")


# ------------------------------------------------------- the safety property
def test_inversion_never_edits_outcomes(st):
    """The resolution array must be identical before and after."""
    m = _matrix()
    before = list(m.resolution)
    inversion.read(m, DIP, st, lo=0, hi=len(m))
    assert m.resolution == before, "the lab mutated historical outcomes"


def test_complement_payoff_is_exact_arithmetic(st):
    """Inverted return must be the real NO payoff, not a sign flip."""
    m = _matrix(n=400)
    rd = inversion.read(m, DIP, st, lo=0, hi=len(m), with_stats=False)
    o, i = rd["ORIGINAL"], rd["INVERTED"]
    assert o.n == i.n > 0
    # A sign flip would make expectancies exact negatives of each other.
    # The real complement is not symmetric, because the denominators differ
    # ((res-p)/p versus ((1-res)-(1-p))/(1-p)) and cost is paid on both.
    assert o.expectancy != pytest.approx(-i.expectancy, abs=1e-6), (
        "inverted return looks like a negated original, not a NO position")


def test_costs_are_charged_on_both_sides(st):
    """Inverting a rule that loses purely to cost must not rescue it.

    Priced in a narrow band on purpose. Ratio returns are heavy-tailed — a win
    at p=0.06 pays +15 — so over a wide price range the sample mean of a
    few hundred no-edge observations swings by more than the effect being
    tested. That is a property of the metric, not of the lab, and testing
    through it would make this assertion flaky rather than meaningful.
    """
    m = _matrix(n=20_000, edge_strength=0.0, price_lo=0.35, price_hi=0.65)
    rd = inversion.read(m, DIP, st, lo=0, hi=len(m), with_stats=False)
    assert rd["ORIGINAL"].n > 1000, "fixture produced too few firings"
    assert rd["ORIGINAL"].expectancy < 0.02, rd["ORIGINAL"].expectancy
    assert rd["INVERTED"].expectancy < 0.02, (
        f"inverting a coin flip produced {rd['INVERTED'].expectancy:+.4f} — "
        f"cost is not being charged on the complement")


def test_no_trade_is_identically_zero(st):
    m = _matrix(n=500)
    rd = inversion.read(m, DIP, st, lo=0, hi=len(m), with_stats=False)
    assert rd["NO_TRADE"].expectancy == 0.0
    assert rd["NO_TRADE"].matched_excess == 0.0


# ------------------------------------------------------------ the baseline
def test_complement_uses_the_complement_band(st):
    """A NO at 0.05 must be judged against NO longshots, not YES ones."""
    m = _matrix(n=800)
    yes = build_matched(m, st, 0, len(m), side="YES")
    no = build_matched(m, st, 0, len(m), side="NO")
    assert yes.side == "YES" and no.side == "NO"
    i = next(iter(yes.bucket))
    p = m.cols["price"][i]
    assert yes.bucket[i][0] == band_index(p)
    assert no.bucket[i][0] == band_index(1.0 - p), (
        "the complement was bucketed by the YES price band")


def test_both_baselines_sum_to_zero(st):
    m = _matrix(n=2000)
    for side in ("YES", "NO"):
        mb = build_matched(m, st, 0, len(m), side=side)
        ex = [e for e in (mb.excess(i) for i in range(len(m)))
              if e is not None]
        assert abs(stats.mean(ex)) < 1e-9, f"{side} baseline is not centred"


# --------------------------------------------------------------- verdicts
def _judge(m, st, **kw):
    rd = inversion.read(m, DIP, st, lo=0, hi=len(m))
    return inversion.judge("TEST", "dip fires", rd, **kw)


def test_ratio_returns_are_heavy_tailed(st):
    """Documents WHY the no-edge tests use a narrow price band."""
    wide = _matrix(n=3000, edge_strength=0.0)
    narrow = _matrix(n=3000, edge_strength=0.0, price_lo=0.35, price_hi=0.65)
    rw = inversion.read(wide, DIP, st, lo=0, hi=len(wide), with_stats=False)
    rn = inversion.read(narrow, DIP, st, lo=0, hi=len(narrow),
                        with_stats=False)
    assert abs(rw["ORIGINAL"].expectancy) > abs(rn["ORIGINAL"].expectancy), (
        "the wide-band fixture was expected to be the noisier one")


def test_a_real_one_sided_edge_reads_as_block_too_strict(st):
    v = _judge(_matrix(edge_side="YES"), st)
    assert v.verdict == "BLOCK_TOO_STRICT"
    assert "COSTING MONEY" in v.block_assessment


def test_an_edge_on_the_complement_reads_as_predictive_inverted(st):
    v = _judge(_matrix(edge_side="NO"), st)
    assert v.verdict == "PREDICTIVE_INVERTED"
    assert "information" in v.detail.lower()
    assert "licence to unblock" in v.detail


def test_no_edge_reads_as_correct_or_uninformative(st):
    m = _matrix(n=20_000, edge_strength=0.0, price_lo=0.35, price_hi=0.65)
    v = _judge(m, st)
    assert v.verdict in ("BLOCK_CORRECT", "NO_INFORMATION"), v.detail


def test_both_sides_tradeable_is_flagged_not_celebrated(st):
    """A two-sided 'edge' is almost always a degenerate bucket."""
    good = inversion.Reading("ORIGINAL", n=500, n_comparable=500,
                             expectancy=0.1, matched_excess=0.05,
                             p_value=1e-9)
    also = inversion.Reading("INVERTED", n=500, n_comparable=500,
                             expectancy=0.1, matched_excess=0.05,
                             p_value=1e-9)
    v = inversion.judge("X", "x", {"ORIGINAL": good, "INVERTED": also},
                        bh_threshold=0.05)
    assert v.verdict == "CONTRADICTORY"
    assert "SUSPECT" in v.block_assessment


def test_thin_evidence_refuses_to_judge(st):
    small = inversion.Reading("ORIGINAL", n=5, expectancy=5.0,
                              matched_excess=5.0, p_value=1e-12)
    v = inversion.judge("X", "x", {"ORIGINAL": small}, bh_threshold=0.05)
    assert v.verdict == "INSUFFICIENT_EVIDENCE"
    assert v.block_assessment == "UNKNOWN"


def test_bh_threshold_can_veto_a_spectacular_reading(st):
    """Significance must be judged against the pass's own denominator."""
    strong = inversion.Reading("ORIGINAL", n=500, n_comparable=500,
                               expectancy=0.2, matched_excess=0.1,
                               p_value=0.004)
    weak = inversion.Reading("INVERTED", n=500, n_comparable=500,
                             expectancy=-0.2, matched_excess=-0.1,
                             p_value=1.0)
    passes = inversion.judge("X", "x", {"ORIGINAL": strong, "INVERTED": weak},
                             bh_threshold=0.05)
    assert passes.verdict == "BLOCK_TOO_STRICT"
    vetoed = inversion.judge("X", "x", {"ORIGINAL": strong, "INVERTED": weak},
                             bh_threshold=0.0001)
    assert vetoed.verdict != "BLOCK_TOO_STRICT", (
        "a p=0.004 reading survived a 0.0001 threshold")


# ------------------------------------------------------------------ pass
def test_gate_proxies_use_real_thresholds(st):
    m = _matrix()
    proxies = inversion.gate_proxies(m, st)
    assert proxies
    names = {n for n, _ in proxies}
    assert any("CRASH" in n for n in names)
    for _, h in proxies:
        assert h.rules and h.statement


def test_pass_counts_every_interpretation_as_a_test(st, store):
    m = _matrix(n=1200)
    from pqv3.research import matrix as M
    M.build.__wrapped__ if hasattr(M.build, "__wrapped__") else None
    p = inversion.InversionPass(pass_id="x")
    # Two interpretations scored per condition must both be counted.
    rd = inversion.read(m, DIP, st, lo=0, hi=len(m))
    scored = [k for k in ("ORIGINAL", "INVERTED") if rd.get(k)]
    assert len(scored) == 2, "an interpretation went uncounted"
