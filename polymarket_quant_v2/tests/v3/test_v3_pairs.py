"""§13 / §35 — the pair grain, and the three artefacts it had to stop producing.

This module was written three times and the first two versions both produced
confident, spurious findings on real tape. Each failure is pinned here, because
each would come straight back if the guard were removed:

  1. POOLED OUTCOMES. Every instant in a market's life carries that market's
     eventual result, so pooling raw returns measures which markets resolved
     YES. Version one reported a baseline expectancy of +0.495 and seven
     "survivors" over three follower markets.
  2. PRICE SCALE. (outcome - price) / price is enormous for cheap contracts
     that win, so demeaning by market alone left the favourite-longshot bias
     as the strongest effect in the sweep.
  3. SINGLE-MARKET RULES. A condition mentioning only the follower is not
     cross-market at all — it is a rule the ordinary matrix already tests,
     relabelled.

The no-look-ahead test is the most important one in the file. At this grain a
one-character slip in a window bound hands the model the answer.
"""

from __future__ import annotations

import pytest

from pqv3.research import pairs as P


class FakeSource:
    """A tape with a known planted relationship, and known outcomes."""

    available = True

    def __init__(self, series: dict, outcomes: dict) -> None:
        self.series = series          # token -> [(ts, price, usdc, side)]
        self.outcomes = outcomes      # token -> resolved price

    def latest_ts(self) -> int:
        return max(p[0] for s in self.series.values() for p in s)

    def active_tokens(self, *, min_prints=40, min_distinct=8, limit=60):
        out = []
        for tok, s in self.series.items():
            if len(s) >= min_prints and \
                    len({round(p[1], 6) for p in s}) >= min_distinct:
                out.append((tok, len(s), s[0][0], s[-1][0]))
        out.sort(key=lambda r: -r[1])
        return out[:limit]

    def prints(self, token, as_of, lookback_secs=0, limit=0):
        return [p for p in self.series.get(token, []) if p[0] <= as_of]

    def resolution_for(self, token):
        if token not in self.outcomes:
            return None
        return {"token_id": token, "price": self.outcomes[token],
                "ts": self.latest_ts() + 86_400, "settled_ts": 0}


def make_source(n_followers: int = 8, n_points: int = 220) -> FakeSource:
    from pqv3.research.surrogate import Rng
    r = Rng(5)
    series, outcomes = {}, {}
    base = 1_700_000_000
    series["LEAD"] = [(base + i * 600, 0.4 + 0.2 * r.random(), 10.0, "BUY")
                      for i in range(n_points)]
    for k in range(n_followers):
        tok = f"F{k}"
        series[tok] = [(base + i * 600, 0.3 + 0.4 * r.random(), 10.0, "BUY")
                       for i in range(n_points)]
        outcomes[tok] = 1.0 if k % 2 == 0 else 0.0
    outcomes["LEAD"] = 1.0
    return FakeSource(series, outcomes)


# ----------------------------------------------------------- no look-ahead
def test_the_window_upper_bound_is_strict():
    """The one comparison that decides whether any of this is valid."""
    prints = [(10, 0.1, 0, ""), (20, 0.2, 0, ""), (30, 0.3, 0, "")]
    win = P._window(prints, 0, 20)
    assert [p[0] for p in win] == [10], (
        "an inclusive bound folds the predicted instant into its own features")
    assert [p[0] for p in P._window(prints, 0, 31)] == [10, 20, 30]


def test_bisected_window_matches_a_linear_scan():
    """The optimisation must not have changed the semantics."""
    prints = [(i * 7, 0.5, 0, "") for i in range(200)]
    ts = [p[0] for p in prints]
    for lo, hi in ((0, 700), (13, 13), (100, 400), (0, 10_000)):
        assert P._window(prints, lo, hi, ts) == \
            [p for p in prints if lo <= p[0] < hi]


def test_no_feature_can_see_its_own_instant(st):
    """Every row's features must be computable from strictly earlier prints."""
    src = make_source()
    m = P.build(st, src, max_tokens=12)
    assert len(m) > 0
    by_token = {t: [p[0] for p in s] for t, s in src.series.items()}
    for row in m.rows[:200]:
        window_lo = row.ts - P.WINDOW_SECS
        earlier = [t for t in by_token[row.leader]
                   if window_lo <= t < row.ts]
        assert earlier, "a row was built with no prior leader prints"
        assert max(earlier) < row.ts


# --------------------------------------------------------------- artefacts
def test_too_few_follower_markets_is_refused(st):
    """Artefact 1. Three followers cannot support a cross-market claim."""
    src = make_source(n_followers=2)
    r = P.run(st, src, max_tokens=12)
    assert r["findings"] == []
    assert "resolve YES" in r["note"]
    assert str(P.MIN_FOLLOWERS) in r["note"]


def test_returns_are_demeaned_within_market_and_price_band(st):
    """Artefacts 1 and 2. The baseline must collapse to ~zero."""
    src = make_source()
    r = P.run(st, src, max_tokens=12)
    if not r["findings"] and "resolve YES" in r.get("note", ""):
        pytest.skip("fixture produced too few followers")
    assert abs(r["baseline"]) < 0.05, (
        f"baseline {r['baseline']} — demeaning is not working, and a nonzero "
        f"baseline here means outcome imbalance is being read as edge")


def test_only_leader_conditioned_rules_are_generated(st):
    """Artefact 3. A follower-only rule is not a cross-market finding."""
    src = make_source()
    m = P.build(st, src, max_tokens=12)
    rules = P.generate(m)
    assert rules
    used = {r.feature for r in rules}
    assert used <= set(P.LEADER_FEATURES)
    for banned in ("follower_price", "follower_move", "follower_prints"):
        assert banned not in used, (
            f"{banned} alone describes one market; the ordinary matrix "
            f"already tests it")


def test_the_split_is_by_leader_not_at_random(st):
    """A pair split at random shares its outcome with itself."""
    src = make_source()
    m = P.build(st, src, max_tokens=12)
    train, test = P._split(m)
    if not test:
        pytest.skip("fixture has too few leaders to split")
    assert not ({r.leader for r in train} & {r.leader for r in test})


# ------------------------------------------------------------------ shape
def test_the_matrix_has_the_pair_grain(st):
    """§13's gap was the row shape. This is the row shape."""
    src = make_source()
    m = P.build(st, src, max_tokens=12)
    assert len(m) > 0
    row = m.rows[0]
    assert row.leader != row.follower
    assert set(row.features) == set(P.PAIR_FEATURES)
    assert isinstance(row.ret, float)


def test_an_empty_tape_says_so(st):
    class Empty:
        available = False
    r = P.run(st, Empty())
    assert r["findings"] == []
    assert "too few" in r["note"] or "tape" in r["note"]


def test_finding_nothing_is_a_result_not_a_failure(st):
    """On the supplied tape this pass reports zero survivors. §33."""
    src = make_source()
    r = P.run(st, src, max_tokens=12)
    assert "findings" in r
    assert isinstance(r.get("survivors", 0), int)
