"""THE RESEARCH REWARD FUNCTION — what deserves the next unit of compute.

This is the one number in the system that is allowed to be opinionated, and
the reason it is safe to let it be opinionated is that it decides nothing.
It orders a queue. Every gate that can move a candidate toward being traded
lives in `library.next_status`, reads `validations`, and has never heard of
this module.

That separation is §17 and §18 stated as an import graph, so it is worth being
explicit about which way the arrows run:

    reward.py  ->  reads library evidence, adversarial reports, convergence
    reward.py  ->  writes NOTHING
    allocation ->  reads reward
    next_status -> does not import reward, and reward does not import it

The scoring itself is designed against one specific failure. A research
system that is rewarded for producing validated strategies will find that the
cheapest path to that reward is to lower what validation means. So the reward
here is deliberately **not** "how close is this to validating". It is "how
much would testing this again TEACH us" — which is maximised by candidates
that are promising AND under-evidenced, and which falls for a candidate that
is merely flattering. A strategy with a beautiful in-sample number and no
unseen record scores on its novelty and nothing else.

The shape is multiplicative for the quality terms and additive for the
information terms, and both halves are bounded. Multiplicative quality means
a zero anywhere — no robustness, refuted by attack, a record that is one
market's story — cannot be compensated for by a flattering number somewhere
else, which is the same discipline `library.evidence_score` uses and for the
same reason. Additive information means an unexplored candidate can still
earn attention without having to be good first, which is the starvation loop
`allocation.py` exists to break.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# The bound on the whole score. Priority is compared against `research_priority`
# values that live in 0..1.5, and the allocator sorts on the product, so an
# unbounded term here would let one dimension take the entire slate.
SCORE_MIN = 0.0
SCORE_MAX = 2.0

# Convergence is a research signal and never evidence (§3 of the directive,
# §12 of the second). The bound is what enforces that in code rather than in a
# comment: even perfect convergence across five independent sources can at
# most raise a candidate's place in the queue by a quarter, and it can never
# reach a gate at all.
CONVERGENCE_MAX_BONUS = 0.25

# Likewise for meta-structure and family weights, which arrive already bounded
# from `meta.weights` (0.6..1.6). Reward multiplies rather than re-derives.

# A dead-ended family is throttled, not silenced. 0.35 keeps it in the queue
# behind everything with a live question, which is what "abandon consistently
# unproductive branches" should mean for a search that must stay open.
DEAD_END_MULTIPLIER = 0.35

# How much of a candidate's score can come from being under-explored. High
# on purpose: the audit's finding was 153 of 231 candidates never evaluated
# once, and the fix for that is a real information term, not a bigger reserve.
EXPLORATION_MAX = 0.6


@dataclass
class RewardBreakdown:
    """One candidate's research priority, and the sentences behind it.

    §13 of the directive asks the dashboard to answer two questions in words:
    why is this candidate receiving more research, and why was that one
    stopped. Deriving those from the components at display time would let the
    explanation drift from the arithmetic; they are produced here, by the same
    function that produced the number, from the same values.
    """

    candidate_id: str = ""
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)
    rewards: list[str] = field(default_factory=list)
    penalties: list[str] = field(default_factory=list)
    why_more: str = ""
    why_stopped: str = ""

    def to_dict(self) -> dict:
        return {
            "candidate": self.candidate_id,
            "score": round(self.score, 4),
            "components": {k: round(v, 4)
                           for k, v in self.components.items()},
            "rewards": list(self.rewards),
            "penalties": list(self.penalties),
            "whyMoreResearch": self.why_more,
            "whyStopped": self.why_stopped,
        }


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score(entry: dict, cumulative: dict, cfg, *,
          adversarial: Optional[Any] = None,
          diversity: Optional[dict] = None,
          convergence: float = 0.0,
          structure_weight: float = 1.0,
          family_weight: float = 1.0,
          motif_weight: float = 1.0,
          motif_note: str = "",
          dead_end: str = "",
          attempts: Optional[dict] = None,
          eligible_markets: int = -1) -> RewardBreakdown:
    """The research-quality score for one candidate. Allocation only.

    `diversity` is the candidate's evidence spread, from
    `eligibility.diversity_of` — how many distinct categories, eras and price
    regions its evidence covers. `convergence` is the hypothesis layer's
    priority for the relationship this candidate expresses. `eligible_markets`
    is how many unseen markets it could legitimately be given next; -1 means
    unknown, and it is then not scored on it rather than scored as zero.
    """
    out = RewardBreakdown(candidate_id=str(entry.get("id") or ""))
    status = str(entry.get("status") or "")
    trades = int(cumulative.get("trades") or 0)
    markets = int(cumulative.get("markets") or 0)
    expectancy = float(cumulative.get("expectancy") or 0.0)
    top_share = float(cumulative.get("top_share") or 0.0)

    # Terminal states earn nothing. Retirement is the ladder's decision and
    # this module does not second-guess it; a rejected candidate is brought
    # back by `rejected_for_recheck`, which does not consult the reward.
    if status in ("retired", "quarantined"):
        out.why_stopped = (f"terminal state '{status}' - the ladder has "
                           "finished with this version")
        return out

    # -- 1. QUALITY. Multiplicative, so nothing compensates for a zero. ------

    if trades == 0:
        # No record to judge, so quality is neutral rather than zero. Scoring
        # an untested candidate as bad is the circularity that produced the
        # 153-never-evaluated finding, arriving through the reward instead of
        # through the sort.
        quality = 0.5
        out.components["quality"] = quality
    else:
        expectancy_term = 1.0 if expectancy > 0 else 0.1
        if expectancy <= 0:
            out.penalties.append(
                f"negative unseen expectancy ({expectancy:+.4f})")
        else:
            out.rewards.append(
                f"positive unseen expectancy ({expectancy:+.4f})")

        breadth = _clamp(markets / max(1, cfg.oos_min_markets))
        if markets >= cfg.oos_min_markets:
            out.rewards.append(f"{markets} independent OOS markets")
        elif markets <= 1:
            out.penalties.append("evidence from a single market")

        sample = _clamp(trades / max(1, cfg.oos_min_trades))
        if trades < 10:
            out.penalties.append(f"tiny sample ({trades} trades)")

        diversification = _clamp(1.0 - top_share)
        if top_share > 0.6:
            out.penalties.append(
                f"P&L concentrated in one market ({top_share:.0%})")

        overfit = _overfit_penalty(entry, cumulative)
        if overfit > 0.3:
            out.penalties.append(
                f"in-sample/OOS divergence ({overfit:.2f})")

        robustness = _robustness_term(adversarial, out)

        quality = (expectancy_term * max(0.15, breadth) * max(0.15, sample)
                   * max(0.2, diversification) * (1.0 - 0.5 * overfit)
                   * robustness)
        out.components["quality"] = round(quality, 4)
        out.components["breadth"] = round(breadth, 4)
        out.components["sample"] = round(sample, 4)
        out.components["diversification"] = round(diversification, 4)
        out.components["robustness"] = round(robustness, 4)

    # -- 2. INFORMATION. Additive: what would testing this again TEACH? -----

    information = 0.0

    never_tested = not (attempts or {}).get("evidence")
    if never_tested and trades == 0:
        information += EXPLORATION_MAX
        out.rewards.append("never evaluated - unknown, therefore informative")

    # Under-covered dimensions. A candidate tested in one category across one
    # era has not been shown to generalise; testing it somewhere different is
    # worth more than testing a well-covered candidate again. §7 in one term.
    gaps = 0
    if diversity:
        for key, label in (("categories", "category"), ("eras", "time period"),
                           ("bands", "probability band"),
                           ("temporal_classes", "walk-forward position")):
            if int(diversity.get(key) or 0) <= 1 and markets >= 1:
                gaps += 1
                out.rewards.append(f"untested outside one {label}")
        information += min(0.3, 0.1 * gaps)
        out.components["diversityGaps"] = float(gaps)

    # Near miss: real signal, not enough of it. The cheapest possible source
    # of a genuine strategy, because most of the evidence already exists.
    if 0 < trades < cfg.oos_min_trades and expectancy > 0:
        information += 0.25
        out.rewards.append("near miss - positive but underpowered")

    if 0 <= eligible_markets == 0:
        information = 0.0
        out.penalties.append("no eligible unseen market remains")
        out.why_stopped = ("nothing left to test it on: every eligible market "
                           "is contaminated, has already testified, or is "
                           "parked after repeated non-observations")

    out.components["information"] = round(information, 4)

    # -- 3. STEERING. Bounded multipliers that may reorder, never decide. ---

    convergence_bonus = CONVERGENCE_MAX_BONUS * _clamp(float(convergence))
    if convergence_bonus > 0.05:
        out.rewards.append(
            f"independent sources converge on this relationship "
            f"({convergence:.2f})")
    out.components["convergence"] = round(convergence_bonus, 4)

    # The MOTIF weight arrives already bounded from `motif.weight_for`
    # (0.6..1.6) and is multiplied in exactly like the structure and family
    # weights. It is steering and nothing else: it enters `total` at the very
    # end, after quality and information have been computed from this
    # candidate's OWN evidence, so a strong family can move a candidate up the
    # queue and cannot add a single trade to its record.
    motif_term = _clamp(float(motif_weight), 0.5, 2.0)
    out.components["motif"] = round(motif_term, 4)
    if motif_note and abs(motif_term - 1.0) > 0.02:
        (out.rewards if motif_term > 1.0 else out.penalties).append(motif_note)

    steering = float(structure_weight) * float(family_weight) * motif_term
    out.components["steering"] = round(steering, 4)

    total = (out.components.get("quality", 0.0) + information
             + convergence_bonus) * steering

    if dead_end:
        total *= DEAD_END_MULTIPLIER
        out.penalties.append(f"family repeatedly failed: {dead_end}")
        out.why_stopped = out.why_stopped or (
            f"deprioritised: independent candidates in this family have "
            f"repeatedly failed the same way ({dead_end}). Still queued - a "
            "throttle, not a ban")

    out.score = round(max(SCORE_MIN, min(SCORE_MAX, total)), 4)

    # -- 4. THE SENTENCES ---------------------------------------------------

    if not out.why_more and out.score > 0:
        out.why_more = _explain_more(out, trades, markets, expectancy)
    if not out.why_stopped and out.score <= 0.05:
        out.why_stopped = _explain_stopped(out, status, trades, expectancy)
    return out


def _robustness_term(adversarial: Optional[Any],
                     out: RewardBreakdown) -> float:
    """How much a candidate's survival under attack is worth to its priority.

    Un-attacked is 0.7 rather than 1.0. That gap is the point: it means an
    untested claim ranks BELOW an equivalent one that has been attacked and
    held, so surviving attack is genuinely rewarded rather than merely not
    punished — and a candidate nobody has attacked yet still ranks above one
    that was attacked and broke.
    """
    if adversarial is None:
        return 0.7
    verdict = getattr(adversarial, "verdict", "")
    robustness = float(getattr(adversarial, "robustness", 0.0) or 0.0)
    coverage = float(getattr(adversarial, "coverage", 0.0) or 0.0)
    if verdict == "NOT_ATTACKED":
        return 0.7
    failed = list(getattr(adversarial, "failed_tests", []) or [])
    if failed:
        out.penalties.append("failed adversarial: " + ", ".join(failed[:3]))
    elif verdict == "SURVIVED":
        out.rewards.append(
            f"survived {getattr(adversarial, 'tests_run', 0)} adversarial "
            f"test(s) at {coverage:.0%} coverage")
    # Thin coverage is discounted toward the un-attacked baseline rather than
    # taken at face value, so a candidate that could only be asked three
    # questions does not outrank one that was asked ten and answered them.
    confidence = _clamp(0.4 + 0.6 * coverage)
    return _clamp(0.7 * (1.0 - confidence) + robustness * confidence, 0.05,
                  1.0)


def _overfit_penalty(entry: dict, cumulative: dict) -> float:
    from .library import overfit_risk
    return overfit_risk(float(entry.get("in_win") or 0.0), cumulative)


def _explain_more(out: RewardBreakdown, trades: int, markets: int,
                  expectancy: float) -> str:
    """§13: 'WHY IS THIS CANDIDATE RECEIVING MORE RESEARCH?', in a sentence."""
    if not out.rewards:
        return (f"baseline research priority: {trades} unseen trade(s) across "
                f"{markets} market(s), nothing yet distinguishing it")
    lead = out.rewards[0]
    rest = out.rewards[1:3]
    sentence = f"receiving more research because {lead}"
    if rest:
        sentence += "; also " + "; ".join(rest)
    if out.penalties:
        sentence += f" (despite {out.penalties[0]})"
    return sentence


def _explain_stopped(out: RewardBreakdown, status: str, trades: int,
                     expectancy: float) -> str:
    """§13: 'WHY WAS THIS CANDIDATE STOPPED?', in a sentence."""
    if out.penalties:
        return ("deprioritised because " + "; ".join(out.penalties[:3])
                + ". The ladder's verdict is unchanged - this affects "
                  "research order only")
    return (f"deprioritised at status '{status}' with {trades} unseen "
            f"trade(s) and expectancy {expectancy:+.4f}")


def summary(breakdowns: Iterable[RewardBreakdown]) -> dict:
    """The research-health view of the reward itself (§13)."""
    rows = list(breakdowns)
    if not rows:
        return {"rewardScored": 0}
    scored = sorted(rows, key=lambda b: -b.score)
    penalised = [b for b in rows if b.penalties]
    return {
        "rewardScored": len(rows),
        "rewardMean": round(sum(b.score for b in rows) / len(rows), 4),
        "rewardTop": [b.candidate_id for b in scored[:5]],
        "rewardStopped": sum(1 for b in rows if b.why_stopped),
        "rewardPenalised": len(penalised),
        "rewardTopReason": scored[0].why_more if scored else "",
    }
