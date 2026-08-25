"""Systematic hypothesis generation with a controlled, reported search space.

Two failure modes this file exists to prevent, and they pull in opposite
directions:

**Uncontrolled combinatorial explosion.** 26 features x 8 thresholds x 3
operators x 3-deep conjunctions is millions of candidates. Testing millions
against 136,000 observations guarantees that some fit beautifully by luck, and
the multiple-testing correction then becomes so severe that nothing real can
survive either. The search is capped at 2-rule conjunctions over a curated
feature list with quantile-derived thresholds.

**An unreported denominator.** A p-value is meaningless without the number of
tests that produced it. Every hypothesis is registered here with a stable id
before it is evaluated, and the pass records `tested` *and* `distinct_tested` —
because on this dataset they differ by a large factor and paying the correction
for redundant tests makes the threshold stricter than the evidence requires for
no benefit.

**Inert axes.** Four features are structurally inert on the current data:
`w_settled_n`, `w_roll_win_rate`, `w_consec_losses`, `w_edge_t` are all
identically zero for essentially every row, because `resolutions.settled_ts` is
0 in all 8,116 rows so no trade ever had settled track record behind it. V2
measured `pit_evidence_share = 0.00` for every wallet tested. Generating rules
over them costs the full multiple-testing penalty and can never produce a
finding. `live_features()` detects this from the data rather than hard-coding
it, so the axes switch themselves back on once `pqv3 collect --backfill-settled`
has repaired enough settlement times.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field

from .matrix import Matrix

# Features worth searching, grouped by the dimension of the brief they cover.
# Deliberately a curated list rather than "every column": three of the columns
# are identifiers and several are near-duplicates of each other.
FEATURE_GROUPS = {
    "price": ("price", "price_vs_wallet_norm"),
    "size": ("notional", "rel_notional", "w_avg_notional"),
    "wallet_history": ("w_settled_n", "w_win_rate", "w_roi",
                       "w_roll_win_rate", "w_roll_roi", "w_edge_t",
                       "w_consec_losses", "w_consec_wins"),
    "wallet_activity": ("w_seen_n", "w_secs_since_prev", "w_open_notional",
                        "w_token_repeat", "w_market_repeat"),
    "microstructure": ("market_recent_prints", "market_price_move",
                       "market_velocity", "tape_price_gap"),
    "timing": ("hour_of_day", "secs_to_settle"),
}

ALL_FEATURES = tuple(f for g in FEATURE_GROUPS.values() for f in g)

# Quantile grid. Five interior cut points: enough to locate an effect, few
# enough that the denominator stays interpretable.
QUANTILES = (0.20, 0.35, 0.50, 0.65, 0.80)

OPS = ("ge", "le")


@dataclass(frozen=True)
class Rule:
    feature: str
    op: str
    value: float

    def holds(self, v: float) -> bool:
        return v >= self.value if self.op == "ge" else v <= self.value

    def __str__(self) -> str:
        return f"{self.feature} {'>=' if self.op == 'ge' else '<='} {self.value:g}"


@dataclass(frozen=True)
class Hypothesis:
    hypothesis_id: str
    family: str
    rules: tuple
    statement: str

    @property
    def n_params(self) -> int:
        return len(self.rules)

    @property
    def features(self) -> tuple:
        return tuple(r.feature for r in self.rules)

    def to_dict(self) -> dict:
        return {"hypothesis_id": self.hypothesis_id, "family": self.family,
                "statement": self.statement, "n_params": self.n_params,
                "features": list(self.features),
                "rules": [{"feature": r.feature, "op": r.op, "value": r.value}
                          for r in self.rules]}


def _hid(rules: tuple) -> str:
    key = "|".join(f"{r.feature}:{r.op}:{r.value:.6g}" for r in sorted(
        rules, key=lambda r: (r.feature, r.op, r.value)))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _statement(rules: tuple) -> str:
    return "buy when " + " and ".join(str(r) for r in rules)


def live_features(m: Matrix, *, min_distinct: int = 3,
                  min_nonzero_share: float = 0.02) -> tuple:
    """Features that actually vary in this data.

    A feature that is identically zero cannot produce a finding, but it DOES
    consume multiple-testing budget. Measuring this from the matrix rather than
    hard-coding a blocklist means the inert axes re-enable themselves once the
    data that makes them meaningful arrives.
    """
    out = []
    for f in ALL_FEATURES:
        col = m.cols.get(f) or []
        if not col:
            continue
        nonzero = sum(1 for v in col if v not in (0.0, -1.0))
        if nonzero / len(col) < min_nonzero_share:
            continue
        if len({round(v, 6) for v in col[:5000]}) < min_distinct:
            continue
        out.append(f)
    return tuple(out)


def inert_features(m: Matrix) -> tuple:
    live = set(live_features(m))
    return tuple(f for f in ALL_FEATURES if f not in live)


@dataclass
class SearchSpace:
    hypotheses: list = field(default_factory=list)
    tested: int = 0
    distinct: int = 0
    inert: tuple = ()
    live: tuple = ()
    depth: int = 2
    note: str = ""

    def to_dict(self) -> dict:
        return {"tested": self.tested, "distinct_tested": self.distinct,
                "redundancy": round(self.tested / self.distinct, 2)
                if self.distinct else 0.0,
                "inert_features": list(self.inert),
                "live_features": list(self.live),
                "depth": self.depth, "note": self.note}


def generate(m: Matrix, *, depth: int = 2, max_hypotheses: int = 20_000,
             families: tuple = ()) -> SearchSpace:
    """Build the candidate set, and report its true size.

    `tested` counts every transformation the grid defines. `distinct` counts
    those that survive deduplication by rule set. On this data the two differ
    substantially — identical thresholds arise from different quantiles when a
    feature is coarse — and reporting only the larger number would make the BH
    threshold stricter than the evidence warrants.
    """
    live = live_features(m)
    inert = inert_features(m)
    space = SearchSpace(live=live, inert=inert, depth=depth)

    if not live:
        space.note = "no feature in this matrix varies enough to search"
        return space

    groups = {g: tuple(f for f in fs if f in live)
              for g, fs in FEATURE_GROUPS.items()}
    groups = {g: fs for g, fs in groups.items() if fs}
    if families:
        groups = {g: fs for g, fs in groups.items() if g in families}

    # Single-feature rules first.
    singles: list = []
    for feats in groups.values():
        for f in feats:
            qs = m.quantiles(f, QUANTILES)
            for op, v in itertools.product(OPS, qs):
                singles.append(Rule(f, op, round(v, 6)))
    space.tested += len(singles)

    seen: set = set()
    for r in singles:
        h = _hid((r,))
        if h in seen:
            continue
        seen.add(h)
        space.hypotheses.append(Hypothesis(
            h, _family_of(r.feature), (r,), _statement((r,))))

    # Two-feature conjunctions ACROSS groups only. Two rules on the same group
    # are usually two views of the same quantity (`price >= 0.6` and
    # `price <= 0.9`), which inflates the denominator without adding an
    # independent test.
    if depth >= 2:
        gnames = sorted(groups)
        for ga, gb in itertools.combinations(gnames, 2):
            for fa, fb in itertools.product(groups[ga], groups[gb]):
                qa = m.quantiles(fa, QUANTILES)
                qb = m.quantiles(fb, QUANTILES)
                for (opa, va), (opb, vb) in itertools.product(
                        itertools.product(OPS, qa), itertools.product(OPS, qb)):
                    space.tested += 1
                    rules = (Rule(fa, opa, round(va, 6)),
                             Rule(fb, opb, round(vb, 6)))
                    h = _hid(rules)
                    if h in seen:
                        continue
                    seen.add(h)
                    if len(space.hypotheses) < max_hypotheses:
                        space.hypotheses.append(Hypothesis(
                            h, f"{ga}+{gb}", rules, _statement(rules)))

    space.distinct = len(space.hypotheses)
    if space.distinct >= max_hypotheses:
        space.note = (f"generation capped at {max_hypotheses} distinct "
                      f"hypotheses; {space.tested} transformations were "
                      f"defined. The cap is reported so the correction is not "
                      f"computed against a denominator smaller than the search")
    if inert:
        space.note += (
            (" | " if space.note else "")
            + f"{len(inert)} feature(s) excluded as inert on this data: "
              f"{', '.join(inert)}. They are identically zero because "
              f"resolutions.settled_ts is unpopulated; searching them would "
              f"cost multiple-testing budget and could not produce a finding.")
    return space


def _family_of(feature: str) -> str:
    for g, fs in FEATURE_GROUPS.items():
        if feature in fs:
            return g
    return "other"


def admit_mask(m: Matrix, h: Hypothesis, lo: int, hi: int) -> list:
    """Row indices in [lo, hi) admitted by every rule.

    Receives the feature columns and the row window. Deliberately never
    receives `m.resolution` — a rule cannot consult the answer even by
    accident, because the answer is not in scope here.
    """
    cols = [(m.cols[r.feature], r) for r in h.rules]
    out = []
    for i in range(lo, hi):
        for col, r in cols:
            if not r.holds(col[i]):
                break
        else:
            out.append(i)
    return out
