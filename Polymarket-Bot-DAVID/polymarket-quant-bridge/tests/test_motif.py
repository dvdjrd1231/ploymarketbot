"""Family & motif intelligence: grouping, provenance, scoring, mutation.

The tests that matter most here are the NEGATIVE ones. A motif layer is easy
to make look clever and easy to make dishonest, and the two failure modes are
the same failure: counting one piece of evidence twice. So the first thing
pinned is that two candidates measured on the same markets are one
confirmation, and the last thing pinned is that no amount of family evidence
can move a candidate's status.
"""

from __future__ import annotations

import time

import pytest

from pqb import motif
from pqb.library import StrategyLibrary


def _row(cid, family="mean-reversion", rule=None, status="validating",
         created=0.0, signature=None):
    rule = rule or {"type": "sequence", "direction": "up", "hold_bars": 15,
                    "chain": ["a", "b", "c"]}
    return {"id": cid, "signature": signature or cid.split("#")[0],
            "family": family, "rule": rule, "status": status,
            "created_ts": created or time.time(), "source": "sequence",
            "in_win": 0.6}


def _ledger(markets, trades=10, pnl=1.0, ts=1_700_000_000.0, period=""):
    return [{"market_id": m, "ts": ts + i * 86_400, "period": period,
             "trades": trades, "wins": int(trades * 0.6), "pnl": pnl,
             "drawdown": 0.1} for i, m in enumerate(markets)]


# -- 1. grouping -------------------------------------------------------------


def test_structurally_equivalent_rules_land_on_one_motif():
    """Two differently NAMED rules with the same structure are one motif."""
    a = _row("sig-a#v1", rule={"type": "sequence", "direction": "up",
                               "hold_bars": 10, "chain": ["x", "y"],
                               "entry_feature": "flow_z_z"})
    b = _row("sig-b#v1", rule={"type": "sequence", "direction": "up",
                               "hold_bars": 12, "chain": ["p", "q"],
                               "entry_feature": "flow_z_mean"})
    assert motif.structural_signature(a) == motif.structural_signature(b)


def test_direction_vocabulary_is_normalised():
    up = {"type": "sequence", "direction": "up"}
    long_ = {"type": "wallet_behavior", "direction": "long"}
    high = {"type": "longshot", "side": "high"}
    assert motif._direction_of(up) == "long"
    assert motif._direction_of(long_) == "long"
    assert motif._direction_of(high) == "long"
    assert motif._direction_of({"direction": "short"}) == "short"


def test_evidence_shape_motif_is_recognised_not_hardcoded():
    """`2t/1m x 1v` is recognised as a SHAPE, and the single-market bucket is
    kept distinct — the recurrence in the operator's board is a thing to
    investigate, never a thing to reward."""
    assert motif.shape_of({"trades": 2, "markets": 1}, 1) == "1-2t/1mx1v"
    assert motif.shape_of({"trades": 3, "markets": 3}, 5) == "3-9t/3-5mx5v+"
    assert motif.shape_of({"trades": 0, "markets": 0}, 1) == "untested"


# -- 2. provenance: the whole point ------------------------------------------


def test_shared_markets_are_not_two_independent_confirmations():
    rows = [_row("a#v1", created=1), _row("b#v1", created=2)]
    ledgers = {"a#v1": _ledger(["m1", "m2", "m3"]),
               "b#v1": _ledger(["m1", "m2", "m3"])}
    records = motif.mine(rows, ledgers)
    record = records["family=mean-reversion"]
    assert len(record.candidates) == 2
    # Three markets, not six.
    assert len(record.markets) == 3
    # One confirmation, not two.
    assert len(record.independent_candidates()) == 1


def test_disjoint_markets_are_two_independent_confirmations():
    rows = [_row("a#v1", created=1), _row("b#v1", created=2)]
    ledgers = {"a#v1": _ledger(["m1", "m2"]),
               "b#v1": _ledger(["m3", "m4"])}
    records = motif.mine(rows, ledgers)
    record = records["family=mean-reversion"]
    assert len(record.markets) == 4
    assert len(record.independent_candidates()) == 2


def test_a_market_contributes_its_pnl_once():
    rows = [_row("a#v1", created=1), _row("b#v1", created=2)]
    ledgers = {"a#v1": _ledger(["m1"], trades=10, pnl=5.0),
               "b#v1": _ledger(["m1"], trades=10, pnl=5.0)}
    record = motif.mine(rows, ledgers)["family=mean-reversion"]
    assert record.trades == 10          # not 20
    assert record.pnl == pytest.approx(5.0)


def test_independence_walk_is_chronological_not_best_first():
    """The kept confirmation is the one that got there first, so the answer
    cannot be improved by preferring whichever candidate scored well."""
    rows = [_row("early#v1", created=1), _row("late#v1", created=2)]
    ledgers = {"early#v1": _ledger(["m1"], pnl=-9.0),
               "late#v1": _ledger(["m1"], pnl=+9.0)}
    record = motif.mine(rows, ledgers)["family=mean-reversion"]
    assert record.independent_candidates() == ["early#v1"]
    assert record.pnl == pytest.approx(-9.0)


# -- 3. standing and scoring -------------------------------------------------


def test_a_thin_motif_has_no_opinion():
    rows = [_row("a#v1", created=1)]
    records = motif.mine(rows, {"a#v1": _ledger(["m1"])})
    scored = motif.score_all(records)["family=mean-reversion"]
    assert not scored.score
    assert scored.weight == 1.0
    assert "no standing" in scored.why_deprioritised


def _wide_motif(n=5, positive=True, categories=("sports", "politics", "crypto")):
    rows, ledgers, cats = [], {}, {}
    for i in range(n):
        cid = f"c{i}#v1"
        rows.append(_row(cid, created=float(i)))
        markets = [f"m{i}-{j}" for j in range(3)]
        ledgers[cid] = _ledger(markets, trades=10,
                               pnl=2.0 if positive else -2.0,
                               ts=1_700_000_000.0 + i * 40 * 86_400)
        for j, market in enumerate(markets):
            cats[market] = categories[j % len(categories)]
    return rows, ledgers, cats


def test_a_broad_replicated_motif_earns_standing_and_an_explanation():
    rows, ledgers, cats = _wide_motif()
    records = motif.mine(rows, ledgers, market_categories=cats)
    record = records["family=mean-reversion"]
    assert record.has_standing
    assert record.replication_rate == 1.0
    scored = motif.score_all(records)["family=mean-reversion"]
    assert scored.score > 0
    # Weight is SHRUNK toward the library's own base rate. In a library where
    # everything replicates, nothing is distinguished — that is the intended
    # answer, not a missing bonus. Discrimination is pinned separately below.
    assert scored.weight >= 1.0
    assert "independent candidate" in scored.why_elevated
    # The explanation is generated from stored counts, so the counts it
    # states must be the counts in the record.
    assert str(len(record.markets)) in scored.why_elevated


def test_a_working_motif_outranks_a_failing_one_in_the_same_library():
    """The weight is comparative: it says which structure deserves the next
    unit of compute, not whether a structure is good in the abstract."""
    good_rows, good_ledgers, cats = _wide_motif(n=5)
    bad_rows, bad_ledgers, bad_cats = _wide_motif(n=5, positive=False)
    for row in bad_rows:
        row["id"] = "bad-" + row["id"]
        row["signature"] = "bad-" + row["signature"]
        row["family"] = "momentum"
        row["status"] = "rejected"
    bad_ledgers = {"bad-" + k: v for k, v in bad_ledgers.items()}
    bad_ledgers = {k: [dict(e, market_id="bad-" + e["market_id"]) for e in v]
                   for k, v in bad_ledgers.items()}
    cats.update({"bad-" + k: v for k, v in bad_cats.items()})
    records = motif.mine(good_rows + bad_rows, {**good_ledgers, **bad_ledgers},
                         market_categories=cats)
    scores = motif.score_all(records)
    assert scores["family=mean-reversion"].weight >         scores["family=momentum"].weight
    assert scores["family=mean-reversion"].score >         scores["family=momentum"].score


def test_weights_stay_inside_their_bound():
    rows, ledgers, cats = _wide_motif(n=12)
    scores = motif.score_all(motif.mine(rows, ledgers, market_categories=cats))
    for scored in scores.values():
        assert motif.WEIGHT_MIN <= scored.weight <= motif.WEIGHT_MAX


def test_candidate_weight_is_the_geometric_mean_and_bounded():
    rows, ledgers, cats = _wide_motif()
    scores = motif.score_all(motif.mine(rows, ledgers, market_categories=cats))
    weight = motif.weight_for(rows[0], scores, {"trades": 30, "markets": 3}, 1)
    assert motif.WEIGHT_MIN <= weight <= motif.WEIGHT_MAX


# -- 4. failure motifs -------------------------------------------------------


def test_recurring_failure_is_named_and_throttles_not_bans():
    rows, ledgers, cats = _wide_motif(n=6, positive=False)
    for row in rows:
        row["status"] = "rejected"
    records = motif.mine(rows, ledgers, market_categories=cats)
    scored = motif.score_all(records)["family=mean-reversion"]
    assert scored.failure_motif
    assert scored.weight <= 0.75
    # A throttle, not a ban: it stays in the queue.
    assert scored.weight >= motif.WEIGHT_MIN
    assert "deprioritised because" in scored.why_deprioritised


def test_single_market_dependence_is_a_failure_motif():
    rows, ledgers = [], {}
    for i in range(6):
        cid = f"c{i}#v1"
        rows.append(_row(cid, created=float(i)))
        ledgers[cid] = _ledger([f"m{i}"], trades=5, pnl=0.01)
    ledgers["c0#v1"] = _ledger(["m0"], trades=5, pnl=99.0)
    record = motif.mine(rows, ledgers)["family=mean-reversion"]
    assert motif.classify_failure(record) in (
        "DEPENDS_ON_ONE_MARKET", "SINGLE_CATEGORY_DEPENDENCE")


# -- 5. mutation -------------------------------------------------------------


def test_mutations_change_one_thing_at_a_time():
    row = _row("a#v1", rule={"type": "sequence", "direction": "up",
                             "hold_bars": 12, "gap_bars": 10,
                             "chain": ["a", "b", "c"]})
    produced = motif.mutations(row, budget=8)
    assert produced
    for rule, describe, tag in produced:
        differing = {k for k in set(rule) | set(row["rule"])
                     if rule.get(k) != row["rule"].get(k)}
        differing -= {"variant_of", "variant", "motif_mutation"}
        assert len(differing) == 1, (tag, differing)
        assert rule["variant_of"] == "a#v1"
        assert describe


def test_mutations_include_inverse_and_remove_one_dimension():
    row = _row("a#v1", rule={"type": "sequence", "direction": "up",
                             "hold_bars": 12, "chain": ["a", "b", "c"],
                             "regime": "calm"})
    tags = {tag for _r, _d, tag in motif.mutations(row, budget=12)}
    assert "motif-inverse" in tags
    assert "motif-drop-regime" in tags
    assert {"motif-shorter-hold", "motif-longer-hold"} <= tags
    assert any(t.startswith("motif-drop-") and "link" in t for t in tags)


def test_mutations_inherit_no_evidence():
    row = _row("a#v1")
    row["oos_trades"], row["oos_markets"] = 40, 9
    for rule, _d, _t in motif.mutations(row):
        assert "oos_trades" not in rule
        assert "oos_markets" not in rule
        assert "status" not in rule


def test_mutation_budget_is_respected():
    row = _row("a#v1", rule={"type": "sequence", "direction": "up",
                             "hold_bars": 12, "gap_bars": 10,
                             "chain": ["a", "b", "c"], "regime": "calm",
                             "category": "sports"})
    assert len(motif.mutations(row, budget=2)) == 2
    assert len(motif.mutations(row, budget=0)) == 0


# -- 6. search scale ---------------------------------------------------------


def test_search_scale_reports_the_denominator():
    rows, ledgers, cats = _wide_motif(n=6)
    records = motif.mine(rows, ledgers, market_categories=cats)
    scale = motif.search_scale(records, motif.score_all(records))
    payload = scale.to_dict()
    assert payload["motifsExamined"] >= payload["motifsWithStanding"]
    assert payload["motifsExamined"] > 1
    assert 0.0 <= payload["motifSurvivalShare"] <= 1.0


# -- 7. versioning and lineage ----------------------------------------------


def test_a_changed_definition_starts_a_new_version(tmp_path):
    store = motif.MotifStore(tmp_path / "motifs.sqlite3")
    v1 = store.version_for("family=x", {"dims": ["a", "b"]})
    again = store.version_for("family=x", {"dims": ["a", "b"]})
    v2 = store.version_for("family=x", {"dims": ["a", "b", "c"]})
    assert (v1, again, v2) == (1, 1, 2)
    store.close()


def test_history_is_append_only_and_keeps_old_conclusions(tmp_path):
    store = motif.MotifStore(tmp_path / "motifs.sqlite3")
    rows, ledgers, cats = _wide_motif()
    records = motif.mine(rows, ledgers, market_categories=cats)
    scores = motif.score_all(records)
    key = "family=mean-reversion"
    store.record(records[key], scores[key], 1)
    store.record(records[key], scores[key], 2)
    history = store.history(key)
    assert len(history) == 2
    assert {int(h["version"]) for h in history} == {1, 2}
    store.close()


def test_lineage_links_survive(tmp_path):
    store = motif.MotifStore(tmp_path / "motifs.sqlite3")
    store.link("family=x", "MUTATION", "sig#v2", "motif-longer-hold")
    store.link("family=x", "CANDIDATE", "sig#v1")
    assert len(store.links_from("family=x")) == 2
    assert store.lineage_of("sig#v2")[0]["kind"] == "MUTATION"
    store.close()


# -- 8. the hard rule: research only ----------------------------------------


def test_motif_layer_writes_nothing_to_the_library(tmp_path):
    """Mining a library must not change a single row in it."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    cid = library.upsert_candidate(
        "sig", {"type": "sequence", "direction": "up", "hold_bars": 5},
        "a rule", family="mean-reversion")
    library.record_validation(cid, "m1", 10, 6, 2.0, 0.1)
    before = library.all_strategies()[0]
    before_cum = library.cumulative(cid)

    records = motif.mine(library.all_strategies(), library.evidence_ledgers())
    motif.score_all(records)

    after = library.all_strategies()[0]
    assert after == before
    assert library.cumulative(cid) == before_cum
    library.close()


def test_family_evidence_never_becomes_candidate_evidence(tmp_path):
    """A new candidate in a strong motif starts at zero. All of §5."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    strong = []
    for i in range(5):
        cid = library.upsert_candidate(
            f"sig{i}", {"type": "sequence", "direction": "up",
                        "hold_bars": 5},
            f"rule {i}", family="mean-reversion")
        for j in range(3):
            library.record_validation(cid, f"m{i}-{j}", 20, 15, 4.0, 0.05)
        strong.append(cid)

    newcomer = library.upsert_candidate(
        "signew", {"type": "sequence", "direction": "up", "hold_bars": 5},
        "the newcomer", family="mean-reversion")

    records = motif.mine(library.all_strategies(), library.evidence_ledgers())
    scores = motif.score_all(records)
    weight = motif.weight_for(
        [r for r in library.all_strategies() if r["id"] == newcomer][0],
        scores, library.cumulative(newcomer), 1)

    # The motif is strong...
    assert scores["family=mean-reversion"].score > 0
    assert weight >= 1.0
    # ...and the newcomer still has nothing.
    cumulative = library.cumulative(newcomer)
    assert cumulative["trades"] == 0
    assert cumulative["markets"] == 0
    assert library.all_strategies()[-1]["status"] == "new"
    library.close()


def test_motif_weight_cannot_reach_a_validation_gate():
    """`reward.score` is where the weight lands, and reward is not an input to
    `library.next_status`. Pinned as an import-graph fact, not a comment."""
    import inspect

    from pqb import library as library_mod

    source = inspect.getsource(library_mod.next_status)
    assert "motif" not in source
    assert "reward" not in source
    assert "motif" not in inspect.getsource(library_mod.evidence_score)


def test_reward_motif_weight_is_bounded_steering(tmp_path):
    from types import SimpleNamespace

    from pqb import reward

    cfg = SimpleNamespace(oos_min_trades=30, oos_min_markets=3)
    entry = {"id": "a#v1", "status": "validating", "in_win": 0.5}
    cumulative = {"trades": 20, "markets": 3, "expectancy": 0.02,
                  "win_rate": 0.55, "top_share": 0.3}
    plain = reward.score(entry, cumulative, cfg)
    boosted = reward.score(entry, cumulative, cfg, motif_weight=1.6)
    damped = reward.score(entry, cumulative, cfg, motif_weight=0.6)
    assert boosted.score > plain.score > damped.score
    # Bounded: the whole score stays inside the reward's own ceiling.
    assert boosted.score <= reward.SCORE_MAX
    # An absurd weight cannot escape the clamp.
    absurd = reward.score(entry, cumulative, cfg, motif_weight=99.0)
    assert absurd.components["motif"] <= 2.0


def test_run_pass_proposes_only_from_motifs_with_standing(tmp_path):
    rows, ledgers, cats = _wide_motif(n=5)
    result = motif.run_pass(rows, ledgers, market_categories=cats,
                            store=motif.MotifStore(tmp_path / "m.sqlite3"),
                            mutation_budget=4)
    assert result.mutations
    assert len(result.mutations) <= 4
    parents = {str(p.get("id")) for p, _r, _d in result.mutations}
    # Only independent confirmations may parent a mutation.
    record = result.records["family=mean-reversion"]
    assert parents <= set(record.independent_candidates())


def test_the_view_row_carries_motif_fields_through_json():
    """strategies.json is the contract between the pass and both UIs. A field
    that survives `to_dict` but not `from_dict` shows as a silent zero."""
    from pqb.research import DiscoveredStrategy

    strategy = DiscoveredStrategy(rule={"type": "sequence"}, signature="s#v1")
    strategy.motif = "family=mean-reversion"
    strategy.motif_weight = 1.35
    strategy.family_research_score = 0.61
    strategy.family_replication = 0.75
    strategy.family_independent_markets = 14
    strategy.family_independent_candidates = 4
    strategy.family_failure_motif = ""
    strategy.why_family_elevated = "elevated because ..."
    strategy.why_family_deprioritised = ""
    back = DiscoveredStrategy.from_dict(strategy.to_dict())
    assert back.motif == strategy.motif
    assert back.motif_weight == pytest.approx(1.35)
    assert back.family_research_score == pytest.approx(0.61)
    assert back.family_independent_candidates == 4
    assert back.why_family_elevated == "elevated because ..."
    # And the thing that must NOT change: tradability still reads status.
    assert not back.tradable
    back.family_research_score = 1.0
    back.family_replication = 1.0
    assert not back.tradable


def test_a_registered_mutation_inherits_exclusions_and_no_evidence(tmp_path):
    """The registration contract §8 and §5 together, against a real library."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    parent_rule = {"type": "sequence", "direction": "up", "hold_bars": 12,
                   "chain": ["a", "b", "c"]}
    parent_id = library.upsert_candidate(
        "parent", parent_rule, "the parent", family="mean-reversion",
        discovery_markets={"m-discovery"})
    library.record_validation(parent_id, "m1", 30, 20, 6.0, 0.1)
    parent = [r for r in library.all_strategies() if r["id"] == parent_id][0]

    from pqb.research import family_of, signature_of

    rule, describe, _tag = motif.mutations(parent, budget=1)[0]
    mutant_id = library.upsert_candidate(
        signature_of(rule), rule, describe,
        discovery_markets=library.discovery_markets(parent_id),
        family=family_of(rule), parent_id=parent_id)

    assert mutant_id != parent_id
    mutant_cum = library.cumulative(mutant_id)
    assert mutant_cum["trades"] == 0 and mutant_cum["markets"] == 0
    # The parent's discovery contamination is inherited: the mutant may not be
    # validated on data that suggested its parent.
    assert "m-discovery" in library.discovery_markets(mutant_id)
    assert library.excluded_markets(mutant_id) >= {"m-discovery"}
    library.close()


def test_run_pass_on_a_thin_library_proposes_nothing():
    rows = [_row("a#v1", created=1)]
    result = motif.run_pass(rows, {"a#v1": _ledger(["m1"])})
    assert result.mutations == []
    assert result.summary()["motifsWithStanding"] == 0
