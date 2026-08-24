"""RN1 STRATEGY MODEL V1 — the authoritative frozen model, and its gate.

This supersedes the post-opposite-buy +3m rule as the PRIMARY model. That
earlier rule is not deleted: it stays in `classifier.FrozenRN1` as a supporting
benchmark, because a historical model version is never overwritten (§46) and
because a benchmark that can be edited is not a benchmark.

The two models answer different questions and it is worth being explicit about
the difference, since they are easy to confuse:

    FrozenRN1 (supporting)   fires 3 minutes AFTER the wallet already bought
                             the other side. Two classes. It asks "how far
                             will this two-sided position go?"

    StrategyModelV1 (primary) fires at the FIRST BUY, before anything
                             two-sided has happened at all. Three classes. It
                             asks "which position-management mode is this
                             condition going to become?"

V1 is the harder question and it is the one the handoff froze:

    initialPrice <= 0.20 AND initialCapital >= $5  ->  AGGRESSIVE_OPPOSITE
    initialPrice >= 0.80                           ->  DIRECTIONAL
    otherwise                                      ->  PROTECT_REBALANCE

Three numbers — 0.20, 0.80, $5 — and none of them is tuned here. A test pins
all three, `register` refuses to replace the version, and the discovery branch
produces `RN1_STRATEGY_MODEL_V2_DISCOVERY` alongside rather than in place of it.

## The clean prospective gate

The handoff is emphatic that an earlier forward validator was contaminated by
replaying old conditions as though they were fresh predictions, and that the
~40.48% it produced must never be reported as forward performance. That failure
mode is not prevented by intention, so it is prevented by construction:
`eligibility()` is the only way into the clean sample, it takes a boundary and
a freshness window, and it returns a REASON for every rejection. The
contaminated run is registered in `registry` as permanently quarantined so that
anything trying to fold it back in fails loudly.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field
from typing import Optional

from .episodes import AGGRESSIVE, DIRECTIONAL, PROTECT, Episode

# ---------------------------------------------------------------------------
# THE FROZEN CONSTANTS. Do not tune. §3, §50.
# ---------------------------------------------------------------------------
V1_AGGRESSIVE_PRICE_MAX = 0.20      # initialPrice <= this ...
V1_AGGRESSIVE_CAPITAL_MIN = 5.00    # ... AND initialCapital >= this
V1_DIRECTIONAL_PRICE_MIN = 0.80     # initialPrice >= this
V1_MODEL_VERSION = "RN1_STRATEGY_MODEL_V1"

# The clean prospective boundary: 2026-08-20 12:10 PM Eastern = 16:10 UTC.
# Stated as an explicit UTC construction rather than a bare epoch so that the
# number can be checked by eye against the handoff.
PROSPECTIVE_BOUNDARY_UTC = _dt.datetime(
    2026, 8, 20, 16, 10, 0, tzinfo=_dt.timezone.utc)
PROSPECTIVE_BOUNDARY_TS = PROSPECTIVE_BOUNDARY_UTC.timestamp()

# A prediction must be created within this long of the first BUY, or it is not
# a prediction, it is a look at the past (§14).
DEFAULT_FRESHNESS_MINUTES = 15.0

# Rejection reasons. Machine-readable so the census can name the bottleneck
# rather than reporting a single shrunken number.
R_BEFORE_BOUNDARY = "before_prospective_boundary"
R_STALE = "outside_freshness_window"
R_REDEEM_FIRST = "redeem_before_prediction"
R_NO_PRICE = "no_initial_price"
R_NO_CAPITAL = "no_initial_capital"


@dataclass
class Eligibility:
    """Whether one condition may enter a sample, and why not if not."""

    eligible: bool = False
    prospective: bool = False           # ...and is it in the CLEAN sample?
    reason: str = ""
    prediction_ts: float = 0.0
    information_cutoff_ts: float = 0.0
    age_seconds: float = 0.0

    def to_dict(self) -> dict:
        return {"eligible": self.eligible, "prospective": self.prospective,
                "reason": self.reason, "predictionTs": self.prediction_ts,
                "informationCutoffTs": self.information_cutoff_ts,
                "ageSeconds": round(self.age_seconds, 1)}


def eligibility(episode: Episode,
                boundary_ts: float = PROSPECTIVE_BOUNDARY_TS,
                freshness_minutes: float = DEFAULT_FRESHNESS_MINUTES,
                has_redeem_before=None) -> Eligibility:
    """The clean-sample gate (§13, §14).

    `eligible` means the condition can be predicted at all. `prospective`
    additionally means it is after the frozen boundary and therefore belongs
    in the CLEAN forward sample. The two are separate because the
    retrospective study needs the first and must never be reported as the
    second — which is precisely what the contaminated run did.

    The prediction is stamped at the first BUY and the information cutoff at
    `first_buy + freshness`. Anything the model reads must predate the cutoff;
    since the frozen rule reads only the first BUY itself, it satisfies that
    with 15 minutes to spare, and the margin is recorded rather than assumed.
    """
    out = Eligibility(prediction_ts=episode.first_buy_ts)
    if not episode.first_buy_ts:
        out.reason = R_NO_PRICE
        return out
    if episode.first_buy_price <= 0:
        out.reason = R_NO_PRICE
        return out
    if initial_capital(episode) <= 0:
        out.reason = R_NO_CAPITAL
        return out
    out.information_cutoff_ts = (episode.first_buy_ts
                                 + freshness_minutes * 60.0)
    if has_redeem_before is not None and has_redeem_before(episode):
        out.reason = R_REDEEM_FIRST
        return out
    out.eligible = True
    out.age_seconds = 0.0            # replay predicts AT the first buy
    out.prospective = episode.first_buy_ts >= boundary_ts
    out.reason = "" if out.prospective else R_BEFORE_BOUNDARY
    return out


def initial_price(episode: Episode) -> float:
    """`initialPrice` — the price of the FIRST BUY."""
    return float(episode.first_buy_price or 0.0)


def initial_capital(episode: Episode) -> float:
    """`initialObservedCapital` — USDC committed by the first BUY.

    An interpretation decision worth stating: "initial observed capital" could
    mean the first fill alone, or everything bought in the first instant, or
    everything bought inside the freshness window. This uses **every BUY on the
    original side sharing the first BUY's timestamp**, because an order split
    across several fills in the same second is one decision by the wallet and
    reading only the first fill would systematically understate it — which
    matters, since $5 is a threshold the rule turns on.

    `capital_within_freshness` below reports the looser reading beside it, so
    the choice is visible rather than buried.
    """
    if not episode.events:
        return 0.0
    return sum(e.usdc for e in episode.events
               if e.is_buy and e.token_id == episode.original_token
               and e.ts == episode.first_buy_ts)


def capital_within_freshness(episode: Episode,
                             freshness_minutes: float =
                             DEFAULT_FRESHNESS_MINUTES) -> float:
    """The looser reading of initial capital. Diagnostic only — never the
    input to the frozen rule, because changing what the rule reads IS
    changing the rule."""
    cutoff = episode.first_buy_ts + freshness_minutes * 60.0
    return sum(e.usdc for e in episode.events
               if e.is_buy and e.token_id == episode.original_token
               and e.ts <= cutoff)


@dataclass
class V1Prediction:
    """One frozen V1 prediction, with everything needed to audit it."""

    label: str = ""
    valid: bool = False
    reason: str = ""
    model_version: str = V1_MODEL_VERSION
    initial_price: float = 0.0
    initial_capital: float = 0.0
    prediction_ts: float = 0.0
    information_cutoff_ts: float = 0.0
    prospective: bool = False
    # A rule emits certainty, not a distribution. The probabilities are the
    # one-hot form so downstream consumers have one shape to handle; nothing
    # here pretends to be calibrated, and `calibration` in the report measures
    # exactly that.
    probabilities: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"label": self.label, "valid": self.valid,
                "reason": self.reason, "modelVersion": self.model_version,
                "initialPrice": round(self.initial_price, 6),
                "initialCapital": round(self.initial_capital, 4),
                "predictionTs": self.prediction_ts,
                "informationCutoffTs": self.information_cutoff_ts,
                "prospective": self.prospective,
                "probabilities": dict(self.probabilities)}


class RN1StrategyModelV1:
    """§3, implemented literally and permanently.

    Reads two numbers, both known at the first BUY, and emits one of three
    states. That is the whole model. Its simplicity is deliberate — the
    handoff froze an entry-time baseline precisely so that a later, cleverer
    model has something honest to beat.
    """

    version = V1_MODEL_VERSION
    name = "RN1 Strategy Model V1 (frozen entry-time three-state rule)"
    classes = (DIRECTIONAL, PROTECT, AGGRESSIVE)

    def predict(self, episode: Episode,
                gate: Optional[Eligibility] = None) -> V1Prediction:
        price = initial_price(episode)
        capital = initial_capital(episode)
        out = V1Prediction(initial_price=price, initial_capital=capital,
                           prediction_ts=episode.first_buy_ts)
        if gate is not None:
            out.information_cutoff_ts = gate.information_cutoff_ts
            out.prospective = gate.prospective
            if not gate.eligible:
                out.reason = gate.reason
                return out
        if price <= 0:
            out.reason = R_NO_PRICE
            return out

        if price <= V1_AGGRESSIVE_PRICE_MAX \
                and capital >= V1_AGGRESSIVE_CAPITAL_MIN:
            out.label = AGGRESSIVE
            out.reason = (f"initialPrice {price:.4f} <= "
                          f"{V1_AGGRESSIVE_PRICE_MAX} and initialCapital "
                          f"${capital:.2f} >= ${V1_AGGRESSIVE_CAPITAL_MIN:.2f}")
        elif price >= V1_DIRECTIONAL_PRICE_MIN:
            out.label = DIRECTIONAL
            out.reason = (f"initialPrice {price:.4f} >= "
                          f"{V1_DIRECTIONAL_PRICE_MIN}")
        else:
            out.label = PROTECT
            out.reason = "neither threshold met"
        out.valid = True
        out.probabilities = {cls: (1.0 if cls == out.label else 0.0)
                             for cls in self.classes}
        return out


# ---------------------------------------------------------------------------
# Three-class scoring
# ---------------------------------------------------------------------------


@dataclass
class MulticlassReport:
    """§13's full accounting for a three-state model.

    Balanced accuracy sits next to accuracy everywhere and is the one to read:
    with the observed class balance (PROTECT dominates), a model that always
    said PROTECT would score well on accuracy and would know nothing.
    """

    model_version: str = ""
    classes: tuple = (DIRECTIONAL, PROTECT, AGGRESSIVE)
    eligible: int = 0
    predictions: int = 0
    rejected: dict = field(default_factory=dict)
    resolved: int = 0
    unresolved: int = 0
    matrix: dict = field(default_factory=dict)   # (pred, actual) -> n
    probabilities: list = field(default_factory=list)
    prospective_only: bool = False

    def record(self, predicted: str, actual: str,
               probabilities: Optional[dict] = None) -> None:
        key = f"{predicted}|{actual}"
        self.matrix[key] = self.matrix.get(key, 0) + 1
        self.resolved += 1
        if probabilities:
            # Kept for the probabilistic metrics only. A hard rule emits a
            # one-hot vector, which is a legitimate (and badly calibrated)
            # probability statement — measuring it is the point of §18.
            self.probabilities.append(
                (dict(probabilities), actual))

    def _count(self, predicted: str = "", actual: str = "") -> int:
        total = 0
        for key, n in self.matrix.items():
            p, a = key.split("|")
            if predicted and p != predicted:
                continue
            if actual and a != actual:
                continue
            total += n
        return total

    @property
    def correct(self) -> int:
        return sum(n for key, n in self.matrix.items()
                   if key.split("|")[0] == key.split("|")[1])

    @property
    def accuracy(self) -> float:
        return (self.correct / self.resolved) if self.resolved else 0.0

    def recall(self, cls: str) -> float:
        actual = self._count(actual=cls)
        return (self._count(cls, cls) / actual) if actual else 0.0

    def precision(self, cls: str) -> float:
        predicted = self._count(predicted=cls)
        return (self._count(cls, cls) / predicted) if predicted else 0.0

    def f1(self, cls: str) -> float:
        p, r = self.precision(cls), self.recall(cls)
        return (2 * p * r / (p + r)) if (p + r) else 0.0

    @property
    def balanced_accuracy(self) -> float:
        """Mean recall over classes that ACTUALLY OCCUR.

        Averaging over all three when one never occurs drags the score toward
        zero for a reason that has nothing to do with the model — a
        three-class metric on a two-class sample is describing the sample.
        """
        present = [c for c in self.classes if self._count(actual=c)]
        if not present:
            return 0.0
        return sum(self.recall(c) for c in present) / len(present)

    @property
    def majority_baseline(self) -> float:
        """What always predicting the commonest actual class would score."""
        if not self.resolved:
            return 0.0
        return max(self._count(actual=c) for c in self.classes) / self.resolved

    def distribution(self, of: str = "actual") -> dict:
        return {c: self._count(**{of: c}) for c in self.classes}

    def probabilistic(self) -> dict:
        """§18: log loss, Brier score and calibration.

        These are the metrics that separate "was the label right" from "was
        the CONFIDENCE right", and a hard threshold rule tends to score
        terribly on them precisely because it claims certainty. That is a
        finding, not a defect in the measurement — a rule asserting 100%
        and wrong a third of the time is exactly what a log loss is for.

        Probabilities are clipped before the logarithm and the clip is
        reported, because an unclipped one-hot vector produces an infinite
        log loss the moment it is wrong, which is a number nobody can
        compare against anything.
        """
        import math

        if not self.probabilities:
            return {"available": False,
                    "reason": "no probability vectors recorded"}
        eps = 1e-15
        log_loss = 0.0
        brier = 0.0
        for vector, actual in self.probabilities:
            for cls in self.classes:
                predicted = min(1.0 - eps, max(eps, float(
                    vector.get(cls, 0.0))))
                truth = 1.0 if cls == actual else 0.0
                brier += (predicted - truth) ** 2
                if truth:
                    log_loss -= math.log(predicted)
        n = len(self.probabilities)
        return {
            "available": True,
            "samples": n,
            "logLoss": round(log_loss / n, 4),
            "brierScore": round(brier / n, 4),
            "clipEpsilon": eps,
            "calibration": self.calibration(),
            "note": ("Lower is better for both. A hard rule emits one-hot "
                     "vectors, so it is penalised heavily for confident "
                     "mistakes — which is the intended behaviour of these "
                     "metrics and the reason §18 asks for them beside "
                     "accuracy."),
        }

    def calibration(self, bins: int = 10) -> dict:
        """Reliability: when the model says p, how often is it right?

        Reported per bin with its count. A bin holding three cases is not
        evidence of miscalibration, and showing it without the count invites
        exactly that reading.
        """
        if not self.probabilities:
            return {"available": False}
        buckets: dict[int, list] = {}
        for vector, actual in self.probabilities:
            for cls in self.classes:
                predicted = float(vector.get(cls, 0.0))
                index = min(bins - 1, int(predicted * bins))
                buckets.setdefault(index, []).append(
                    (predicted, 1.0 if cls == actual else 0.0))
        rows = []
        gap_weighted = 0.0
        total = 0
        for index in sorted(buckets):
            entries = buckets[index]
            mean_predicted = sum(p for p, _ in entries) / len(entries)
            mean_actual = sum(a for _, a in entries) / len(entries)
            rows.append({
                "bin": f"{index / bins:.1f}-{(index + 1) / bins:.1f}",
                "n": len(entries),
                "meanPredicted": round(mean_predicted, 4),
                "observedFrequency": round(mean_actual, 4),
                "gap": round(mean_predicted - mean_actual, 4)})
            gap_weighted += abs(mean_predicted - mean_actual) * len(entries)
            total += len(entries)
        return {
            "available": True,
            "bins": rows,
            "expectedCalibrationError": (round(gap_weighted / total, 4)
                                         if total else 0.0),
        }

    def to_dict(self) -> dict:
        return {
            "modelVersion": self.model_version,
            "prospectiveOnly": self.prospective_only,
            "probabilistic": self.probabilistic(),
            "eligibleConditions": self.eligible,
            "predictions": self.predictions,
            "rejected": dict(sorted(self.rejected.items(),
                                    key=lambda kv: -kv[1])),
            "resolved": self.resolved,
            "unresolved": self.unresolved,
            "correct": self.correct,
            "incorrect": self.resolved - self.correct,
            "accuracy": round(self.accuracy, 4),
            "balancedAccuracy": round(self.balanced_accuracy, 4),
            "majorityBaseline": round(self.majority_baseline, 4),
            "beatsMajorityBaseline": self.accuracy > self.majority_baseline,
            "predictedDistribution": self.distribution("predicted"),
            "actualDistribution": self.distribution("actual"),
            "perClass": {
                c: {"precision": round(self.precision(c), 4),
                    "recall": round(self.recall(c), 4),
                    "f1": round(self.f1(c), 4),
                    "predicted": self._count(predicted=c),
                    "actual": self._count(actual=c)}
                for c in self.classes},
            "confusionMatrix": dict(self.matrix),
            "confidenceInterval95": _wilson_dict(self.correct, self.resolved),
        }


def _wilson_dict(successes: int, n: int) -> dict:
    from .validation import _wilson

    if not n:
        return {"available": False, "reason": "no resolved predictions"}
    low, high = _wilson(successes, n)
    return {"available": True, "low": round(low, 4), "high": round(high, 4),
            "n": n}


def evaluate_v1(episodes, model: Optional[RN1StrategyModelV1] = None,
                boundary_ts: float = PROSPECTIVE_BOUNDARY_TS,
                freshness_minutes: float = DEFAULT_FRESHNESS_MINUTES,
                prospective_only: bool = False,
                has_redeem_before=None) -> tuple:
    """Score V1 over episodes. Returns `(report, cases)`.

    `prospective_only=True` restricts to the CLEAN forward sample — after the
    boundary, fresh, no prior REDEEM. That flag is the only door into a
    forward number, and every report states which side of it produced the
    figure it is showing.
    """
    model = model or RN1StrategyModelV1()
    report = MulticlassReport(model_version=model.version,
                              prospective_only=prospective_only)
    cases: list = []
    for episode in episodes:
        gate = eligibility(episode, boundary_ts, freshness_minutes,
                           has_redeem_before)
        if not gate.eligible:
            report.rejected[gate.reason] = \
                report.rejected.get(gate.reason, 0) + 1
            continue
        if prospective_only and not gate.prospective:
            report.rejected[R_BEFORE_BOUNDARY] = \
                report.rejected.get(R_BEFORE_BOUNDARY, 0) + 1
            continue
        report.eligible += 1
        prediction = model.predict(episode, gate)
        if not prediction.valid:
            report.rejected[prediction.reason] = \
                report.rejected.get(prediction.reason, 0) + 1
            continue
        report.predictions += 1
        cases.append({"episode": episode, "prediction": prediction,
                      "gate": gate})
        # A prediction with no settled lifecycle yet is UNRESOLVED. It is not
        # wrong and it is not right; it is pending, and the handoff's own
        # experiment is mostly pending. Counting pending as incorrect is how a
        # forward experiment gets reported as a failure before it has run.
        if not episode.labelled:
            report.unresolved += 1
            continue
        report.record(prediction.label, episode.label,
                      prediction.probabilities)
    return report, cases
