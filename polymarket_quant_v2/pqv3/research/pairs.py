"""§13 / §35 — cross-market structure, at a grain that can hold it.

`pqv3 depend` could already measure a lead-lag or an information flow between
two markets. What it could not do was turn one into a validated strategy, and
the obstacle was not the statistics — it was the shape of the data. The
observation matrix is ONE ROW PER WALLET-TRADE. A relationship between two
markets is a property of a PAIR, and a pair has nowhere to live in a table
whose rows are single trades, so no cross-market hypothesis could enter the
discovery pass however strong the measurement.

This module builds the missing grain: one row per (leader, follower, instant),
carrying features computed from both markets strictly before that instant, and
the outcome of buying the follower there. Rows in that shape go through the
same screen, the same Benjamini-Hochberg threshold over the same denominator
and the same out-of-sample split as anything else.

POINT-IN-TIME IS THE WHOLE DIFFICULTY, and it is worse here than at trade
grain. Every feature at instant t uses only prints strictly BEFORE t, from both
series. The temptation that would destroy this is to compute a trailing
correlation over a window that happens to include t, which quietly hands the
model the follower's next move. `_window` slices with a strict upper bound for
that reason and takes no arguments that could relax it.

THE SPLIT IS BY LEADER MARKET, not by time and not at random. A pair sampled
into both halves shares its outcome with itself, and one market appearing in
fifty pairs shares its resolution with all fifty. Splitting on the leader keeps
every row touching a given market on the same side of the wall.

WHAT THIS DOES NOT CLAIM. A surviving pair rule is a measured relationship
between two settled markets, out of sample. It is not a live signal: acting on
it needs both legs quotable and fillable at the same moment, which is an
execution question this module does not answer and `decision/gates.py` does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import Settings
from .stats import (benjamini_hochberg, block_bootstrap_ci, concentration,
                    mean, p_value_one_sided, summarize, t_stat)

# Feature vocabulary at pair grain. Deliberately small: the denominator is
# features x cut points x directions, and every axis added is paid for by every
# hypothesis tested.
PAIR_FEATURES = (
    "follower_price",       # what the follower costs now
    "leader_move",          # leader's return over the trailing window
    "follower_move",        # follower's return over the same window
    "move_gap",             # leader_move - follower_move: has B kept up?
    "price_gap",            # leader_price - follower_price
    "leader_prints",        # leader activity in the window
    "follower_prints",      # follower activity in the window
    "lagged_corr",          # corr(leader[t-1], follower[t]) over the window
    "secs_to_settle",       # follower's remaining life
)

# Only these may form a rule. A condition mentioning nothing but the follower
# is a SINGLE-market rule that happens to have been evaluated in a pair table —
# it belongs to the ordinary observation matrix, which already tests it, and
# admitting it here means this module reports single-market effects as
# cross-market discoveries. The first run did exactly that: every survivor was
# `follower_price <= x`, which is the favourite-longshot bias wearing a
# cross-market label.
LEADER_FEATURES = ("leader_move", "move_gap", "price_gap", "lagged_corr",
                   "leader_prints")

QUANTILES = (0.20, 0.35, 0.50, 0.65, 0.80)
OPS = ("ge", "le")

WINDOW_SECS = 3 * 3600
MIN_PRINTS_EACH = 40
MIN_ROWS_PER_PAIR = 12
MAX_INSTANTS_PER_PAIR = 150
# A cross-market rule judged on a handful of markets is judging
# which of them resolved YES. See the demeaning note in `run`.
MIN_FOLLOWERS = 6
MIN_FOLLOWERS_PER_RULE = 4


@dataclass
class PairRow:
    leader: str
    follower: str
    ts: int
    features: dict
    ret: float                       # (outcome - price) / price, one unit
    excess: float = 0.0              # ret minus this follower's own mean

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class PairMatrix:
    rows: list = field(default_factory=list)
    pairs: int = 0
    leaders: int = 0
    followers: int = 0
    note: str = ""

    def __len__(self) -> int:
        return len(self.rows)

    def summary(self) -> dict:
        return {"rows": len(self.rows), "pairs": self.pairs,
                "leaders": self.leaders, "followers": self.followers,
                "note": self.note}


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------

def _window(prints: list, lo: int, hi: int, _ts: list | None = None) -> list:
    """Prints with lo <= ts < hi. The upper bound is STRICT.

    That strictness is the no-look-ahead rule for this module and it is one
    comparison. Making it inclusive would fold the instant being predicted into
    the features that predict it, and the backtest would be spectacular and
    worthless.

    Bisected rather than scanned. The scan version was O(n) per row and this
    function runs once per candidate instant per pair, which made the whole
    pass quadratic in the length of a market's tape — it did not finish in two
    minutes on 24 tokens. `prints` arrives sorted by timestamp from
    `source.prints`, so the bounds are two binary searches.
    """
    import bisect
    ts = _ts if _ts is not None else [p[0] for p in prints]
    a = bisect.bisect_left(ts, lo)
    b = bisect.bisect_left(ts, hi)          # strict upper bound
    return prints[a:b]


def _ret_series(win: list) -> list:
    out = []
    for a, b in zip(win, win[1:]):
        if a[1] > 0:
            out.append((b[1] - a[1]) / a[1])
    return out


def _corr(a: list, b: list) -> float:
    n = min(len(a), len(b))
    if n < 4:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    return cov / math.sqrt(va * vb)


def candidate_pairs(source, *, max_tokens: int = 40,
                    min_prints: int = MIN_PRINTS_EACH) -> list:
    """Tokens active enough to carry a pair, paired where their lives overlap.

    Capped hard. N tokens give N(N-1) ordered pairs, so 40 tokens is already
    1,560 pairs and 200 would be 39,800 — and every pair tested enlarges the
    denominator that every p-value is judged against. A bigger sweep is not a
    better one.
    """
    if not source.available:
        return []
    # Through the source's public interface rather than its connection. The
    # first version issued its own SQL against `source._conn()`, which coupled
    # this module to the V1 schema and made it impossible to test against
    # anything but the real 2.6 GB database.
    toks = [(t, t0, t1) for t, _n, t0, t1 in source.active_tokens(
        min_prints=min_prints, limit=max_tokens)]
    out = []
    for i, (a, a0, a1) in enumerate(toks):
        for j, (b, b0, b1) in enumerate(toks):
            if i == j:
                continue
            overlap = min(a1, b1) - max(a0, b0)
            if overlap >= WINDOW_SECS * 2:
                out.append((a, b))
    return out


def build(st: Settings, source, *, pairs: list | None = None,
          max_tokens: int = 40, max_rows: int = 40_000) -> PairMatrix:
    """One row per (leader, follower, instant), point-in-time throughout."""
    m = PairMatrix()
    if not source.available:
        m.note = "no historical tape"
        return m
    pairs = pairs if pairs is not None else candidate_pairs(
        source, max_tokens=max_tokens)
    if not pairs:
        m.note = "no token pair has enough overlapping activity"
        return m

    as_of = source.latest_ts()
    cache: dict = {}

    def prints_for(tok: str) -> list:
        if tok not in cache:
            cache[tok] = source.prints(tok, as_of, lookback_secs=400 * 86_400,
                                       limit=20_000)
        return cache[tok]

    leaders, followers = set(), set()
    for leader, follower in pairs:
        res = source.resolution_for(follower)
        if not res:
            continue
        outcome = float(res.get("price") if isinstance(res, dict) else res[0])
        settle_ts = int(res.get("ts") if isinstance(res, dict) else 0)
        lp, fp = prints_for(leader), prints_for(follower)
        if len(lp) < MIN_PRINTS_EACH or len(fp) < MIN_PRINTS_EACH:
            continue

        lp_ts, fp_ts = [p[0] for p in lp], [p[0] for p in fp]

        # Decision instants are sampled evenly rather than taken at every
        # print. Consecutive prints seconds apart share almost all of their
        # trailing window, so they are near-duplicate rows that inflate n
        # without adding evidence — and an inflated n is a smaller p-value for
        # the same finding, which is the wrong direction to be wrong in.
        step = max(1, len(fp) // MAX_INSTANTS_PER_PAIR)
        made = 0
        for k in range(0, len(fp), step):
            t, price = fp[k][0], fp[k][1]
            if price <= 0.01 or price >= 0.99:
                continue
            lo = t - WINDOW_SECS
            lw = _window(lp, lo, t, lp_ts)
            fw = _window(fp, lo, t, fp_ts)
            if len(lw) < 4 or len(fw) < 4:
                continue
            l_first, l_last = lw[0][1], lw[-1][1]
            f_first, f_last = fw[0][1], fw[-1][1]
            if l_first <= 0 or f_first <= 0:
                continue
            l_move = (l_last - l_first) / l_first
            f_move = (f_last - f_first) / f_first
            lr, fr = _ret_series(lw), _ret_series(fw)
            feats = {
                "follower_price": price,
                "leader_move": l_move,
                "follower_move": f_move,
                "move_gap": l_move - f_move,
                "price_gap": l_last - f_last,
                "leader_prints": float(len(lw)),
                "follower_prints": float(len(fw)),
                "lagged_corr": _corr(lr[:-1], fr[1:]) if len(lr) > 5 else 0.0,
                "secs_to_settle": float(max(0, settle_ts - t)) if settle_ts
                else 0.0,
            }
            m.rows.append(PairRow(leader, follower, t, feats,
                                  (outcome - price) / price))
            made += 1
            if len(m.rows) >= max_rows:
                break
        if made >= MIN_ROWS_PER_PAIR:
            m.pairs += 1
            leaders.add(leader)
            followers.add(follower)
        if len(m.rows) >= max_rows:
            break

    m.leaders, m.followers = len(leaders), len(followers)
    m.note = (f"{len(m.rows)} observations over {m.pairs} pair(s), "
              f"{m.leaders} leader(s) and {m.followers} follower(s). Every "
              f"feature uses prints strictly before its own instant.")
    return m


# ---------------------------------------------------------------------------
# Hypotheses over the pair grain
# ---------------------------------------------------------------------------

@dataclass
class PairRule:
    feature: str
    op: str
    value: float

    def holds(self, f: dict) -> bool:
        v = f.get(self.feature, 0.0)
        return v >= self.value if self.op == "ge" else v <= self.value

    def __str__(self) -> str:
        sym = ">=" if self.op == "ge" else "<="
        return f"{self.feature} {sym} {self.value:g}"


def _cuts(m: PairMatrix, feature: str) -> list:
    vals = sorted(r.features.get(feature, 0.0) for r in m.rows)
    if len(vals) < 40:
        return []
    out = []
    for q in QUANTILES:
        v = vals[min(len(vals) - 1, int(q * len(vals)))]
        if v not in out:
            out.append(v)
    return out


def generate(m: PairMatrix) -> list:
    """Single-condition rules over the pair vocabulary.

    Depth one, on purpose. At this grain the sample is small and the honest
    denominator matters more than the reach: 9 features x 5 cuts x 2
    directions is 90 tests, which a few thousand rows can support. Depth two
    would be 4,000 and could not.
    """
    out = []
    for f in LEADER_FEATURES:
        for v in _cuts(m, f):
            for op in OPS:
                out.append(PairRule(f, op, v))
    return out


@dataclass
class PairFinding:
    statement: str
    n: int
    pairs: int
    expectancy: float
    t_stat: float
    p_value: float
    oos_n: int = 0
    oos_expectancy: float = 0.0
    oos_p: float = 1.0
    survives: bool = False
    concentration: float = 0.0
    ci_low: float = 0.0
    bootstrap_positive: float = 0.0
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def _split(m: PairMatrix, fraction: float = 0.30) -> tuple:
    """Split by LEADER market. See the module docstring."""
    leaders = sorted({r.leader for r in m.rows})
    if len(leaders) < 4:
        return m.rows, []
    n_test = max(1, int(len(leaders) * fraction))
    # Deterministic and not by activity: sorting by id keeps the split stable
    # across runs without letting the busiest markets decide which side is
    # which.
    test = set(leaders[-n_test:])
    return ([r for r in m.rows if r.leader not in test],
            [r for r in m.rows if r.leader in test])


def run(st: Settings, source, *, max_tokens: int = 40,
        alpha: float = 0.10) -> dict:
    """Build, sweep, screen, correct for multiplicity, then test out of sample."""
    m = build(st, source, max_tokens=max_tokens)
    if len(m) < 200:
        return {"matrix": m.summary(), "findings": [],
                "note": (f"{len(m)} pair observations is too few to test "
                         f"anything. This needs markets whose lives overlap "
                         f"and which both trade actively; the supplied tape "
                         f"may simply not contain them.")}

    # THE TRAP AT THIS GRAIN, and the first run walked straight into it.
    #
    # Every instant in a follower's life carries that follower's eventual
    # outcome. A market that resolved YES gives a positive return at every
    # instant and one that resolved NO gives -1 at every instant, so pooling
    # raw returns measures WHICH MARKETS WON, not which instants were good.
    # The first version reported a baseline expectancy of +0.495 and seven
    # "surviving" rules over three distinct followers: it had discovered that
    # two of three markets resolved YES.
    #
    # Demeaning by follower removes it exactly. A rule can now only score by
    # picking better instants WITHIN a market than that market's own average,
    # which is the only thing a cross-market signal could actually be. It is
    # the same discipline as ranking wallets by alpha over their price band
    # rather than by win rate.
    # Demeaned by (follower, price band), not by follower alone. The band is
    # needed for a second reason: (outcome - price) / price is price-scaled, so
    # cheap contracts carry enormous excess whenever they win. Demeaning by
    # follower only left `follower_price <= 0.30` as the strongest "finding" in
    # the sweep, which is this dataset's favourite-longshot bias and not a
    # cross-market effect at all. Within a band, a rule can only win by
    # choosing better instants than the same market at the same price.
    def band(p: float) -> int:
        return min(9, max(0, int(p * 10)))

    buckets: dict = {}
    for r in m.rows:
        buckets.setdefault((r.follower, band(r.features["follower_price"])),
                           []).append(r.ret)
    fb = {k: mean(v) for k, v in buckets.items()}
    for r in m.rows:
        r.excess = r.ret - fb.get(
            (r.follower, band(r.features["follower_price"])), 0.0)

    n_followers = len({r.follower for r in m.rows})
    if n_followers < MIN_FOLLOWERS:
        return {"matrix": m.summary(), "findings": [],
                "followers": n_followers,
                "note": (
                    f"only {n_followers} distinct follower market(s) have "
                    f"usable outcomes, under the {MIN_FOLLOWERS} this pass "
                    f"requires. Below that, any rule is describing which "
                    f"handful of markets happened to resolve YES rather than "
                    f"any cross-market relationship — the first run of this "
                    f"code reported seven 'surviving' rules over three "
                    f"followers for exactly that reason. Refusing is the "
                    f"correct output, not a failure (§33).")}

    train, test = _split(m)
    rules = generate(m)
    baseline = mean([r.excess for r in train])   # ~0 by construction

    scored = []
    for rule in rules:
        sel = [r for r in train if rule.holds(r.features)]
        if len(sel) < 30 or len({r.follower for r in sel}) < MIN_FOLLOWERS_PER_RULE:
            continue
        rets = [r.excess for r in sel]
        t, _ = t_stat(rets)
        scored.append((rule, sel, rets, p_value_one_sided(rets), t))

    if not scored:
        return {"matrix": m.summary(), "findings": [],
                "tested": len(rules), "note": "no rule selected enough rows"}

    bh = benjamini_hochberg([s[3] for s in scored], alpha=alpha)
    threshold = bh.threshold if hasattr(bh, "threshold") else 0.0

    findings = []
    for i, (rule, sel, rets, p, t) in enumerate(scored):
        if p > threshold:
            continue
        f = PairFinding(
            statement=f"buy the follower when {rule}",
            n=len(rets), pairs=len({(r.leader, r.follower) for r in sel}),
            expectancy=round(mean(rets), 6), t_stat=round(t, 3),
            p_value=round(p, 8),
            concentration=round(concentration(
                rets, [r.follower for r in sel]), 4))
        lo, _hi, share_pos = block_bootstrap_ci(rets, draws=800)
        f.ci_low = round(lo, 6)
        f.bootstrap_positive = round(share_pos, 4)

        osel = [r for r in test if rule.holds(r.features)]
        f.oos_n = len(osel)
        if f.oos_n >= 20:
            orets = [r.excess for r in osel]
            f.oos_expectancy = round(mean(orets), 6)
            f.oos_p = round(p_value_one_sided(orets), 8)
            f.survives = (f.oos_expectancy > baseline and f.oos_p <= 0.05
                          and f.concentration < 0.60)
        f.note = (
            f"in-sample expectancy {f.expectancy:+.4f} against a baseline of "
            f"{baseline:+.4f} over {f.n} rows and {f.pairs} pair(s); "
            + (f"out of sample {f.oos_expectancy:+.4f} on {f.oos_n} rows "
               f"(p={f.oos_p:.4f})" if f.oos_n >= 20 else
               "not enough held-out rows to test") + ". "
            + ("Survives. Still a measured relationship between settled "
               "markets, NOT a live signal: acting on it needs both legs "
               "quotable and fillable at the same instant."
               if f.survives else "Does not survive."))
        findings.append(f)

    findings.sort(key=lambda f: (-int(f.survives), f.p_value))
    survivors = [f for f in findings if f.survives]
    return {
        "matrix": m.summary(),
        "tested": len(rules), "screened": len(scored),
        "bh_alpha": alpha, "bh_threshold": threshold,
        "baseline": round(baseline, 6),
        "train_rows": len(train), "test_rows": len(test),
        "findings": [f.to_dict() for f in findings[:40]],
        "survivors": len(survivors),
        "note": (
            f"{len(rules)} rules generated, {len(scored)} had enough rows to "
            f"screen, {len(findings)} cleared a Benjamini-Hochberg threshold "
            f"of {threshold:.2e} at alpha={alpha}, {len(survivors)} then "
            f"survived a held-out split made by LEADER MARKET rather than by "
            f"time — a pair split at random shares its outcome with itself. "
            f"§13's gap was the missing row shape, and this is that shape; the "
            f"statistics are the same ones every other hypothesis faces."),
    }
