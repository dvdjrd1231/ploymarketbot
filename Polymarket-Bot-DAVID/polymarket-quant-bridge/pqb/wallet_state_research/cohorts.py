"""Why the base rate differs — measured, not speculated.

The study reports two facts that sit awkwardly together. Conditional on a
wallet reaching the two-sided state, this tape reproduces the source's
PROTECT/AGGRESSIVE split to within about a point. Unconditionally, 7.8% of
conditions here go two-sided against the source's 79.2%.

Listing candidate explanations for that gap is not research. Each one is a
FILTER, every filter can be applied to this tape, and the two-sided rate under
it can be measured — so this module replaces the paragraph of speculation with
a table of numbers, and then uses the result for something.

Two outputs:

* **A decomposition.** For each hypothesis about who the source was studying,
  what is the two-sided rate among conditions matching it? A hypothesis that
  moves 7.8% toward 79.2% is doing explanatory work; one that does not is
  wrong and gets to be reported as wrong.
* **A SOURCE-COMPARABLE COHORT.** The cohort whose two-sided rate lands
  closest to 79.2%, chosen from the filters BEFORE any accuracy is computed on
  it. Grading V1 on the full 282k-condition population is an unfair
  reproduction test — the source never claimed its rule applied to every
  wallet on Polymarket — and grading it on a cohort picked for making V1 look
  good would be worse. Selecting on the BASE RATE, which is a property of the
  population rather than of the model, is the compromise: it makes the
  populations comparable without consulting the model's performance.

That ordering is the whole safeguard, and `select_cohort` takes no model and
no labels-vs-predictions comparison as input so that it cannot be violated by
accident.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from .episodes import AGGRESSIVE, DIRECTIONAL, PROTECT, Episode
from .features import category_of
from .strategy_v1 import initial_capital, initial_price

# The source's own headline: 79.2% of its 250 completed conditions became
# two-sided. A benchmark to aim a filter at, never an expected outcome.
SOURCE_TWO_SIDED_RATE = 0.792

# A cohort below this many conditions is described but never selected: a
# filter that leaves forty conditions can hit any base rate by accident.
MIN_COHORT = 500


@dataclass
class Cohort:
    """One hypothesis about who the source was studying, and what it implies."""

    name: str
    description: str
    conditions: int = 0
    two_sided: int = 0
    directional: int = 0
    protect: int = 0
    aggressive: int = 0
    eligible: bool = False

    @property
    def two_sided_rate(self) -> float:
        return (self.two_sided / self.conditions) if self.conditions else 0.0

    @property
    def gap_to_source(self) -> float:
        return abs(self.two_sided_rate - SOURCE_TWO_SIDED_RATE)

    @property
    def explains(self) -> float:
        """Share of the base-rate gap this filter closes, 0..1.

        Negative movement is reported as 0 rather than as a negative number:
        a filter that makes the gap worse has not explained a negative amount
        of it, it has simply been refuted.
        """
        return 0.0

    def to_dict(self, baseline_rate: float = 0.0) -> dict:
        closed = 0.0
        span = SOURCE_TWO_SIDED_RATE - baseline_rate
        if span > 0:
            closed = max(0.0, min(1.0,
                                  (self.two_sided_rate - baseline_rate) / span))
        return {
            "cohort": self.name,
            "description": self.description,
            "conditions": self.conditions,
            "twoSidedRate": round(self.two_sided_rate, 4),
            "gapToSource": round(self.gap_to_source, 4),
            "shareOfGapClosed": round(closed, 4),
            "distribution": {
                "DIRECTIONAL": self.directional,
                "PROTECT_REBALANCE": self.protect,
                "AGGRESSIVE_OPPOSITE": self.aggressive},
            "eligibleForSelection": self.eligible,
        }


def _tally(cohort: Cohort, episodes: Iterable[Episode]) -> Cohort:
    for episode in episodes:
        cohort.conditions += 1
        if episode.switched:
            cohort.two_sided += 1
        if episode.label == DIRECTIONAL:
            cohort.directional += 1
        elif episode.label == PROTECT:
            cohort.protect += 1
        elif episode.label == AGGRESSIVE:
            cohort.aggressive += 1
    cohort.eligible = cohort.conditions >= MIN_COHORT
    return cohort


def _wallet_condition_counts(episodes: Iterable[Episode]) -> dict:
    counts: dict[str, int] = {}
    for episode in episodes:
        counts[episode.wallet] = counts.get(episode.wallet, 0) + 1
    return counts


def filters(episodes: list) -> list:
    """Every hypothesis, as a `(name, description, predicate)` triple.

    Fixed and reported in full — including the ones that fail — because the
    point is to find out which explanation is right, and a search that only
    reports its winner is the multiple-testing failure this codebase spends
    most of its comments avoiding.
    """
    counts = _wallet_condition_counts(episodes)

    return [
        ("all", "every (wallet, condition) with a BUY — the study's default",
         lambda e: True),
        ("active_wallet_10",
         "wallets with >= 10 conditions on this tape: most of the 70k place "
         "one trade and never return, and a one-trade wallet cannot hedge",
         lambda e: counts.get(e.wallet, 0) >= 10),
        ("active_wallet_50",
         "wallets with >= 50 conditions — a serious participant",
         lambda e: counts.get(e.wallet, 0) >= 50),
        ("active_wallet_200",
         "wallets with >= 200 conditions — the professional tail",
         lambda e: counts.get(e.wallet, 0) >= 200),
        ("capital_5",
         "first BUY of at least $5 — the same floor V1's aggressive arm uses",
         lambda e: initial_capital(e) >= 5.0),
        ("capital_50",
         "first BUY of at least $50: hedging a $2 position costs more in fees "
         "than the position is worth",
         lambda e: initial_capital(e) >= 50.0),
        ("capital_500", "first BUY of at least $500",
         lambda e: initial_capital(e) >= 500.0),
        ("mid_price",
         "first BUY between 20c and 80c — neither arm of V1's price rule "
         "fires, and a near-certain outcome has little to rebalance against",
         lambda e: 0.20 < initial_price(e) < 0.80),
        ("economics",
         "economics markets — the source's own 50-condition sample was "
         "economics, where it reported 47/50 becoming two-sided",
         lambda e: category_of(e.question) == "economics"),
        ("multi_event",
         "conditions with more than one observed event: a single-event "
         "condition cannot show a transition by construction",
         lambda e: len(e.events) > 1),
        ("settled_or_redeemed",
         "conditions finished by fact (settlement or redemption) rather than "
         "by the quiet-period heuristic",
         lambda e: e.label_quality in ("resolved", "redeemed")),
        ("active_and_funded",
         "wallets with >= 50 conditions AND a first BUY of at least $50 — "
         "the two strongest single filters, combined",
         lambda e: (counts.get(e.wallet, 0) >= 50
                    and initial_capital(e) >= 50.0)),
        ("active_funded_mid",
         "active, funded, and priced in the middle — the closest this tape "
         "can come to 'a serious wallet managing a real position'",
         lambda e: (counts.get(e.wallet, 0) >= 50
                    and initial_capital(e) >= 50.0
                    and 0.20 < initial_price(e) < 0.80)),
    ]


@dataclass
class Decomposition:
    """The whole table, plus which filter was chosen and why."""

    baseline_rate: float = 0.0
    cohorts: list = field(default_factory=list)
    selected: Optional[str] = None
    selected_reason: str = ""

    def to_dict(self) -> dict:
        rows = [c.to_dict(self.baseline_rate) for c in self.cohorts]
        rows.sort(key=lambda r: r["gapToSource"])
        return {
            "sourceTwoSidedRate": SOURCE_TWO_SIDED_RATE,
            "baselineTwoSidedRate": round(self.baseline_rate, 4),
            "minCohort": MIN_COHORT,
            "cohorts": rows,
            "selectedCohort": self.selected,
            "selectionReason": self.selected_reason,
            "note": ("Each row is a HYPOTHESIS about who the source was "
                     "studying, applied to this tape and measured. Selection "
                     "is on the BASE RATE — a property of the population — "
                     "and never on accuracy, so the comparable cohort cannot "
                     "be chosen for flattering the model."),
        }


def decompose(episodes: list) -> Decomposition:
    """Measure the two-sided rate under every hypothesis."""
    out = Decomposition()
    rows = list(episodes)
    for name, description, predicate in filters(rows):
        cohort = _tally(Cohort(name=name, description=description),
                        (e for e in rows if predicate(e)))
        out.cohorts.append(cohort)
        if name == "all":
            out.baseline_rate = cohort.two_sided_rate

    eligible = [c for c in out.cohorts
                if c.eligible and c.name != "all"]
    if not eligible:
        out.selected_reason = (
            f"no filter left at least {MIN_COHORT} conditions — this tape "
            "cannot support a source-comparable cohort")
        return out
    best = min(eligible, key=lambda c: c.gap_to_source)
    out.selected = best.name
    if best.gap_to_source > 0.25:
        out.selected_reason = (
            f"closest available is '{best.name}' at "
            f"{best.two_sided_rate:.1%} two-sided, still "
            f"{best.gap_to_source:.1%} from the source's "
            f"{SOURCE_TWO_SIDED_RATE:.1%}. NO filter on this tape reproduces "
            "the source's population — so the base-rate gap is NOT explained "
            "by any of these hypotheses, and the comparison below should be "
            "read as the closest available rather than as a like-for-like "
            "reproduction.")
    else:
        out.selected_reason = (
            f"'{best.name}' reaches {best.two_sided_rate:.1%} two-sided "
            f"against the source's {SOURCE_TWO_SIDED_RATE:.1%} — within "
            f"{best.gap_to_source:.1%}, closing "
            f"{(best.two_sided_rate - out.baseline_rate) / max(1e-9, SOURCE_TWO_SIDED_RATE - out.baseline_rate):.0%} "
            "of the gap. This is the fairest available reproduction "
            "population.")
    return out


def select_cohort(episodes: list, decomposition: Decomposition) -> list:
    """The episodes belonging to the selected cohort.

    Takes no model, no predictions and no accuracy. That is deliberate: the
    only input to the choice is the base rate, so a cohort cannot be picked
    for making anything look good.
    """
    if not decomposition.selected:
        return []
    for name, _description, predicate in filters(list(episodes)):
        if name == decomposition.selected:
            return [e for e in episodes if predicate(e)]
    return []
