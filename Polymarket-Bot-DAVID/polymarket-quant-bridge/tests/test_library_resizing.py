"""Voiding evidence measured at the wrong account size, and re-opening the
verdicts that rested on it.

Two audit findings meet here:

* Fixing the sizing does not fix the RECORD. Cumulative evidence is summed
  across every market a strategy ever testified on and is never reset by
  design. Leave the old rows in and ~100x-scale losses keep being added to
  correctly-sized ones, so nothing can ever climb back to a positive
  cumulative expectancy and the sizing fix produces no visible effect.

* ``reopen_single_market_rejections`` only re-opened rejections carried by
  fewer than two markets. In the delivered library that matched **nothing**
  — all 146 rejections already had two or more markets behind them — so the
  re-open ran every pass and moved nothing, while the whole rejected pile
  stayed discarded on arithmetic that was wrong by two orders of magnitude.
"""

from __future__ import annotations

import time

import pytest

from pqb.library import SIZING_EPOCH_KEY, StrategyLibrary


@pytest.fixture
def library(tmp_path):
    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    yield lib
    lib.close()


def _register(library, name: str, markets: int, pnl_per_market: float,
              status: str = "rejected", trades: int = 30):
    """A strategy with `markets` markets of evidence behind it."""
    strategy_id = library.upsert_candidate(
        signature=name, rule={"direction": "long", "entry_feature": name},
        describe=f"{name} rule")
    for i in range(markets):
        library.record_validation(strategy_id, f"{name}-market-{i}",
                                  trades=trades, wins=trades // 3,
                                  pnl=pnl_per_market, drawdown=abs(pnl_per_market))
    library.record_pass(strategy_id, trades * markets, trades, pnl_per_market)
    library.set_status(strategy_id, status, "negative expectancy")
    return strategy_id


# -- the old rule matched nothing ---------------------------------------------

def test_the_breadth_rule_alone_leaves_the_delivered_pile_untouched(library):
    """Reproduces the shipped state: every rejection has 2+ markets."""
    for i in range(5):
        _register(library, f"multi{i}", markets=3, pnl_per_market=-4000.0)

    assert library.reopen_single_market_rejections() == 0
    statuses = {row["status"] for row in library.all_strategies()}
    assert statuses == {"rejected"}


def test_the_breadth_rule_still_fires_where_it_should(library):
    single = _register(library, "single", markets=1, pnl_per_market=-4000.0)
    assert library.reopen_single_market_rejections() == 1
    assert _status(library, single) == "validating"


# -- the widened rule ----------------------------------------------------------

def test_pre_epoch_rejections_reopen_once_an_epoch_exists(library):
    ids = [_register(library, f"multi{i}", markets=3, pnl_per_market=-4000.0)
           for i in range(5)]

    # No epoch stamped yet: sizing is not a reason for anything.
    assert library.reopen_rejections() == {"breadth": 0, "sizing": 0,
                                           "held_by_guard": 0}

    library.set_meta(SIZING_EPOCH_KEY, repr(time.time()))
    counts = library.reopen_rejections()

    assert counts["sizing"] == 5
    assert all(_status(library, i) == "validating" for i in ids)


def test_a_rejection_carrying_post_epoch_evidence_is_left_alone(library):
    """Once re-challenged on correctly-sized data, the verdict is real."""
    strategy_id = _register(library, "retested", markets=3,
                            pnl_per_market=-40.0)
    library.set_meta(SIZING_EPOCH_KEY, repr(time.time() - 3600))

    # Evidence recorded AFTER the epoch: this rejection is properly earned.
    library.record_validation(strategy_id, "fresh-market", trades=30, wins=8,
                              pnl=-40.0, drawdown=40.0)
    library.set_status(strategy_id, "rejected", "negative expectancy")

    counts = library.reopen_rejections()
    assert counts["sizing"] == 0
    assert _status(library, strategy_id) == "rejected"


def test_reopening_does_not_churn_across_repeated_passes(library):
    """The guard that matters: a re-open needs evidence it does not have."""
    strategy_id = _register(library, "churny", markets=3,
                            pnl_per_market=-4000.0)
    library.set_meta(SIZING_EPOCH_KEY, repr(time.time()))

    assert library.reopen_rejections()["sizing"] == 1

    # The candidate is re-challenged, fails again on correctly-sized data.
    library.record_validation(strategy_id, "post-epoch-market", trades=30,
                              wins=7, pnl=-40.0, drawdown=40.0)
    library.set_status(strategy_id, "rejected", "negative expectancy")

    for _ in range(5):
        assert library.reopen_rejections()["sizing"] == 0
    assert _status(library, strategy_id) == "rejected"


def test_the_reopen_ceiling_is_a_hard_backstop(library):
    strategy_id = _register(library, "stubborn", markets=3,
                            pnl_per_market=-4000.0)
    library.set_meta(SIZING_EPOCH_KEY, repr(time.time()))

    for _ in range(3):
        library.reopen_rejections(max_reopens=3)
        library.set_status(strategy_id, "rejected", "still bad")

    counts = library.reopen_rejections(max_reopens=3)
    assert counts["sizing"] == 0
    assert counts["held_by_guard"] == 1
    assert _status(library, strategy_id) == "rejected"


def test_rejected_candidates_rotate_back_for_another_swing(library):
    for i in range(12):
        _register(library, f"r{i}", markets=3, pnl_per_market=-4000.0)

    batch = library.rejected_for_recheck(limit=5)
    assert len(batch) == 5
    assert all(row["status"] == "rejected" for row in batch)
    assert all(isinstance(row["rule"], dict) for row in batch)


# -- voiding the evidence -------------------------------------------------------

def test_reset_clears_evidence_and_keeps_the_rules(library):
    strategy_id = _register(library, "poisoned", markets=4,
                            pnl_per_market=-2750.25)

    before = library.evidence_summary()
    assert before["validations"] == 4
    assert before["passes"] == 1

    result = library.reset_for_resizing(note="audit fix")

    after = library.evidence_summary()
    assert after["validations"] == 0
    assert after["passes"] == 0
    assert after["statuses"] == {"new": 1}

    # The knowledge survives; only the verdict is gone.
    rows = library.all_strategies()
    assert len(rows) == 1
    assert rows[0]["rule"] == {"direction": "long", "entry_feature": "poisoned"}
    assert library.cumulative(strategy_id)["trades"] == 0
    assert result["before"]["validations"] == 4


def test_reset_stamps_an_epoch_that_later_evidence_is_measured_against(library):
    _register(library, "x", markets=2, pnl_per_market=-1000.0)
    before = time.time()
    library.reset_for_resizing()

    epoch = library.sizing_epoch()
    assert epoch >= before


def test_reset_returns_rejections_to_play(library):
    """The verdicts were conclusions drawn from what was just deleted."""
    ids = [_register(library, f"r{i}", markets=3, pnl_per_market=-4000.0)
           for i in range(4)]
    library.reset_for_resizing()

    assert all(_status(library, i) == "new" for i in ids)
    # 'new' is evaluable; 'rejected' was not.
    assert len(library.evaluable()) == 4


def test_reset_preserves_discovery_exclusions(library):
    """Voiding a verdict must not let a rule validate on its own training data."""
    strategy_id = library.upsert_candidate(
        "sig", {"direction": "long"}, "d",
        discovery_markets={"train-a", "train-b"})
    library.record_validation(strategy_id, "m1", 30, 10, -5000.0, 100.0)

    library.reset_for_resizing()

    assert library.excluded_markets(strategy_id) == {"train-a", "train-b"}


def _status(library, strategy_id: str) -> str:
    return next(row["status"] for row in library.all_strategies()
                if row["id"] == strategy_id)
