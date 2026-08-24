"""The high-confidence trade filter: extremely selective, empirically honest."""

from __future__ import annotations

import json

from pqb.config import HighConfidenceConfig
from pqb.decision.high_confidence import HighConfidenceFilter
from pqb.journal import Journal


def cfg(**kw) -> HighConfidenceConfig:
    base = HighConfidenceConfig()
    for key, value in kw.items():
        setattr(base, key, value)
    return base


GOOD_ROW = {"ms_state": 2.0, "ms_exhaustion": 10.0, "ms_chg_300s": 0.04,
            "ms_imbalance": 0.5, "ms_liquidity_chg": 0.1, "ask": 0.5}


def check(filter_, **overrides):
    args = dict(score=0.75, category="sports", stake=10.0,
                features=dict(GOOD_ROW), spread=0.02, depth=100.0,
                ev_per_dollar=0.05, wallet_net=10.0, context=None)
    args.update(overrides)
    return filter_.evaluate(**args)


# -- structure gates ---------------------------------------------------------

def test_a_clean_developing_setup_passes():
    verdict = check(HighConfidenceFilter(cfg()))
    assert verdict.allow
    assert verdict.provisional          # no history yet, and it says so


def test_exhausted_impulse_is_rejected():
    row = dict(GOOD_ROW, ms_exhaustion=80.0)
    verdict = check(HighConfidenceFilter(cfg()), features=row)
    assert not verdict.allow


def test_wrong_state_is_rejected():
    verdict = check(HighConfidenceFilter(cfg()),
                    features=dict(GOOD_ROW, ms_state=4.0))   # exhaustion state
    assert not verdict.allow


def test_thin_depth_is_rejected():
    verdict = check(HighConfidenceFilter(cfg()), depth=20.0, stake=10.0)
    assert not verdict.allow            # needs 3x stake, has 2x


def test_negative_ev_is_rejected():
    verdict = check(HighConfidenceFilter(cfg()), ev_per_dollar=-0.01)
    assert not verdict.allow


def test_contradictory_evidence_rejects():
    row = dict(GOOD_ROW, ms_liquidity_chg=-0.5,      # book draining
               ms_imbalance=-0.5)                     # flow against the move
    verdict = check(HighConfidenceFilter(cfg()), features=row)
    assert not verdict.allow
    assert any("argue against" in r for r in verdict.reasons)


# -- empirical gates ---------------------------------------------------------

def _journal_with_setups(tmp_path, wins: list[int]):
    j = Journal(tmp_path / "j.sqlite3", mode="dry_run")
    for i, won in enumerate(wins):
        j.execute(
            "INSERT INTO decisions(ts, action, score, category, features) "
            "VALUES(?,?,?,?,?)",
            (1000.0 + i, "BUY", 0.75, "sports",
             json.dumps({"ms_state": 2.0})))
        decision_id = j.query("SELECT MAX(id) i FROM decisions")[0]["i"]
        j.execute(
            "INSERT INTO lifecycles(token_id, status, entry_decision_id, "
            "realized_pnl, exit_ts) VALUES(?,?,?,?,?)",
            (f"t{i}", "CLOSED", decision_id,
             1.0 if won else -1.0, 2000.0 + i))
    return j


def test_live_mode_blocks_unproven_setups(tmp_path):
    filter_ = HighConfidenceFilter(cfg(), journal=None, live=True)
    verdict = check(filter_)
    assert not verdict.allow
    assert any("unproven" in r for r in verdict.reasons)


def test_validated_strong_setup_passes_with_history(tmp_path):
    journal = _journal_with_setups(tmp_path, [1] * 14 + [0, 1, 1, 1])
    filter_ = HighConfidenceFilter(cfg(), journal=journal, live=True)
    verdict = check(filter_)
    assert verdict.allow, verdict.reasons
    assert not verdict.provisional
    assert verdict.evidence["setupSample"] == 18


def test_weak_history_is_rejected(tmp_path):
    journal = _journal_with_setups(tmp_path, [1, 0] * 8)     # 50% winners
    filter_ = HighConfidenceFilter(cfg(), journal=journal, live=False)
    verdict = check(filter_)
    assert not verdict.allow
    assert any("under the" in r for r in verdict.reasons)


def test_out_of_sample_decay_is_rejected(tmp_path):
    """Great on the old data, cold on the newest slice: overfitting's shape."""
    journal = _journal_with_setups(tmp_path, [1] * 14 + [0, 0, 0, 0, 0, 0])
    filter_ = HighConfidenceFilter(cfg(), journal=journal, live=False)
    verdict = check(filter_)
    assert not verdict.allow
    assert any("overfitting" in r for r in verdict.reasons)
