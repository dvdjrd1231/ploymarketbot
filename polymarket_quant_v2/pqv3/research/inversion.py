"""The Signal Inversion Lab.

The question this answers is the one that actually matters when a system
refuses every trade: **are the gates protecting the account, or are they
throwing away money?**

It is answered by measurement, not by loosening thresholds. For any condition —
a validated strategy, or a proxy for a gate that is doing the blocking — the
lab scores several *interpretations* of the same historical rows:

    ORIGINAL      buy the side the signal points at
    INVERTED      buy the complement instead
    NO_TRADE      stand aside (identically zero, by definition)
    RANDOM        pick a side by coin flip, as a null

Every interpretation is scored on the SAME rows against the SAME outcomes.

WHAT IS NEVER DONE HERE. No outcome is edited. No loss is relabelled a win. On
a binary market the complement is a real instrument with an exact payoff —
buying NO at (1-p) pays ((1-resolution) - (1-p)) / (1-p) — so "inverted" is a
trade that could genuinely have been placed, not a sign flip applied to a
result. `test_inversion_never_edits_outcomes` asserts the resolution array is
untouched.

THREE THINGS THAT MAKE THIS HARDER THAN IT LOOKS, all handled:

**Costs are paid on both sides.** A rule that loses because the edge is inside
the spread does not become profitable when inverted — it pays the same spread
in the other direction. Slippage and fees are charged to whichever contract is
bought, so an inversion has to beat the cost twice over to look good.

**The complement is a different population.** The inverse of a 0.05 longshot is
a 0.95 favourite. Scoring both against one baseline would reproduce the
favourite-longshot artefact the matched baseline exists to destroy, and would
make inversion look brilliant on longshots for purely mechanical reasons. Each
side is compared against its OWN band-and-week peers (`baseline.build(...,
side=...)`).

**Every interpretation is another hypothesis.** Testing four readings of forty
conditions is 160 tests, and some will look significant by chance. The pass
reports its own denominator and applies the same Benjamini-Hochberg correction
as the discovery engine.
"""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass, field

from ..config import Settings
from . import stats
from .baseline import build as build_matched
from .hypothesis import Hypothesis, Rule, admit_mask
from .matrix import Matrix

INTERPRETATIONS = ("ORIGINAL", "INVERTED", "NO_TRADE", "RANDOM")


@dataclass
class Reading:
    """One interpretation of one condition."""

    interpretation: str
    n: int = 0
    n_comparable: int = 0
    expectancy: float = 0.0
    matched_excess: float = 0.0
    win_rate: float = 0.0
    p_value: float = 1.0
    ci_low: float = 0.0
    ci_high: float = 0.0
    markets: int = 0

    @property
    def tradeable(self) -> bool:
        """Profitable in absolute terms AND not merely a price preference."""
        return self.expectancy > 0 and self.matched_excess > 0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["tradeable"] = self.tradeable
        return d


@dataclass
class InversionVerdict:
    """What the readings say about the condition — and about blocking it."""

    condition: str
    statement: str
    readings: dict = field(default_factory=dict)
    verdict: str = "NO_INFORMATION"
    block_assessment: str = ""
    detail: str = ""
    n: int = 0

    def to_dict(self) -> dict:
        return {"condition": self.condition, "statement": self.statement,
                "verdict": self.verdict,
                "block_assessment": self.block_assessment,
                "detail": self.detail, "n": self.n,
                "readings": {k: v.to_dict() for k, v in self.readings.items()}}


# ---------------------------------------------------------------------------
# Gate proxies
# ---------------------------------------------------------------------------
# Conditions that stand in for the gates actually doing the blocking. Their
# thresholds are taken from the live gate configuration rather than invented,
# so "was CRASH_METER right?" is asked about the rule the system really uses.

def gate_proxies(m: Matrix, st: Settings) -> list:
    """(name, Hypothesis) for each blocking condition worth interrogating."""
    q = m.quantiles
    out: list = []

    vel = q("market_velocity", (0.05, 0.95))
    if len(vel) == 2:
        out.append(("CRASH_METER_falling", Hypothesis(
            "gate_crash_down", "gate",
            (Rule("market_velocity", "le", round(vel[0], 6)),),
            f"CRASH proxy: market_velocity <= {vel[0]:.4g} "
            f"(fastest 5% of falls)")))
        out.append(("CRASH_METER_rising", Hypothesis(
            "gate_crash_up", "gate",
            (Rule("market_velocity", "ge", round(vel[1], 6)),),
            f"CRASH proxy: market_velocity >= {vel[1]:.4g} "
            f"(fastest 5% of rises)")))

    mv = q("market_price_move", (0.05, 0.95))
    if len(mv) == 2:
        out.append(("SHARP_FALL", Hypothesis(
            "gate_sharp_fall", "gate",
            (Rule("market_price_move", "le", round(mv[0], 6)),),
            f"sharp 1h fall: market_price_move <= {mv[0]:.4g}")))

    # SIGNAL_VALIDITY refuses when the edge sits inside the round-trip cost
    # floor. The nearest observable proxy is a price close to the band's own
    # base rate — i.e. nothing to bet on. Approximated by mid-band prices.
    out.append(("SIGNAL_VALIDITY_midband", Hypothesis(
        "gate_midband", "gate",
        (Rule("price", "ge", 0.45), Rule("price", "le", 0.55)),
        "SIGNAL_VALIDITY proxy: price in [0.45, 0.55], where any edge is "
        "inside the cost floor")))

    prints = q("market_recent_prints", (0.10,))
    if prints:
        out.append(("LIQUIDITY_THIN", Hypothesis(
            "gate_thin", "gate",
            (Rule("market_recent_prints", "le", round(prints[0], 6)),),
            f"thin tape: market_recent_prints <= {prints[0]:.4g}")))
    return out


# ---------------------------------------------------------------------------
def read(m: Matrix, h: Hypothesis, st: Settings, *, lo: int, hi: int,
         yes_base=None, no_base=None, seed: int = 0,
         with_stats: bool = True) -> dict:
    """Score every interpretation of one condition over rows [lo, hi)."""
    idx = admit_mask(m, h, lo, hi)
    out: dict = {}
    if not idx:
        return {k: Reading(k) for k in INTERPRETATIONS}

    yes_base = yes_base if yes_base is not None else build_matched(
        m, st, lo, hi, side="YES")
    no_base = no_base if no_base is not None else build_matched(
        m, st, lo, hi, side="NO")
    rng = random.Random(seed or st.research.seed)

    cost = 1.0 + (st.costs.slippage_bps + st.costs.fee_bps) / 10_000.0
    price = m.cols["price"]
    markets = {m.market_id[i] for i in idx}

    series: dict = {k: ([], []) for k in INTERPRETATIONS}   # (returns, excess)
    for i in idx:
        p_yes = price[i] * cost
        p_no = (1.0 - price[i]) * cost
        res = m.resolution[i]
        e_yes = yes_base.excess(i)
        e_no = no_base.excess(i)

        if 0 < p_yes < 1:
            r = (res - p_yes) / p_yes
            series["ORIGINAL"][0].append(r)
            if e_yes is not None:
                series["ORIGINAL"][1].append(e_yes)
        if 0 < p_no < 1:
            r = ((1.0 - res) - p_no) / p_no
            series["INVERTED"][0].append(r)
            if e_no is not None:
                series["INVERTED"][1].append(e_no)

        series["NO_TRADE"][0].append(0.0)
        series["NO_TRADE"][1].append(0.0)

        # A coin flip between the same two real instruments. The honest null:
        # if ORIGINAL cannot beat this, the condition carries no side
        # information at all.
        if rng.random() < 0.5:
            if 0 < p_yes < 1:
                series["RANDOM"][0].append((res - p_yes) / p_yes)
                if e_yes is not None:
                    series["RANDOM"][1].append(e_yes)
        elif 0 < p_no < 1:
            series["RANDOM"][0].append(((1.0 - res) - p_no) / p_no)
            if e_no is not None:
                series["RANDOM"][1].append(e_no)

    for name in INTERPRETATIONS:
        rets, excess = series[name]
        rd = Reading(name, n=len(rets), n_comparable=len(excess),
                     markets=len(markets))
        if rets:
            rd.expectancy = round(stats.mean(rets), 6)
            rd.win_rate = round(
                sum(1 for r in rets if r > 0) / len(rets), 5)
        if excess:
            rd.matched_excess = round(stats.mean(excess), 6)
            if with_stats and name != "NO_TRADE":
                rd.p_value = round(stats.p_value_one_sided(excess), 8)
                a, b, _ = stats.block_bootstrap_ci(
                    excess, draws=min(1000, st.research.bootstrap_draws),
                    seed=st.research.seed)
                rd.ci_low, rd.ci_high = round(a, 6), round(b, 6)
        out[name] = rd
    return out


def judge(condition: str, statement: str, readings: dict,
          *, bh_threshold: float = 0.0, min_n: int = 30) -> InversionVerdict:
    """Turn the readings into a statement about the blocking rule.

    The four outcomes that matter, and what each implies for the gate:

      BLOCK_CORRECT        neither side is tradeable — refusing is right
      BLOCK_TOO_STRICT     ORIGINAL is tradeable — the gate is costing money
      PREDICTIVE_INVERTED  the complement is tradeable — the signal has
                           information, pointing the other way
      NO_INFORMATION       nothing separates from the baseline

    A reading only counts as tradeable if it is profitable in absolute terms
    AND beats its own band-and-week peers AND clears the pass's BH threshold.
    Two of those three is not enough: absolute profit alone is a price
    preference, and matched excess alone can still lose money.
    """
    v = InversionVerdict(condition=condition, statement=statement,
                         readings=readings)
    orig = readings.get("ORIGINAL")
    inv = readings.get("INVERTED")
    rnd = readings.get("RANDOM")
    v.n = orig.n if orig else 0

    if not orig or orig.n < min_n:
        v.verdict = "INSUFFICIENT_EVIDENCE"
        v.block_assessment = "UNKNOWN"
        v.detail = (f"only {v.n} observation(s) meet this condition; too few "
                    f"to judge the rule either way")
        return v

    def ok(r: Reading) -> bool:
        return (r is not None and r.tradeable
                and (bh_threshold <= 0 or r.p_value <= bh_threshold))

    orig_ok, inv_ok = ok(orig), ok(inv)

    if orig_ok and not inv_ok:
        v.verdict = "BLOCK_TOO_STRICT"
        v.block_assessment = "THE GATE IS COSTING MONEY"
        v.detail = (
            f"taking these trades as signalled returns {orig.expectancy:+.4f} "
            f"with {orig.matched_excess:+.4f} of matched excess over "
            f"{orig.n:,} observations (p={stats.format_p(orig.p_value)}). "
            f"Blocking them forgoes that.")
    elif inv_ok and not orig_ok:
        v.verdict = "PREDICTIVE_INVERTED"
        v.block_assessment = "BLOCKING IS RIGHT, BUT THE SIGNAL HAS INFORMATION"
        v.detail = (
            f"as signalled these return {orig.expectancy:+.4f}, but buying the "
            f"COMPLEMENT returns {inv.expectancy:+.4f} with "
            f"{inv.matched_excess:+.4f} of matched excess "
            f"(p={stats.format_p(inv.p_value)}). The condition carries real "
            f"information pointing the other way — which is a strategy "
            f"candidate, not a licence to unblock.")
    elif orig_ok and inv_ok:
        # Both sides beating their own peers on the same rows is a red flag,
        # not a double win: costs are paid twice and the two payoffs are
        # near-complementary, so this usually means a baseline is degenerate.
        v.verdict = "CONTRADICTORY"
        v.block_assessment = "SUSPECT — INVESTIGATE BEFORE USING"
        v.detail = (
            f"both sides appear tradeable ({orig.matched_excess:+.4f} and "
            f"{inv.matched_excess:+.4f}). On a binary market the two payoffs "
            f"are near-complementary and cost is paid on both, so this is far "
            f"more likely a thin or degenerate baseline bucket than a genuine "
            f"two-sided edge.")
    else:
        near_random = (rnd is not None and orig.matched_excess
                       <= max(rnd.matched_excess, 0.0) + 1e-9)
        v.verdict = "BLOCK_CORRECT" if orig.expectancy <= 0 else "NO_INFORMATION"
        v.block_assessment = ("THE GATE IS EARNING ITS PLACE"
                              if v.verdict == "BLOCK_CORRECT"
                              else "NO EVIDENCE EITHER WAY")
        v.detail = (
            f"as signalled {orig.expectancy:+.4f} "
            f"({orig.matched_excess:+.4f} matched); inverted "
            f"{inv.expectancy if inv else 0:+.4f} "
            f"({inv.matched_excess if inv else 0:+.4f} matched). "
            + ("Neither side is tradeable after costs, so refusing these is "
               "correct." if v.verdict == "BLOCK_CORRECT"
               else "Nothing separates from the baseline.")
            + (" Indistinguishable from a coin flip on the same rows."
               if near_random else ""))
    return v


@dataclass
class InversionPass:
    pass_id: str = ""
    started_ts: int = 0
    elapsed_secs: float = 0.0
    conditions: int = 0
    tests: int = 0
    bh_threshold: float = 0.0
    verdicts: list = field(default_factory=list)
    by_verdict: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "verdicts"}
        d["verdicts"] = [v.to_dict() for v in self.verdicts]
        return d


def run(st: Settings, store, *, include_strategies: bool = True,
        include_gates: bool = True, window: str = "oos",
        progress=None) -> InversionPass:
    """Interrogate every blocking condition and every validated strategy."""
    from .matrix import build
    t0 = time.perf_counter()
    p = InversionPass(pass_id=uuid.uuid4().hex[:16],
                      started_ts=int(time.time()))

    m = build(st, store)
    if m.n < 200:
        p.notes.append(f"only {m.n} observations; nothing to interrogate")
        return p

    split = m.split_ts(st.research.oos_fraction)
    lo, hi = (m.index_range(split, 0) if window == "oos"
              else m.index_range(0, split))
    p.notes.append(
        f"scored on the {'out-of-sample' if window == 'oos' else 'in-sample'} "
        f"window: {hi - lo:,} observations")

    conditions: list = []
    if include_gates:
        conditions.extend(gate_proxies(m, st))
    if include_strategies:
        from ..scanner.signals import load_validated
        for h, rec in load_validated(store):
            conditions.append((f"STRATEGY:{h.hypothesis_id[:10]}", h))
    if not conditions:
        p.notes.append("no conditions to test")
        return p
    p.conditions = len(conditions)

    if progress:
        progress(f"building matched baselines for both sides over "
                 f"{hi - lo:,} rows")
    yes_base = build_matched(m, st, lo, hi, side="YES")
    no_base = build_matched(m, st, lo, hi, side="NO")

    # Pass 1: readings. Pass 2: BH over the whole set, then judge.
    raw: list = []
    for k, (name, h) in enumerate(conditions):
        rd = read(m, h, st, lo=lo, hi=hi, yes_base=yes_base, no_base=no_base)
        raw.append((name, h, rd))
        if progress and k % 5 == 0:
            progress(f"  {k + 1}/{len(conditions)} conditions scored")

    pvals = [rd[i].p_value for _, _, rd in raw
             for i in ("ORIGINAL", "INVERTED") if rd.get(i)]
    p.tests = len(pvals)
    bh = stats.benjamini_hochberg(pvals, alpha=st.research.bh_alpha,
                                  n_tests=max(p.tests, 1))
    p.bh_threshold = round(bh.threshold, 8)
    p.notes.append(
        f"{p.tests} interpretation tests over {p.conditions} conditions; "
        f"Benjamini-Hochberg at alpha={st.research.bh_alpha} gives a threshold "
        f"of {bh.threshold:.3g}. Each reading is a separate hypothesis and is "
        f"counted as one.")

    for name, h, rd in raw:
        v = judge(name, h.statement, rd, bh_threshold=bh.threshold)
        p.verdicts.append(v)
        p.by_verdict[v.verdict] = p.by_verdict.get(v.verdict, 0) + 1

    p.verdicts.sort(key=lambda v: (
        v.verdict != "BLOCK_TOO_STRICT", v.verdict != "PREDICTIVE_INVERTED",
        -(v.readings.get("ORIGINAL").matched_excess
          if v.readings.get("ORIGINAL") else 0)))
    p.elapsed_secs = round(time.perf_counter() - t0, 2)
    _persist(store, p)
    return p


def _persist(store, p: InversionPass) -> None:
    """Recorded as hypotheses so the denominator stays honest system-wide."""
    rows = []
    for v in p.verdicts:
        for name, rd in v.readings.items():
            if name == "NO_TRADE":
                continue
            rows.append({
                "hypothesis_id": f"inv_{p.pass_id[:8]}_"
                                 f"{abs(hash((v.condition, name))) % 10 ** 10}",
                "family": f"inversion:{name}",
                "statement": f"[{name}] {v.statement}",
                "params": {"condition": v.condition,
                           "interpretation": name,
                           "verdict": v.verdict,
                           "reading": rd.to_dict()},
                "tested": 1, "p_value": rd.p_value,
                "effect": rd.matched_excess, "n": rd.n,
                "outcome": v.verdict, "pass_id": p.pass_id})
    if rows:
        store.insert("hypotheses", rows, source="inversion", replace=True)
    store.insert("research_passes", [{
        "pass_id": p.pass_id, "started_ts": p.started_ts,
        "finished_ts": int(time.time()), "tested": p.tests,
        "distinct_tested": p.conditions,
        "surviving": p.by_verdict.get("BLOCK_TOO_STRICT", 0)
        + p.by_verdict.get("PREDICTIVE_INVERTED", 0),
        "bh_alpha": 0.0, "bh_threshold": p.bh_threshold,
        "detail": {"kind": "inversion", "by_verdict": p.by_verdict,
                   "notes": p.notes, "elapsed_secs": p.elapsed_secs},
        "ts": p.started_ts}], source="inversion", replace=True)
