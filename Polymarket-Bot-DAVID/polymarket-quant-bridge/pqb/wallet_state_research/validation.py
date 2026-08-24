"""Chronological validation, and the cross-sections that test generalisation.

Part 9 forbids random splitting and the reason is specific rather than
stylistic: these episodes are not independent draws. One wallet's episodes are
correlated with each other, episodes in one market share an outcome, and a
random split puts an episode's own neighbours on both sides of the line — so
the model is tested on information it was trained on, and the score is a
measurement of that leak.

So everything here splits on TIME, at the signal instant.

    [ development ][ validation ][ HOLDOUT ]
      tune here      choose here   touch once

The holdout is enforced, not promised: `Split.holdout` is only reachable
through `evaluate_holdout`, which refuses to run until `freeze()` has been
called, and `freeze()` records what was frozen. That makes "the holdout did not
influence the model" a property of the call graph rather than of anyone's
discipline.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .classifier import ClassificationReport, evaluate
from .episodes import AGGRESSIVE, PROTECT, Episode


def signal_time(episode: Episode) -> float:
    return episode.first_opposite_ts or episode.first_buy_ts


@dataclass
class Split:
    """A chronological three-way split, with the holdout under guard."""

    development: list = field(default_factory=list)
    validation: list = field(default_factory=list)
    _holdout: list = field(default_factory=list)
    dev_end_ts: float = 0.0
    val_end_ts: float = 0.0
    frozen: bool = False
    frozen_description: str = ""

    def freeze(self, description: str) -> None:
        """Declare the strategy final. Required before the holdout opens.

        `description` is stored and printed in the report, so a holdout number
        always arrives with a statement of exactly what was frozen before it
        was produced.
        """
        self.frozen = True
        self.frozen_description = description

    @property
    def holdout_size(self) -> int:
        """Countable without opening it — a census is not a peek."""
        return len(self._holdout)

    def holdout(self) -> list:
        if not self.frozen:
            raise RuntimeError(
                "The untouched holdout was requested before freeze() was "
                "called. Part 9: thresholds, features, model choice, wallet "
                "and market selection must all be fixed first. Call "
                "split.freeze('what was frozen') and say what you froze.")
        return list(self._holdout)

    def to_dict(self) -> dict:
        return {"development": len(self.development),
                "validation": len(self.validation),
                "holdout": self.holdout_size,
                "devEndTs": self.dev_end_ts, "valEndTs": self.val_end_ts,
                "frozen": self.frozen,
                "frozenDescription": self.frozen_description}


def chronological_split(episodes: Iterable[Episode],
                        dev_fraction: float = 0.5,
                        val_fraction: float = 0.25) -> Split:
    """Split by signal time. Boundaries fall on time, not on index count.

    Index-based splitting would put two episodes from the same instant on
    opposite sides of a boundary, which is a small leak that grows with how
    bursty the tape is — and this tape is bursty.
    """
    ordered = sorted(episodes, key=signal_time)
    if not ordered:
        return Split()
    times = [signal_time(e) for e in ordered]
    dev_end = times[min(len(times) - 1, int(len(times) * dev_fraction))]
    val_end = times[min(len(times) - 1,
                        int(len(times) * (dev_fraction + val_fraction)))]
    split = Split(dev_end_ts=dev_end, val_end_ts=val_end)
    for episode in ordered:
        ts = signal_time(episode)
        if ts <= dev_end:
            split.development.append(episode)
        elif ts <= val_end:
            split.validation.append(episode)
        else:
            split._holdout.append(episode)
    return split


@dataclass
class WalkForwardFold:
    fold: int = 0
    train_n: int = 0
    test_n: int = 0
    train_end_ts: float = 0.0
    test_end_ts: float = 0.0
    report: Optional[ClassificationReport] = None

    def to_dict(self) -> dict:
        return {"fold": self.fold, "trainEpisodes": self.train_n,
                "testEpisodes": self.test_n,
                "trainEndTs": self.train_end_ts, "testEndTs": self.test_end_ts,
                "classification": (self.report.to_dict() if self.report
                                   else {})}


def walk_forward(episodes: Iterable[Episode], model_factory: Callable,
                 horizon_minutes: float, folds: int = 4,
                 features_of=None, min_train: int = 50) -> dict:
    """Expanding-window walk-forward (Part 9's preferred shape).

    `model_factory(train_episodes)` returns the model to test on the NEXT
    window. For the frozen rule it ignores its argument entirely, which is the
    point: the frozen benchmark is identical in every fold, so any variation
    across folds is variation in the DATA rather than in the fit — and that is
    the single most useful thing a walk-forward can tell you about a rule
    someone else tuned on data you do not have.
    """
    ordered = sorted((e for e in episodes if e.switched), key=signal_time)
    out: dict = {"folds": [], "horizonMinutes": horizon_minutes,
                 "episodes": len(ordered)}
    if len(ordered) < min_train + folds:
        out["available"] = False
        out["reason"] = (f"{len(ordered)} switched episodes is below the "
                         f"{min_train + folds} needed for {folds} folds")
        return out

    boundaries = []
    step = (len(ordered) - min_train) / float(folds)
    for i in range(folds):
        boundaries.append(min_train + int(step * i))
    boundaries.append(len(ordered))

    accuracies, balanced = [], []
    for index in range(folds):
        train = ordered[:boundaries[index]]
        test = ordered[boundaries[index]:boundaries[index + 1]]
        if not test:
            continue
        model = model_factory(train)
        if model is None:
            continue
        report, _cases = evaluate(model, test, horizon_minutes, features_of)
        fold = WalkForwardFold(
            fold=index + 1, train_n=len(train), test_n=len(test),
            train_end_ts=signal_time(train[-1]) if train else 0.0,
            test_end_ts=signal_time(test[-1]), report=report)
        out["folds"].append(fold.to_dict())
        if report.graded:
            accuracies.append(report.accuracy)
            balanced.append(report.balanced_accuracy)

    out["available"] = bool(accuracies)
    if accuracies:
        out["meanAccuracy"] = round(sum(accuracies) / len(accuracies), 4)
        out["meanBalancedAccuracy"] = round(sum(balanced) / len(balanced), 4)
        out["worstFoldAccuracy"] = round(min(accuracies), 4)
        out["bestFoldAccuracy"] = round(max(accuracies), 4)
        out["accuracyStdev"] = round(
            (sum((a - out["meanAccuracy"]) ** 2 for a in accuracies)
             / len(accuracies)) ** 0.5, 4)
        out["stability"] = (
            "stable across folds" if out["accuracyStdev"] < 0.05 else
            "UNSTABLE across folds — the result depends on which weeks were "
            "tested, which is what an overfitted or regime-bound rule looks "
            "like")
    return out


# ---------------------------------------------------------------------------
# Part 10 / 11 — cross-wallet and cross-market
# ---------------------------------------------------------------------------

# Evidence tiers. Deliberately conservative: with a 90-day tape most wallets
# will land in INSUFFICIENT, and saying so is the honest result.
TIER_MEANINGFUL = "statistically meaningful"
TIER_PROMISING = "promising but inconclusive"
TIER_NEGATIVE = "negative evidence"
TIER_INSUFFICIENT = "insufficient sample"


def _wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson interval — correct at small n, where normal approximation is not.

    At n=12 the normal interval can extend past 1.0, which is how a wallet with
    11 of 12 correct ends up reported as '92% (CI up to 104%)'.
    """
    if n <= 0:
        return 0.0, 0.0
    p = successes / n
    denominator = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denominator), \
        min(1.0, (centre + margin) / denominator)


def _tier(report: ClassificationReport, min_samples: int) -> tuple[str, str]:
    """Rank by RELIABILITY, not by raw score (Part 10 is explicit)."""
    if report.graded < min_samples:
        return TIER_INSUFFICIENT, (
            f"{report.graded} graded case(s), below the {min_samples} floor")
    lo, hi = _wilson(report.tp + report.tn, report.graded)
    base = max(report.base_rate, 1.0 - report.base_rate)
    if lo > base:
        return TIER_MEANINGFUL, (
            f"accuracy {report.accuracy:.1%} with a 95% lower bound of "
            f"{lo:.1%}, above the {base:.1%} majority-class baseline")
    if hi < base:
        return TIER_NEGATIVE, (
            f"accuracy {report.accuracy:.1%}; the whole 95% interval sits "
            f"BELOW the {base:.1%} majority-class baseline — the rule is "
            "worse than guessing the common class here")
    return TIER_PROMISING, (
        f"accuracy {report.accuracy:.1%} (95% CI {lo:.1%}-{hi:.1%}) straddles "
        f"the {base:.1%} majority-class baseline — not distinguishable yet")


@dataclass
class CohortResult:
    """One wallet's, or one market cohort's, evidence."""

    key: str = ""
    episodes: int = 0
    switched: int = 0
    report: Optional[ClassificationReport] = None
    trading: Optional[dict] = None
    tier: str = ""
    tier_reason: str = ""

    def to_dict(self) -> dict:
        out = {"key": self.key, "episodes": self.episodes,
               "switched": self.switched, "tier": self.tier,
               "tierReason": self.tier_reason}
        if self.report is not None:
            report = self.report.to_dict()
            out.update({
                "graded": report["graded"],
                "accuracy": report["accuracy"],
                "balancedAccuracy": report["balancedAccuracy"],
                "aggressivePrecision": report["aggressivePrecision"],
                "aggressiveRecall": report["aggressiveRecall"],
                "protectPrecision": report["protectPrecision"],
                "protectRecall": report["protectRecall"],
                "aggressiveSignals": report["confusionMatrix"][
                    "predAggressive_actualAggressive"]
                + report["confusionMatrix"]["predAggressive_actualProtect"],
                "baseRateAggressive": report["baseRateAggressive"],
            })
        if self.trading is not None:
            settled = (self.trading.get("settled") or {})
            out.update({
                "tradesSettled": settled.get("trades", 0),
                "roi": settled.get("roi"),
                "netPnl": settled.get("netPnl"),
                "maxDrawdown": settled.get("maxDrawdown"),
            })
        return out


def cross_wallet(episodes: Iterable[Episode], model: Any,
                 horizon_minutes: float, min_samples: int = 12,
                 features_of=None, trade_fn=None) -> dict:
    """Part 10: run the FROZEN rule, unoptimised, against every wallet.

    Unoptimised is the whole experiment. Tuning per wallet first and then
    reporting that it "generalises" would only demonstrate that a two-parameter
    rule can be fitted to anything.
    """
    by_wallet: dict[str, list] = {}
    for episode in episodes:
        by_wallet.setdefault(episode.wallet, []).append(episode)

    results: list[CohortResult] = []
    for wallet, rows in by_wallet.items():
        switched = [e for e in rows if e.switched]
        if not switched:
            continue
        report, cases = evaluate(model, switched, horizon_minutes, features_of)
        result = CohortResult(key=wallet, episodes=len(rows),
                              switched=len(switched), report=report)
        result.tier, result.tier_reason = _tier(report, min_samples)
        if trade_fn is not None and report.graded >= min_samples:
            result.trading = trade_fn(cases)
        results.append(result)

    graded = [r for r in results if r.report and r.report.graded >= min_samples]
    accuracies = sorted(r.report.accuracy for r in graded)
    meaningful = [r for r in graded if r.tier == TIER_MEANINGFUL]
    negative = [r for r in graded if r.tier == TIER_NEGATIVE]

    # Ranked by the LOWER BOUND, not by the point estimate. A wallet with 5
    # of 5 correct has a better score and worse evidence than one with 40 of
    # 55, and ranking on the score puts the anecdote on top.
    ranked = sorted(graded, key=lambda r: -_wilson(
        r.report.tp + r.report.tn, r.report.graded)[0])
    return {
        "walletsSeen": len(by_wallet),
        "walletsWithSwitches": len(results),
        "walletsWithEnoughData": len(graded),
        "minSamples": min_samples,
        "statisticallyMeaningful": len(meaningful),
        "negativeEvidence": len(negative),
        "promising": len(graded) - len(meaningful) - len(negative),
        "medianAccuracy": (round(accuracies[len(accuracies) // 2], 4)
                           if accuracies else None),
        "meanAccuracy": (round(sum(accuracies) / len(accuracies), 4)
                         if accuracies else None),
        "best": ranked[0].to_dict() if ranked else None,
        "worst": ranked[-1].to_dict() if ranked else None,
        "wallets": [r.to_dict() for r in ranked[:200]],
        "reading": _cross_wallet_reading(len(graded), len(meaningful),
                                         len(negative)),
    }


def _cross_wallet_reading(graded: int, meaningful: int, negative: int) -> str:
    if not graded:
        return ("no wallet reached the minimum sample size — this tape cannot "
                "answer the cross-wallet question yet")
    share = meaningful / graded
    if share >= 0.5:
        return (f"{meaningful} of {graded} wallets show evidence above their "
                "own majority-class baseline: the behaviour looks like a "
                "GENERAL pattern rather than an RN1 quirk")
    if meaningful == 0:
        return (f"none of {graded} wallets beat their own majority-class "
                "baseline with statistical support: on this data the "
                "relationship does NOT generalise across wallets")
    return (f"{meaningful} of {graded} wallets show support and {negative} "
            "show negative evidence: the effect is wallet-dependent, not "
            "universal — which makes wallet identity itself a candidate "
            "feature rather than a nuisance")


def cross_market(episodes: Iterable[Episode], model: Any,
                 horizon_minutes: float, category_of,
                 min_samples: int = 12, features_of=None,
                 trade_fn=None) -> dict:
    """Part 11: by category, by liquidity proxy, by market age.

    Not aggregated into one number, on purpose. 'Works in crypto, fails in
    sports' and 'mediocre everywhere' produce the same average and mean
    opposite things.
    """
    cohorts: dict[str, dict[str, list]] = {
        "category": {}, "episode_size": {}, "switch_speed": {}}
    for episode in episodes:
        if not episode.switched:
            continue
        cohorts["category"].setdefault(
            category_of(episode.question), []).append(episode)
        cohorts["episode_size"].setdefault(
            _size_bucket(episode), []).append(episode)
        cohorts["switch_speed"].setdefault(
            _speed_bucket(episode), []).append(episode)

    out: dict = {"dimensions": {}, "minSamples": min_samples,
                 "note": ("Category is a KEYWORD HEURISTIC over the market "
                          "question: the tape carries no category field. "
                          "Liquidity is proxied by the wallet's own deployed "
                          "notional, since order-book depth is unavailable for "
                          "most tokens. Both are labelled approximations.")}
    for dimension, buckets in cohorts.items():
        rows = []
        for name, group in sorted(buckets.items()):
            report, cases = evaluate(model, group, horizon_minutes,
                                     features_of)
            result = CohortResult(key=name, episodes=len(group),
                                  switched=len(group), report=report)
            result.tier, result.tier_reason = _tier(report, min_samples)
            if trade_fn is not None and report.graded >= min_samples:
                result.trading = trade_fn(cases)
            rows.append(result.to_dict())
        rows.sort(key=lambda r: -(r.get("accuracy") or 0.0))
        usable = [r for r in rows if r["graded"] >= min_samples]
        out["dimensions"][dimension] = {
            "buckets": rows,
            "best": usable[0]["key"] if usable else None,
            "worst": usable[-1]["key"] if usable else None,
            "spread": (round(usable[0]["accuracy"] - usable[-1]["accuracy"], 4)
                       if len(usable) > 1 else None),
        }
    return out


def _size_bucket(episode: Episode) -> str:
    """Deployed notional — the best liquidity proxy this data supports."""
    notional = sum(abs(e.usdc) for e in episode.events)
    if notional < 10:
        return "tiny (<$10)"
    if notional < 100:
        return "small ($10-100)"
    if notional < 1_000:
        return "medium ($100-1k)"
    return "large (>$1k)"


def _speed_bucket(episode: Episode) -> str:
    seconds = episode.seconds_to_switch
    if seconds <= 60:
        return "switch within 1m"
    if seconds <= 900:
        return "switch within 15m"
    if seconds <= 86_400:
        return "switch within a day"
    return "switch after a day"
