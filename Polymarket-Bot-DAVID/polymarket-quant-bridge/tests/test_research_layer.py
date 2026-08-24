"""The autonomous research layer, where it meets the existing pipeline.

Three things are pinned here that no single module owns:

* the declared adversarial battery actually reaches the hypothesis records
  (it was declared and never executed, so every record read as "not attacked
  yet" and the convergence ranking scored everything at zero),
* a composite is a genuinely new candidate rather than a relabelled parent,
* a variant that no replay engine honours is not a second experiment.

The last one is the sharpest. `delay_bars` was written onto registered
DELAYED-ENTRY candidates and read by nothing, so the variant replayed
identically to its parent and its "independent" evidence was a copy of
evidence that already existed — a fabricated confirmation produced by the
adversarial machinery itself.
"""

from __future__ import annotations

from pqb.adversarial import (FAILED, INVERSE_WON, NOT_RUN, SURVIVED,
                             AdversarialReport)
from pqb.convergence import (COMPOSABLE, compose, composites,
                             fold_adversarial, run_pass)
from pqb.hypothesis import (ADVERSARIAL_TEST, REJECTED, SUPPORTED,
                            Hypothesis, HypothesisStore, Observation)
from pqb.library import StrategyLibrary
from pqb.research import directed_expansions
from pqb.sources import SOURCE_CONVERGENT


def _report(cid, **results):
    return AdversarialReport(
        candidate_id=cid, results=dict(results),
        details={k: f"measured {k}" for k in results},
        failure_states=[f"{k}: measured {k}" for k, v in results.items()
                        if v == FAILED])


# -- the fold -----------------------------------------------------------------

def test_one_candidates_failure_is_information_about_the_relationship():
    """Pessimistic on purpose. A relationship is a claim about the market:
    one candidate showing the claim breaks under attack says something about
    the claim, while one surviving says something only about that candidate.
    Averaging would let eight weak candidates launder a refutation."""
    reports = {"a": _report("a", market_subsets=SURVIVED),
               "b": _report("b", market_subsets=FAILED)}
    folded, _details, _states = fold_adversarial(["a", "b"], reports)
    assert folded["market_subsets"] == FAILED


def test_a_candidate_with_no_report_contributes_nothing_either_way():
    reports = {"a": _report("a", cost_stress=SURVIVED)}
    folded, _details, _states = fold_adversarial(["a", "missing"], reports)
    assert folded == {"cost_stress": SURVIVED}


def test_unrun_tests_do_not_enter_the_fold():
    """A test that could not be run is not a test that passed."""
    folded, _details, _states = fold_adversarial(
        ["a"], {"a": _report("a", placebo=NOT_RUN, cost_stress=SURVIVED)})
    assert "placebo" not in folded


def test_an_inverse_win_survives_the_fold_rather_than_being_smoothed_away():
    reports = {"a": _report("a", inverse=SURVIVED),
               "b": _report("b", inverse=INVERSE_WON)}
    folded, _details, _states = fold_adversarial(["a", "b"], reports)
    assert folded["inverse"] == INVERSE_WON


# -- composition --------------------------------------------------------------

def test_a_composite_only_imports_fields_a_replay_engine_reads():
    """A composite carrying a key nothing honours replays identically to its
    parent, registers as its own candidate, and then reports the parent's
    evidence as independent confirmation of the combination."""
    anchor = {"type": "sequence", "chain": ["a", "b"], "hold_bars": 10,
              "gap_bars": 5, "direction": "up"}
    partner = {"type": "sequence", "chain": ["c", "d"], "hold_bars": 40,
               "gap_bars": 5, "direction": "up", "regime": "calm"}
    composite = compose(anchor, partner)
    assert composite["hold_bars"] == 40          # timing imported
    assert composite["chain"] == ["a", "b"]      # entry NOT imported
    assert "regime" not in composite             # nothing reads it
    assert composite["composed_fields"] == ["hold_bars"]


def test_two_entry_conditions_are_never_anded_together():
    """The combination would have a much smaller sample, and the shrinking
    sample is invisible in the composite's own statistics."""
    for kind, fields in COMPOSABLE.items():
        assert "chain" not in fields
        assert "entry_feature" not in fields
        assert "entry_op" not in fields


def test_combining_a_rule_with_a_copy_of_itself_produces_nothing():
    rule = {"type": "sequence", "chain": ["a"], "hold_bars": 10}
    assert compose(rule, dict(rule)) is None


def test_rules_of_different_types_do_not_compose():
    assert compose({"type": "sequence", "hold_bars": 5},
                   {"type": "longshot", "side": "low"}) is None


def test_a_composite_excludes_both_parents_markets(tmp_path):
    """The load-bearing part. A composite validated on a market that
    suggested EITHER component would be proving a combination on the data
    that produced it, and only one component coming from there makes no
    difference to the leak."""
    supported = Hypothesis(
        signature="sigA", status=SUPPORTED, relationship="A",
        pattern=Observation(direction="long"), candidates=["a#v1"],
        markets={"M1", "M2"})
    partner = Hypothesis(
        signature="sigB", status=SUPPORTED, relationship="B",
        pattern=Observation(direction="long"), candidates=["b#v1"],
        markets={"M3"})
    rules = {"a#v1": {"type": "sequence", "chain": ["x"], "hold_bars": 10},
             "b#v1": {"type": "sequence", "chain": ["y"], "hold_bars": 60}}

    specs = composites(supported, [supported, partner], rules)
    assert len(specs) == 1
    assert specs[0].exclusions() == {"M1", "M2", "M3"}
    assert specs[0].source == SOURCE_CONVERGENT
    # Zero inherited evidence: registration confers nothing.
    assert specs[0].in_score == 0.0


def test_an_unsupported_hypothesis_composes_nothing():
    """Composition is for relationships that have already survived the
    battery, not a way around it."""
    weak = Hypothesis(signature="s", status=ADVERSARIAL_TEST,
                      candidates=["a#v1"], markets={"M1"})
    assert composites(weak, [weak], {"a#v1": {"type": "sequence"}}) == []


# -- the pass -----------------------------------------------------------------

def _view_row(cid, rule, markets, expectancy=0.05, trades=40, source="QUANT"):
    return {"id": cid, "rule": rule, "source": source, "family": "F",
            "markets": markets, "periods": ["2026-01 .. 2026-02"],
            "regimes": ["calm"], "oos_trades": trades,
            "oos_expectancy": expectancy}


def test_the_battery_reaches_the_hypothesis_records(tmp_path):
    """Before this, `adversarial` was empty on every record — so
    `convergence_priority` read every hypothesis as merely un-attacked and
    the whole battery was decoration."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    store = HypothesisStore(tmp_path / "hypotheses.sqlite3")
    try:
        rule = {"type": "sequence", "chain": ["drop", "settle"],
                "direction": "up", "prob_lo": 0.5, "prob_hi": 0.6,
                "hold_bars": 10}
        rows = [_view_row("a#v1", rule, ["M1", "M2"])]
        reports = {"a#v1": _report("a#v1", market_subsets=FAILED,
                                   cost_stress=SURVIVED,
                                   temporal_split=SURVIVED)}

        summary = run_pass(library, store, rows, reports=reports)

        assert summary["hypothesesAttackedThisPass"] == 1
        record = next(h for h in store.all() if h.adversarial)
        assert record.adversarial["market_subsets"]["result"] == FAILED
        # The reason travels with the verdict. §14: a decision read back in
        # six months must say which candidate broke and how, not just that
        # something did.
        assert "a#v1" in record.adversarial["market_subsets"]["detail"]
        assert record.failure_states
        # One decisive failure rejects; surviving only earns SUPPORTED, and
        # SUPPORTED is still not validation.
        assert record.status == REJECTED

        # ...and the transition is in the append-only log with its reason.
        assert any(e["to_status"] == REJECTED and "market_subsets" in
                   e["reason"] for e in store.history(record.id))
    finally:
        store.close()
        library.close()


def test_hypothesis_breadth_is_no_longer_structurally_zero(tmp_path):
    """`convergence_priority` is multiplicative with a breadth term, and the
    research pass was passing an empty market list — so every hypothesis
    scored exactly 0.0 and the convergence ranking ordered nothing."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    store = HypothesisStore(tmp_path / "hypotheses.sqlite3")
    try:
        rule = {"type": "sequence", "chain": ["drop", "settle"],
                "direction": "up", "prob_lo": 0.5, "prob_hi": 0.6}
        markets = [f"M{i}" for i in range(12)]
        run_pass(library, store,
                 [_view_row(f"c{i}#v1", rule, markets, source=src)
                  for i, src in enumerate(("QUANT", "SEQUENCE_STATE",
                                           "WALLET_BEHAVIOR"))])
        recorded = store.all()
        assert recorded
        assert max(h.independent_markets for h in recorded) == 12
    finally:
        store.close()
        library.close()


def test_the_pass_still_runs_with_no_reports_at_all(tmp_path):
    """Everything in this layer is additive. With the battery switched off
    the pass must behave exactly as it did before it existed."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    store = HypothesisStore(tmp_path / "hypotheses.sqlite3")
    try:
        summary = run_pass(library, store, [
            _view_row("a#v1", {"type": "sequence", "chain": ["x"],
                               "direction": "up"}, ["M1"])])
        assert summary["hypothesesAttackedThisPass"] == 0
        assert summary["compositesRegistered"] == 0
    finally:
        store.close()
        library.close()


# -- failure-directed variants ------------------------------------------------

def test_a_cost_failure_asks_for_a_longer_hold_not_a_retune():
    entry = {"id": "c#v1", "describe": "chain",
             "rule": {"type": "sequence", "chain": ["a", "b"],
                      "hold_bars": 10}}
    variants = directed_expansions(entry, "LENGTHEN_HOLD")
    assert len(variants) == 1
    rule, describe = variants[0]
    assert rule["hold_bars"] == 30
    assert rule["variant"] == "directed-longer-hold"
    assert rule["variant_of"] == "c#v1"
    assert "costs beat the move" in describe


def test_a_rule_that_never_fired_is_asked_a_simpler_question():
    """Shortening the chain both loosens the entry and REDUCES complexity,
    which is the direction complexity is supposed to move without evidence."""
    entry = {"id": "c#v1", "rule": {"type": "sequence",
                                    "chain": ["a", "b", "c", "d"]}}
    rule, _describe = directed_expansions(entry, "LOOSEN_ENTRY")[0]
    assert rule["chain"] == ["c", "d"]


def test_a_directive_with_no_applicable_variant_produces_nothing():
    entry = {"id": "c#v1", "rule": {"type": "longshot", "side": "low"}}
    assert directed_expansions(entry, "LENGTHEN_HOLD") == []
    assert directed_expansions({"id": "x", "rule": {"type": "sequence"}},
                               "") == []


# -- the variant that was not a variant ---------------------------------------

# -- the objective, end to end ------------------------------------------------

def test_a_prettier_headline_does_not_win_the_research_budget(tmp_path):
    """The whole point of the layer, exercised through the real components.

    Two candidates. The FAKE one has three times the headline expectancy —
    and all of it is one market, with every other market losing. The REAL one
    is modest and consistent across eight. A system that allocates on
    expectancy researches the fake one harder; a system that allocates on
    what would be LEARNED researches the real one, because the fake one's
    number has already been explained.

    This is also the reward-hacking guard in miniature: if the reward ever
    starts tracking "how good does this look", this test fails.
    """
    from pqb import adversarial as adv
    from pqb import experiments as exp
    from pqb import reward
    from pqb.config import Config
    from pqb.eligibility import (MarketEligibilityService, diversity_of,
                                 records_from_entries)

    cfg = Config()
    cfg.root = tmp_path
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    memory = exp.ExperimentStore(tmp_path / "experiments.sqlite3")
    try:
        real = library.upsert_candidate(
            "sigReal", {"type": "sequence", "chain": ["a", "b"],
                        "direction": "up", "hold_bars": 10}, "real",
            family="F1")
        fake = library.upsert_candidate(
            "sigFake", {"type": "sequence", "chain": ["c"],
                        "direction": "up", "hold_bars": 10}, "fake",
            family="F2")
        for i in range(8):
            library.record_validation(real, f"G{i}", trades=12, wins=8,
                                      pnl=0.6, drawdown=0.1,
                                      temporal_class="forward")
        library.record_validation(fake, "BIG", trades=20, wins=19, pnl=14.0,
                                  drawdown=0.2)
        for i in range(4):
            library.record_validation(fake, f"F{i}", trades=12, wins=4,
                                      pnl=-0.9, drawdown=0.5)

        rows = library.all_strategies()
        cumulative = {r["id"]: library.cumulative(r["id"]) for r in rows}
        # The fake one genuinely does look better by the headline number.
        assert cumulative[fake]["expectancy"] > cumulative[real]["expectancy"]

        reports = {r["id"]: adv.attack(
            r, cumulative[r["id"]], library.market_ledger(r["id"]),
            cfg.research,
            adv.siblings_of(r, rows, lambda i: cumulative[i]))
            for r in rows}
        assert reports[fake].verdict == "BROKEN"
        assert reports[real].verdict == "SURVIVED"

        entries = [{"market": f"G{i}", "token": f"t{i}",
                    "csv": str(tmp_path / "x.csv"), "firstTs": 1.0,
                    "lastTs": 2.0, "rows": 300, "category": "Sports"}
                   for i in range(8)]
        service = MarketEligibilityService(records_from_entries(entries),
                                           library)

        scores = {r["id"]: reward.score(
            r, cumulative[r["id"]], cfg.research,
            adversarial=reports[r["id"]],
            diversity=diversity_of(library, r["id"], cumulative[r["id"]],
                                   service),
            attempts={"evidence": cumulative[r["id"]]["markets"]})
            for r in rows}

        # ...and it still loses the budget.
        assert scores[real].score > scores[fake].score
        assert "survived" in scores[real].why_more
        assert any("concentrated" in p for p in scores[fake].penalties)

        # The failure is remembered as what it actually is: not refuted,
        # untested — so the follow-up asks for breadth rather than a retune.
        record = exp.from_candidate(
            next(r for r in rows if r["id"] == fake), cumulative[fake],
            cfg.research, adversarial=reports[fake])
        assert memory.record(record) is True
        assert record.failure_reason == exp.SINGLE_MARKET_DEPENDENCE
        assert record.directive == "NEEDS_BREADTH"

        # And neither candidate's STATUS was touched by any of it.
        after = {r["id"]: r["status"] for r in library.all_strategies()}
        assert after[fake] == after[real] == "new"
    finally:
        memory.close()
        library.close()


def _shocked_series(n: int = 400) -> list[dict]:
    """A series with periodic sharp drops, so an entry a few bars later
    demonstrably buys at a different price."""
    import random

    random.seed(7)
    rows, price = [], 0.50
    for i in range(n):
        if i % 40 == 10:
            price -= 0.05
        elif i % 40 == 12:
            price += 0.01
        else:
            price += random.uniform(-0.002, 0.003)
        price = min(0.9, max(0.1, price))
        rows.append({"ts": float(i * 60), "price": price, "spread": 0.01,
                     "volume": 100.0 + random.random() * 10,
                     "liquidity": 1000.0})
    return rows


def test_delayed_entry_now_changes_what_the_replay_does():
    """`delay_bars` was written onto registered DELAYED-ENTRY candidates and
    read by no replay engine. The variant therefore produced an identical
    result to its parent, and that duplicate was counted as an independent
    second experiment about entry timing — an adversarial test fabricating
    its own confirmation."""
    from pqb.analytics.sequences import frozen_replay

    rows = _shocked_series()
    base = {"type": "sequence", "chain": ["sharp_drop"], "direction": "up",
            "hold_bars": 5}
    delayed = dict(base, delay_bars=6)

    immediate = frozen_replay(rows, base, 0.0)
    later = frozen_replay(rows, delayed, 0.0)

    assert immediate["trades"] > 0 and later["trades"] > 0
    assert immediate["pnl"] != later["pnl"]
    # ...and specifically: this edge does NOT survive being entered late,
    # which is exactly the question the variant exists to ask.
    assert later["expectancy"] < immediate["expectancy"]


def test_a_zero_delay_rule_is_unchanged_by_the_fix():
    """Every mined chain carries no `delay_bars`. If reading the field moved
    their results, the fix would have silently invalidated the whole
    existing library's evidence."""
    from pqb.analytics.sequences import frozen_replay

    rows = _shocked_series()
    rule = {"type": "sequence", "chain": ["sharp_drop"], "direction": "up",
            "hold_bars": 5}
    assert frozen_replay(rows, rule, 0.0) == frozen_replay(
        rows, dict(rule, delay_bars=0), 0.0)


def test_sharp_move_replay_honours_delayed_entry_too():
    from pqb.analytics.sharp_moves import detect, frozen_replay

    rows = _shocked_series()
    events = detect(rows)
    assert events, "fixture must produce sharp moves to test against"
    template = events[0]
    rule = {"type": "sharp_move", "move_direction": template.direction,
            "price_region": template.price_region,
            "liquidity": "thin" if template.log_liquidity < 9.0 else "deep",
            "direction": "up", "hold_bars": 5}

    immediate = frozen_replay(rows, rule, 0.0)
    later = frozen_replay(rows, dict(rule, delay_bars=6), 0.0)
    assert immediate["trades"] > 0
    assert immediate["pnl"] != later["pnl"]


def test_composition_actually_fires_through_a_whole_pass(tmp_path):
    """The gap this test closes: every composition test above calls
    `composites` directly. Nothing proved the PASS could reach it, and on
    real data it never had — so the §10 path was unit-tested and
    production-unproven at the same time.

    Reaching it needs three things at once: two hypotheses similar enough to
    group, drawn from genuinely DIFFERENT sources so the group is
    independent, and an anchor that has already survived the battery. That
    conjunction is the design, not an obstacle to route around.
    """
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    store = HypothesisStore(tmp_path / "hypotheses.sqlite3")
    try:
        anchor_rule = {"type": "sequence", "chain": ["drop", "settle"],
                       "direction": "up", "prob_lo": 0.5, "prob_hi": 0.6,
                       "hold_bars": 10}
        # Same phenomenon, different HOLD, and — critically — a different
        # discovery source, which is what makes the group independent.
        partner_rule = dict(anchor_rule, hold_bars=60)
        rows = [
            _view_row("a#v1", anchor_rule, ["M1", "M2"], source="SEQUENCE"),
            _view_row("b#v1", partner_rule, ["M3", "M4"], source="WALLET"),
        ]
        # Both survive the battery, so both reach SUPPORTED.
        # Names from `convergence.ADVERSARIAL_TESTS`, which is a SHORTER list
        # than the candidate-level battery — and two of its eight are
        # `placebo` and `liquidity_stress`, which could not run at all until
        # the probe existed. A hypothesis needs three of these before the
        # fold will say anything other than "only N of the battery run", so
        # the two dormant tests were also holding SUPPORTED out of reach.
        clean = dict(market_subsets=SURVIVED, cost_stress=SURVIVED,
                     slippage_stress=SURVIVED, placebo=SURVIVED,
                     liquidity_stress=SURVIVED)
        reports = {"a#v1": _report("a#v1", **clean),
                   "b#v1": _report("b#v1", **clean)}

        before = len(library.all_strategies())
        summary = run_pass(library, store, rows, reports=reports)
        after = library.all_strategies()

        assert summary["compositesRegistered"] >= 1, summary
        composite = next(s for s in after
                         if str(s["id"]).startswith("comp|"))
        # It is a CANDIDATE, not a conclusion: zero evidence, and every
        # market either parent touched is permanently off limits to it.
        assert library.cumulative(composite["id"])["trades"] == 0
        assert composite["status"] == "new"
        assert len(after) == before + summary["compositesRegistered"]
    finally:
        store.close()
        library.close()


def test_a_single_source_group_composes_nothing(tmp_path):
    """Why real data registered zero composites, pinned so it reads as the
    design rather than as a bug. §10 combines INDEPENDENTLY discovered
    signals; two candidates found the same way are one signal wearing two
    identities, and composing them would manufacture a combination out of a
    single observation."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    store = HypothesisStore(tmp_path / "hypotheses.sqlite3")
    try:
        rule = {"type": "sequence", "chain": ["drop", "settle"],
                "direction": "up", "prob_lo": 0.5, "prob_hi": 0.6,
                "hold_bars": 10}
        rows = [
            _view_row("a#v1", rule, ["M1", "M2"], source="SEQUENCE_STATE"),
            _view_row("b#v1", dict(rule, hold_bars=60), ["M3"],
                      source="SEQUENCE_STATE"),
        ]
        clean = dict(market_subsets=SURVIVED, cost_stress=SURVIVED,
                     temporal_split=SURVIVED)
        summary = run_pass(library, store, rows,
                           reports={"a#v1": _report("a#v1", **clean),
                                    "b#v1": _report("b#v1", **clean)})
        assert summary["compositesRegistered"] == 0
    finally:
        store.close()
        library.close()
