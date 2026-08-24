"""ADVERSARIAL SELF-CHALLENGE: the automatic attempt to break what looks good.

`convergence.ADVERSARIAL_TESTS` has always named the battery. Nothing ran it.
`record_adversarial` had no caller, `adversarial_verdict` folded results that
were never produced, and every hypothesis in the store carried an empty
`adversarial` dict — which `convergence_priority` reads as "not attacked yet"
and scores at 0.5, the same as a hypothesis that had been attacked and drawn.
A battery that is declared but not executed is worse than no battery, because
the field exists and reads as evidence of a test.

This module runs it. The rule that makes it safe is that it attacks
**evidence the candidate has already earned** — the per-market ledger the
validation engine itself wrote — rather than commissioning new replays. That
choice is not only about compute:

* It cannot contaminate anything. No market is consumed, no new evidence is
  recorded, `validations` is never written. The module holds a read handle on
  the library and a write handle on nothing.
* It cannot be gamed into producing a pass. Leaving out the best market and
  re-summing is arithmetic on frozen rows; there is no threshold to move and
  no sample to re-draw.
* It is deterministic and reproducible from persisted state, which is what
  §14 of the directive asks for — the same ledger yields the same verdict
  next month.

What that costs is honesty about reach. Two questions genuinely cannot be
asked of a frozen ledger — whether the signal beats firing at random, and
whether the edge survives where the book is thin — because both need
information the ledger does not carry. Rather than score them as passes or
drop them from the denominator, this module declares them and accepts an
optional `Probe` from the caller that owns the replay machinery and the tape
(`research.py`). With no probe they report NOT RUN with the reason and count
against `coverage`; with one they run for real. A battery that quietly scores
unrun tests as passes is the reward-hacking failure mode this layer exists to
prevent, and `coverage` is published beside `robustness` precisely so a thin
attack cannot read as a thorough one.

The probe may only ANSWER questions. It is handed the candidate's own frozen
rule and its own evidence markets, and what it returns is a verdict string
from the vocabulary below. It cannot write evidence, consume a market, or
reach the status ladder, so the separation in §17 survives contact with it.

**Nothing here can promote, demote, validate or reject.** The output is a
robustness number and a list of named failure states. Both feed research
priority (`reward.py`) and the audit record (`experiments.py`). The status
ladder in `library.next_status` neither imports this module nor is imported by
it, and that separation is the whole point of §17.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable

# -- verdict vocabulary -------------------------------------------------------

# Deliberately the same four words `convergence.adversarial_verdict` already
# folds, so a candidate-level result can be lifted to its hypothesis without
# translation. A translation layer between two vocabularies for one concept is
# where "failed" quietly becomes "inconclusive".
SURVIVED = "survived"
INCONCLUSIVE = "inconclusive"
FAILED = "failed"
INVERSE_WON = "inverse_won"
NOT_RUN = ""

# Rolled-up verdicts for the candidate as a whole.
V_BROKEN = "BROKEN"
V_WEAKENED = "WEAKENED"
V_SURVIVED = "SURVIVED"
V_NOT_ATTACKED = "NOT_ATTACKED"

# What each result is worth when the robustness score is folded. A failure is
# not zeroed: zeroing it would drop the candidate out of every future slate,
# and a candidate that fails one attack on eight markets deserves to be
# deprioritised, not disappeared — the ladder, not this module, decides
# whether it is finished.
_WEIGHT = {SURVIVED: 1.0, INCONCLUSIVE: 0.6, FAILED: 0.2, INVERSE_WON: 0.35}

# Evidence floors. Below each of these the test does not run, because the
# answer would be a statement about the sample size rather than about the
# candidate. This is the single most important set of constants in the file:
# an attack run on two markets "survives" almost always, and a battery of
# eight such survivals is exactly the manufactured confidence §5 forbids.
MIN_MARKETS_FOR_SUBSETS = 4
MIN_MARKETS_FOR_LEAVE_ONE_OUT = 3
MIN_MARKETS_FOR_TEMPORAL = 4
MIN_TRADES_TO_ATTACK = 10

# Cost stress, as a multiple of the assumed round-trip spread already charged.
# 0.5 = "what if trading cost half again as much as we assumed"; the slippage
# tier doubles it. Applied only where the units are comparable — see
# `_per_share_units`.
COST_STRESS_MULTIPLE = 0.5
SLIPPAGE_STRESS_MULTIPLE = 1.0

# Per-market expectancies this far inside their own standard error are not
# distinguishable from the spread between markets. Two is the conventional
# reading; one is the boundary below which the pooled number says nothing.
DISPERSION_STRONG = 2.0
DISPERSION_WEAK = 1.0

# Rule types whose replays return dollars PER SHARE, which is the unit
# `assumed_spread` is quoted in. The bridge path sizes real positions, so its
# expectancy is dollars per position and the two cannot be compared without a
# position size that is not stored on the evidence row. Rather than invent
# one, the cost tests decline to run there and say why.
_PER_SHARE_TYPES = ("sequence", "sharp_move", "longshot", "wallet_behavior",
                    "wallet_state")

# Concentration ceilings. The lower one matches the library's own promotion
# gate so the two layers cannot disagree about what "concentrated" means.
CONCENTRATION_WARN = 0.50
CONCENTRATION_FAIL = 0.70

# How far apart two sibling versions' expectancies may sit before the family
# is called parameter-sensitive. Sign disagreement is the real signal; this
# catches the case where both are positive but one is a rounding error.
SENSITIVITY_RATIO = 0.25


# -- what the battery is told about a candidate's relatives -------------------

@dataclass(frozen=True)
class Sibling:
    """Another library row that is a deliberate perturbation of this one.

    Siblings are how two of the battery's tests get asked at all without
    commissioning replays: the system has ALREADY registered inverse variants,
    hold variants and re-thresholded versions as their own candidates with
    their own independent evidence (see `research.variant_expansions`). Reading
    that evidence back is a free parameter-sensitivity study — and, unlike a
    re-run with a nudged threshold, every number in it was earned on markets
    the sibling was never fitted to.
    """

    id: str
    relation: str               # threshold | window | inverse
    trades: int = 0
    expectancy: float = 0.0
    markets: int = 0

    @property
    def speaks(self) -> bool:
        """Whether this sibling has enough of a record to be worth hearing."""
        return self.trades >= MIN_TRADES_TO_ATTACK and self.markets >= 2


@dataclass
class AdversarialReport:
    """One candidate, attacked. Carries no authority over its status."""

    candidate_id: str = ""
    results: dict[str, str] = field(default_factory=dict)
    details: dict[str, str] = field(default_factory=dict)
    robustness: float = 0.0
    coverage: float = 0.0
    verdict: str = V_NOT_ATTACKED
    failure_states: list[str] = field(default_factory=list)

    @property
    def tests_run(self) -> int:
        return sum(1 for r in self.results.values() if r)

    @property
    def failed_tests(self) -> list[str]:
        return sorted(name for name, r in self.results.items() if r == FAILED)

    def to_dict(self) -> dict:
        return {
            "candidate": self.candidate_id,
            "verdict": self.verdict,
            "robustness": round(self.robustness, 4),
            "coverage": round(self.coverage, 4),
            "testsRun": self.tests_run,
            "failed": self.failed_tests,
            "failureStates": list(self.failure_states),
            "results": {k: v for k, v in self.results.items() if v},
            "details": {k: v for k, v in self.details.items()
                        if self.results.get(k)},
        }


# -- the tests ----------------------------------------------------------------

def _expectancy(rows: Iterable[dict]) -> tuple[float, int]:
    """Per-trade expectancy and trade count over a slice of the ledger."""
    trades = sum(int(r.get("trades") or 0) for r in rows)
    pnl = sum(float(r.get("pnl") or 0.0) for r in rows)
    return ((pnl / trades) if trades else 0.0), trades


def _half(market_id: str) -> int:
    """Deterministic, candidate-independent split of the evidence markets.

    Hashing the market id rather than taking every other row means the split
    is the same on every pass and cannot be nudged by the order the ledger
    happens to come back in. Candidate-independent is deliberate too: two
    candidates tested on the same markets get the same halves, so their
    subset results are comparable with each other.
    """
    return int(hashlib.sha1(str(market_id).encode()).hexdigest(), 16) % 2


def leave_one_market_out(ledger: list[dict]) -> tuple[str, str]:
    """Does the edge survive losing its best market? (§4)

    The single most useful attack available for free, because the failure it
    catches — one market carrying the entire record — is both the commonest
    way a prediction-market backtest lies and completely invisible in a
    headline expectancy.
    """
    if len(ledger) < MIN_MARKETS_FOR_LEAVE_ONE_OUT:
        return NOT_RUN, (f"needs {MIN_MARKETS_FOR_LEAVE_ONE_OUT} evidence "
                         f"markets, has {len(ledger)}")
    best = max(ledger, key=lambda r: float(r.get("pnl") or 0.0))
    rest = [r for r in ledger if r is not best]
    full, _ = _expectancy(ledger)
    without, trades = _expectancy(rest)
    if trades <= 0:
        return NOT_RUN, "no trades outside the best market"
    detail = (f"expectancy {full:+.4f} -> {without:+.4f} without "
              f"{str(best.get('market_id'))[:18]}")
    if without <= 0:
        return FAILED, detail + " (the edge IS that market)"
    if full > 0 and without < full * 0.5:
        return INCONCLUSIVE, detail + " (halved)"
    return SURVIVED, detail


def market_subsets(ledger: list[dict]) -> tuple[str, str]:
    """Does it hold on a different subset of markets? (§4)"""
    if len(ledger) < MIN_MARKETS_FOR_SUBSETS:
        return NOT_RUN, (f"needs {MIN_MARKETS_FOR_SUBSETS} evidence markets, "
                         f"has {len(ledger)}")
    left = [r for r in ledger if _half(r.get("market_id")) == 0]
    right = [r for r in ledger if _half(r.get("market_id")) == 1]
    if not left or not right:
        return NOT_RUN, "the hash split put every market on one side"
    a, ta = _expectancy(left)
    b, tb = _expectancy(right)
    if ta < 5 or tb < 5:
        return NOT_RUN, f"a side is too thin ({ta} / {tb} trades)"
    detail = (f"half A {a:+.4f} over {ta} trades, half B {b:+.4f} over "
              f"{tb} trades")
    if a > 0 and b > 0:
        return SURVIVED, detail
    if a <= 0 and b <= 0:
        return FAILED, detail + " (neither half is positive)"
    return INCONCLUSIVE, detail + " (only one half is positive)"


def temporal_split(ledger: list[dict]) -> tuple[str, str]:
    """Does the edge disappear in another time period? (§4)

    Split by when the evidence was RECORDED, which is the only clock the
    validation rows carry. That is a weaker temporal test than splitting by
    when the markets traded, and the detail string says so rather than
    letting the result read stronger than it is.
    """
    if len(ledger) < MIN_MARKETS_FOR_TEMPORAL:
        return NOT_RUN, (f"needs {MIN_MARKETS_FOR_TEMPORAL} evidence markets, "
                         f"has {len(ledger)}")
    ordered = sorted(ledger, key=lambda r: float(r.get("ts") or 0.0))
    cut = len(ordered) // 2
    early, late = ordered[:cut], ordered[cut:]
    a, ta = _expectancy(early)
    b, tb = _expectancy(late)
    if ta < 5 or tb < 5:
        return NOT_RUN, f"a period is too thin ({ta} / {tb} trades)"
    detail = (f"earlier evidence {a:+.4f}, later evidence {b:+.4f} "
              "(by recording order, not market date)")
    if a > 0 and b > 0:
        return SURVIVED, detail
    if b <= 0 < a:
        return FAILED, detail + " (decaying)"
    if a <= 0 and b <= 0:
        return FAILED, detail
    return INCONCLUSIVE, detail + " (only the later period is positive)"


def concentration(cumulative: dict) -> tuple[str, str]:
    """Does one market create most of the profit? (§4)"""
    markets = int(cumulative.get("markets") or 0)
    if markets < 2:
        return NOT_RUN, "one market cannot be concentrated against itself"
    share = float(cumulative.get("top_share") or 0.0)
    detail = f"top market carries {share:.0%} of positive P&L"
    if share > CONCENTRATION_FAIL:
        return FAILED, detail
    if share > CONCENTRATION_WARN:
        return INCONCLUSIVE, detail
    return SURVIVED, detail


def sample_depth(cumulative: dict, cfg) -> tuple[str, str]:
    """Does it depend on a tiny number of trades? (§4)

    Not a duplicate of the validation gate. The gate asks whether there is
    enough evidence to promote; this asks whether the RESULT is thin enough
    that the other tests' verdicts should be discounted — which is why it can
    return FAILED for a candidate the ladder is perfectly happy to leave at
    `validating`.
    """
    trades = int(cumulative.get("trades") or 0)
    markets = int(cumulative.get("markets") or 0)
    detail = f"{trades} trades across {markets} market(s)"
    if trades == 0:
        # A candidate that has never traded has not been tested, which is a
        # NON-OBSERVATION and not a failure — the same distinction the
        # library draws between a zero-trade attempt and a validation row.
        # Returning FAILED here would make "never allocated a market" and
        # "attacked and broke" the same word.
        return NOT_RUN, "no unseen evidence to attack yet"
    if trades < MIN_TRADES_TO_ATTACK:
        return FAILED, detail + " (too thin to mean anything)"
    if trades < int(getattr(cfg, "oos_min_trades", 30)) or markets < 3:
        return INCONCLUSIVE, detail
    return SURVIVED, detail


def _per_share_units(rule: dict) -> bool:
    return str((rule or {}).get("type") or "threshold") in _PER_SHARE_TYPES


def cost_stress(cumulative: dict, rule: dict, cfg,
                multiple: float = COST_STRESS_MULTIPLE) -> tuple[str, str]:
    """Does it survive costs being worse than we assumed? (§4)

    Runs only where expectancy and the assumed spread are quoted in the same
    unit. The bridge path books dollars per POSITION and the spread is dollars
    per SHARE; subtracting one from the other would produce a number, and the
    number would be meaningless in whichever direction it happened to fall.
    `edge_vs_dispersion` asks those rules the unit-free version
    of the question instead.
    """
    if not _per_share_units(rule):
        return NOT_RUN, ("expectancy is per position for this rule type; the "
                         "spread is per share - see edge_vs_dispersion")
    trades = int(cumulative.get("trades") or 0)
    if trades < MIN_TRADES_TO_ATTACK:
        return NOT_RUN, f"only {trades} trades"
    spread = float(getattr(cfg, "assumed_spread", 0.01))
    extra = spread * float(multiple)
    expectancy = float(cumulative.get("expectancy") or 0.0)
    net = expectancy - extra
    detail = (f"expectancy {expectancy:+.4f} - {extra:.4f}/trade extra cost "
              f"= {net:+.4f}")
    if net > 0:
        return SURVIVED, detail
    if net > -0.5 * extra:
        return INCONCLUSIVE, detail
    return FAILED, detail


def edge_vs_dispersion(ledger: list[dict]) -> tuple[str, str]:
    """Is the edge large relative to how much it varies between markets?

    The unit-free robustness question, and the only one every rule type can
    be asked — the cost tests above need per-share units and the bridge path
    does not have them.

    This started life as a "margin haircut" that charged a fixed share of the
    candidate's mean absolute P&L as a proxy for extra costs. Run against the
    real library it failed 179 of 179 candidates, which is the signature of a
    miscalibrated test rather than a universally fragile library. The reason
    is worth recording: the ledger stores P&L per MARKET, so summing absolute
    values adds up cross-market cancellation, and any candidate whose markets
    disagree — which is most of them — was charged a "cost" several times its
    own edge. It was measuring dispersion and calling it costs.

    So it measures dispersion, and says so. The statistic is the standard
    error of the per-market expectancies: an edge inside one standard error of
    zero is indistinguishable from the spread between markets, however good
    the pooled number looks. This is the closest thing to a t-statistic the
    stored evidence supports, and it is diagnostic only — the promotion gate
    remains `next_status`, which has never heard of it.
    """
    usable = [r for r in ledger if int(r.get("trades") or 0) > 0]
    if len(usable) < MIN_MARKETS_FOR_SUBSETS:
        return NOT_RUN, (f"needs {MIN_MARKETS_FOR_SUBSETS} evidence markets, "
                         f"has {len(usable)}")
    per_market = [float(r["pnl"]) / int(r["trades"]) for r in usable]
    n = len(per_market)
    mean = sum(per_market) / n
    variance = sum((x - mean) ** 2 for x in per_market) / (n - 1)
    if variance <= 0:
        return SURVIVED, f"identical expectancy in all {n} markets"
    stderr = (variance / n) ** 0.5
    t = mean / stderr
    detail = (f"per-market expectancy {mean:+.4f} +/- {stderr:.4f} across "
              f"{n} markets (t={t:.2f})")
    if t >= DISPERSION_STRONG:
        return SURVIVED, detail
    if t >= DISPERSION_WEAK:
        return INCONCLUSIVE, detail
    return FAILED, detail + " - inside the spread between markets"


def drawdown_stress(cumulative: dict) -> tuple[str, str]:
    """Are the drawdown characteristics survivable? (§4, §5's risk term)"""
    pnl = float(cumulative.get("pnl") or 0.0)
    drawdown = abs(float(cumulative.get("drawdown") or 0.0))
    if pnl <= 0 or drawdown <= 0:
        return NOT_RUN, "no positive P&L or no recorded drawdown"
    ratio = drawdown / pnl
    detail = f"worst market drawdown is {ratio:.2f}x total P&L"
    if ratio <= 0.5:
        return SURVIVED, detail
    if ratio <= 1.5:
        return INCONCLUSIVE, detail
    return FAILED, detail


def neighbour_thresholds(siblings: Iterable[Sibling]) -> tuple[str, str]:
    """Does a small parameter change destroy the edge? (§4)

    Asked of re-thresholded sibling VERSIONS, each of which earned its own
    independent evidence. A family whose versions disagree about the sign of
    the effect has not found a relationship; it has found one threshold.
    """
    speaking = [s for s in siblings
                if s.relation == "threshold" and s.speaks]
    if not speaking:
        return NOT_RUN, "no re-thresholded sibling has enough evidence yet"
    signs = {s.expectancy > 0 for s in speaking}
    detail = ", ".join(f"{s.id[:28]} {s.expectancy:+.4f}" for s in speaking)
    if len(signs) > 1:
        return FAILED, detail + " (siblings disagree on the sign)"
    if all(s.expectancy > 0 for s in speaking):
        return SURVIVED, detail
    return FAILED, detail + " (every sibling is negative)"


def alternative_windows(siblings: Iterable[Sibling]) -> tuple[str, str]:
    """Does the effect survive a different holding window? (§4, §12)

    Read from the half/double hold variants the pass already registers. An
    edge that exists only at exactly the mined holding period is a property of
    that period, and §12 asks whether the edge lives in the entry, the exit or
    the duration — this is the free half of that answer.
    """
    speaking = [s for s in siblings if s.relation == "window" and s.speaks]
    if not speaking:
        return NOT_RUN, "no hold variant has enough evidence yet"
    detail = ", ".join(f"{s.id[:28]} {s.expectancy:+.4f}" for s in speaking)
    positive = sum(1 for s in speaking if s.expectancy > 0)
    if positive == len(speaking):
        return SURVIVED, detail
    if positive:
        return INCONCLUSIVE, detail + " (mixed across windows)"
    return FAILED, detail + " (no other window works)"


def inverse_check(cumulative: dict,
                  siblings: Iterable[Sibling]) -> tuple[str, str]:
    """Does reversing the signal work equally well? (§4, §9)

    The most misread test in the battery, so the meaning is fixed here: the
    inverse doing BETTER is not a disqualification, it is a finding. The
    relationship may be real and pointing the other way, and the inverse
    already exists as its own candidate with its own evidence and its own trip
    up the ladder. What IS a disqualification is both directions paying —
    then there is no directional edge, only volatility or a cost artefact.
    """
    speaking = [s for s in siblings if s.relation == "inverse" and s.speaks]
    if not speaking:
        return NOT_RUN, "no inverse sibling has enough evidence yet"
    mine = float(cumulative.get("expectancy") or 0.0)
    best = max(speaking, key=lambda s: s.expectancy)
    detail = f"this {mine:+.4f} vs inverse {best.expectancy:+.4f}"
    if best.expectancy > mine > 0:
        return INVERSE_WON, detail + " (real, and possibly backwards)"
    if best.expectancy > 0 and mine > 0:
        return FAILED, detail + " (both directions pay - not directional)"
    if best.expectancy > mine:
        return INVERSE_WON, detail
    return SURVIVED, detail


# Tests that need more than the ledger, and what supplies it. A `Probe` can
# answer these two; without one they stay NOT RUN. Named either way so
# `coverage` counts them against us rather than quietly shrinking the
# denominator — reporting a battery of six as "6/6 survived" when the design
# calls for fourteen is the arithmetic version of a cherry-pick.
PROBE_TESTS = {
    "placebo": "no probe supplied - randomised-entry control not run",
    "liquidity_stress": "no probe supplied - book depth unavailable",
}

# Still genuinely unrunnable here, and honest about why: entry timing is not a
# perturbation of this record, it is a different experiment, and the pass
# already registers it as its own candidate with its own independent evidence.
UNRUNNABLE_WITHOUT_REPLAY = {
    "delayed_entry": "registered as its own candidate; see variants",
}

ALL_TESTS = ("leave_one_market_out", "market_subsets", "temporal_split",
             "concentration", "sample_depth", "cost_stress",
             "slippage_stress", "edge_vs_dispersion", "drawdown_stress",
             "neighbour_thresholds", "alternative_windows", "inverse",
             *PROBE_TESTS, *UNRUNNABLE_WITHOUT_REPLAY)


class Probe:
    """What the battery needs from the layer that owns the tape.

    Subclassed in `research.py`, where the pool CSVs, the cost model and the
    replay engines live. Both methods return the same `(result, detail)` pair
    every other test returns, and either may decline with `NOT_RUN` — a probe
    that cannot answer for this candidate says so rather than guessing, and
    the coverage figure absorbs the difference.
    """

    def placebo(self, entry: dict, cumulative: dict,
                ledger: list[dict]) -> tuple[str, str]:
        """Does the signal beat firing at random on the same markets?"""
        return NOT_RUN, PROBE_TESTS["placebo"]

    def liquidity_stress(self, entry: dict, cumulative: dict,
                         ledger: list[dict]) -> tuple[str, str]:
        """Does the edge survive where the book is actually thin?"""
        return NOT_RUN, PROBE_TESTS["liquidity_stress"]


# -- the battery --------------------------------------------------------------

def attack(entry: dict, cumulative: dict, ledger: list[dict], cfg,
           siblings: Iterable[Sibling] = (),
           probe: "Probe | None" = None) -> AdversarialReport:
    """Run every runnable test against one candidate's own evidence.

    `entry` is a library row, `cumulative` its `library.cumulative()`, and
    `ledger` its `library.market_ledger()`. `probe`, when supplied, answers
    the two questions the ledger cannot (see `Probe`). Nothing is written
    anywhere; the caller decides what to do with the report, and what it may
    do with it is bounded to research priority and the audit record.
    """
    siblings = list(siblings)
    report = AdversarialReport(candidate_id=str(entry.get("id") or ""))
    rule = entry.get("rule") or {}

    # The same guard `worth_attacking` applies, enforced at the entry point so
    # a caller that skips the filter cannot produce a page of meaningless
    # BROKEN verdicts. A record that is already negative has not been broken
    # by anything this module did.
    if float(cumulative.get("expectancy") or 0.0) <= 0.0:
        report.details["not_attacked"] = (
            "expectancy is not positive - there is no apparent edge to "
            "disprove, and every split of a losing record loses")
        return report

    outcomes: dict[str, tuple[str, str]] = {
        "leave_one_market_out": leave_one_market_out(ledger),
        "market_subsets": market_subsets(ledger),
        "temporal_split": temporal_split(ledger),
        "concentration": concentration(cumulative),
        "sample_depth": sample_depth(cumulative, cfg),
        "cost_stress": cost_stress(cumulative, rule, cfg,
                                   COST_STRESS_MULTIPLE),
        "slippage_stress": cost_stress(cumulative, rule, cfg,
                                       SLIPPAGE_STRESS_MULTIPLE),
        "edge_vs_dispersion": edge_vs_dispersion(ledger),
        "drawdown_stress": drawdown_stress(cumulative),
        "neighbour_thresholds": neighbour_thresholds(siblings),
        "alternative_windows": alternative_windows(siblings),
        "inverse": inverse_check(cumulative, siblings),
    }
    # The two the ledger cannot answer. A probe that raises is a probe that
    # failed, not a candidate that survived: the test is recorded NOT RUN
    # with the reason, so a broken tape reads as missing coverage rather than
    # as a pass. This is the one place where an exception could silently
    # become confidence, and it does not.
    for name, decline in PROBE_TESTS.items():
        if probe is None:
            outcomes[name] = (NOT_RUN, decline)
            continue
        try:
            outcomes[name] = getattr(probe, name)(entry, cumulative, ledger)
        except Exception as exc:                          # noqa: BLE001
            outcomes[name] = (NOT_RUN,
                              f"probe failed: {type(exc).__name__}: "
                              f"{str(exc)[:80]}")

    for name, why in UNRUNNABLE_WITHOUT_REPLAY.items():
        outcomes[name] = (NOT_RUN, why)

    for name, (result, detail) in outcomes.items():
        report.results[name] = result
        report.details[name] = detail

    run = [r for r in report.results.values() if r]
    report.coverage = round(len(run) / len(ALL_TESTS), 4)
    if not run:
        report.verdict = V_NOT_ATTACKED
        report.robustness = 0.0
        return report

    # GEOMETRIC mean, for the same reason `meta.weight_for` uses one: a
    # candidate is asked a dozen questions at once, and multiplying the
    # answers would turn "passed most of them" into a number
    # indistinguishable from "failed all of them".
    product = 1.0
    for result in run:
        product *= _WEIGHT.get(result, 0.6)
    report.robustness = round(product ** (1.0 / len(run)), 4)

    failed = report.failed_tests
    if failed:
        report.verdict = V_BROKEN
    elif any(r == INVERSE_WON for r in run) or \
            any(r == INCONCLUSIVE for r in run):
        report.verdict = V_WEAKENED
    elif len(run) >= 4:
        report.verdict = V_SURVIVED
    else:
        # Fewer than four runnable tests is not a clean sheet, it is a thin
        # attack. Calling it SURVIVED would let a candidate with three
        # evidence markets outrank one that was attacked properly and drew.
        report.verdict = V_WEAKENED

    report.failure_states = [
        f"{name}: {report.details.get(name, '')}"
        for name in failed]
    return report


def siblings_of(entry: dict, rows: Iterable[dict],
                cumulative_of) -> list[Sibling]:
    """Find the perturbations of this candidate that already exist.

    Three relations, from what the pass already registers:

    * **threshold** — another VERSION of the same rule family. Same shape,
      different numbers, its own independent evidence.
    * **window** — a hold variant, tagged `half-hold` / `double-hold` /
      `early-exit` on the rule when it was registered.
    * **inverse** — an `inverse` or `directionality-test` variant.

    A variant is matched by its recorded `variant_of` where present, falling
    back to the signature for pre-variant rows. Matching on rule shape instead
    was rejected: two candidates can share a shape by coincidence, and a
    coincidence counted as a controlled perturbation is a fabricated test.
    """
    entry_id = str(entry.get("id") or "")
    signature = str(entry.get("signature") or "")
    out: list[Sibling] = []
    for row in rows:
        row_id = str(row.get("id") or "")
        if row_id == entry_id:
            continue
        rule = row.get("rule") or {}
        variant = str(rule.get("variant") or "")
        parent = str(rule.get("variant_of") or row.get("parent_id") or "")
        related_to_me = parent == entry_id
        same_family = (signature and str(row.get("signature") or "")
                       == signature)
        if not related_to_me and not same_family:
            continue
        if variant in ("inverse", "directionality-test"):
            relation = "inverse"
        elif variant in ("half-hold", "double-hold", "early-exit",
                         "delayed-entry"):
            relation = "window"
        elif same_family and not variant:
            relation = "threshold"
        else:
            continue
        stats = cumulative_of(row_id)
        out.append(Sibling(
            id=row_id, relation=relation,
            trades=int(stats.get("trades") or 0),
            expectancy=float(stats.get("expectancy") or 0.0),
            markets=int(stats.get("markets") or 0)))
    return out


def worth_attacking(cumulative: dict, status: str) -> bool:
    """Which candidates the battery is spent on. §4: *every PROMISING
    strategy* must have an automatic attempt made to disprove it.

    The word promising is load-bearing, and leaving it out was a real bug.
    Run against the real library, the first version of this attacked all 179
    candidates with any evidence and returned BROKEN for every single one —
    because most of them have negative expectancy, and on a losing record
    every test fails trivially. Leave out the best market and a loser is still
    a loser; split it in half and both halves lose. None of that is a finding.
    It is the arithmetic of attacking something that was already refuted, and
    a report saying BROKEN 179 times conveys exactly as much as one saying
    SURVIVED 179 times.

    There is nothing to disprove about a candidate the ladder has already
    seen lose. The battery exists for the ones that look good.
    """
    if status in ("retired", "quarantined"):
        return False
    if int(cumulative.get("trades") or 0) < MIN_TRADES_TO_ATTACK:
        return False
    return float(cumulative.get("expectancy") or 0.0) > 0.0


def summary(reports: Iterable[AdversarialReport]) -> dict:
    """§13's adversarial block, from the reports actually produced."""
    reports = list(reports)
    by_verdict: dict[str, int] = {}
    failed_by_test: dict[str, int] = {}
    for report in reports:
        by_verdict[report.verdict] = by_verdict.get(report.verdict, 0) + 1
        for name in report.failed_tests:
            failed_by_test[name] = failed_by_test.get(name, 0) + 1
    attacked = [r for r in reports if r.verdict != V_NOT_ATTACKED]
    return {
        "adversarialCandidatesAttacked": len(attacked),
        "adversarialTestsRun": sum(r.tests_run for r in reports),
        "adversarialTestsFailed": sum(len(r.failed_tests) for r in reports),
        "adversarialByVerdict": by_verdict,
        "adversarialFailuresByTest": dict(
            sorted(failed_by_test.items(), key=lambda kv: -kv[1])),
        "adversarialMeanRobustness": round(
            sum(r.robustness for r in attacked) / len(attacked), 4)
        if attacked else 0.0,
        "adversarialMeanCoverage": round(
            sum(r.coverage for r in attacked) / len(attacked), 4)
        if attacked else 0.0,
        "adversarialInverseWon": sum(
            1 for r in reports if r.results.get("inverse") == INVERSE_WON),
    }
