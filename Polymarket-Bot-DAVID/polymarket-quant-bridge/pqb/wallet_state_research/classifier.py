"""The frozen RN1 rule, and the registry of everything measured against it.

`FrozenRN1` is a permanent benchmark. Its two thresholds are module constants,
they are asserted by a test, nothing in this package writes to them, and every
optimised model is a NEW registry entry with its own version id rather than a
replacement. Part 14 asks for that and the reason is not ceremony: the moment
the benchmark can be retuned, "the optimised model beat the benchmark" stops
being a statement about anything.

The classifier answers Question A only — what will the wallet do. Whether that
prediction is worth trading on is `backtest`'s problem, and the two are kept in
different files so a good number in one is never quietly read as a good number
in the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol

from .episodes import AGGRESSIVE, DIRECTIONAL, PROTECT, Snapshot

# ---------------------------------------------------------------------------
# THE FROZEN THRESHOLDS. Do not tune. Do not "improve". A test asserts these
# exact values, because a benchmark that drifts is not a benchmark.
# ---------------------------------------------------------------------------
RN1_INVENTORY_RATIO_THRESHOLD = 0.91043
RN1_SHARES_NEEDED_THRESHOLD = 0.810012
RN1_HORIZON_MINUTES = 3.0


@dataclass
class Prediction:
    """One classification, with everything needed to audit it."""

    label: str = ""
    aggressive_probability: float = 0.0
    protect_probability: float = 0.0
    model_version: str = ""
    valid: bool = False
    reason: str = ""
    margin: float = 0.0          # distance from the decision boundary, 0..1
    features_used: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "label": self.label, "valid": self.valid, "reason": self.reason,
            "aggressiveProbability": round(self.aggressive_probability, 4),
            "protectProbability": round(self.protect_probability, 4),
            "modelVersion": self.model_version,
            "margin": round(self.margin, 6),
            "featuresUsed": {k: (round(v, 6) if isinstance(v, float) else v)
                             for k, v in self.features_used.items()},
        }


class BehaviorModel(Protocol):
    """What every model in the registry must offer. Deliberately tiny."""

    version: str
    name: str

    def predict(self, snapshot: Snapshot,
                features: Optional[dict] = None) -> Prediction: ...


class FrozenRN1:
    """PART 2, implemented literally and never touched again.

        if inventoryRatio >= 0.91043
           or sharesNeededOppositeToZero <= 0.810012:
            AGGRESSIVE_OPPOSITE
        else:
            PROTECT_REBALANCE

    Two details the brief leaves implicit and this implementation makes
    explicit, because both change the numbers:

    * **A missing feature is not a satisfied condition.** `OR` over a None is
      not False-by-default here; if `sharesNeededOppositeToZero` cannot be
      computed the rule still fires on the ratio alone, but if the RATIO
      cannot be computed the snapshot is INVALID and no prediction is made.
      Scoring an unanswerable case as PROTECT would inflate whichever class is
      more common.
    * **The rule is a rule, not a probability.** It emits 1.0/0.0 with a
      `margin` alongside — the normalised distance from whichever threshold
      decided it — so the confidence tiers in `signal` have something real to
      grade rather than a fabricated probability.
    """

    version = "RN1_FROZEN_V1"
    name = "Frozen RN1 two-threshold rule (+3m)"

    def predict(self, snapshot: Snapshot,
                features: Optional[dict] = None) -> Prediction:
        out = Prediction(model_version=self.version)
        if not snapshot.valid:
            out.reason = snapshot.invalid_reason or "invalid snapshot"
            return out
        ratio = snapshot.inventory_ratio
        if ratio is None:
            out.reason = "inventory ratio undefined (original side not held)"
            return out
        needed = snapshot.shares_needed_opposite_to_zero()
        out.features_used = {"inventoryRatio": ratio,
                             "sharesNeededOppositeToZero": needed}
        out.valid = True

        by_ratio = ratio >= RN1_INVENTORY_RATIO_THRESHOLD
        by_needed = (needed is not None
                     and needed <= RN1_SHARES_NEEDED_THRESHOLD)
        aggressive = by_ratio or by_needed

        out.label = AGGRESSIVE if aggressive else PROTECT
        out.aggressive_probability = 1.0 if aggressive else 0.0
        out.protect_probability = 0.0 if aggressive else 1.0
        out.reason = ("inventoryRatio" if by_ratio else
                      "sharesNeededOppositeToZero" if by_needed else
                      "neither threshold met")
        out.margin = _rule_margin(ratio, needed)
        return out


def _rule_margin(ratio: float, needed: Optional[float]) -> float:
    """How far this case sits from the decision boundary, 0..1.

    The maximum of the two normalised distances, because either condition
    alone can decide the class. A case sitting on top of both thresholds is
    the one the rule knows least about, and `signal`'s confidence tiers need
    to be able to say so.
    """
    ratio_gap = abs(ratio - RN1_INVENTORY_RATIO_THRESHOLD) \
        / max(1e-9, RN1_INVENTORY_RATIO_THRESHOLD)
    if needed is None:
        return min(1.0, ratio_gap)
    needed_gap = abs(needed - RN1_SHARES_NEEDED_THRESHOLD) \
        / max(1e-9, RN1_SHARES_NEEDED_THRESHOLD)
    return min(1.0, max(ratio_gap, needed_gap))


class AlwaysAggressive:
    """The majority-class baseline. Part 15 requires it and it is brutal:
    if the frozen rule cannot beat 'predict the common class every time', its
    accuracy is a statement about the class balance, not about the rule."""

    version = "BASELINE_ALWAYS_AGGRESSIVE_V1"
    name = "Baseline: always predict AGGRESSIVE"

    def predict(self, snapshot: Snapshot,
                features: Optional[dict] = None) -> Prediction:
        if not snapshot.valid:
            return Prediction(model_version=self.version,
                              reason=snapshot.invalid_reason)
        return Prediction(label=AGGRESSIVE, aggressive_probability=1.0,
                          model_version=self.version, valid=True,
                          reason="baseline", margin=0.0)


class AlwaysProtect:
    version = "BASELINE_ALWAYS_PROTECT_V1"
    name = "Baseline: always predict PROTECT"

    def predict(self, snapshot: Snapshot,
                features: Optional[dict] = None) -> Prediction:
        if not snapshot.valid:
            return Prediction(model_version=self.version,
                              reason=snapshot.invalid_reason)
        return Prediction(label=PROTECT, protect_probability=1.0,
                          model_version=self.version, valid=True,
                          reason="baseline", margin=0.0)


@dataclass
class ThresholdRule:
    """A TUNED two-threshold rule of the same shape as the frozen one.

    Kept structurally identical on purpose. Comparing the frozen rule against
    a gradient-boosted ensemble conflates two questions — 'were the thresholds
    right' and 'is a rule the right shape' — and only the first one is
    answerable from a sample this size. This model answers the first.

    Fitted parameters live on the instance and the version string carries the
    window they were fitted on, so a result can never be reported without the
    provenance of the numbers behind it.
    """

    ratio_threshold: float = RN1_INVENTORY_RATIO_THRESHOLD
    needed_threshold: float = RN1_SHARES_NEEDED_THRESHOLD
    version: str = "THRESHOLD_TUNED_V1"
    name: str = "Tuned two-threshold rule"
    fitted_on: str = ""
    fitted_n: int = 0

    def predict(self, snapshot: Snapshot,
                features: Optional[dict] = None) -> Prediction:
        out = Prediction(model_version=self.version)
        if not snapshot.valid:
            out.reason = snapshot.invalid_reason or "invalid snapshot"
            return out
        ratio = snapshot.inventory_ratio
        if ratio is None:
            out.reason = "inventory ratio undefined"
            return out
        needed = snapshot.shares_needed_opposite_to_zero()
        aggressive = (ratio >= self.ratio_threshold
                      or (needed is not None
                          and needed <= self.needed_threshold))
        out.valid = True
        out.label = AGGRESSIVE if aggressive else PROTECT
        out.aggressive_probability = 1.0 if aggressive else 0.0
        out.protect_probability = 0.0 if aggressive else 1.0
        out.features_used = {"inventoryRatio": ratio,
                             "sharesNeededOppositeToZero": needed}
        out.margin = _rule_margin(ratio, needed)
        return out


@dataclass
class ProbabilityModel:
    """Wrapper for a fitted scikit-learn-style classifier.

    The estimator is injected rather than constructed here, so this package
    does not depend on scikit-learn at import time — the frozen rule, the
    cross-wallet study and the whole backtest run on a machine without it, and
    only `discovery` needs it.
    """

    estimator: Any
    feature_names: list
    version: str = "UNIVERSAL_WALLET_STATE_V1"
    name: str = "Fitted probability model"
    threshold: float = 0.5
    fitted_on: str = ""
    fitted_n: int = 0

    def predict(self, snapshot: Snapshot,
                features: Optional[dict] = None) -> Prediction:
        out = Prediction(model_version=self.version)
        if not snapshot.valid or features is None:
            out.reason = (snapshot.invalid_reason
                          or "no feature vector supplied")
            return out
        row, missing = [], []
        for name in self.feature_names:
            value = features.get(name)
            if value is None:
                missing.append(name)
                row.append(0.0)
            else:
                row.append(float(value))
        if missing:
            # A model asked to guess at its own inputs is not making a
            # prediction. Reported, not imputed silently.
            out.reason = "missing features: " + ", ".join(missing[:4])
            return out
        try:
            probability = float(self.estimator.predict_proba([row])[0][1])
        except Exception as exc:                         # noqa: BLE001
            out.reason = f"estimator failed: {exc}"
            return out
        out.valid = True
        out.aggressive_probability = probability
        out.protect_probability = 1.0 - probability
        out.label = AGGRESSIVE if probability >= self.threshold else PROTECT
        out.margin = min(1.0, abs(probability - self.threshold) * 2.0)
        out.features_used = dict(zip(self.feature_names, row))
        return out


# The registry. `FrozenRN1` is first and permanent; discovery ADDS entries.
REGISTRY: dict[str, Any] = {
    FrozenRN1.version: FrozenRN1(),
    AlwaysAggressive.version: AlwaysAggressive(),
    AlwaysProtect.version: AlwaysProtect(),
}


def register(model: Any) -> str:
    """Add a model. Refuses to overwrite the frozen benchmark (Part 14)."""
    version = str(getattr(model, "version", "") or "")
    if not version:
        raise ValueError("a model must carry a version id")
    if version == FrozenRN1.version:
        raise ValueError(
            "RN1_FROZEN_V1 is the permanent benchmark and cannot be replaced. "
            "Register an optimised model under its own version id and report "
            "the two side by side.")
    REGISTRY[version] = model
    return version


# ---------------------------------------------------------------------------
# Scoring — Question A only
# ---------------------------------------------------------------------------


@dataclass
class ClassificationReport:
    """Question A's answer, with every count the brief asks for.

    `balanced_accuracy` is reported next to `accuracy` everywhere and is the
    one to read: with an unbalanced population, accuracy is mostly a
    description of the population.
    """

    model_version: str = ""
    horizon_minutes: float = 0.0
    switched: int = 0
    valid_snapshots: int = 0
    invalid_snapshots: int = 0
    invalid_reasons: dict = field(default_factory=dict)
    graded: int = 0
    directional_excluded: int = 0
    truncated_excluded: int = 0
    # confusion, on the two-class population
    tp: int = 0          # predicted AGGRESSIVE, was AGGRESSIVE
    fp: int = 0          # predicted AGGRESSIVE, was PROTECT
    tn: int = 0          # predicted PROTECT,    was PROTECT
    fn: int = 0          # predicted PROTECT,    was AGGRESSIVE

    @property
    def actual_aggressive(self) -> int:
        return self.tp + self.fn

    @property
    def actual_protect(self) -> int:
        return self.tn + self.fp

    @property
    def predicted_aggressive(self) -> int:
        return self.tp + self.fp

    @property
    def accuracy(self) -> float:
        return ((self.tp + self.tn) / self.graded) if self.graded else 0.0

    @property
    def aggressive_recall(self) -> float:
        actual = self.actual_aggressive
        return (self.tp / actual) if actual else 0.0

    @property
    def protect_recall(self) -> float:
        actual = self.actual_protect
        return (self.tn / actual) if actual else 0.0

    @property
    def balanced_accuracy(self) -> float:
        return (self.aggressive_recall + self.protect_recall) / 2.0

    @property
    def aggressive_precision(self) -> float:
        predicted = self.predicted_aggressive
        return (self.tp / predicted) if predicted else 0.0

    @property
    def protect_precision(self) -> float:
        predicted = self.tn + self.fn
        return (self.tn / predicted) if predicted else 0.0

    @property
    def base_rate(self) -> float:
        """Share of the graded population that is AGGRESSIVE — the number
        `accuracy` must be read against."""
        return (self.actual_aggressive / self.graded) if self.graded else 0.0

    def to_dict(self) -> dict:
        return {
            "modelVersion": self.model_version,
            "horizonMinutes": self.horizon_minutes,
            "switchedConditions": self.switched,
            "validSnapshots": self.valid_snapshots,
            "invalidSnapshots": self.invalid_snapshots,
            "invalidReasons": dict(self.invalid_reasons),
            "graded": self.graded,
            "directionalExcluded": self.directional_excluded,
            "truncatedExcluded": self.truncated_excluded,
            "protectCount": self.actual_protect,
            "aggressiveCount": self.actual_aggressive,
            "baseRateAggressive": round(self.base_rate, 4),
            "accuracy": round(self.accuracy, 4),
            "balancedAccuracy": round(self.balanced_accuracy, 4),
            "aggressivePrecision": round(self.aggressive_precision, 4),
            "aggressiveRecall": round(self.aggressive_recall, 4),
            "protectPrecision": round(self.protect_precision, 4),
            "protectRecall": round(self.protect_recall, 4),
            "confusionMatrix": {
                "predAggressive_actualAggressive": self.tp,
                "predAggressive_actualProtect": self.fp,
                "predProtect_actualProtect": self.tn,
                "predProtect_actualAggressive": self.fn,
            },
        }


def evaluate(model: Any, episodes, horizon_minutes: float,
             features_of=None) -> tuple[ClassificationReport, list]:
    """Grade a model over episodes. Returns the report and the per-case rows.

    The per-case rows are returned rather than only the aggregate because
    every later stage — profitability, cross-section, bootstrap — needs the
    individual cases, and recomputing them would let two parts of the report
    disagree about the same episode.
    """
    report = ClassificationReport(
        model_version=str(getattr(model, "version", "?")),
        horizon_minutes=horizon_minutes)
    cases: list = []
    for episode in episodes:
        if not episode.switched:
            continue
        report.switched += 1
        snapshot = episode.snapshot(horizon_minutes)
        if not snapshot.valid:
            report.invalid_snapshots += 1
            reason = snapshot.invalid_reason or "unknown"
            report.invalid_reasons[reason] = \
                report.invalid_reasons.get(reason, 0) + 1
            continue
        report.valid_snapshots += 1
        features = features_of(episode, snapshot) if features_of else None
        prediction = model.predict(snapshot, features)
        cases.append({"episode": episode, "snapshot": snapshot,
                      "prediction": prediction})
        if not prediction.valid:
            continue
        if episode.label_quality == "truncated":
            report.truncated_excluded += 1
            continue
        if episode.label == DIRECTIONAL:
            report.directional_excluded += 1
            continue
        if not episode.two_class:
            continue
        report.graded += 1
        actual_aggressive = episode.label == AGGRESSIVE
        predicted_aggressive = prediction.label == AGGRESSIVE
        if predicted_aggressive and actual_aggressive:
            report.tp += 1
        elif predicted_aggressive:
            report.fp += 1
        elif actual_aggressive:
            report.fn += 1
        else:
            report.tn += 1
    return report, cases
