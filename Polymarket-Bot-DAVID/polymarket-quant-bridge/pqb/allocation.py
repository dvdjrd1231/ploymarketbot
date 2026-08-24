"""Who gets the next replay: exploitation, balanced against exploration.

The audit's second hard number. 153 of 231 candidates had never received a
single validation evaluation, because allocation was a straight sort on
research priority and priority is built from evidence a candidate has already
collected. A candidate with no evidence scores low, so it is never allocated,
so it collects no evidence. Nothing about that loop is a bug in any one line —
it is a bandit run at zero exploration, and it converges on whatever happened
to be measured first.

The fix is a reserved share of every cycle, not a tweak to the ranking. Three
strata, filled in order, each capped so no one of them can take the budget:

* **EXPLORATION** — candidates that have never been allocated a market. Not
  ranked at all: ranking them would reintroduce the same circularity, so they
  are taken round-robin across families, oldest first.
* **NEAR MISS** — candidates showing OOS signal but underpowered. These are
  the cheapest possible source of a real strategy, because most of the
  evidence already exists and the blocker is usually breadth.
* **EXPLOITATION** — priority-ordered, exactly as before.

The exploration reserve is a floor, not a quota: if there are fewer untested
candidates than the reserve allows, the remainder flows to exploitation rather
than going unspent.

This module decides only WHAT IS RESEARCHED NEXT. It is deliberately incapable
of touching status, evidence, or validation (§17) — nothing here returns
anything but an ordering.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

# Share of each cycle's slots reserved for candidates that have never been
# allocated a single market.
DEFAULT_EXPLORE_FRACTION = 0.40

# ...and for near misses: real signal, not enough of it.
DEFAULT_NEAR_MISS_FRACTION = 0.15


@dataclass
class Allocatable:
    """One candidate, as the allocator needs to see it."""

    id: str
    family: str
    priority: float = 0.0
    maturity: str = ""
    attempts: int = 0           # markets ever allocated, evidence or not
    created_ts: float = 0.0

    @property
    def never_tested(self) -> bool:
        return self.attempts <= 0

    @property
    def near_miss(self) -> bool:
        return self.maturity == "NEAR_MISS"


@dataclass
class Allocation:
    """The chosen slate, and where each slot went — so the split is
    auditable rather than asserted (§23's DISCOVERY SOURCES block)."""

    chosen: list[Allocatable]
    explored: int = 0
    near_missed: int = 0
    exploited: int = 0
    skipped_family_cap: int = 0

    @property
    def ids(self) -> list[str]:
        return [c.id for c in self.chosen]

    def summary(self) -> dict:
        return {
            "allocatedTotal": len(self.chosen),
            "allocatedExploration": self.explored,
            "allocatedNearMiss": self.near_missed,
            "allocatedExploitation": self.exploited,
            "allocationSkippedFamilyCap": self.skipped_family_cap,
        }


def allocate(candidates: Iterable[Allocatable], slots: int,
             per_family_cap: int = 6,
             explore_fraction: float = DEFAULT_EXPLORE_FRACTION,
             near_miss_fraction: float = DEFAULT_NEAR_MISS_FRACTION
             ) -> Allocation:
    """Choose this cycle's slate. Pure: no I/O, no state, fully testable."""
    pool = [c for c in candidates]
    slots = max(0, int(slots))
    result = Allocation(chosen=[])
    if not pool or not slots:
        return result

    taken: set[str] = set()
    per_family: dict[str, int] = {}

    def _take(candidate: Allocatable) -> bool:
        if candidate.id in taken:
            return False
        if per_family_cap > 0 and \
                per_family.get(candidate.family, 0) >= per_family_cap:
            result.skipped_family_cap += 1
            return False
        per_family[candidate.family] = per_family.get(candidate.family, 0) + 1
        taken.add(candidate.id)
        result.chosen.append(candidate)
        return True

    # 1. EXPLORATION. Round-robin across families, oldest first inside each,
    # so a family that discovered forty variants in one pass cannot swallow
    # the reserve that exists to give every family a first look.
    explore_slots = int(slots * max(0.0, explore_fraction))
    untested_by_family: dict[str, list[Allocatable]] = {}
    for candidate in sorted((c for c in pool if c.never_tested),
                            key=lambda c: c.created_ts):
        untested_by_family.setdefault(candidate.family, []).append(candidate)
    while explore_slots > 0 and untested_by_family:
        for family in list(untested_by_family):
            if explore_slots <= 0:
                break
            queue = untested_by_family[family]
            if not queue:
                del untested_by_family[family]
                continue
            candidate = queue.pop(0)
            if _take(candidate):
                explore_slots -= 1
                result.explored += 1

    # 2. NEAR MISSES, most promising first. Already carrying evidence, so
    # ranking them does not have the circularity problem exploration does.
    near_slots = int(slots * max(0.0, near_miss_fraction))
    for candidate in sorted((c for c in pool
                             if c.near_miss and c.id not in taken),
                            key=lambda c: -c.priority):
        if near_slots <= 0 or len(result.chosen) >= slots:
            break
        if _take(candidate):
            near_slots -= 1
            result.near_missed += 1

    # 3. EXPLOITATION takes what is left — including any exploration or
    # near-miss slots that found no takers. A reserve is a floor on attention,
    # never a hold on unused compute.
    for candidate in sorted((c for c in pool if c.id not in taken),
                            key=lambda c: -c.priority):
        if len(result.chosen) >= slots:
            break
        if _take(candidate):
            result.exploited += 1

    return result


def from_library(library, config, slots: int,
                 priorities: Optional[dict[str, float]] = None,
                 maturities: Optional[dict[str, str]] = None) -> Allocation:
    """Build the slate from library state. The adapter; `allocate` is the
    decision, and it stays free of storage concerns."""
    from .research import family_of

    attempts = library.attempt_summaries()
    pool: list[Allocatable] = []
    for row in library.evaluable():
        seen = attempts.get(row["id"]) or {}
        pool.append(Allocatable(
            id=row["id"],
            family=(str(row.get("family") or "")
                    or family_of(row.get("rule") or {})),
            priority=float((priorities or {}).get(row["id"], 0.0)),
            maturity=str((maturities or {}).get(row["id"], "")),
            attempts=int(seen.get("evidence", 0)) + int(seen.get("zeroTrades", 0))
            + int(seen.get("errors", 0)),
            created_ts=float(row.get("created_ts") or 0.0)))
    return allocate(
        pool, slots,
        per_family_cap=int(getattr(config, "oos_family_slots", 6)),
        explore_fraction=float(getattr(config, "oos_explore_fraction",
                                       DEFAULT_EXPLORE_FRACTION)),
        near_miss_fraction=float(getattr(config, "oos_near_miss_fraction",
                                         DEFAULT_NEAR_MISS_FRACTION)))
