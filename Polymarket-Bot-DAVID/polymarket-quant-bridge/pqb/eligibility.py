"""OOS_MARKET_ELIGIBILITY — one authoritative answer to one question.

    Which markets are legitimately eligible for this candidate right now?

Before this module the answer was assembled inline at the replay loop from a
single set subtraction, which meant every other eligibility concern — does the
series still exist on disk, does it carry the columns this rule needs, is it
chronologically after the rule was discovered — was either not asked or asked
somewhere else. §4 asks for one component that owns all of them, and for the
answer to be explainable per market rather than a silent skip.

**On walk-forward timing.** The audit's finding is precise: *a market that is
unseen but older than the candidate's discovery date should not automatically
qualify as true forward OOS.* Note what it does not say. Such a market is still
genuinely unseen evidence — the rule was never fitted to it — and refusing it
outright would delete most of a store built from settled history, which is the
opposite of §2's purpose. What it must not do is masquerade as forward
validation. So every piece of evidence is CLASSIFIED at the moment it is
recorded:

* ``forward``    — the market began after the candidate was discovered. True
                   walk-forward evidence, and the only kind that can satisfy a
                   forward-validation requirement.
* ``concurrent`` — the market was already running when the candidate was
                   discovered. Unseen, but it shares its window with the
                   discovery data and may share an event with it.
* ``historical`` — the market had already ended. Unseen and independent, but
                   backward-looking by construction.

All three count toward independent market breadth, because all three are
markets the rule was never fitted to. Only ``forward`` counts toward forward
confirmation, and the class is stored on the evidence row so the distinction
survives in the record rather than being recomputed from whatever the clock
says later.

**On which eligible market to spend next.** Eligibility answers *may we use
this market*; it does not follow that every permitted market is equally worth
using. §7 of the research directive: a candidate whose entire record sits in
one category, one month and one depth of market has not been shown to
generalise, and the next market that could show it is one from a different
environment — not whichever happened to sort first. So inside each temporal
class the eligible markets are ordered by INFORMATION GAIN: how many of the
candidate's uncovered environments this market would newly cover.

The ordering is inside the temporal class, never across it. Forward evidence
is the only kind that can satisfy a forward-validation requirement, and
trading that away for variety would be swapping a validation property for a
research preference — precisely the direction this system does not allow.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

FORWARD = "forward"
CONCURRENT = "concurrent"
HISTORICAL = "historical"
UNKNOWN = "unknown"

# Rejection reasons, machine-readable so the census can name the bottleneck.
R_DISCOVERY = "discovery_contamination"
R_TESTIFIED = "already_testified"
R_PARKED = "parked_after_non_observation"
R_MISSING = "series_file_missing"
R_THIN = "series_too_thin"
R_FEATURES = "features_absent_from_series"


# The environment dimensions a market belongs to. Coarse on purpose: the
# question is "is this a meaningfully different place to test?", and a finer
# grain would answer "yes" for every market and make the term meaningless.
TRAIT_DIMENSIONS = ("category", "era", "depth")


@dataclass(frozen=True)
class MarketRecord:
    """One validation series, with everything eligibility needs to judge it."""

    market_id: str
    token_id: str
    csv: Path
    first_ts: float = 0.0
    last_ts: float = 0.0
    rows: int = 0
    source: str = ""            # export | pool
    # The market's environment, for information-gain ordering. Empty is a
    # legitimate value and is treated as "unknown", never as a category of
    # its own — otherwise every market missing metadata would look like a
    # cluster of identical environments and suppress each other.
    category: str = ""

    def temporal_class(self, discovered_ts: float) -> str:
        """Where this market sits relative to the candidate's discovery."""
        if not discovered_ts or not self.first_ts:
            return UNKNOWN
        if self.first_ts >= discovered_ts:
            return FORWARD
        if self.last_ts and self.last_ts >= discovered_ts:
            return CONCURRENT
        return HISTORICAL

    @property
    def era(self) -> str:
        """The month the series ended in — the cheapest honest proxy for
        'a different time period'. Derived from the market's own clock, not
        from when we happened to process it."""
        stamp = self.last_ts or self.first_ts
        if not stamp:
            return ""
        return time.strftime("%Y-%m", time.gmtime(float(stamp)))

    @property
    def depth(self) -> str:
        """How much history the series carries, banded.

        A stand-in for market structure: a 90-row tape and a 4,000-row tape
        are different trading environments whatever their category says, and
        row count is the one structural fact every record already has without
        re-reading the CSV.
        """
        if not self.rows:
            return ""
        if self.rows < 200:
            return "thin"
        if self.rows < 800:
            return "medium"
        return "deep"

    def traits(self) -> dict[str, str]:
        """This market's environment, dimension by dimension."""
        return {"category": (self.category or "").strip().lower(),
                "era": self.era, "depth": self.depth}


@dataclass
class Eligibility:
    """The verdict for one candidate: what it may use, and why not the rest."""

    candidate_id: str
    markets: list[MarketRecord] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)
    classes: dict[str, str] = field(default_factory=dict)

    @property
    def market_ids(self) -> set[str]:
        return {m.market_id for m in self.markets}

    # What environments this candidate's EXISTING evidence already covers,
    # and how much new ground each eligible market would break. Reported on
    # the verdict so the allocator and the dashboard read the same numbers
    # the replay loop ordered on.
    covered: dict[str, set] = field(default_factory=dict)
    gains: dict[str, int] = field(default_factory=dict)

    def forward_available(self) -> int:
        return sum(1 for m in self.markets
                   if self.classes.get(m.market_id) == FORWARD)

    def diversity(self) -> dict[str, int]:
        """How many distinct environments the existing evidence spans.

        The input to §7's question and to the reward's information term: a
        candidate at 1 across every dimension is a local result, however good
        its expectancy looks.
        """
        return {key: len(values) for key, values in self.covered.items()}

    def unexplored(self) -> list[str]:
        """Dimensions where the record is one environment deep, named."""
        return sorted(key for key, values in self.covered.items()
                      if len(values) <= 1)

    def next_action(self) -> str:
        """§24: what would allow this candidate to progress."""
        if self.markets:
            return (f"QUEUED_FOR_OOS ({len(self.markets)} eligible, "
                    f"{self.forward_available()} forward)")
        if self.rejected.get(R_FEATURES):
            return "BLOCKED_ON_FEATURES (rule needs columns the series lack)"
        if self.rejected.get(R_PARKED):
            return "EXHAUSTED (every eligible market attempted without a fire)"
        if self.rejected.get(R_TESTIFIED):
            return "POOL_EXHAUSTED (every eligible market has testified)"
        return "WAITING_FOR_DATA (backfill will widen the pool)"


class MarketEligibilityService:
    """The single component every candidate asks. Holds no candidate state."""

    def __init__(self, markets: Iterable[MarketRecord], library,
                 feature_domain=None, min_rows: int = 0):
        self._markets = [m for m in markets]
        self._library = library
        self._domain = feature_domain
        self._min_rows = int(min_rows)
        self._by_id = {m.market_id: m for m in self._markets}

    def _covered_traits(self, testified: Iterable[str]) -> dict[str, set]:
        """The environments this candidate's existing evidence already spans.

        Only markets still in the pool can be described, because their traits
        come from the pool record. A market that testified and has since been
        pruned from the cache is counted as coverage we cannot see — which
        makes the candidate look LESS covered than it is, and so slightly
        over-eager to diversify. That is the safe direction to be wrong in:
        the failure it risks is a redundant test, and the failure the other
        direction risks is concluding generalisation from a gap in the index.
        """
        covered: dict[str, set] = {key: set() for key in TRAIT_DIMENSIONS}
        for market_id in testified:
            record = self._by_id.get(str(market_id))
            if record is None:
                continue
            for key, value in record.traits().items():
                if value:
                    covered[key].add(value)
        return covered

    @staticmethod
    def _information_gain(market: MarketRecord,
                          covered: dict[str, set]) -> int:
        """How many uncovered environments this market would newly cover.

        A market with no metadata scores zero rather than negative: it is not
        penalised for being undescribed, it simply cannot be argued for on
        diversity grounds and falls back to the size tiebreak.
        """
        gain = 0
        for key, value in market.traits().items():
            if value and value not in covered.get(key, set()):
                gain += 1
        return gain

    def for_candidate(self, candidate: dict) -> Eligibility:
        """Everything legitimately usable by this candidate, right now."""
        cid = str(candidate["id"])
        verdict = Eligibility(candidate_id=cid)
        discovery = self._library.discovery_markets(cid)
        testified = self._library.evidence_markets(cid)
        parked = self._library.parked_markets(cid)
        discovered_ts = float(candidate.get("created_ts") or 0.0)

        rule_ok = True
        if self._domain is not None:
            rule_ok, _problems = self._domain.admits(candidate.get("rule")
                                                     or {})

        def _reject(reason: str) -> None:
            verdict.rejected[reason] = verdict.rejected.get(reason, 0) + 1

        for market in self._markets:
            if not rule_ok:
                _reject(R_FEATURES)
                continue
            if market.market_id in discovery:
                _reject(R_DISCOVERY)
                continue
            if market.market_id in testified:
                _reject(R_TESTIFIED)
                continue
            if market.market_id in parked:
                _reject(R_PARKED)
                continue
            if not market.csv.exists():
                _reject(R_MISSING)
                continue
            if self._min_rows and market.rows and market.rows < self._min_rows:
                _reject(R_THIN)
                continue
            verdict.markets.append(market)
            verdict.classes[market.market_id] = \
                market.temporal_class(discovered_ts)

        # §7: what would this candidate LEARN from each market it may use.
        # Computed against the environments its existing evidence already
        # covers, so the answer is specific to this candidate's record rather
        # than a property of the pool.
        verdict.covered = self._covered_traits(testified)
        verdict.gains = {m.market_id: self._information_gain(m,
                                                             verdict.covered)
                         for m in verdict.markets}

        # Forward evidence first. When the budget runs out mid-candidate — and
        # it usually does — the markets that were spent should be the ones
        # that can satisfy a forward-validation requirement, not whichever
        # happened to sort first. Information gain orders WITHIN a class and
        # never across one: variety is a research preference and forward
        # position is a validation property, and trading the second for the
        # first would be this layer reaching into the ladder's business.
        order = {FORWARD: 0, CONCURRENT: 1, UNKNOWN: 2, HISTORICAL: 3}
        verdict.markets.sort(
            key=lambda m: (order.get(verdict.classes.get(m.market_id,
                                                         UNKNOWN), 4),
                           -verdict.gains.get(m.market_id, 0),
                           -m.rows))
        return verdict

    def census(self) -> dict:
        traits: dict[str, set] = {key: set() for key in TRAIT_DIMENSIONS}
        for market in self._markets:
            for key, value in market.traits().items():
                if value:
                    traits[key].add(value)
        return {
            "eligibilityPoolSize": len(self._markets),
            "eligibilityPoolRows": sum(m.rows for m in self._markets),
            # The ceiling on how diverse any candidate's evidence can get.
            # When a dimension reads 1 here, no candidate can be faulted for
            # failing to generalise across it — the pool has nowhere else to
            # go, and that is a finding about the DATA.
            "eligibilityPoolEnvironments": {k: len(v)
                                            for k, v in traits.items()},
        }


def classify_pool(markets: Iterable[MarketRecord],
                  discovered_ts: float) -> dict[str, int]:
    """How a pool breaks down for a candidate discovered at this moment.
    Reported so 'no forward evidence exists yet' is visible as a property of
    the DATA rather than looking like a candidate's failing."""
    out: dict[str, int] = {}
    for market in markets:
        key = market.temporal_class(discovered_ts)
        out[key] = out.get(key, 0) + 1
    return out


def records_from_entries(entries: Iterable[dict]) -> list[MarketRecord]:
    """Adapter from the research pass's `eval_entries` shape."""
    out = []
    for entry in entries:
        out.append(MarketRecord(
            market_id=str(entry.get("market") or ""),
            token_id=str(entry.get("token") or ""),
            csv=Path(entry.get("csv") or ""),
            first_ts=float(entry.get("firstTs") or 0.0),
            last_ts=float(entry.get("lastTs") or 0.0),
            rows=int(entry.get("rows") or 0),
            source=str(entry.get("source") or "export"),
            category=str(entry.get("category") or "")))
    return out


def diversity_of(library, candidate_id: str, cumulative: dict,
                 service: Optional["MarketEligibilityService"] = None
                 ) -> dict[str, int]:
    """How many distinct environments one candidate's evidence spans.

    Combines what the eligibility service can see about the markets that
    testified (category, era, depth) with the walk-forward classes the
    library persisted on the evidence rows themselves. The second half is
    authoritative and the first is best-effort, which is why they are counted
    separately rather than summed into one 'diversity' number that could not
    be traced back to either.
    """
    out = {key: 0 for key in TRAIT_DIMENSIONS}
    if service is not None:
        covered = service._covered_traits(          # noqa: SLF001 - same module
            library.evidence_markets(candidate_id))
        out.update({key: len(values) for key, values in covered.items()})
    out["temporal_classes"] = len(
        cumulative.get("markets_by_temporal_class") or {})
    # Kept under the name the reward function reads, so the two cannot drift
    # apart through a rename on one side.
    out["categories"] = out.get("category", 0)
    out["eras"] = out.get("era", 0)
    out["bands"] = out.get("depth", 0)
    return out
