"""Meta-discovery asks which research STRUCTURES survive, and only allocates.

The danger this layer carries is specific: it learns from the library's own
results, so if it were allowed to be confident on thin evidence, or to feed
anything but research priority, it would become a slow-motion overfit of the
entire research programme. These tests pin the three guards that stop that.
"""

from __future__ import annotations

from pqb import meta
from pqb.sources import (SOURCE_QUANT, SOURCE_SEQUENCE_STATE,
                         SOURCE_WALLET_BEHAVIOR)


def _row(cid, source=SOURCE_QUANT, rule=None, status="validating",
         trades=20, markets=5, expectancy=0.02, forward=0):
    return {"id": cid, "source": source, "family": "fam",
            "rule": rule or {"type": "threshold", "entry_feature": "price",
                             "entry_op": ">"},
            "status": status, "oos_trades": trades, "oos_markets": markets,
            "oos_expectancy": expectancy, "oos_forward_markets": forward}


def _population(engine, n, status, expectancy, start=0):
    return [_row(f"{engine}{i}", source=engine, status=status,
                 expectancy=expectancy) for i in range(start, start + n)]


# -- what it measures ---------------------------------------------------------

def test_a_candidate_belongs_to_several_structures_at_once():
    """The point: 'sequence discovery works, but only at length two' is a
    claim no single label could make."""
    structures = dict(meta.structures_of(
        {"source": SOURCE_SEQUENCE_STATE, "family": "sequence-event",
         "rule": {"type": "sequence", "chain": ["a", "b"], "hold_bars": 10,
                  "direction": "up"}}))

    assert structures["engine"] == SOURCE_SEQUENCE_STATE
    assert structures["sequence_length"] == "2"
    assert structures["holding_period"] == "short"
    assert structures["complexity"] in ("simple", "moderate", "complex")


def test_survival_rate_ignores_untested_candidates():
    """Counting untested candidates as failures would penalise whichever
    structure the allocator has not reached yet — and the allocator reads
    this, so that is a loop where being under-researched keeps you there."""
    rows = (_population(SOURCE_QUANT, 4, "validated", 0.03)
            + [_row(f"untested{i}", source=SOURCE_QUANT, status="new",
                    trades=0, markets=0, expectancy=0.0) for i in range(6)])
    records = meta.measure(rows)

    engine = records[f"engine={SOURCE_QUANT}"]
    assert engine.candidates == 10
    assert engine.tested == 4
    assert engine.survival_rate == 1.0        # 4 of 4 tested, not 4 of 10


# -- the guards ---------------------------------------------------------------

def test_a_thin_record_gets_no_opinion_at_all():
    """Structures are many and evidence is scarce. Without this the layer
    would mostly amplify noise, and amplified noise looks like insight."""
    rows = _population(SOURCE_QUANT, 3, "validated", 0.05)
    records = meta.measure(rows)
    table = meta.weights(records)

    assert records[f"engine={SOURCE_QUANT}"].has_standing is False
    assert table[f"engine={SOURCE_QUANT}"] == 1.0
    assert meta.summary(records, table)["metaHasOpinion"] is False


def test_a_structure_needs_market_breadth_not_just_candidates():
    """Four candidates all tested on one market know nothing."""
    narrow = [_row(f"n{i}", status="validated", markets=1)
              for i in range(8)]
    records = meta.measure(narrow)
    # 8 candidates x 1 market = 8 markets, exactly at the floor.
    assert records["engine=QUANT"].evidence_markets == 8
    assert records["engine=QUANT"].has_standing is True

    thinner = [_row(f"t{i}", status="validated", markets=0)
               for i in range(8)]
    assert meta.measure(thinner)["engine=QUANT"].has_standing is False


def test_weights_are_bounded_however_lopsided_the_record():
    """Meta-learning steers effort; it never decides. A structure that has
    won every single time still cannot take over the queue."""
    perfect = _population(SOURCE_QUANT, 30, "validated", 0.10)
    awful = _population(SOURCE_WALLET_BEHAVIOR, 30, "rejected", -0.10)
    table = meta.weights(meta.measure(perfect + awful))

    assert table[f"engine={SOURCE_QUANT}"] <= meta.WEIGHT_MAX
    assert table[f"engine={SOURCE_WALLET_BEHAVIOR}"] >= meta.WEIGHT_MIN
    assert table[f"engine={SOURCE_QUANT}"] > \
        table[f"engine={SOURCE_WALLET_BEHAVIOR}"]


def test_structures_are_judged_against_the_librarys_own_base_rate():
    """Judging against zero would mark every structure a winner in a library
    that mostly works — a statement about the library, not the structure."""
    # A library where everything survives: nothing stands out.
    uniform = (_population(SOURCE_QUANT, 20, "validated", 0.03)
               + _population(SOURCE_SEQUENCE_STATE, 20, "validated", 0.03))
    table = meta.weights(meta.measure(uniform))
    assert abs(table[f"engine={SOURCE_QUANT}"] - 1.0) < 0.05
    assert abs(table[f"engine={SOURCE_SEQUENCE_STATE}"] - 1.0) < 0.05


def test_a_candidates_weight_is_a_mean_not_a_product():
    """A candidate belongs to six or seven structures. Multiplying them would
    compound a mild preference into a landslide."""
    table = {f"engine={SOURCE_QUANT}": 1.5, "family=fam": 1.5,
             "complexity=simple": 1.5, "feature_family=price": 1.5}
    row = {"source": SOURCE_QUANT, "family": "fam",
           "rule": {"type": "threshold", "entry_feature": "price"}}

    combined = meta.weight_for(row, table)
    assert combined <= meta.WEIGHT_MAX
    assert combined < 1.5 ** 4                  # emphatically not the product


def test_an_unknown_structure_is_neutral():
    assert meta.weight_for({"source": "NEW_ENGINE", "rule": {}}, {}) == 1.0


# -- what it must never do ----------------------------------------------------

def test_meta_discovery_never_writes_to_the_record(tmp_path):
    """§10: do not use future validation results to rewrite historical
    validation results. This layer only reads."""
    from pqb.library import StrategyLibrary

    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    sid = lib.upsert_candidate("sig", {"type": "threshold",
                                       "entry_feature": "price"}, "r")
    lib.record_validation(sid, "M1", trades=10, wins=6, pnl=1.0, drawdown=0.1)
    before = lib.cumulative(sid)

    rows = [_row(sid)] + _population(SOURCE_QUANT, 20, "validated", 0.05)
    table = meta.weights(meta.measure(rows))
    meta.weight_for(rows[0], table)
    meta.report(meta.measure(rows), table)

    assert lib.cumulative(sid) == before
    row = next(r for r in lib.all_strategies() if r["id"] == sid)
    assert row["status"] == "new"           # nothing promoted, nothing moved


def test_the_report_says_nothing_yet_rather_than_pretending():
    """Until structures have records, the correct output is 'no opinion' —
    not a screen full of confident-looking 1.0s."""
    records = meta.measure(_population(SOURCE_QUANT, 2, "validated", 0.05))
    table = meta.weights(records)
    result = meta.summary(records, table)

    assert result["metaHasOpinion"] is False
    assert result["metaStructuresWithStanding"] == 0
    assert result["metaStrongest"] == ""


def test_report_ranks_structures_and_marks_which_ones_count():
    rows = (_population(SOURCE_QUANT, 20, "validated", 0.05)
            + _population(SOURCE_WALLET_BEHAVIOR, 20, "rejected", -0.05)
            + _population(SOURCE_SEQUENCE_STATE, 2, "validated", 0.05))
    records = meta.measure(rows)
    table = meta.weights(records)
    entries = meta.report(records, table, min_candidates=1)

    by_key = {e["structure"]: e for e in entries}
    assert by_key[f"engine={SOURCE_QUANT}"]["standing"] is True
    assert by_key[f"engine={SOURCE_SEQUENCE_STATE}"]["standing"] is False
    # Ordered strongest first.
    assert entries[0]["weight"] >= entries[-1]["weight"]
