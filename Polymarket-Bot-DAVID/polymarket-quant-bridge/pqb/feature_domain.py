"""FEATURE_VALIDITY_DOMAIN — what a feature actually means, per environment.

The failure this exists to prevent is silent and total. Discovery searches the
live-captured series, which carries a real order book; validation replays the
historical series, where no book exists and `bid`, `ask`, `spread` and `depth`
are pinned to neutral constants (see `analytics/history_series`). A rule
discovered on `spread_z` is therefore looked up, during validation, in a column
that never varies. It cannot fire. The candidate does not fail validation — it
never gets tested at all, and reports as merely untested forever.

The audit measured it: 42 of 121 historical feature columns were effectively
constant, and 38 existing threshold rules depended on features unavailable in
meaningful historical validation data. That is the difference between a
research pipeline that is starving and one that is broken in a way nobody can
see from the dashboard.

The rule enforced here is one sentence: **a strategy may only be discovered for
validation if every feature it requires exists, and varies, in the domain it
will be validated in.** Candidates that already violate it are QUARANTINED —
never deleted — with a permanent explanation, because §28 requires the library
to keep its whole history including its mistakes.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# The status a candidate is moved to when its features cannot exist in the
# validation data. Terminal for research purposes, reversible in principle:
# if a domain later gains the feature (a book history becomes available, say),
# a rebuild of the domain releases it.
QUARANTINE_STATUS = "quarantined"
QUARANTINE_REASON = "FEATURE_NOT_AVAILABLE_IN_VALIDATION_DATA"

# A column whose values move less than this across a whole series is treated as
# constant. Not zero: floating-point noise and one-tick jitter in an otherwise
# pinned column would otherwise read as variance and defeat the whole check.
MIN_VARIANCE = 1e-9

# A feature must be present in at least this share of the sampled validation
# series to count as available. One series carrying a column does not make it
# usable evidence across a pool.
MIN_COVERAGE = 0.5


@dataclass
class FeatureValidity:
    """One feature's standing in one domain, with everything §5 asks for."""

    name: str
    live_available: bool = False
    historical_available: bool = False
    oos_available: bool = False
    variance: float = 0.0
    observations: int = 0
    coverage: float = 0.0
    earliest_ts: float = 0.0
    source: str = ""

    @property
    def usable_for_validation(self) -> bool:
        return self.oos_available

    def why_not(self) -> str:
        if self.oos_available:
            return ""
        if not self.historical_available:
            return f"{self.name}: absent from validation series"
        if self.coverage < MIN_COVERAGE:
            return (f"{self.name}: present in only {self.coverage:.0%} of "
                    f"validation series (need {MIN_COVERAGE:.0%})")
        return (f"{self.name}: constant in validation data "
                f"(variance {self.variance:.2e})")


@dataclass
class FeatureDomain:
    """The measured validity of every feature across the validation pool."""

    features: dict[str, FeatureValidity] = field(default_factory=dict)
    series_sampled: int = 0
    rows_sampled: int = 0
    # True when no validation series have been built yet — a first run, or a
    # freshly cleared pool. There is nothing to measure against, so the gate
    # admits everything and says so. Refusing every candidate because the
    # evidence about the evidence has not been gathered yet would be the
    # research equivalent of failing closed on a missing thermometer.
    permissive: bool = False

    # -- queries -------------------------------------------------------------

    def resolve(self, name: str) -> tuple[Optional[str], str]:
        """Which measured column decides this feature's validity, and how.

        THE correction this class needed. The gate measures the raw columns
        an exported series carries, but a bridge-path rule is not replayed
        against those columns — `research._oos_context` runs the bridge's own
        feature engineer over them first, and the frame the rule actually
        meets carries ~988 engineered columns rather than the ~121 on disk.
        Judging `bid_accel` by asking whether a CSV has a `bid_accel` column
        therefore answered the wrong question, and answered it "no" for 159
        of 235 candidates — two thirds of the library quarantined for
        features that were available all along.

        Resolution mirrors `bridge.live_features.required_columns`, which is
        the function deciding what to FEED the engineer: a referenced name
        maps back to its base by longest-prefix match. Using its inverse to
        decide what the engineer PRODUCES is consistent by construction —
        if the two ever disagree, the replay is being fed columns it cannot
        use, which is a different bug and a louder one.

        -> (column, "direct" | "derived" | "global"), or (None, "unknown")
        """
        if name in self.features:
            return name, "direct"
        matches = [c for c in self.features
                   if name.startswith(f"{c}_")]
        if matches:
            return max(matches, key=len), "derived"
        # Global and cross-column features (px_momentum_20, regime_breakout)
        # map to no single base; the engineer derives them from the price
        # series, which `required_columns` feeds unconditionally.
        if "price" in self.features:
            return "price", "global"
        return None, "unknown"

    def usable(self, name: str) -> bool:
        if self.permissive:
            return True
        column, _how = self.resolve(name)
        entry = self.features.get(column or "")
        return bool(entry and entry.usable_for_validation)

    def unusable(self, names: Iterable[str]) -> list[str]:
        """Which of these features cannot carry a validation, with reasons."""
        if self.permissive:
            return []
        out = []
        for name in sorted(set(names)):
            column, how = self.resolve(name)
            entry = self.features.get(column or "")
            if entry is None:
                out.append(f"{name}: absent from validation series")
            elif not entry.usable_for_validation:
                if how == "direct":
                    out.append(entry.why_not())
                else:
                    # Name the BASE column, because that is what has to be
                    # fixed. "price_band_z is constant" sends someone
                    # looking for a column that was never on disk.
                    out.append(f"{name}: derived from {column}, which is "
                               f"unusable ({entry.why_not()})")
        return out

    def admits(self, rule: dict) -> tuple[bool, list[str]]:
        """May this rule be registered for validation? Reasons if not."""
        problems = self.unusable(features_of(rule))
        return (not problems), problems

    def constant_columns(self) -> list[str]:
        return sorted(name for name, f in self.features.items()
                      if f.historical_available and f.variance <= MIN_VARIANCE)

    def summary(self) -> dict:
        usable = sum(1 for f in self.features.values()
                     if f.usable_for_validation)
        return {
            "featureDomainPermissive": self.permissive,
            "featuresKnown": len(self.features),
            "featuresUsableInValidation": usable,
            "featuresConstantInValidation": len(self.constant_columns()),
            "seriesSampled": self.series_sampled,
            "rowsSampled": self.rows_sampled,
        }

    # -- persistence ---------------------------------------------------------

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "seriesSampled": self.series_sampled,
            "rowsSampled": self.rows_sampled,
            "features": {n: asdict(f) for n, f in self.features.items()},
        }, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> Optional["FeatureDomain"]:
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        domain = cls(series_sampled=int(raw.get("seriesSampled") or 0),
                     rows_sampled=int(raw.get("rowsSampled") or 0))
        for name, entry in (raw.get("features") or {}).items():
            entry.pop("name", None)
            domain.features[name] = FeatureValidity(name=name, **entry)
        return domain


def features_of(rule: dict) -> set[str]:
    """Every feature column a rule depends on, whatever kind of rule it is.

    Rule types that do not read the feature frame at all — sequence chains,
    longshots, wallet-state and wallet-behavior rules replay against raw tapes
    with their own mechanics — declare no feature dependencies and are
    therefore never quarantined by this gate. That is correct, not an
    oversight: the gate answers "does this column exist in the validation
    frame?", and those rules do not ask the frame anything.
    """
    kind = str(rule.get("type") or "threshold")
    if kind in ("sequence", "longshot", "wallet_state", "wallet_behavior",
                "sharp_move"):
        return set()
    out = set()
    for key in ("entry_feature", "filter_feature", "exit_feature"):
        value = rule.get(key)
        if value:
            out.add(str(value))
    for value in (rule.get("features") or []):
        out.add(str(value))
    return out


def build_domain(series_paths: Iterable[Path], live_features: Iterable[str],
                 max_series: int = 40,
                 max_rows_per_series: int = 4000) -> FeatureDomain:
    """Measure the validation domain from the OOS pool's own CSVs.

    Sampled rather than exhaustive: variance is a property of the column, not
    of how many series we read, and §26 asks for more information per unit of
    compute. Coverage is the share of sampled series carrying the column at
    all; variance is pooled across them, so a column that is pinned in every
    series reads as constant even though each series alone has zero variance
    for a different reason.
    """
    paths = [Path(p) for p in series_paths][:max_series]
    domain = FeatureDomain(series_sampled=0)
    # Welford accumulators, so a wide pool never materialises in memory.
    counts: dict[str, int] = {}
    means: dict[str, float] = {}
    m2s: dict[str, float] = {}
    present_in: dict[str, int] = {}
    earliest: dict[str, float] = {}

    for path in paths:
        try:
            with open(path, newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle)
                columns = list(reader.fieldnames or [])
                if not columns:
                    continue
                domain.series_sampled += 1
                for column in columns:
                    present_in[column] = present_in.get(column, 0) + 1
                for index, row in enumerate(reader):
                    if index >= max_rows_per_series:
                        break
                    domain.rows_sampled += 1
                    stamp = _float(row.get("ts"))
                    for column in columns:
                        raw = row.get(column)
                        if raw is None or raw == "":
                            continue
                        value = _float(raw)
                        if value is None:
                            continue
                        n = counts.get(column, 0) + 1
                        counts[column] = n
                        delta = value - means.get(column, 0.0)
                        means[column] = means.get(column, 0.0) + delta / n
                        m2s[column] = (m2s.get(column, 0.0)
                                       + delta * (value - means[column]))
                        if stamp and (column not in earliest
                                      or stamp < earliest[column]):
                            earliest[column] = stamp
        except OSError:
            continue

    sampled = max(1, domain.series_sampled)
    live = set(live_features)
    for column in set(present_in) | live:
        n = counts.get(column, 0)
        variance = (m2s.get(column, 0.0) / n) if n > 1 else 0.0
        coverage = present_in.get(column, 0) / sampled
        historical = present_in.get(column, 0) > 0
        domain.features[column] = FeatureValidity(
            name=column,
            live_available=column in live,
            historical_available=historical,
            oos_available=(historical and coverage >= MIN_COVERAGE
                           and variance > MIN_VARIANCE),
            variance=round(variance, 12),
            observations=n,
            coverage=round(coverage, 4),
            earliest_ts=earliest.get(column, 0.0),
            source="oos_pool",
        )
    return domain


def quarantine_incompatible(library, domain: FeatureDomain,
                            log=None) -> list[tuple[str, str]]:
    """Move every candidate whose features cannot exist in validation data.

    Preserved, not deleted: status becomes `quarantined` and the reason names
    the offending columns, so the record of what was discovered and why it
    could never be tested survives in full. Already-terminal rows are left
    alone — re-labelling a retired candidate would rewrite a settled verdict.
    """
    moved: list[tuple[str, str]] = []
    released = 0
    for row in library.all_strategies():
        if row["status"] in ("retired", "rejected"):
            continue
        ok, problems = domain.admits(row.get("rule") or {})

        # RELEASE. Quarantine is a statement about the validation data, not a
        # verdict on the rule, so it has to be revisitable — the pool grows
        # every pass, and the gate itself can be wrong (it was: it judged
        # engineered features by asking whether the raw CSV carried them, and
        # held two thirds of the library on a question the replay never
        # asks). Without this branch a gate bug is permanent, because the
        # loop below skips anything already quarantined.
        #
        # Released at `new`, never at the status held before quarantine: the
        # evidence rows were preserved untouched, so the ladder re-derives
        # the real standing from them on the next validation. Restoring a
        # remembered `validated` here would let a data-availability accident
        # hand back a trading status that no evidence had re-earned.
        if row["status"] == QUARANTINE_STATUS:
            if ok:
                library.set_status(
                    row["id"], "new",
                    "released from quarantine: the features this rule needs "
                    "are available in validation data after all; evidence "
                    "preserved, status re-earned from the ladder")
                released += 1
                if log:
                    log(f"    released {row['id'][:44]} from quarantine")
            continue

        if ok:
            continue
        reason = f"{QUARANTINE_REASON}: " + "; ".join(problems[:3])
        library.set_status(row["id"], QUARANTINE_STATUS, reason[:400])
        moved.append((row["id"], reason))
        if log:
            log(f"    quarantined {row['id'][:44]}: {problems[0]}")
    if released and log:
        log(f"  {released} candidate(s) released from quarantine - they "
            "re-enter the queue and must earn their status from the "
            "evidence on record")
    return moved


def _float(value) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(out) or math.isinf(out) else out
