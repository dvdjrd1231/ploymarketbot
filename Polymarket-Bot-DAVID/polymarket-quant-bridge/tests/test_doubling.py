"""
Doubling-rule state machine (prompt section 7), including every edge case the
specification calls out: partial fill during the mass close, a trigger arriving
while an order is in flight, the end of the progression, and baseline
persistence across a restart.
"""

from __future__ import annotations

import pytest

from conftest import MemoryStore
from pqb.doubling import FLATTENING, NORMAL, DoublingRule

PROGRESSION = [0.19, 0.29, 0.41, 0.47]


def rule(store, progression=None, **kwargs) -> DoublingRule:
    return DoublingRule(store, progression or PROGRESSION, **kwargs)


# --- baseline ---------------------------------------------------------------

def test_baseline_is_adopted_once(store):
    r = rule(store)
    assert r.baseline is None
    assert r.initialize(100.0) is True
    assert r.baseline == 100.0
    # A second call must not re-base: that would discard progress to the target.
    assert r.initialize(180.0) is False
    assert r.baseline == 100.0


def test_baseline_ignores_a_zero_portfolio(store):
    r = rule(store)
    assert r.initialize(0.0) is False
    assert r.baseline is None


def test_target_is_baseline_times_multiple(store):
    r = rule(store, multiple=2.0)
    r.initialize(50.0)
    assert r.target == 100.0


# --- triggering -------------------------------------------------------------

def test_does_not_trigger_below_target(store):
    r = rule(store)
    r.initialize(100.0)
    assert r.should_trigger(199.99) is False


def test_triggers_at_exactly_the_target(store):
    r = rule(store)
    r.initialize(100.0)
    assert r.should_trigger(200.0) is True


def test_trigger_moves_to_flattening_but_does_not_advance(store):
    r = rule(store)
    r.initialize(100.0)
    size_before = r.min_trade_size
    assert r.begin_flatten(200.0) is True
    assert r.state == FLATTENING
    # The size must not move until the book is actually flat.
    assert r.min_trade_size == size_before
    assert r.index == 0


def test_begin_flatten_refuses_when_the_trigger_no_longer_holds(store):
    r = rule(store)
    r.initialize(100.0)
    assert r.begin_flatten(150.0) is False
    assert r.state == NORMAL


def test_no_re_trigger_while_flattening(store):
    r = rule(store)
    r.initialize(100.0)
    r.begin_flatten(200.0)
    assert r.should_trigger(400.0) is False


def test_disabled_rule_never_triggers(store):
    r = rule(store, enabled=False)
    r.initialize(100.0)
    assert r.should_trigger(1_000.0) is False


# --- completing: the edge cases -------------------------------------------

def test_partial_fill_blocks_completion(store):
    """A mass close that only partly filled must not advance the size."""
    r = rule(store)
    r.initialize(100.0)
    r.begin_flatten(200.0)
    assert r.complete_flatten(200.0, open_positions=1, pending_orders=0) is False
    assert r.state == FLATTENING
    assert r.index == 0
    # Next cycle, once the remainder closed:
    assert r.complete_flatten(200.0, 0, 0) is True
    assert r.state == NORMAL
    assert r.index == 1


def test_in_flight_order_blocks_completion(store):
    r = rule(store)
    r.initialize(100.0)
    r.begin_flatten(200.0)
    assert r.complete_flatten(200.0, open_positions=0, pending_orders=1) is False
    assert r.state == FLATTENING


def test_completion_advances_and_rebases(store):
    r = rule(store)
    r.initialize(100.0)
    r.begin_flatten(210.0)
    assert r.complete_flatten(210.0) is True
    assert r.min_trade_size == PROGRESSION[1]
    assert r.baseline == 210.0
    assert r.target == 420.0
    assert r.status().completed == 1


def test_completion_outside_flattening_is_refused(store):
    r = rule(store)
    r.initialize(100.0)
    assert r.complete_flatten(500.0) is False
    assert r.index == 0


def test_progression_clamps_at_the_end(store):
    r = rule(store)
    r.initialize(10.0)
    value = 10.0
    for _ in range(len(PROGRESSION) + 3):
        value *= 2
        r.begin_flatten(value)
        r.complete_flatten(value)
    assert r.at_end_of_progression
    assert r.min_trade_size == PROGRESSION[-1]
    assert r.index == len(PROGRESSION) - 1
    # Still functional at the end: it re-bases, the size just stops growing.
    assert r.baseline == value


def test_full_progression_matches_the_specification(store):
    from pqb.config import DEFAULT_PROGRESSION
    r = rule(store, progression=DEFAULT_PROGRESSION)
    assert len(DEFAULT_PROGRESSION) == 50
    assert r.min_trade_size == 0.19
    value = 1.0
    r.initialize(value)
    for expected in DEFAULT_PROGRESSION[1:]:
        value *= 2
        r.begin_flatten(value)
        r.complete_flatten(value)
        assert r.min_trade_size == expected


# --- persistence ------------------------------------------------------------

def test_state_survives_a_restart(store):
    r = rule(store)
    r.initialize(100.0)
    r.begin_flatten(200.0)
    r.complete_flatten(200.0)

    restarted = rule(store)          # same store, fresh object
    assert restarted.baseline == 200.0
    assert restarted.index == 1
    assert restarted.min_trade_size == PROGRESSION[1]
    assert restarted.state == NORMAL


def test_restart_mid_flatten_resumes_flattening(store):
    """A crash between the trigger and the close must not lose the flatten."""
    r = rule(store)
    r.initialize(100.0)
    r.begin_flatten(200.0)

    restarted = rule(store)
    assert restarted.state == FLATTENING
    assert restarted.flattening is True
    assert restarted.index == 0


def test_corrupt_index_is_clamped_not_fatal():
    store = MemoryStore({"doubling.index": 999, "doubling.baseline": 10.0})
    r = rule(store)
    assert r.index == len(PROGRESSION) - 1
    assert r.min_trade_size == PROGRESSION[-1]


def test_empty_progression_is_rejected(store):
    with pytest.raises(ValueError):
        DoublingRule(store, [])


def test_reset_clears_everything(store):
    r = rule(store)
    r.initialize(100.0)
    r.begin_flatten(200.0)
    r.complete_flatten(200.0)
    r.reset()
    assert r.baseline is None
    assert r.index == 0
    assert r.state == NORMAL
