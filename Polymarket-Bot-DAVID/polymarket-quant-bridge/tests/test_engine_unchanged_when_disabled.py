"""§58 — the existing engine must behave identically with the module disabled.

The brief asks for the literal experiment: run the engine, save the baseline,
run it again with the new subsystem present but switched off, and diff the
outputs. Not a code review, not an argument — a comparison.

That is what this file does. It drives the REAL `BaselineDecisionEngine` over
a fixed set of contexts and compares its decisions field by field between two
worlds:

    world A: `pqb.wallet_state_research` has never been imported
    world B: every module of it is imported and its config object exists

If those two ever disagree, the isolation claim is false and everything else
in the subsystem is unsafe to enable. The comparison covers entries, holds,
exits and the do-nothing path, because a subsystem could plausibly perturb one
and not the others.

Complementary to, not a replacement for, `test_wallet_state_research`'s
import-graph assertion: that one proves nothing imports the package, this one
proves that importing it anyway changes nothing.
"""

from __future__ import annotations

import importlib
import sys
import time

import pytest

import importlib
import sys

import pytest

# The battery is built with the EXISTING engine test's own helpers rather than
# re-derived here. Two reasons: they are already known to construct valid
# model objects, and a regression harness that drifts from the suite it is
# guarding is worse than no harness at all.
from tests.test_engine import context, engine, market, position

_MODULES = (
    "pqb.wallet_state_research",
    "pqb.wallet_state_research.episodes",
    "pqb.wallet_state_research.events",
    "pqb.wallet_state_research.features",
    "pqb.wallet_state_research.classifier",
    "pqb.wallet_state_research.strategy_v1",
    "pqb.wallet_state_research.states",
    "pqb.wallet_state_research.structure",
    "pqb.wallet_state_research.registry",
    "pqb.wallet_state_research.pricing",
    "pqb.wallet_state_research.backtest",
    "pqb.wallet_state_research.validation",
    "pqb.wallet_state_research.discovery",
    "pqb.wallet_state_research.discovery_v1",
    "pqb.wallet_state_research.signal",
    "pqb.wallet_state_research.lean_adapter",
    "pqb.wallet_state_research.report",
    "pqb.wallet_state_research.runner",
)


def _contexts() -> list:
    """A battery covering entry, hold, exit and do-nothing.

    A regression test over an inert battery proves nothing, so the last test
    in this file pins that these really do produce a variety of decisions.
    """
    return [
        ("entry-candidate", context(markets=[market()], wallets=1)),
        ("held-winner", context(positions=[position(entry=0.40, mark=0.62,
                                                    peak=0.65)],
                                markets=[market(bid=0.62, ask=0.63)])),
        ("held-loser", context(positions=[position(entry=0.60, mark=0.31)],
                               markets=[market(bid=0.31, ask=0.32)])),
        ("stop-out", context(positions=[position(entry=0.80, mark=0.20)],
                             markets=[market(bid=0.20, ask=0.21)])),
        ("no-candidates", context(markets=[])),
        ("flattening", context(positions=[position()], markets=[market()],
                               flattening=True, flatten_reason="kill_switch")),
    ]


def _fingerprint(decisions) -> list:
    """Everything about a decision that could move money or be journalled."""
    return [{
        "action": d.action.value,
        "token": d.token_id,
        "market": d.market_id,
        "outcome": d.outcome,
        "sizeUsdc": round(float(d.size_usdc or 0.0), 8),
        "sizeShares": round(float(d.size_shares or 0.0), 8),
        "limitPrice": (round(float(d.limit_price), 8)
                       if d.limit_price is not None else None),
        "score": round(float(d.score or 0.0), 8),
        "confidence": round(float(d.confidence or 0.0), 8),
        "exitStyle": d.exit_style,
        "reason": d.reason,
        "walletInfluence": d.wallet_influence,
    } for d in decisions]


def _run_all() -> dict:
    brain = engine()
    return {name: _fingerprint(brain.evaluate(ctx))
            for name, ctx in _contexts()}


def _purge_module() -> None:
    for name in [n for n in sys.modules
                 if n.startswith("pqb.wallet_state_research")]:
        del sys.modules[name]


@pytest.fixture
def baseline():
    """World A: the subsystem has never been imported."""
    _purge_module()
    try:
        yield _run_all()
    finally:
        _purge_module()


def test_engine_output_is_identical_with_the_module_imported(baseline):
    """The §58 diff, run rather than argued."""
    for name in _MODULES:
        importlib.import_module(name)
    assert any(n.startswith("pqb.wallet_state_research") for n in sys.modules)

    after = _run_all()
    assert set(after) == set(baseline)
    for name in baseline:
        assert after[name] == baseline[name], (
            f"context '{name}' changed once the research module was imported")


def test_engine_output_is_identical_with_the_config_present(baseline):
    """...and with its configuration object constructed and attached."""
    from pqb.config import WalletStateResearchConfig

    settings = WalletStateResearchConfig()
    assert settings.enabled is False
    brain = engine()
    setattr(brain.cfg, "wallet_state_research", settings)

    after = {name: _fingerprint(brain.evaluate(ctx))
             for name, ctx in _contexts()}
    for name in baseline:
        assert after[name] == baseline[name], (
            f"context '{name}' changed once the config object existed")


def test_the_signal_call_returns_nothing_and_leaves_the_engine_alone(baseline):
    """Calling the adapter mid-cycle must not perturb the next decision."""
    from pqb.config import WalletStateResearchConfig
    from pqb.wallet_state_research import lean_adapter, signal

    class _Cfg:
        wallet_state_research = WalletStateResearchConfig()

    for _ in range(5):
        assert signal.get_signal(_Cfg()) is signal.NO_SIGNAL
        assert lean_adapter.get_alpha_features(
            _Cfg()) is lean_adapter.NO_ALPHA_FEATURES

    after = _run_all()
    for name in baseline:
        assert after[name] == baseline[name]


def test_the_battery_actually_exercises_the_engine(baseline):
    """A regression test over an inert battery proves nothing, so pin that
    the contexts really do produce a variety of decisions."""
    actions = {row["action"] for rows in baseline.values() for row in rows}
    assert len(actions) >= 2, actions
    assert sum(len(rows) for rows in baseline.values()) >= 4
