"""Discovery and validation must operate on the same feature space.

The audit found 42 of 121 historical feature columns effectively constant and
38 threshold rules depending on features unavailable in meaningful validation
data. A rule discovered on a column that is pinned to a constant during
validation cannot fire there — so it is never tested, never rejected, and
reports as merely untested forever. That is the most misleading failure mode
in the system, and these tests close it.
"""

from __future__ import annotations

import csv

from pqb.feature_domain import (MIN_COVERAGE, QUARANTINE_REASON,
                                QUARANTINE_STATUS, FeatureDomain, build_domain,
                                features_of, quarantine_incompatible)
from pqb.library import StrategyLibrary


def _series(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _pool(tmp_path, n_series=3, rows=60):
    """Series shaped like the historical export: price moves, book pinned."""
    paths = []
    for s in range(n_series):
        data = []
        for i in range(rows):
            price = 0.40 + (i + s) * 0.002
            data.append({
                "ts": 1_700_000_000 + i * 60,
                "price": price,
                # The pinned book: bid == ask == price, no depth. Exactly what
                # `history_series` writes, because no historical book exists.
                "spread": 0.0, "depth_bid": 0.0, "quote_is_live": 0.0,
                "tape_trades": 3.0 + (i % 5),
            })
        paths.append(_series(tmp_path / f"s{s}" / "features.csv", data,
                             data[0].keys()))
    return paths


def test_constant_validation_columns_are_detected(tmp_path):
    domain = build_domain(_pool(tmp_path), live_features=["price", "spread"])

    assert domain.usable("price")            # genuinely varies
    assert domain.usable("tape_trades")
    assert not domain.usable("spread")       # pinned to zero, every series
    assert not domain.usable("depth_bid")
    assert not domain.usable("quote_is_live")
    assert set(domain.constant_columns()) >= {"spread", "depth_bid",
                                              "quote_is_live"}
    assert domain.series_sampled == 3


def test_a_rule_on_a_constant_column_cannot_be_registered(tmp_path):
    """Requirement 6: historical-incompatible features cannot create valid
    candidates."""
    domain = build_domain(_pool(tmp_path), live_features=["price", "spread"])

    good = {"type": "threshold", "entry_feature": "price", "entry_op": ">"}
    bad = {"type": "threshold", "entry_feature": "spread", "entry_op": ">"}
    mixed = {"type": "threshold", "entry_feature": "price",
             "filter_feature": "depth_bid"}

    assert domain.admits(good) == (True, [])
    admitted, problems = domain.admits(bad)
    assert not admitted and "spread" in problems[0] and "constant" in problems[0]
    assert not domain.admits(mixed)[0]


def test_tape_based_rules_declare_no_frame_dependencies():
    """Sequences, longshots and wallet rules replay against raw tapes with
    their own mechanics — they never ask the feature frame anything, so the
    gate must not quarantine them."""
    for kind in ("sequence", "longshot", "wallet_state", "wallet_behavior",
                 "sharp_move"):
        assert features_of({"type": kind, "entry_feature": "spread"}) == set()

    assert features_of({"type": "threshold", "entry_feature": "a",
                        "filter_feature": "b"}) == {"a", "b"}


def test_incompatible_candidates_are_quarantined_never_deleted(tmp_path):
    """§28: preserve the record, including the mistakes. A quarantined row
    keeps its history, its version and a permanent explanation."""
    domain = build_domain(_pool(tmp_path), live_features=["price", "spread"])
    lib = StrategyLibrary(tmp_path / "library.sqlite3")

    keep = lib.upsert_candidate(
        "keep", {"type": "threshold", "entry_feature": "price"}, "good")
    drop = lib.upsert_candidate(
        "drop", {"type": "threshold", "entry_feature": "spread"}, "bad")
    lib.record_validation(drop, "M1", trades=4, wins=2, pnl=0.1, drawdown=0.0)

    moved = quarantine_incompatible(lib, domain)

    assert [mid for mid, _reason in moved] == [drop]
    rows = {r["id"]: r for r in lib.all_strategies()}
    assert rows[keep]["status"] == "new"
    assert rows[drop]["status"] == QUARANTINE_STATUS
    assert QUARANTINE_REASON in rows[drop]["retired_reason"]
    assert "spread" in rows[drop]["retired_reason"]
    # Nothing deleted: the evidence it did collect is still there.
    assert lib.cumulative(drop)["markets"] == 1
    # ...and it no longer consumes replay budget.
    assert drop not in {r["id"] for r in lib.evaluable()}


def test_first_run_with_no_validation_series_is_permissive(tmp_path):
    """Nothing to measure yet is not the same as 'nothing is valid'. Failing
    closed on a missing measurement would stop discovery on a fresh install."""
    empty = FeatureDomain(permissive=True)
    assert empty.admits({"type": "threshold",
                         "entry_feature": "anything"}) == (True, [])
    assert empty.summary()["featureDomainPermissive"] is True


def test_domain_round_trips_through_disk(tmp_path):
    domain = build_domain(_pool(tmp_path), live_features=["price"])
    path = tmp_path / "feature-domain.json"
    domain.save(path)

    loaded = FeatureDomain.load(path)
    assert loaded is not None
    assert loaded.usable("price")
    assert not loaded.usable("spread")
    assert loaded.features["spread"].coverage >= MIN_COVERAGE
    assert loaded.features["spread"].historical_available
    assert not loaded.features["spread"].oos_available


# -- engineered features ------------------------------------------------------

def test_a_derived_feature_is_judged_by_the_column_it_comes_from(tmp_path):
    """The bug this closes cost two thirds of the library.

    A bridge-path rule is NOT replayed against the columns on disk:
    `research._oos_context` runs the bridge's feature engineer over them
    first, and the frame the rule meets carries ~988 engineered columns
    against ~121 raw ones. Asking whether the CSV has a literal `price_accel`
    column therefore answered a question the replay never asks — and answered
    it "absent" for 159 of 235 real candidates whose features were available
    the whole time.
    """
    domain = build_domain(_pool(tmp_path), live_features=["price"])

    # `price` varies, so everything the engineer builds from it can vary.
    assert domain.usable("price_accel")
    assert domain.usable("price_vel")
    assert domain.usable("price_z")
    assert domain.resolve("price_accel") == ("price", "derived")


def test_a_derived_feature_of_a_pinned_column_is_still_refused(tmp_path):
    """The other half, and the one that makes the fix safe rather than
    permissive: the velocity of a column that never moves is a column that
    never moves. Resolving to the base must not become admitting everything.
    """
    domain = build_domain(_pool(tmp_path), live_features=["price"])

    assert not domain.usable("spread_vel")
    assert not domain.usable("depth_bid_accel")
    problems = domain.unusable(["spread_vel"])
    # The BASE column is named, because that is what has to be fixed.
    assert "derived from spread" in problems[0]
    assert "constant" in problems[0]


def test_the_longest_prefix_wins(tmp_path):
    """`tape_trades_vel` must resolve to `tape_trades`, not to `tape`. A
    shorter prefix would silently judge a feature by a different column."""
    domain = build_domain(_pool(tmp_path), live_features=["price"])
    assert domain.resolve("tape_trades_vel") == ("tape_trades", "derived")


def test_a_global_feature_falls_back_to_price(tmp_path):
    """px_momentum_20 and regime_breakout map to no single base column — the
    engineer derives them from the price series, which `required_columns`
    feeds unconditionally."""
    domain = build_domain(_pool(tmp_path), live_features=["price"])
    column, how = domain.resolve("regime_breakout")
    assert (column, how) == ("price", "global")
    assert domain.usable("regime_breakout")


# -- release ------------------------------------------------------------------

def test_a_quarantined_candidate_is_released_when_the_gate_was_wrong(tmp_path):
    """Quarantine describes the DATA, not the rule, so it has to be
    revisitable. Without a release branch the loop skips anything already
    quarantined, which makes every gate bug permanent — and this gate had
    one."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    try:
        rule = {"type": "threshold", "entry_feature": "price_accel",
                "entry_op": ">", "entry_threshold": 0.1}
        cid = library.upsert_candidate("t", rule, "derived-feature rule")
        library.set_status(cid, QUARANTINE_STATUS,
                           f"{QUARANTINE_REASON}: price_accel: absent")

        domain = build_domain(_pool(tmp_path), live_features=["price"])
        quarantine_incompatible(library, domain)

        row = next(r for r in library.all_strategies() if r["id"] == cid)
        assert row["status"] == "new"
    finally:
        library.close()


def test_release_does_not_hand_back_a_trading_status(tmp_path):
    """Released at `new`, never at whatever it held before. The evidence rows
    survive quarantine untouched, so the ladder re-derives the real standing
    — but a data-availability accident must not restore a status that no
    evidence re-earned."""
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    try:
        rule = {"type": "threshold", "entry_feature": "price_vel",
                "entry_op": ">", "entry_threshold": 0.1}
        cid = library.upsert_candidate("v", rule, "was validated once")
        library.set_status(cid, "validated", "earned it")
        library.set_status(cid, QUARANTINE_STATUS, "gate said no")

        quarantine_incompatible(
            library, build_domain(_pool(tmp_path), live_features=["price"]))

        row = next(r for r in library.all_strategies() if r["id"] == cid)
        assert row["status"] == "new"
        assert row["status"] != "validated"
    finally:
        library.close()


def test_a_genuinely_unavailable_rule_stays_quarantined(tmp_path):
    library = StrategyLibrary(tmp_path / "library.sqlite3")
    try:
        rule = {"type": "threshold", "entry_feature": "spread_vel",
                "entry_op": ">", "entry_threshold": 0.1}
        cid = library.upsert_candidate("q", rule, "rule on a pinned column")
        library.set_status(cid, QUARANTINE_STATUS, "pinned")

        quarantine_incompatible(
            library, build_domain(_pool(tmp_path), live_features=["price"]))

        row = next(r for r in library.all_strategies() if r["id"] == cid)
        assert row["status"] == QUARANTINE_STATUS
    finally:
        library.close()
