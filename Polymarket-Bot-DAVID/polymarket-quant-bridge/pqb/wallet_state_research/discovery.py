"""Model discovery — interpretable first, and fitted only on development data.

Part 13 asks whether the behaviour can be represented better than two frozen
thresholds, and Part 12 asks whether the answer is universal, per-wallet, or
hybrid. Both are answered here, under two rules that are enforced rather than
intended:

* **Fitting only ever sees the training window.** Every fitter takes the
  training episodes and nothing else, and the version string it stamps records
  the window and the sample size, so a result can never be quoted without the
  provenance of the numbers behind it.
* **Interpretable models come first.** A tuned two-threshold rule of exactly
  the frozen rule's shape is fitted before anything else, because it answers
  the narrow question ("were the thresholds wrong?") that a sample this size
  can actually support. Gradient boosting on a few hundred cases answers a
  question the data has not earned.

scikit-learn is optional. Without it the threshold search and the coverage
comparison still run; the probability models report UNAVAILABLE with the
reason. Nothing in the frozen benchmark or the P&L path depends on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

from .classifier import (AGGRESSIVE, PROTECT, ProbabilityModel, ThresholdRule,
                         evaluate)
from .episodes import Episode

# Feature columns offered to the fitted models. Deliberately a short, stable,
# hand-picked list rather than "everything `features.build` produced": with a
# few hundred training cases, forty columns is a guarantee of fitting noise,
# and a column that is UNAVAILABLE for most rows contributes nothing but a
# hole. Wallet-history columns are included because Part 12 asks whether
# wallet identity is predictive — and excluded automatically for any row that
# has no prior history rather than being zero-filled.
MODEL_FEATURES: tuple[str, ...] = (
    "inventory_ratio",
    "shares_needed_opposite_to_zero",
    "opposite_shares",
    "original_shares",
    "weaker_payoff",
    "payoff_spread",
    "opposite_buy_count",
    "original_buy_count",
    "seconds_original_to_opposite",
    "capital_deployed",
    "opposite_accumulation_rate",
    "last_opposite_price",
)

WALLET_FEATURES: tuple[str, ...] = (
    "wallet_prior_episodes",
    "wallet_switch_rate",
    "wallet_aggressive_rate",
)


def sklearn_available() -> bool:
    try:
        import sklearn  # noqa: F401
        return True
    except ImportError:
        return False


def _rows(episodes: Iterable[Episode], horizon: float, features_of,
          columns: tuple) -> tuple[list, list, list]:
    """`(X, y, episodes)` over the graded, snapshot-valid population."""
    x, y, kept = [], [], []
    for episode in episodes:
        if not episode.switched or not episode.two_class \
                or not episode.labelled:
            continue
        snapshot = episode.snapshot(horizon)
        if not snapshot.valid:
            continue
        vector = features_of(episode, snapshot)
        row, complete = [], True
        for name in columns:
            value = vector.get(name) if isinstance(vector, dict) \
                else vector.values.get(name)
            if value is None:
                complete = False
                break
            row.append(float(value))
        if not complete:
            continue
        x.append(row)
        y.append(1 if episode.label == AGGRESSIVE else 0)
        kept.append(episode)
    return x, y, kept


# ---------------------------------------------------------------------------
# The interpretable model: a tuned rule of the frozen rule's exact shape
# ---------------------------------------------------------------------------


def fit_threshold_rule(train: list, horizon: float, features_of,
                       objective: str = "balanced_accuracy",
                       grid: int = 40) -> Optional[ThresholdRule]:
    """Search the two thresholds on TRAINING data only.

    A coarse grid over the observed quantiles rather than a fine sweep over an
    arbitrary range. Part 14's warning is about searching thousands of
    thresholds until something works; 40x40 over the data's own quantiles is a
    search whose scale is reportable, and the scale is reported.

    Optimises BALANCED accuracy by default. Plain accuracy on an unbalanced
    population is maximised by predicting the majority class everywhere, which
    is a rule with no content and a good-looking score.
    """
    x, y, _kept = _rows(train, horizon, features_of,
                        ("inventory_ratio", "shares_needed_opposite_to_zero"))
    if len(x) < 40:
        return None
    ratios = sorted(row[0] for row in x)
    needs = sorted(row[1] for row in x)
    ratio_grid = _quantile_grid(ratios, grid)
    need_grid = _quantile_grid(needs, grid)

    best_score, best = -1.0, None
    for ratio_threshold in ratio_grid:
        for need_threshold in need_grid:
            tp = fp = tn = fn = 0
            for row, label in zip(x, y):
                predicted = (row[0] >= ratio_threshold
                             or row[1] <= need_threshold)
                if predicted and label:
                    tp += 1
                elif predicted:
                    fp += 1
                elif label:
                    fn += 1
                else:
                    tn += 1
            score = _score(tp, fp, tn, fn, objective)
            if score > best_score:
                best_score, best = score, (ratio_threshold, need_threshold)
    if best is None:
        return None
    return ThresholdRule(
        ratio_threshold=best[0], needed_threshold=best[1],
        version=f"THRESHOLD_TUNED_V1(n={len(x)})",
        name="Tuned two-threshold rule (same shape as frozen RN1)",
        fitted_on=f"{len(x)} development cases", fitted_n=len(x))


def _quantile_grid(values: list, steps: int) -> list:
    if not values:
        return []
    out, n = [], len(values)
    for i in range(steps):
        out.append(values[min(n - 1, int(n * i / steps))])
    return sorted(set(out))


def _score(tp: int, fp: int, tn: int, fn: int, objective: str) -> float:
    total = tp + fp + tn + fn
    if not total:
        return -1.0
    if objective == "accuracy":
        return (tp + tn) / total
    aggressive_recall = tp / (tp + fn) if (tp + fn) else 0.0
    protect_recall = tn / (tn + fp) if (tn + fp) else 0.0
    return (aggressive_recall + protect_recall) / 2.0


# ---------------------------------------------------------------------------
# Models A / B / C
# ---------------------------------------------------------------------------


def fit_probability_model(train: list, horizon: float, features_of,
                          columns: tuple = MODEL_FEATURES,
                          kind: str = "logistic",
                          version: str = "UNIVERSAL_WALLET_STATE_V1"
                          ) -> Optional[ProbabilityModel]:
    """Fit one interpretable probability model. Training window only."""
    if not sklearn_available():
        return None
    x, y, _kept = _rows(train, horizon, features_of, columns)
    if len(x) < 60 or len(set(y)) < 2:
        return None
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeClassifier

    if kind == "logistic":
        estimator = make_pipeline(
            StandardScaler(),
            LogisticRegression(max_iter=1000, class_weight="balanced"))
    elif kind == "tree":
        # Shallow on purpose. A depth-3 tree over a few hundred rows is a
        # readable hypothesis; an unbounded one is a lookup table.
        estimator = DecisionTreeClassifier(
            max_depth=3, min_samples_leaf=max(10, len(x) // 20),
            class_weight="balanced", random_state=20260823)
    elif kind == "forest":
        estimator = RandomForestClassifier(
            n_estimators=200, max_depth=4,
            min_samples_leaf=max(10, len(x) // 20),
            class_weight="balanced", random_state=20260823)
    else:
        return None
    estimator.fit(x, y)
    return ProbabilityModel(
        estimator=estimator, feature_names=list(columns),
        version=f"{version}({kind},n={len(x)})",
        name=f"{kind} over {len(columns)} features",
        fitted_on=f"{len(x)} development cases", fitted_n=len(x))


def fit_wallet_specific(train: list, horizon: float, features_of,
                        min_samples: int = 40) -> dict:
    """MODEL B: one tuned rule per wallet with enough training data.

    Returns `wallet -> ThresholdRule`. Wallets below the floor get no model
    and fall back to the universal one at prediction time — which is the
    honest behaviour and also the thing that makes the A/B/C comparison
    meaningful rather than a comparison of coverage.
    """
    by_wallet: dict[str, list] = {}
    for episode in train:
        by_wallet.setdefault(episode.wallet, []).append(episode)
    out: dict[str, ThresholdRule] = {}
    for wallet, rows in by_wallet.items():
        if len(rows) < min_samples:
            continue
        rule = fit_threshold_rule(rows, horizon, features_of)
        if rule is None:
            continue
        rule.version = f"WALLET_SPECIFIC_V1({wallet[:10]},n={rule.fitted_n})"
        rule.name = f"Tuned rule for {wallet[:12]}..."
        out[wallet] = rule
    return out


@dataclass
class HybridModel:
    """MODEL C: wallet-specific where it exists, universal elsewhere.

    Records which arm answered each case, so 'the hybrid did better' can be
    checked against 'the hybrid only ever used the universal arm'.
    """

    universal: Any
    per_wallet: dict
    version: str = "HYBRID_WALLET_STATE_V1"
    name: str = "Hybrid: wallet-specific where available, universal otherwise"
    used_wallet_arm: int = 0
    used_universal_arm: int = 0
    _wallet: str = ""

    def for_episode(self, episode: Episode) -> "HybridModel":
        self._wallet = episode.wallet
        return self

    def predict(self, snapshot, features=None):
        model = self.per_wallet.get(self._wallet)
        if model is not None:
            self.used_wallet_arm += 1
        else:
            self.used_universal_arm += 1
            model = self.universal
        prediction = model.predict(snapshot, features)
        prediction.model_version = self.version
        return prediction


class _EpisodeAwareWrapper:
    """Adapter so a per-episode model fits `evaluate`'s per-snapshot contract.

    `evaluate` deliberately does not hand the model an episode — a model that
    can see the episode can see its label. This wrapper is the one place that
    exception is made, it passes ONLY the wallet address, and it is used by
    Models B and C alone.
    """

    def __init__(self, hybrid: HybridModel):
        self.hybrid = hybrid
        self.version = hybrid.version
        self.name = hybrid.name
        self._episodes = {}

    def bind(self, episodes: Iterable[Episode]) -> None:
        for episode in episodes:
            snapshot_key = round(episode.first_opposite_ts, 3)
            self._episodes[(episode.market_id, snapshot_key)] = episode

    def predict(self, snapshot, features=None):
        # The snapshot knows its own horizon; recover the episode by the
        # opposite-buy instant it was built from.
        key = round(snapshot.ts - snapshot.horizon_minutes * 60.0, 3)
        for (market, opposite_ts), episode in self._episodes.items():
            if abs(opposite_ts - key) < 1.0:
                self.hybrid.for_episode(episode)
                break
        return self.hybrid.predict(snapshot, features)


def compare_model_families(development: list, validation: list,
                           horizon: float, features_of,
                           min_wallet_samples: int = 40) -> dict:
    """Part 12, end to end: fit on development, score on validation.

    Validation, not holdout. Choosing between model families IS a decision, so
    it must not touch the untouched set — that is the whole reason there are
    three windows rather than two.
    """
    out: dict = {"developmentEpisodes": len(development),
                 "validationEpisodes": len(validation),
                 "sklearnAvailable": sklearn_available(),
                 "models": {}}

    tuned = fit_threshold_rule(development, horizon, features_of)
    if tuned is not None:
        report, _ = evaluate(tuned, validation, horizon, features_of)
        out["models"]["tuned_threshold"] = {
            "version": tuned.version,
            "ratioThreshold": round(tuned.ratio_threshold, 6),
            "neededThreshold": round(tuned.needed_threshold, 6),
            "searchScale": "40 x 40 quantile grid on development data only",
            "validation": report.to_dict()}
    else:
        out["models"]["tuned_threshold"] = {
            "available": False,
            "reason": "fewer than 40 complete development cases"}

    if not sklearn_available():
        out["models"]["universal"] = {
            "available": False,
            "reason": "scikit-learn is not installed; it is an optional "
                      "dependency and only the discovery models need it"}
        return out

    for kind in ("logistic", "tree", "forest"):
        model = fit_probability_model(development, horizon, features_of,
                                      kind=kind)
        if model is None:
            out["models"][f"universal_{kind}"] = {
                "available": False,
                "reason": "fewer than 60 complete development cases, or a "
                          "single class in training"}
            continue
        report, _ = evaluate(model, validation, horizon, features_of)
        entry = {"version": model.version, "validation": report.to_dict()}
        entry["featureImportance"] = _importance(model)
        out["models"][f"universal_{kind}"] = entry

    with_wallet = fit_probability_model(
        development, horizon, features_of,
        columns=MODEL_FEATURES + WALLET_FEATURES, kind="logistic",
        version="UNIVERSAL_PLUS_WALLET_V1")
    if with_wallet is not None:
        report, _ = evaluate(with_wallet, validation, horizon, features_of)
        out["models"]["universal_with_wallet_history"] = {
            "version": with_wallet.version,
            "validation": report.to_dict(),
            "featureImportance": _importance(with_wallet),
            "note": ("Answers Part 12's question directly: does adding the "
                     "wallet's own prior behaviour help? Wallet history is "
                     "built from PRIOR FINISHED episodes only, and rows with "
                     "no prior history are dropped rather than zero-filled — "
                     "so this model is scored on a smaller population and the "
                     "counts must be compared, not just the accuracies.")}

    per_wallet = fit_wallet_specific(development, horizon, features_of,
                                     min_wallet_samples)
    out["models"]["wallet_specific"] = {
        "walletsFitted": len(per_wallet),
        "minSamples": min_wallet_samples,
        "wallets": [{"wallet": w, "ratio": round(r.ratio_threshold, 6),
                     "needed": round(r.needed_threshold, 6), "n": r.fitted_n}
                    for w, r in list(per_wallet.items())[:40]],
    }
    if per_wallet and tuned is not None:
        hybrid = HybridModel(universal=tuned, per_wallet=per_wallet)
        wrapper = _EpisodeAwareWrapper(hybrid)
        wrapper.bind(validation)
        report, _ = evaluate(wrapper, validation, horizon, features_of)
        out["models"]["hybrid"] = {
            "version": hybrid.version,
            "validation": report.to_dict(),
            "usedWalletArm": hybrid.used_wallet_arm,
            "usedUniversalArm": hybrid.used_universal_arm,
            "note": ("If usedWalletArm is near zero the hybrid IS the "
                     "universal model and any difference is noise.")}
    return out


def _importance(model: ProbabilityModel) -> dict:
    """Coefficients or impurity importances, whichever the estimator has."""
    estimator = model.estimator
    try:
        final = estimator[-1] if hasattr(estimator, "__getitem__") \
            else estimator
    except Exception:                                    # noqa: BLE001
        final = estimator
    values = None
    if hasattr(final, "coef_"):
        values = [float(v) for v in final.coef_[0]]
        kind = "logistic coefficient (standardised)"
    elif hasattr(final, "feature_importances_"):
        values = [float(v) for v in final.feature_importances_]
        kind = "impurity importance"
    if values is None:
        return {"available": False}
    pairs = sorted(zip(model.feature_names, values),
                   key=lambda kv: -abs(kv[1]))
    return {"available": True, "kind": kind,
            "ranked": [{"feature": name, "value": round(value, 5)}
                       for name, value in pairs]}
