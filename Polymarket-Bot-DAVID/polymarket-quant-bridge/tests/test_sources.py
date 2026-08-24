"""Discovery is plural. Validation is singular.

Every engine registers into the same library, takes evidence from the same
table and is promoted by the same ladder — that was already true and these
tests hold it true. What is new is the SOURCE label, without which "which
discovery engines produce durable candidates?" cannot be asked at all: a
sequence chain and the inverse of a sequence chain replay identically and are
the same rule `type`, and telling them apart is the entire point of Engine D.
"""

from __future__ import annotations

import pytest

from pqb.library import StrategyLibrary
from pqb.sources import (SOURCE_CONVERGENT, SOURCE_INVERSE_ADVERSARIAL,
                         SOURCE_QUANT, SOURCE_SEQUENCE_STATE,
                         SOURCE_WALLET_BEHAVIOR, CandidateSpec, rule_complexity,
                         source_census, source_of)


def test_every_rule_kind_maps_to_a_discovery_source():
    assert source_of({"type": "threshold"}) == SOURCE_QUANT
    assert source_of({"type": "longshot"}) == SOURCE_QUANT
    assert source_of({"type": "sequence"}) == SOURCE_SEQUENCE_STATE
    assert source_of({"type": "sharp_move"}) == SOURCE_SEQUENCE_STATE
    assert source_of({"type": "wallet_state"}) == SOURCE_WALLET_BEHAVIOR
    assert source_of({"type": "wallet_behavior"}) == SOURCE_WALLET_BEHAVIOR


def test_a_variant_reports_its_own_source_not_its_parents():
    """Requirement 16: inverse candidates receive independent identities.
    Reporting an inverse under its parent's source would hide the exact
    comparison Engine D exists to make."""
    parent = {"type": "sequence", "chain": ["a", "b"], "direction": "up"}
    inverse = dict(parent, direction="down", variant="inverse",
                   variant_of="seq|a|b|up#v1")

    assert source_of(parent) == SOURCE_SEQUENCE_STATE
    assert source_of(inverse) == SOURCE_INVERSE_ADVERSARIAL


def test_convergence_is_a_label_and_never_overrides_adversarial():
    assert source_of({"type": "threshold",
                      "convergent_with": ["x"]}) == SOURCE_CONVERGENT
    # Both at once: the adversarial provenance is the more specific fact.
    assert source_of({"type": "threshold", "variant": "inverse",
                      "convergent_with": ["x"]}) == SOURCE_INVERSE_ADVERSARIAL


def test_the_source_is_persisted_and_never_silently_rewritten(tmp_path):
    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    sid = lib.upsert_candidate("sig", {"type": "sequence", "chain": ["a"]},
                               "r")
    rows = {r["id"]: r for r in lib.all_strategies()}
    assert rows[sid]["source"] == SOURCE_SEQUENCE_STATE

    # Re-discovery must not relabel a candidate's provenance.
    lib.upsert_candidate("sig", {"type": "sequence", "chain": ["a"]}, "r",
                         source=SOURCE_QUANT)
    rows = {r["id"]: r for r in lib.all_strategies()}
    assert rows[sid]["source"] == SOURCE_SEQUENCE_STATE


def test_a_derived_candidate_records_its_parent(tmp_path):
    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    parent = lib.upsert_candidate("p", {"type": "sequence", "chain": ["a"],
                                        "direction": "up"}, "parent")
    child = lib.upsert_candidate(
        "c", {"type": "sequence", "chain": ["a"], "direction": "down",
              "variant": "inverse"}, "inverse",
        source=SOURCE_INVERSE_ADVERSARIAL, parent_id=parent)

    rows = {r["id"]: r for r in lib.all_strategies()}
    assert rows[child]["parent_id"] == parent
    assert rows[child]["source"] == SOURCE_INVERSE_ADVERSARIAL
    # Independent identity means independent evidence: nothing inherited.
    assert lib.cumulative(child)["markets"] == 0


def test_every_engine_uses_the_one_registry(tmp_path):
    """Requirements 17, 18, 19. There is no second way in — `CandidateSpec`
    registers through `upsert_candidate` and nothing else."""
    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    specs = [
        CandidateSpec("q", {"type": "threshold", "entry_feature": "price"},
                      "quant", source=SOURCE_QUANT),
        CandidateSpec("s", {"type": "sequence", "chain": ["a", "b"]},
                      "sequence", source=SOURCE_SEQUENCE_STATE),
        CandidateSpec("w", {"type": "wallet_behavior", "direction": "long"},
                      "wallet", source=SOURCE_WALLET_BEHAVIOR,
                      source_markets={"SRC1"}),
        CandidateSpec("i", {"type": "sequence", "chain": ["a", "b"],
                            "variant": "inverse"}, "inverse",
                      source=SOURCE_INVERSE_ADVERSARIAL),
    ]
    ids = [spec.register(lib) for spec in specs]

    assert len(set(ids)) == 4
    # All four land in the SAME table, with the same empty starting record.
    assert {r["id"] for r in lib.all_strategies()} == set(ids)
    for sid in ids:
        assert lib.cumulative(sid)["markets"] == 0
        assert lib.cumulative(sid)["trades"] == 0
    # A wallet-derived rule's SOURCE markets are permanently excluded: the
    # wallet may generate the hypothesis, never testify for it.
    wallet_id = ids[2]
    assert "SRC1" in lib.excluded_markets(wallet_id)


def test_complexity_counts_the_moving_parts():
    """§9 of the second directive. Complexity is a research-quality metric —
    a complicated rule must earn it with independent results."""
    simple = {"type": "threshold", "entry_feature": "price",
              "entry_op": ">", "direction": "long"}
    complicated = dict(simple, filter_feature="volume", filter_op="<",
                       hold_bars=15, delay_bars=2)

    assert rule_complexity(complicated) > rule_complexity(simple)
    assert rule_complexity({}) == 0
    assert CandidateSpec("s", simple).complexity == rule_complexity(simple)


def test_source_census_reports_tested_and_surviving_per_engine():
    rows = [
        {"source": SOURCE_QUANT, "status": "validated", "oos_trades": 40},
        {"source": SOURCE_QUANT, "status": "new", "oos_trades": 0},
        {"source": SOURCE_SEQUENCE_STATE, "status": "rejected",
         "oos_trades": 12},
        {"source": SOURCE_INVERSE_ADVERSARIAL, "status": "validating",
         "oos_trades": 5},
    ]
    census = source_census(rows)

    assert census[SOURCE_QUANT] == {"candidates": 2, "tested": 1,
                                    "surviving": 1, "rejected": 0}
    assert census[SOURCE_SEQUENCE_STATE]["rejected"] == 1
    assert census[SOURCE_INVERSE_ADVERSARIAL]["surviving"] == 1
    # Engines with nothing yet are present with zeros, not missing — an
    # absent engine and an engine that has found nothing are different.
    assert census[SOURCE_WALLET_BEHAVIOR]["candidates"] == 0


def test_a_high_score_still_cannot_validate_anything(tmp_path):
    """§12. In-sample brilliance is recorded for overfit measurement and is
    incapable of moving status."""
    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    sid = CandidateSpec("s", {"type": "threshold", "entry_feature": "price"},
                        "spectacular", in_score=0.99, in_win=1.0,
                        in_sharpe=9.9).register(lib)

    row = next(r for r in lib.all_strategies() if r["id"] == sid)
    assert row["status"] == "new"
    assert lib.cumulative(sid)["markets"] == 0
