"""STRATEGY_MODEL_V2_DISCOVERY — the three-class families, beside V1.

`discovery.py` compares model families for the two-class post-opposite-buy
question. This file does the same for the harder one V1 actually asks: at the
FIRST BUY, with nothing two-sided yet observed, which of three
position-management modes will this condition become?

Everything here is `STRATEGY_MODEL_V2_DISCOVERY`. V1 is never modified, never
refitted and never replaced — §24 is explicit, and the registry refuses the
collision anyway. V2 exists to give V1 something honest to be measured
against.

Three families, as §17 asks:

    MODEL A  UNIVERSAL          one model across every wallet
    MODEL B  WALLET-SPECIFIC    one model per wallet with enough data
    MODEL C  HYBRID             wallet-specific where it exists, else universal

and one interpretable baseline of V1's own shape — a retuned price/capital
rule — because the narrow question ("were the three thresholds wrong?") is the
one a sample this size can actually answer, and it should be answered before
anyone reaches for a forest.

The features are strictly entry-time. Nothing here may read an opposite-side
buy, an inventory ratio or a payoff state: those exist only after the moment
V1 predicts, and using them would be answering a different question with a
better view.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from .episodes import AGGRESSIVE, DIRECTIONAL, PROTECT, Episode
from .features import category_of
from .strategy_v1 import (MulticlassReport, RN1StrategyModelV1,
                          V1_AGGRESSIVE_CAPITAL_MIN, V1_AGGRESSIVE_PRICE_MAX,
                          V1_DIRECTIONAL_PRICE_MIN, eligibility,
                          initial_capital, initial_price)

CLASSES = (DIRECTIONAL, PROTECT, AGGRESSIVE)

# Entry-time features only. Short and hand-picked: with three classes and a
# heavily unbalanced population, forty columns is a guarantee of fitting the
# majority class and calling it a model.
V1_FEATURES: tuple[str, ...] = (
    "initial_price",
    "initial_capital",
    "log_capital",
    "price_distance_from_half",
    "first_buy_hour_utc",
    "wallet_prior_conditions",
    "wallet_prior_two_sided_rate",
)


def entry_features(episode: Episode, history: Optional[dict] = None) -> dict:
    """Everything knowable at the first BUY, and nothing else.

    `history` is the wallet's record over strictly EARLIER finished
    conditions. Absent means the wallet has no prior record at this instant,
    and the two history columns are then omitted rather than zero-filled —
    zero-filling would tell the model "this wallet never goes two-sided",
    which is a claim, not a missing value.
    """
    import math

    price = initial_price(episode)
    capital = initial_capital(episode)
    if price <= 0 or capital <= 0:
        return {}
    out = {
        "initial_price": price,
        "initial_capital": capital,
        "log_capital": math.log10(max(1e-6, capital)),
        "price_distance_from_half": abs(price - 0.5),
        "first_buy_hour_utc": float(
            int(episode.first_buy_ts // 3600) % 24),
    }
    if history and history.get("conditions"):
        out["wallet_prior_conditions"] = float(history["conditions"])
        out["wallet_prior_two_sided_rate"] = float(
            history["two_sided"] / history["conditions"])
    return out


def prior_history_index(episodes: Iterable[Episode]) -> dict:
    """`episode_key -> the wallet's record over strictly EARLIER conditions`.

    Chronological single pass, folding a condition in only once it has
    finished and only after every later condition has been served the view
    that excludes it. Same construction as `features.history_index`, kept
    separate because the entry-time question needs a different summary — how
    often this wallet has gone two-sided at all, which is the base rate V1's
    three modes sit on top of.
    """
    ordered = sorted(episodes, key=lambda e: e.first_buy_ts)
    out: dict = {}
    running: dict[str, dict] = {}
    pending: dict[str, list] = {}
    for episode in ordered:
        wallet = episode.wallet
        record = running.setdefault(wallet, {"conditions": 0, "two_sided": 0})
        still = []
        for earlier in pending.get(wallet, []):
            if earlier.last_activity_ts < episode.first_buy_ts:
                record["conditions"] += 1
                record["two_sided"] += 1 if earlier.switched else 0
            else:
                still.append(earlier)
        pending[wallet] = still
        out[(wallet, episode.market_id, episode.first_buy_ts)] = dict(record)
        pending.setdefault(wallet, []).append(episode)
    return out


def _rows(episodes: Iterable[Episode], histories: dict,
          columns: tuple) -> tuple:
    x, y, kept = [], [], []
    for episode in episodes:
        if not episode.labelled:
            continue
        gate = eligibility(episode)
        if not gate.eligible:
            continue
        features = entry_features(
            episode, histories.get((episode.wallet, episode.market_id,
                                    episode.first_buy_ts)))
        if not features:
            continue
        row, complete = [], True
        for name in columns:
            value = features.get(name)
            if value is None:
                complete = False
                break
            row.append(float(value))
        if not complete:
            continue
        x.append(row)
        y.append(CLASSES.index(episode.label))
        kept.append(episode)
    return x, y, kept


# ---------------------------------------------------------------------------
# The interpretable baseline: V1's own shape, retuned
# ---------------------------------------------------------------------------


@dataclass
class TunedV1Rule:
    """V1's three-arm shape with the thresholds refitted on TRAINING data.

    Answers the narrow question — were 0.20 / 0.80 / $5 the wrong numbers? —
    without changing the shape, so a difference is attributable to the numbers
    rather than to a change of model class. V1 itself is untouched; this is a
    separate version.
    """

    aggressive_price: float = V1_AGGRESSIVE_PRICE_MAX
    aggressive_capital: float = V1_AGGRESSIVE_CAPITAL_MIN
    directional_price: float = V1_DIRECTIONAL_PRICE_MIN
    version: str = "STRATEGY_MODEL_V2_TUNED_RULE"
    fitted_n: int = 0
    search_scale: int = 0

    def predict_label(self, price: float, capital: float) -> str:
        if price <= self.aggressive_price and capital >= self.aggressive_capital:
            return AGGRESSIVE
        if price >= self.directional_price:
            return DIRECTIONAL
        return PROTECT

    def to_dict(self) -> dict:
        return {"version": self.version,
                "aggressivePriceMax": round(self.aggressive_price, 4),
                "aggressiveCapitalMin": round(self.aggressive_capital, 2),
                "directionalPriceMin": round(self.directional_price, 4),
                "fittedN": self.fitted_n, "searchScale": self.search_scale,
                "frozenV1": {"aggressivePriceMax": V1_AGGRESSIVE_PRICE_MAX,
                             "aggressiveCapitalMin": V1_AGGRESSIVE_CAPITAL_MIN,
                             "directionalPriceMin": V1_DIRECTIONAL_PRICE_MIN}}


def fit_tuned_rule(train: list, histories: dict,
                   price_steps: int = 12, capital_steps: int = 6
                   ) -> Optional[TunedV1Rule]:
    """Coarse grid over V1's three thresholds, on training data only.

    Deliberately coarse and deliberately reported: `search_scale` records how
    many combinations were examined, because the best of N combinations is a
    maximum of N draws before it is an improvement. Optimises BALANCED
    accuracy — plain accuracy on a 93%-DIRECTIONAL population is maximised by
    a rule that says DIRECTIONAL always and knows nothing.
    """
    rows, labels, _kept = _rows(train, histories,
                                ("initial_price", "initial_capital"))
    if len(rows) < 200:
        return None
    prices = sorted(r[0] for r in rows)
    capitals = sorted(r[1] for r in rows)

    def _grid(values, steps):
        n = len(values)
        return sorted({values[min(n - 1, int(n * i / steps))]
                       for i in range(steps)})

    low_grid = [p for p in _grid(prices, price_steps) if p < 0.5]
    high_grid = [p for p in _grid(prices, price_steps) if p > 0.5]
    capital_grid = _grid(capitals, capital_steps)
    if not low_grid or not high_grid or not capital_grid:
        return None

    best, best_score, examined = None, -1.0, 0
    for low in low_grid:
        for high in high_grid:
            for capital in capital_grid:
                examined += 1
                rule = TunedV1Rule(low, capital, high)
                score = _balanced(rule, rows, labels)
                if score > best_score:
                    best_score, best = score, rule
    if best is None:
        return None
    best.fitted_n = len(rows)
    best.search_scale = examined
    return best


def _balanced(rule: TunedV1Rule, rows: list, labels: list) -> float:
    hits = {c: 0 for c in CLASSES}
    totals = {c: 0 for c in CLASSES}
    for row, label in zip(rows, labels):
        actual = CLASSES[label]
        totals[actual] += 1
        if rule.predict_label(row[0], row[1]) == actual:
            hits[actual] += 1
    present = [c for c in CLASSES if totals[c]]
    if not present:
        return -1.0
    return sum(hits[c] / totals[c] for c in present) / len(present)


# ---------------------------------------------------------------------------
# Models A / B / C
# ---------------------------------------------------------------------------


# Training rows handed to an estimator. A forest over six figures of rows
# costs minutes and buys nothing here — the signal, if any, is in a handful of
# entry-time columns. Subsampled deterministically and REPORTED, never
# silently.
MAX_TRAINING_ROWS = 40_000


def _subsample(x: list, y: list, cap: int = MAX_TRAINING_ROWS) -> tuple:
    if len(x) <= cap:
        return x, y, len(x)
    step = len(x) / float(cap)
    picked = [int(i * step) for i in range(cap)]
    return [x[i] for i in picked], [y[i] for i in picked], len(x)


def _fit_estimator(x: list, y: list, kind: str):
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.tree import DecisionTreeClassifier

    if kind == "logistic":
        estimator = make_pipeline(
            StandardScaler(),
            # No `multi_class=`: removed in scikit-learn 1.7+, and the
            # multinomial path is the default for a multi-class target
            # anyway. Passing it raises on new versions and changes nothing
            # on old ones.
            LogisticRegression(max_iter=1000, class_weight="balanced"))
    elif kind == "tree":
        estimator = DecisionTreeClassifier(
            max_depth=4, min_samples_leaf=max(20, len(x) // 50),
            class_weight="balanced", random_state=20260823)
    else:
        estimator = RandomForestClassifier(
            n_estimators=200, max_depth=6,
            min_samples_leaf=max(20, len(x) // 50),
            class_weight="balanced", random_state=20260823, n_jobs=-1)
    estimator.fit(x, y)
    return estimator


def _eligible_rows(episodes: list, histories: dict,
                   report: MulticlassReport) -> list:
    """`[(episode, features), ...]` for everything gradable, counting the rest.

    Split out from scoring so the expensive part — turning features into
    predictions — can be done in ONE batch. Predicting a hundred thousand rows
    one at a time through scikit-learn is roughly a thousand times slower than
    predicting them together, which is the difference between this comparison
    taking seconds and taking longer than the timeout that killed it.
    """
    out = []
    for episode in episodes:
        if not episode.labelled:
            report.unresolved += 1
            continue
        gate = eligibility(episode)
        if not gate.eligible:
            report.rejected[gate.reason] = \
                report.rejected.get(gate.reason, 0) + 1
            continue
        report.eligible += 1
        features = entry_features(
            episode, histories.get((episode.wallet, episode.market_id,
                                    episode.first_buy_ts)))
        out.append((episode, features))
    return out


def _score_rule(predict_label: Callable, episodes: list, histories: dict,
                version: str) -> MulticlassReport:
    """Score a pure-python rule. Row-at-a-time is fine here — no estimator."""
    report = MulticlassReport(model_version=version)
    for episode, features in _eligible_rows(episodes, histories, report):
        label = predict_label(episode, features)
        if label is None:
            report.rejected["incomplete_features"] = \
                report.rejected.get("incomplete_features", 0) + 1
            continue
        report.predictions += 1
        report.record(label, episode.label)
    return report


def _score_estimator(estimator, columns: tuple, episodes: list,
                     histories: dict, version: str) -> MulticlassReport:
    """Score a fitted estimator, batching every prediction into one call."""
    report = MulticlassReport(model_version=version)
    pairs = _eligible_rows(episodes, histories, report)
    batch, kept = [], []
    for episode, features in pairs:
        if not features:
            report.rejected["incomplete_features"] = \
                report.rejected.get("incomplete_features", 0) + 1
            continue
        row = [features.get(name) for name in columns]
        if any(v is None for v in row):
            report.rejected["incomplete_features"] = \
                report.rejected.get("incomplete_features", 0) + 1
            continue
        batch.append([float(v) for v in row])
        kept.append(episode)
    if not batch:
        return report
    predictions = estimator.predict(batch)
    for episode, index in zip(kept, predictions):
        report.predictions += 1
        report.record(CLASSES[int(index)], episode.label)
    return report


def compare_v1_families(development: list, validation: list,
                        min_wallet_samples: int = 200) -> dict:
    """§17 and §24 for the three-class problem. Fit on dev, score on val.

    Validation, not holdout: choosing between families IS a decision and must
    not touch the untouched set.
    """
    histories = prior_history_index(list(development) + list(validation))
    out: dict = {
        "modelVersion": "STRATEGY_MODEL_V2_DISCOVERY",
        "developmentConditions": len(development),
        "validationConditions": len(validation),
        "featureSet": list(V1_FEATURES),
        "note": ("Entry-time features ONLY. No inventory ratio, no opposite "
                 "buy, no payoff state — those exist only after the instant "
                 "V1 predicts. V1 itself is never refitted; everything here "
                 "is a separate version reported beside it (§24)."),
        "models": {},
    }

    # The frozen benchmark, scored on the SAME validation window so the
    # comparison is like for like.
    frozen = RN1StrategyModelV1()
    out["models"]["frozen_v1_benchmark"] = _score_rule(
        lambda e, _f: (frozen.predict(e).label
                       if frozen.predict(e).valid else None),
        validation, histories, "RN1_STRATEGY_MODEL_V1").to_dict()

    tuned = fit_tuned_rule(development, histories)
    if tuned is None:
        out["models"]["tuned_rule"] = {
            "available": False,
            "reason": "fewer than 200 complete development conditions"}
    else:
        report = _score_rule(
            lambda e, f: (tuned.predict_label(f["initial_price"],
                                              f["initial_capital"])
                          if f else None),
            validation, histories, tuned.version)
        out["models"]["tuned_rule"] = {**tuned.to_dict(),
                                       "validation": report.to_dict()}

    try:
        import sklearn  # noqa: F401
    except ImportError:
        out["models"]["universal"] = {
            "available": False,
            "reason": "scikit-learn not installed (optional dependency)"}
        return out

    # MODEL A — UNIVERSAL, on the columns every condition has.
    base_columns = tuple(c for c in V1_FEATURES
                         if not c.startswith("wallet_"))
    for kind in ("logistic", "tree", "forest"):
        x, y, _kept = _rows(development, histories, base_columns)
        if len(x) < 300 or len(set(y)) < 2:
            out["models"][f"universal_{kind}"] = {
                "available": False,
                "reason": f"{len(x)} complete development rows, or a single "
                          "class in training"}
            continue
        x_fit, y_fit, available = _subsample(x, y)
        estimator = _fit_estimator(x_fit, y_fit, kind)
        report = _score_estimator(
            estimator, base_columns, validation, histories,
            f"UNIVERSAL_V1_STATE_V1({kind},n={len(x)})")
        out["models"][f"universal_{kind}"] = {
            "version": f"UNIVERSAL_V1_STATE_V1({kind},n={len(x_fit)})",
            "trainingRows": len(x_fit),
            "trainingRowsAvailable": available,
            "validation": report.to_dict(),
            "featureImportance": _importance(estimator, base_columns)}

    # MODEL A+ — universal PLUS the wallet's own prior base rate.
    x, y, _kept = _rows(development, histories, V1_FEATURES)
    if len(x) >= 300 and len(set(y)) >= 2:
        x_fit, y_fit, available = _subsample(x, y)
        estimator = _fit_estimator(x_fit, y_fit, "logistic")
        report = _score_estimator(estimator, V1_FEATURES, validation,
                                  histories, "UNIVERSAL_PLUS_WALLET_V1")
        out["models"]["universal_with_wallet_history"] = {
            "version": f"UNIVERSAL_PLUS_WALLET_V1(n={len(x_fit)})",
            "trainingRows": len(x_fit),
            "trainingRowsAvailable": available,
            "validation": report.to_dict(),
            "featureImportance": _importance(estimator, V1_FEATURES),
            "note": ("Scored on a SMALLER population — conditions whose "
                     "wallet had no prior record are dropped rather than "
                     "zero-filled, so compare the counts and not just the "
                     "accuracies.")}
    else:
        out["models"]["universal_with_wallet_history"] = {
            "available": False,
            "reason": "too few conditions with a prior wallet record"}

    # MODEL B — WALLET-SPECIFIC.
    by_wallet: dict[str, list] = {}
    for episode in development:
        by_wallet.setdefault(episode.wallet, []).append(episode)
    per_wallet: dict[str, TunedV1Rule] = {}
    for wallet, rows in by_wallet.items():
        if len(rows) < min_wallet_samples:
            continue
        rule = fit_tuned_rule(rows, histories)
        if rule is not None:
            rule.version = f"WALLET_SPECIFIC_V1_STATE_V1({wallet[:10]})"
            per_wallet[wallet] = rule
    out["models"]["wallet_specific"] = {
        "walletsFitted": len(per_wallet),
        "minSamples": min_wallet_samples,
        "wallets": [{"wallet": w, **r.to_dict()}
                    for w, r in list(per_wallet.items())[:25]]}

    # MODEL C — HYBRID.
    if per_wallet and tuned is not None:
        used = {"wallet": 0, "universal": 0}

        def _hybrid(episode, features):
            if not features:
                return None
            rule = per_wallet.get(episode.wallet)
            used["wallet" if rule else "universal"] += 1
            rule = rule or tuned
            return rule.predict_label(features["initial_price"],
                                      features["initial_capital"])

        report = _score_rule(_hybrid, validation, histories,
                             "HYBRID_V1_STATE_V1")
        out["models"]["hybrid"] = {
            "version": "HYBRID_V1_STATE_V1",
            "validation": report.to_dict(),
            "usedWalletArm": used["wallet"],
            "usedUniversalArm": used["universal"],
            "note": ("If usedWalletArm is near zero the hybrid IS the "
                     "universal model and any difference is noise.")}
    out["verdict"] = _verdict(out)
    return out


def _importance(estimator, columns: tuple) -> dict:
    final = estimator[-1] if hasattr(estimator, "__getitem__") else estimator
    if hasattr(final, "coef_"):
        # Multi-class logistic: one row per class. Reported per class rather
        # than averaged, because a feature can push toward DIRECTIONAL and
        # away from AGGRESSIVE and an average would call that "unimportant".
        return {"kind": "logistic coefficient (standardised), per class",
                "perClass": {
                    CLASSES[i]: [{"feature": name, "value": round(float(v), 5)}
                                 for name, v in sorted(
                                     zip(columns, row),
                                     key=lambda kv: -abs(kv[1]))]
                    for i, row in enumerate(final.coef_)}}
    if hasattr(final, "feature_importances_"):
        pairs = sorted(zip(columns, final.feature_importances_),
                       key=lambda kv: -kv[1])
        return {"kind": "impurity importance",
                "ranked": [{"feature": n, "value": round(float(v), 5)}
                           for n, v in pairs]}
    return {"available": False}


def _verdict(block: dict) -> str:
    """Did anything beat the frozen rule, and did anything beat guessing?"""
    models = block.get("models") or {}
    frozen = models.get("frozen_v1_benchmark") or {}
    frozen_balanced = float(frozen.get("balancedAccuracy") or 0.0)
    baseline = float(frozen.get("majorityBaseline") or 0.0)
    best_name, best_balanced = "", frozen_balanced
    for name, entry in models.items():
        if name == "frozen_v1_benchmark":
            continue
        validation = (entry or {}).get("validation") or {}
        balanced = float(validation.get("balancedAccuracy") or 0.0)
        if balanced > best_balanced:
            best_name, best_balanced = name, balanced
    if not best_name:
        return (f"NOTHING BEAT THE FROZEN RULE. V1's balanced accuracy of "
                f"{frozen_balanced:.1%} on the validation window was not "
                "improved on by a retuned rule, a universal model, a "
                "wallet-specific model or a hybrid. On this data the frozen "
                "thresholds are not the limiting factor.")
    return (f"{best_name} reached {best_balanced:.1%} balanced accuracy "
            f"against the frozen rule's {frozen_balanced:.1%} (majority "
            f"baseline {baseline:.1%}). It is a CANDIDATE, not a "
            "replacement: V1 stays frozen, and this must survive the "
            "untouched holdout and a clean prospective window before it "
            "means anything.")
