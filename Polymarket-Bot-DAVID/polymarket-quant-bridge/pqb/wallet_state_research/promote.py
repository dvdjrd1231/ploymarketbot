"""§27 / §49 — putting the V2 candidate through the gates it has not passed.

The discovery branch found that adding one feature — the wallet's own prior
two-sided rate — lifts balanced accuracy from V1's 37.7% to 53.5%. That number
was produced by fitting on a development window and scoring on a validation
window, which is exactly as far as a candidate is allowed to get before it has
proved anything.

§27 requires more: an untouched holdout, hyperparameters frozen beforehand,
and preferably a clean prospective window. This module is the ladder, and it is
built so a rung cannot be skipped:

    fit on DEVELOPMENT
        -> score on VALIDATION            (choose the candidate)
        -> freeze()                       (record exactly what was fixed)
        -> score on HOLDOUT               (opened once, after freeze)
        -> score on CLEAN PROSPECTIVE     (after the frozen boundary)

`evaluate` refuses to open the holdout until `freeze()` has been called, and
the frozen description is carried into the result so a holdout number always
arrives with a statement of what was fixed before it was produced.

The gate at the end is deliberately hard to pass. A candidate must beat the
frozen rule AND beat the majority-class baseline AND hold up on data nobody
tuned against — and even then the verdict is a recommendation to a person, not
a promotion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .episodes import Episode
from .strategy_v1 import (MulticlassReport, PROSPECTIVE_BOUNDARY_TS,
                          RN1StrategyModelV1, eligibility)

# How much better than the frozen rule a candidate must be, in balanced
# accuracy, before the difference is called a difference rather than noise.
MIN_IMPROVEMENT = 0.03

# Below this many holdout cases nothing is concluded either way.
MIN_HOLDOUT = 500


@dataclass
class PromotionResult:
    """One candidate's whole trip through the ladder."""

    candidate_version: str = ""
    frozen_description: str = ""
    development_n: int = 0
    validation: dict = field(default_factory=dict)
    holdout: dict = field(default_factory=dict)
    prospective: dict = field(default_factory=dict)
    baseline_validation: dict = field(default_factory=dict)
    baseline_holdout: dict = field(default_factory=dict)
    baseline_prospective: dict = field(default_factory=dict)
    verdict: str = ""
    stage: str = "research_only"
    checks: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "candidateVersion": self.candidate_version,
            "frozenDescription": self.frozen_description,
            "developmentConditions": self.development_n,
            "validation": self.validation,
            "holdout": self.holdout,
            "cleanProspective": self.prospective,
            "frozenV1Baseline": {
                "validation": self.baseline_validation,
                "holdout": self.baseline_holdout,
                "cleanProspective": self.baseline_prospective},
            "checks": list(self.checks),
            "verdict": self.verdict,
            "recommendedStage": self.stage,
            "note": ("Fitted on development, chosen on validation, then "
                     "frozen and opened on a holdout nobody tuned against. "
                     "V1 itself is untouched throughout — this is a separate "
                     "version, reported beside it (§24)."),
        }


class Ladder:
    """The four windows, with the holdout under a lock."""

    def __init__(self, episodes: list,
                 boundary_ts: float = PROSPECTIVE_BOUNDARY_TS,
                 dev_fraction: float = 0.5, val_fraction: float = 0.25):
        gradable = [e for e in episodes if e.labelled]
        # Split PRE-BOUNDARY history into dev/val/holdout, and keep the
        # post-boundary conditions entirely separate as the clean prospective
        # window. Mixing them would let a prospective case train the model
        # that is later reported as predicting it.
        history = sorted((e for e in gradable
                          if e.first_buy_ts < boundary_ts),
                         key=lambda e: e.first_buy_ts)
        self.prospective = sorted((e for e in gradable
                                   if e.first_buy_ts >= boundary_ts),
                                  key=lambda e: e.first_buy_ts)
        dev_end = int(len(history) * dev_fraction)
        val_end = int(len(history) * (dev_fraction + val_fraction))
        self.development = history[:dev_end]
        self.validation = history[dev_end:val_end]
        self._holdout = history[val_end:]
        self.frozen = False
        self.frozen_description = ""

    @property
    def holdout_size(self) -> int:
        return len(self._holdout)

    def freeze(self, description: str) -> None:
        self.frozen = True
        self.frozen_description = description

    def holdout(self) -> list:
        if not self.frozen:
            raise RuntimeError(
                "The untouched holdout was requested before freeze(). §27: "
                "every hyperparameter must be fixed first, and the fixing "
                "must be stated.")
        return list(self._holdout)

    def to_dict(self) -> dict:
        return {"development": len(self.development),
                "validation": len(self.validation),
                "holdout": self.holdout_size,
                "cleanProspective": len(self.prospective),
                "frozen": self.frozen}


def evaluate(episodes: list, fit: Callable, score: Callable,
             candidate_version: str,
             boundary_ts: float = PROSPECTIVE_BOUNDARY_TS
             ) -> PromotionResult:
    """Run one candidate up the ladder.

    `fit(development)` returns a model; `score(model, episodes, version)`
    returns a `MulticlassReport`. Injected rather than imported so this file
    stays a procedure and knows nothing about any particular model family.
    """
    out = PromotionResult(candidate_version=candidate_version)
    ladder = Ladder(episodes, boundary_ts)
    out.development_n = len(ladder.development)
    if len(ladder.development) < 1_000 or ladder.holdout_size < MIN_HOLDOUT:
        out.verdict = (
            f"INSUFFICIENT DATA: {len(ladder.development)} development and "
            f"{ladder.holdout_size} holdout conditions. Nothing is concluded.")
        return out

    model = fit(ladder.development)
    if model is None:
        out.verdict = "the candidate could not be fitted on this development "\
                      "window"
        return out

    frozen = RN1StrategyModelV1()

    def _frozen_label(episode, _features=None):
        prediction = frozen.predict(episode)
        return prediction.label if prediction.valid else None

    out.validation = score(model, ladder.validation, candidate_version)
    out.baseline_validation = score(_frozen_label, ladder.validation,
                                    "RN1_STRATEGY_MODEL_V1")

    # THE FREEZE. Everything the candidate is made of is now fixed, and the
    # description is what the holdout number will be reported alongside.
    ladder.freeze(
        f"Frozen before the holdout was opened: the {candidate_version} "
        "feature set, its estimator and hyperparameters, the three-class "
        "labelling boundary (1.40), the eligibility gate, and the "
        "development/validation split. The holdout and the clean prospective "
        "window informed none of them.")
    out.frozen_description = ladder.frozen_description

    holdout = ladder.holdout()
    out.holdout = score(model, holdout, candidate_version)
    out.baseline_holdout = score(_frozen_label, holdout,
                                 "RN1_STRATEGY_MODEL_V1")

    if ladder.prospective:
        out.prospective = score(model, ladder.prospective, candidate_version)
        out.baseline_prospective = score(_frozen_label, ladder.prospective,
                                         "RN1_STRATEGY_MODEL_V1")

    out.checks, out.verdict, out.stage = _judge(out)
    return out


def _judge(result: PromotionResult) -> tuple:
    """The gate. Deliberately hard, and it reports each leg separately."""
    checks: list = []

    def _bal(block: dict) -> float:
        return float((block or {}).get("balancedAccuracy") or 0.0)

    def _n(block: dict) -> int:
        return int((block or {}).get("resolved") or 0)

    holdout_gain = _bal(result.holdout) - _bal(result.baseline_holdout)
    beats_frozen = holdout_gain >= MIN_IMPROVEMENT
    checks.append({
        "check": "beats the frozen rule on the untouched holdout",
        "candidate": round(_bal(result.holdout), 4),
        "frozenV1": round(_bal(result.baseline_holdout), 4),
        "delta": round(holdout_gain, 4),
        "required": MIN_IMPROVEMENT,
        "passed": beats_frozen})

    baseline = float((result.holdout or {}).get("majorityBaseline") or 0.0)
    beats_majority = bool((result.holdout or {}).get("beatsMajorityBaseline"))
    checks.append({
        "check": "beats the majority-class baseline on the holdout",
        "accuracy": round(float((result.holdout or {}).get("accuracy") or 0), 4),
        "majorityBaseline": round(baseline, 4),
        "passed": beats_majority})

    enough = _n(result.holdout) >= MIN_HOLDOUT
    checks.append({"check": "holdout sample is large enough",
                   "resolved": _n(result.holdout), "required": MIN_HOLDOUT,
                   "passed": enough})

    prospective_gain = (_bal(result.prospective)
                        - _bal(result.baseline_prospective))
    holds_forward = bool(result.prospective) and prospective_gain >= 0
    checks.append({
        "check": "improvement survives the clean prospective window",
        "candidate": round(_bal(result.prospective), 4),
        "frozenV1": round(_bal(result.baseline_prospective), 4),
        "delta": round(prospective_gain, 4),
        "resolved": _n(result.prospective),
        "passed": holds_forward})

    # Stability: an improvement that exists on validation and vanishes on the
    # holdout is the signature of a fit, and it is the single most common way
    # a candidate like this fails.
    validation_gain = _bal(result.validation) - _bal(result.baseline_validation)
    stable = (validation_gain > 0 and holdout_gain > 0
              and abs(validation_gain - holdout_gain) <= 0.15)
    checks.append({
        "check": "the improvement is stable between validation and holdout",
        "validationDelta": round(validation_gain, 4),
        "holdoutDelta": round(holdout_gain, 4),
        "passed": stable})

    passed = [c for c in checks if c["passed"]]
    if not enough:
        return checks, ("INSUFFICIENT HOLDOUT — nothing concluded"), \
            "research_only"
    if len(passed) == len(checks):
        return checks, (
            f"ALL GATES PASSED. {result.candidate_version} improved balanced "
            f"accuracy by {holdout_gain:+.1%} over the frozen rule on an "
            f"untouched holdout of {_n(result.holdout):,} conditions, beat "
            "the majority baseline, held up in the clean prospective window, "
            "and the improvement was stable across windows. That is a "
            "candidate worth a human review — not a promotion."
        ), "ready_for_human_review"
    if beats_frozen and not beats_majority:
        return checks, (
            f"PARTIAL: it beats the frozen rule by {holdout_gain:+.1%} on the "
            "holdout but still does NOT beat predicting the commonest class. "
            "Beating a rule that is itself worse than guessing is not "
            "evidence of predictive value."
        ), "research_only"
    if not beats_frozen:
        return checks, (
            f"FAILED: the validation improvement did not survive the "
            f"untouched holdout ({holdout_gain:+.1%}, needed "
            f"{MIN_IMPROVEMENT:+.0%}). This is what an overfitted candidate "
            "looks like, and it is why the holdout exists."
        ), "research_only"
    return checks, (
        f"{len(passed)} of {len(checks)} gates passed. Not enough to advance "
        "past research-only."), "research_only"
