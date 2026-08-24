"""Sharp-move research: dislocation stays a hypothesis.

The operator's spec, pinned: detection adapts to each series' own return
distribution (no imported 15% thresholds); the response is measured against
the PRE-MOVE anchor; direction is discovered from the evidence (recovery
AND continuation are both valid answers); candidates must beat costs and
the no-event drift control; frozen replay honors the condition cell; and a
validated pattern still cannot vote in the live engine.
"""

from __future__ import annotations

import pytest

from pqb.analytics.sharp_moves import (describe, detect, frozen_replay,
                                       measure, study)


def _row(price, liquidity=10.0, **kwargs):
    base = {"price": price, "spread_rel": 0.02, "depth_total": 500.0,
            "log_liquidity": liquidity, "ms_trade_rate": 1.0,
            "hours_to_resolution": 24.0}
    base.update(kwargs)
    return base


def _flat(n, price=0.5, liquidity=10.0):
    return [_row(price + 0.0002 * (i % 5), liquidity) for i in range(n)]


def _crash_recovery_series(reps=14, drop=0.12, recover=1.0, liquidity=10.0):
    """Flat -> sharp drop -> recovery of `recover` x the drop. Repeatable."""
    rows = _flat(50, 0.55, liquidity)
    for _ in range(reps):
        base = rows[-1]["price"]
        rows.append(_row(base - drop, liquidity))          # the dislocation
        for i in range(20):
            rows.append(_row(base - drop + drop * recover * (i + 1) / 20,
                             liquidity))
        rows.extend(_flat(20, rows[-1]["price"], liquidity))
    return rows


# -- adaptive detection ------------------------------------------------------

def test_dislocations_are_detected_against_the_series_own_norm():
    events = detect(_crash_recovery_series())
    assert events, "planted crashes were not detected"
    assert all(e.direction == "down" for e in events)
    first = events[0]
    assert first.anchor > first.end_price           # anchor is pre-move
    assert first.magnitude < -0.03


def test_a_flat_series_has_no_dislocations():
    assert detect(_flat(300)) == []


def test_one_move_is_one_event_across_speeds():
    """The 3- and 5-bar scans must not re-count the same crash."""
    rows = _flat(60, 0.55) + [_row(0.40)] + _flat(60, 0.40)
    events = detect(rows)
    assert len(events) == 1


def test_context_rides_on_every_event():
    events = detect(_crash_recovery_series(liquidity=8.0))
    event = events[0]
    assert event.price_region == "40-59c"
    assert event.log_liquidity == 8.0
    assert event.hours_to_resolution == 24.0


# -- anchor-relative measurement and classification --------------------------

def test_full_recovery_is_measured_against_the_anchor():
    rows = _crash_recovery_series(recover=1.0)
    event = measure(detect(rows)[0], rows)
    assert event.recovery_frac[60] == pytest.approx(1.0, abs=0.15)
    assert event.classification in ("full_recovery", "recovery")
    assert event.mfe > 0


def test_continuation_is_classified_not_assumed_away():
    rows = _flat(50, 0.60)
    base = rows[-1]["price"]
    rows.append(_row(base - 0.10))
    for i in range(30):                          # keeps falling
        rows.append(_row(base - 0.10 - 0.004 * (i + 1)))
    rows.extend(_flat(20, rows[-1]["price"]))
    event = measure(detect(rows)[0], rows)
    assert event.classification in ("continuation", "acceleration")
    assert event.mae > 0


# -- the study: evidence decides, controls guard -----------------------------

def test_recovery_pattern_is_discovered_with_direction_up():
    series = [("M1", _crash_recovery_series()),
              ("M2", _crash_recovery_series())]
    result = study(series, min_events=10, cost=0.005, hold_bars=15)
    kept = result["candidates"]
    assert kept, f"nothing kept: {result['funnel']}"
    best = kept[0]
    assert best["move_direction"] == "down"
    assert best["direction"] == "up"             # recovery, from evidence
    assert best["netExpectancy"] > 0
    assert best["recoveryShare"] > 0.5


def test_continuation_pattern_discovers_direction_down():
    def falling():
        """Crash -> deep slide -> slow climb back to base: price stays in
        one region, so the continuation cell keeps its whole sample."""
        rows = _flat(50, 0.70)
        for _ in range(14):
            base = rows[-1]["price"]
            rows.append(_row(base - 0.06))
            for i in range(15):                   # keeps sliding after
                rows.append(_row(base - 0.06 - 0.004 * (i + 1)))
            for i in range(60):                   # gentle climb home
                rows.append(_row(base - 0.12 + 0.002 * (i + 1)))
        return rows
    result = study([("M1", falling()), ("M2", falling())],
                   min_events=10, cost=0.001, hold_bars=15)
    downs = [c for c in result["candidates"]
             if c["direction"] == "down" and c["move_direction"] == "down"]
    assert downs, f"continuation not discovered: {result['funnel']}"
    # The 15-bar hold discovered FOLLOW-the-move economics, even though the
    # 60-bar classification honestly reads the later climb home — the two
    # horizons answer different questions, and both are recorded.
    assert downs[0]["netExpectancy"] > 0


def test_costs_and_drift_control_kill_weak_cells():
    """Full sample, real recovery — but a cost bigger than the move. The
    cell must die on economics, with the reason named."""
    series = [("M1", _crash_recovery_series(drop=0.08, recover=1.0)),
              ("M2", _crash_recovery_series(drop=0.08, recover=1.0))]
    result = study(series, min_events=10, cost=0.09, hold_bars=15)
    assert result["candidates"] == []
    reasons = result["funnel"]["rejectReasons"]
    assert reasons.get("cannot clear costs", 0) \
        + reasons.get("no better than drift (control)", 0) > 0


def test_small_samples_are_named_not_promoted():
    result = study([("M1", _crash_recovery_series(reps=3))],
                   min_events=10, min_markets=1)
    assert result["candidates"] == []
    assert result["funnel"]["rejectReasons"].get("insufficient sample", 0) > 0


def test_funnel_reports_every_stage():
    result = study([("M1", _flat(200))])
    for key in ("sharpMovesDetected", "usableEvents", "conditionCells",
                "cellsWithSample", "netPositive", "kept", "rejectReasons",
                "responseClasses", "baselineWindows", "baselineAbsMove"):
        assert key in result["funnel"]


# -- frozen replay (OOS discipline) ------------------------------------------

_RULE = {"type": "sharp_move", "move_direction": "down",
         "price_region": "40-59c", "liquidity": "deep",
         "direction": "up", "hold_bars": 15}


def test_frozen_replay_scores_unseen_series():
    stats = frozen_replay(_crash_recovery_series(), _RULE, cost=0.005)
    assert stats["trades"] > 0
    assert stats["expectancy"] > 0


def test_frozen_replay_honors_the_condition_cell():
    thin = _crash_recovery_series(liquidity=5.0)     # thin book, same moves
    stats = frozen_replay(thin, _RULE, cost=0.005)
    assert stats["trades"] == 0                      # cell says deep only


def test_frozen_replay_punishes_the_wrong_direction():
    rule = dict(_RULE, direction="down")
    stats = frozen_replay(_crash_recovery_series(), rule, cost=0.005)
    assert stats["trades"] > 0
    assert stats["expectancy"] < 0


# -- identity and the execution bar ------------------------------------------

def test_signature_is_the_condition_cell():
    from pqb.research import signature_of

    assert signature_of(_RULE) == "sharp|down|40-59c|deep|up"
    retimed = dict(_RULE, hold_bars=30)
    assert signature_of(retimed) == signature_of(_RULE)   # same family


def test_validated_sharp_patterns_cannot_vote(tmp_path):
    from pqb.bridge.lean_engine import LeanDecisionEngine
    from pqb.config import Config
    from pqb.research import DiscoveredStrategy

    cfg = Config()
    cfg.root = tmp_path
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    sharp = DiscoveredStrategy(rule=dict(_RULE), signature="sharp|x",
                               describe="SHARP")
    sharp.status = "validated"
    engine.strategies = [sharp]
    assert engine.trading_strategies == []


def test_sharp_moves_feed_the_sequence_vocabulary():
    """§9: a sharp move is an EVENT the chain search can build on."""
    from pqb.analytics.sequences import extract_events

    rows = _crash_recovery_series()
    kinds = {e.kind for e in extract_events(rows)}
    assert "sharp_drop" in kinds


def test_describe_reads_naturally():
    assert "fade" in describe(_RULE)
    follow = dict(_RULE, direction="down")
    assert "follow" in describe(follow)
