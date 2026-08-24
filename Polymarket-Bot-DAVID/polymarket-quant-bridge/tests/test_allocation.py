"""Research allocation must explore, not only exploit.

The audit: 153 of 231 candidates had never received a single validation
evaluation. Not because anyone decided they were bad — because allocation was a
straight sort on research priority, and priority is built from evidence a
candidate has already collected. No evidence, low priority, never allocated,
never any evidence. These tests pin the reserve that breaks that loop.
"""

from __future__ import annotations

from pqb.allocation import Allocatable, allocate


def _c(cid, family="f", priority=0.0, maturity="", attempts=0, created=0.0):
    return Allocatable(id=cid, family=family, priority=priority,
                       maturity=maturity, attempts=attempts,
                       created_ts=created)


def test_untested_candidates_actually_receive_research():
    """Requirement 11. Under a pure priority sort the ten untested candidates
    below never get a slot, because they have no evidence to be ranked on."""
    established = [_c(f"old{i}", family=f"f{i}", priority=0.9, attempts=20)
                   for i in range(10)]
    untested = [_c(f"new{i}", family=f"g{i}", priority=0.0, attempts=0,
                   created=float(i)) for i in range(10)]

    result = allocate(established + untested, slots=10,
                      explore_fraction=0.4, near_miss_fraction=0.0)

    assert result.explored == 4                 # the reserved 40%
    assert len(result.chosen) == 10
    chosen = set(result.ids)
    assert len([i for i in chosen if i.startswith("new")]) == 4
    assert len([i for i in chosen if i.startswith("old")]) == 6


def test_promising_candidates_receive_additional_allocation():
    """Requirement 12. Near misses have real signal and are usually one
    market short — the cheapest available route to a real strategy."""
    noise = [_c(f"n{i}", family=f"f{i}", priority=0.1, attempts=5)
             for i in range(20)]
    near = [_c(f"near{i}", family=f"m{i}", priority=0.5,
               maturity="NEAR_MISS", attempts=5) for i in range(4)]

    result = allocate(noise + near, slots=10, explore_fraction=0.0,
                      near_miss_fraction=0.15)

    assert result.near_missed == 1
    assert any(i.startswith("near") for i in result.ids)


def test_positive_candidates_cannot_consume_the_entire_budget():
    """§13's headline. Twenty strong incumbents, one untested newcomer: the
    newcomer still gets looked at."""
    strong = [_c(f"s{i}", family="winner", priority=1.0, attempts=50)
              for i in range(20)]
    newcomer = _c("fresh", family="unknown", priority=0.0, attempts=0)

    result = allocate(strong + [newcomer], slots=8)

    assert "fresh" in result.ids


def test_no_family_owns_the_slate():
    """Diversity is enforced against the family, so forty variants of one idea
    discovered in one pass cannot crowd out every other line of research."""
    crowd = [_c(f"c{i}", family="one-idea", priority=1.0, attempts=9)
             for i in range(30)]
    others = [_c(f"o{i}", family=f"idea{i}", priority=0.5, attempts=9)
              for i in range(5)]

    result = allocate(crowd + others, slots=12, per_family_cap=3,
                      explore_fraction=0.0, near_miss_fraction=0.0)

    families = [c.family for c in result.chosen]
    assert families.count("one-idea") == 3
    assert result.skipped_family_cap > 0


def test_an_unfilled_reserve_flows_to_exploitation():
    """A reserve is a floor on attention, never a hold on unused compute."""
    established = [_c(f"e{i}", family=f"f{i}", priority=0.9, attempts=11)
                   for i in range(10)]

    result = allocate(established, slots=10, explore_fraction=0.5,
                      near_miss_fraction=0.2)

    assert result.explored == 0 and result.near_missed == 0
    assert result.exploited == 10               # nothing left idle


def test_exploration_is_round_robin_across_families_not_ranked():
    """Ranking the untested would reintroduce the circularity the reserve
    exists to break; taking them in discovery order would let one prolific
    family swallow the whole reserve."""
    prolific = [_c(f"p{i}", family="prolific", attempts=0, created=float(i))
                for i in range(10)]
    lonely = [_c("lonely", family="rare", attempts=0, created=99.0)]

    result = allocate(prolific + lonely, slots=4, explore_fraction=1.0,
                      near_miss_fraction=0.0, per_family_cap=6)

    assert "lonely" in result.ids               # despite being discovered last
    assert result.explored == 4


def test_allocation_decides_research_order_and_nothing_else():
    """§17: priority determines what gets researched next. It must never
    determine what is validated — the allocator returns an ordering and has no
    other output at all."""
    result = allocate([_c("a", priority=1.0), _c("b", priority=0.1)], slots=2)

    assert set(result.summary()) == {
        "allocatedTotal", "allocatedExploration", "allocatedNearMiss",
        "allocatedExploitation", "allocationSkippedFamilyCap"}
    assert not hasattr(result, "status")
    assert not hasattr(result, "validated")
