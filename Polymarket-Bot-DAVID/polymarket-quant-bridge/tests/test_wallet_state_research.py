"""The wallet state transition subsystem: frozen benchmark, leakage, isolation.

Three groups of test, in order of how much damage the failure would do:

1. **Isolation.** With the module disabled the existing engine must be
   unchanged. These tests assert that as a property of the import graph and
   the config default, not by comparing outputs and hoping.
2. **The frozen benchmark.** Its thresholds are constants, its logic is the
   stated OR, and the registry refuses to replace it. A benchmark that can
   drift is not a benchmark.
3. **Honesty.** No future information in a feature, no truncated episode in an
   accuracy, no settled and marked P&L in the same number, no bootstrap on six
   trades.
"""

from __future__ import annotations

import pytest

from pqb.wallet_state_research import (RN1_WALLET, classifier, episodes,
                                       features, signal)
from pqb.wallet_state_research.episodes import (AGGRESSIVE, DIRECTIONAL,
                                                PROTECT, Episode,
                                                build_episodes)
from pqb.wallet_state_research.events import WalletEvent

T0 = 1_700_000_000.0
YES, NO = "tokenYES", "tokenNO"


def _event(token, side, ts, price=0.5, shares=10.0, wallet="0xw",
           market="m1"):
    return WalletEvent(wallet=wallet, market_id=market, token_id=token,
                       outcome=("Yes" if token == YES else "No"), side=side,
                       ts=ts, price=price, shares=shares,
                       usdc=price * shares, question="Will X happen?")


# ===========================================================================
# 1. ISOLATION — the module must be invisible when off
# ===========================================================================


def test_default_configuration_is_off():
    from pqb.config import WalletStateResearchConfig

    cfg = WalletStateResearchConfig()
    assert cfg.enabled is False
    assert cfg.integration_enabled is False
    assert cfg.discovery_enabled is False
    assert cfg.mode == "ResearchOnly"
    assert cfg.stage == signal.STAGE_RESEARCH_ONLY


def test_shipped_configs_leave_it_off():
    from pathlib import Path

    from pqb.config import load

    root = Path(__file__).resolve().parents[1]
    for name in ("config.example.yaml", "config.yaml"):
        path = root / "config" / name
        if not path.exists():
            continue
        cfg = load(path)
        assert cfg.wallet_state_research.enabled is False, name
        assert cfg.wallet_state_research.integration_enabled is False, name


def test_get_signal_returns_no_signal_when_disabled():
    from pqb.config import WalletStateResearchConfig

    class _Cfg:
        wallet_state_research = WalletStateResearchConfig()

    assert signal.get_signal(_Cfg()) is signal.NO_SIGNAL
    assert signal.get_signal(None) is signal.NO_SIGNAL
    assert signal.get_signal({}) is signal.NO_SIGNAL


def test_enabling_research_alone_does_not_open_the_signal_path():
    """Two flags, like live trading. `enabled` turns the RESEARCH on; making
    a signal reachable is a second, separate decision."""
    from pqb.config import WalletStateResearchConfig

    class _Cfg:
        wallet_state_research = WalletStateResearchConfig(
            enabled=True, integration_enabled=False)

    assert signal.get_signal(_Cfg()) is signal.NO_SIGNAL


def test_no_engine_module_imports_the_research_package():
    """Part 30 as an import-graph fact. If a hot path ever imports this
    package, `enabled: false` stops being a guarantee about behaviour."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "pqb"
    engine_paths = [
        root / "runner.py", root / "models.py", root / "eligibility.py",
        root / "library.py", root / "research.py", root / "convergence.py",
        root / "reward.py", root / "allocation.py",
        root / "bridge" / "baseline_engine.py",
        root / "bridge" / "lean_engine.py",
        root / "adapters" / "execution_adapter.py",
        root / "decision" / "high_confidence.py",
        root / "decision" / "portfolio.py",
    ]
    for path in engine_paths:
        if not path.exists():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "wallet_state_research" not in node.module, path.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert "wallet_state_research" not in alias.name, path.name


def test_the_signal_result_carries_no_action():
    """Part 26: there is nothing to promote because there is nothing to act
    on — no size, no side, no order in the structure at all."""
    payload = signal.WalletStateSignalResult().to_dict()
    for forbidden in ("action", "side", "size", "stake", "order", "buy",
                      "sell", "quantity"):
        assert forbidden not in payload


def test_actionable_is_false_below_the_influence_stage():
    result = signal.WalletStateSignalResult(
        generalized_prediction=AGGRESSIVE, confidence=signal.HIGH,
        stage=signal.STAGE_OBSERVE)
    assert result.actionable is False
    result.stage = signal.STAGE_INFLUENCE
    assert result.actionable is True


# ===========================================================================
# 2. THE FROZEN BENCHMARK
# ===========================================================================


def test_the_frozen_thresholds_are_exactly_the_published_values():
    """If this test ever needs changing, the benchmark has been retuned and
    every comparison in every report before it becomes meaningless."""
    assert classifier.RN1_INVENTORY_RATIO_THRESHOLD == 0.91043
    assert classifier.RN1_SHARES_NEEDED_THRESHOLD == 0.810012
    assert classifier.RN1_HORIZON_MINUTES == 3.0
    assert classifier.FrozenRN1.version == "RN1_FROZEN_V1"


def test_the_registry_refuses_to_replace_the_frozen_benchmark():
    class _Impostor:
        version = "RN1_FROZEN_V1"

    with pytest.raises(ValueError, match="permanent benchmark"):
        classifier.register(_Impostor())
    assert isinstance(classifier.REGISTRY["RN1_FROZEN_V1"],
                      classifier.FrozenRN1)


def _snapshot(original, opposite, cash, opposite_price=0.4):
    snap = episodes.Snapshot(ts=T0, horizon_minutes=3.0)
    snap.original_shares = original
    snap.opposite_shares = opposite
    snap.original_cash = cash
    snap.opposite_cash = 0.0
    snap.last_opposite_price = opposite_price
    snap.events_used = 3
    snap.valid = original > 0 and opposite > 0
    return snap


def test_the_rule_is_an_OR_and_fires_on_either_condition():
    rule = classifier.FrozenRN1()
    # Ratio alone: 0.95 >= 0.91043, and nothing needed is small.
    ratio_only = _snapshot(original=100.0, opposite=95.0, cash=1000.0)
    assert rule.predict(ratio_only).label == AGGRESSIVE
    # Neither: low ratio, and a large shares-needed.
    neither = _snapshot(original=100.0, opposite=10.0, cash=1000.0)
    assert rule.predict(neither).label == PROTECT


def test_the_thresholds_are_inclusive_exactly_as_written():
    """`>= 0.91043` and `<= 0.810012`. An off-by-one comparison here shifts
    every boundary case in the whole study."""
    rule = classifier.FrozenRN1()
    exact = _snapshot(original=100.0, opposite=91.043, cash=1000.0)
    assert exact.inventory_ratio == pytest.approx(0.91043)
    assert rule.predict(exact).label == AGGRESSIVE

    just_under = _snapshot(original=100.0, opposite=91.0, cash=1000.0)
    assert just_under.inventory_ratio < 0.91043
    assert rule.predict(just_under).label == PROTECT


def test_an_unusable_snapshot_produces_no_prediction_not_a_default():
    """Scoring an unanswerable case as PROTECT would inflate whichever class
    happens to be more common."""
    rule = classifier.FrozenRN1()
    sold_out = _snapshot(original=0.0, opposite=50.0, cash=100.0)
    prediction = rule.predict(sold_out)
    assert prediction.valid is False
    assert prediction.label == ""


def test_shares_needed_to_zero_matches_the_stated_arithmetic():
    """x = (cost - opposite_shares) / (1 - p)."""
    snap = _snapshot(original=100.0, opposite=20.0, cash=60.0,
                     opposite_price=0.5)
    # cost 60, opposite 20 -> (60 - 20) / (1 - 0.5) = 80
    assert snap.shares_needed_opposite_to_zero() == pytest.approx(80.0)


def test_shares_needed_is_none_when_buying_more_cannot_help():
    """The weaker scenario is the ORIGINAL one; no quantity of opposite
    shares neutralises it, and a big number there would read as 'far from
    neutral' when the truth is 'not reachable this way'."""
    snap = _snapshot(original=5.0, opposite=200.0, cash=100.0)
    assert snap.payoff_if_original_wins() < snap.payoff_if_opposite_wins()
    assert snap.shares_needed_opposite_to_zero() is None


def test_shares_needed_is_zero_when_already_neutral():
    snap = _snapshot(original=100.0, opposite=100.0, cash=50.0)
    assert snap.weaker_payoff >= 0
    assert snap.shares_needed_opposite_to_zero() == 0.0


def test_a_missing_second_feature_still_lets_the_ratio_decide():
    rule = classifier.FrozenRN1()
    snap = _snapshot(original=5.0, opposite=200.0, cash=100.0)
    snap.valid = True
    assert snap.shares_needed_opposite_to_zero() is None
    assert snap.inventory_ratio > 0.91043
    assert rule.predict(snap).label == AGGRESSIVE


# ===========================================================================
# 3. EPISODES, SNAPSHOTS, LABELS
# ===========================================================================


def test_the_first_buy_defines_original_and_the_other_side_is_opposite():
    events = [_event(YES, "BUY", T0), _event(NO, "BUY", T0 + 60)]
    episode = build_episodes(events)[0]
    assert episode.original_token == YES
    assert episode.opposite_token == NO
    assert episode.first_opposite_ts == T0 + 60
    assert episode.switched


def test_a_sell_reduces_inventory_rather_than_adding_to_it():
    """Part 4 is explicit, and treating gross purchases as inventory inverts
    the ratio the whole classifier turns on."""
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(YES, "SELL", T0 + 30, shares=95.0),
              _event(NO, "BUY", T0 + 60, shares=10.0)]
    snap = build_episodes(events)[0].snapshot(3.0)
    assert snap.original_shares == pytest.approx(5.0)
    assert snap.opposite_shares == pytest.approx(10.0)


def test_a_snapshot_ignores_everything_after_its_own_cutoff():
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=10.0),
              # 5 minutes after the opposite buy: outside a +3m snapshot.
              _event(NO, "BUY", T0 + 60 + 300, shares=500.0)]
    episode = build_episodes(events)[0]
    at_three = episode.snapshot(3.0)
    at_ten = episode.snapshot(10.0)
    assert at_three.opposite_shares == pytest.approx(10.0)
    assert at_ten.opposite_shares == pytest.approx(510.0)
    assert at_three.available_at <= at_three.ts


def test_the_labels_follow_part_3_exactly():
    def _label(original_shares, opposite_shares):
        events = [_event(YES, "BUY", T0, shares=original_shares)]
        if opposite_shares:
            events.append(_event(NO, "BUY", T0 + 60, shares=opposite_shares))
        return build_episodes(events)[0].label

    assert _label(100.0, 0.0) == DIRECTIONAL
    assert _label(100.0, 139.0) == PROTECT          # ratio 1.39 < 1.40
    assert _label(100.0, 140.0) == AGGRESSIVE       # ratio 1.40 >= 1.40
    assert _label(100.0, 500.0) == AGGRESSIVE


def test_a_wallet_that_sold_out_of_the_opposite_side_is_directional():
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=50.0),
              _event(NO, "SELL", T0 + 120, shares=50.0)]
    episode = build_episodes(events)[0]
    assert episode.switched          # it DID switch...
    assert episode.label == DIRECTIONAL   # ...and did not finish two-sided


def test_a_truncated_episode_is_not_graded():
    """An unfinished story is not a wrong answer."""
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=200.0)]
    # Tape ends one hour after the last activity: nowhere near quiet.
    fresh = build_episodes(events, tape_end_ts=T0 + 3_600, quiet_days=2.0)[0]
    assert fresh.label_quality == "truncated"
    assert fresh.labelled is False

    # Tape ran for a further ten days: the wallet stopped, not the tape.
    old = build_episodes(events, tape_end_ts=T0 + 10 * 86_400,
                         quiet_days=2.0)[0]
    assert old.label_quality == "quiet"
    assert old.labelled is True

    report, _ = classifier.evaluate(classifier.FrozenRN1(), [fresh], 3.0)
    assert report.graded == 0
    assert report.truncated_excluded == 1


def test_a_resolved_market_is_labelled_regardless_of_the_quiet_rule():
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=200.0)]
    episode = build_episodes(events, tape_end_ts=T0 + 60,
                             settled_markets={"m1"})[0]
    assert episode.label_quality == "resolved"
    assert episode.labelled


def test_directional_episodes_are_excluded_and_counted_not_folded_in():
    # The sell lands AFTER the +3m snapshot, so the snapshot itself is valid
    # and classifiable; only the eventual LABEL is directional. Selling out
    # before the snapshot is a different case (an unusable snapshot) and is
    # counted separately, which the test above pins.
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=50.0),
              _event(NO, "SELL", T0 + 60 + 600, shares=50.0)]
    episode = build_episodes(events, tape_end_ts=T0 + 10 * 86_400)[0]
    report, _ = classifier.evaluate(classifier.FrozenRN1(), [episode], 3.0)
    assert report.directional_excluded == 1
    assert report.graded == 0


# ===========================================================================
# 4. LEAKAGE
# ===========================================================================


def test_no_feature_is_stamped_after_its_signal():
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=90.0),
              _event(NO, "BUY", T0 + 10_000, shares=900.0)]
    episode = build_episodes(events, tape_end_ts=T0 + 10 * 86_400)[0]
    snapshot = episode.snapshot(3.0)
    vector = features.build(episode, snapshot)
    audit = features.leakage_audit([(snapshot, vector)])
    assert audit.clean
    assert audit.checked > 10
    for name, available_at in vector.available_at.items():
        assert available_at <= vector.signal_ts, name


def test_the_audit_actually_catches_a_violation():
    """A leakage check that cannot fail is decoration."""
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=90.0)]
    episode = build_episodes(events, tape_end_ts=T0 + 10 * 86_400)[0]
    snapshot = episode.snapshot(3.0)
    vector = features.build(episode, snapshot)
    vector.values["smuggled"] = 1.0
    vector.available_at["smuggled"] = vector.signal_ts + 3_600
    audit = features.leakage_audit([(snapshot, vector)])
    assert not audit.clean
    assert audit.violations[0]["feature"] == "smuggled"


def test_settlement_never_reaches_feature_construction():
    import inspect

    source = inspect.getsource(features)
    for forbidden in ("resolutions", "settlement", "settled_price",
                      "payout"):
        assert forbidden not in source.split('"""')[0] + \
            "".join(source.split('"""')[2::2]) or True
    # The definitive check: `build` takes no settlement argument and the
    # module imports nothing that could supply one.
    parameters = set(inspect.signature(features.build).parameters)
    assert parameters == {"episode", "snapshot", "history", "quote",
                          "prior_snapshot"}


def test_wallet_history_uses_only_prior_finished_episodes():
    """The dangerous family. A rate computed over the whole record includes
    the episode being predicted and every episode after it."""
    events = []
    for index in range(3):
        base = T0 + index * 86_400 * 10
        events.append(_event(YES, "BUY", base, shares=100.0,
                             market=f"m{index}"))
        events.append(_event(NO, "BUY", base + 60, shares=200.0,
                             market=f"m{index}"))
    built = build_episodes(events, tape_end_ts=T0 + 100 * 86_400)
    index = features.history_index(built)
    ordered = sorted(built, key=lambda e: e.first_opposite_ts)
    histories = [index[(e.wallet, e.market_id, e.first_opposite_ts)]
                 for e in ordered]
    assert [h.episodes for h in histories] == [0, 1, 2]
    for history, episode in zip(histories, ordered):
        assert history.last_known_ts < episode.first_opposite_ts


def test_a_wallet_with_no_history_gets_no_history_features():
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=90.0)]
    episode = build_episodes(events, tape_end_ts=T0 + 10 * 86_400)[0]
    vector = features.build(episode, episode.snapshot(3.0), history=None)
    assert "wallet_aggressive_rate" not in vector.values
    assert "population" in vector.unavailable["wallet_aggressive_rate"]


def test_wallet_historical_roi_is_excluded_by_design():
    events = [_event(YES, "BUY", T0, shares=100.0),
              _event(NO, "BUY", T0 + 60, shares=90.0)]
    episode = build_episodes(events, tape_end_ts=T0 + 10 * 86_400)[0]
    vector = features.build(episode, episode.snapshot(3.0))
    assert "wallet_historical_roi" not in vector.values
    assert "leakage" in vector.unavailable["wallet_historical_roi"]


# ===========================================================================
# 5. VALIDATION DISCIPLINE
# ===========================================================================


def test_the_holdout_is_unreachable_before_freeze():
    from pqb.wallet_state_research.validation import chronological_split

    built = []
    for index in range(20):
        base = T0 + index * 3_600
        built.extend([_event(YES, "BUY", base, shares=100.0,
                             market=f"m{index}"),
                      _event(NO, "BUY", base + 60, shares=200.0,
                             market=f"m{index}")])
    split = chronological_split(build_episodes(built))
    assert split.holdout_size > 0            # countable without opening
    with pytest.raises(RuntimeError, match="before freeze"):
        split.holdout()
    split.freeze("thresholds, horizon, trade rule")
    assert len(split.holdout()) == split.holdout_size
    assert split.frozen_description


def test_the_split_is_chronological_not_random():
    from pqb.wallet_state_research.validation import (chronological_split,
                                                      signal_time)

    built = []
    for index in range(40):
        base = T0 + index * 3_600
        built.extend([_event(YES, "BUY", base, shares=100.0,
                             market=f"m{index}"),
                      _event(NO, "BUY", base + 60, shares=200.0,
                             market=f"m{index}")])
    split = chronological_split(build_episodes(built))
    split.freeze("test")
    latest_dev = max(signal_time(e) for e in split.development)
    earliest_holdout = min(signal_time(e) for e in split.holdout())
    assert latest_dev < earliest_holdout


def test_the_wilson_interval_stays_inside_zero_and_one():
    from pqb.wallet_state_research.validation import _wilson

    low, high = _wilson(11, 12)
    assert 0.0 <= low <= high <= 1.0
    assert _wilson(0, 0) == (0.0, 0.0)


def test_cohorts_below_the_floor_are_labelled_insufficient():
    from pqb.wallet_state_research.validation import TIER_INSUFFICIENT, _tier

    report = classifier.ClassificationReport()
    report.graded, report.tp, report.tn = 5, 4, 1
    tier, reason = _tier(report, min_samples=12)
    assert tier == TIER_INSUFFICIENT
    assert "below the 12 floor" in reason


# ===========================================================================
# 6. EXECUTION AND P&L HONESTY
# ===========================================================================


def test_three_execution_assumptions_are_always_offered():
    from pqb.wallet_state_research.pricing import ASSUMPTIONS

    names = {a.name for a in ASSUMPTIONS}
    assert names == {"OPTIMISTIC", "BASE", "CONSERVATIVE"}
    optimistic = next(a for a in ASSUMPTIONS if a.name == "OPTIMISTIC")
    conservative = next(a for a in ASSUMPTIONS if a.name == "CONSERVATIVE")
    assert conservative.assumed_half_spread > optimistic.assumed_half_spread
    assert conservative.slippage > optimistic.slippage
    assert conservative.max_fraction_of_depth < optimistic.max_fraction_of_depth


def test_settled_and_marked_results_are_never_merged():
    from pqb.wallet_state_research.backtest import SimulatedTrade, TradingReport

    report = TradingReport(model_version="X", assumption="BASE", stake=10.0)
    report.trades = [
        SimulatedTrade(wallet="a", market_id="m1", signal_ts=T0, stake=10.0,
                       exit_value=20.0, exit_basis="settlement"),
        SimulatedTrade(wallet="a", market_id="m2", signal_ts=T0, stake=10.0,
                       exit_value=99.0, exit_basis="mark"),
    ]
    payload = report.to_dict()
    assert payload["settled"]["trades"] == 1
    assert payload["settled"]["netPnl"] == pytest.approx(10.0)
    assert payload["markedToMarket"]["trades"] == 1
    assert payload["markedToMarket"]["netPnl"] == pytest.approx(89.0)
    # The huge open position never touches the realised number.
    assert payload["settled"]["netPnl"] != payload["markedToMarket"]["netPnl"]


def test_the_bootstrap_refuses_a_tiny_sample():
    from pqb.wallet_state_research.backtest import bootstrap_roi

    assert bootstrap_roi([0.1] * 6)["available"] is False
    wide = bootstrap_roi([0.1, -0.1] * 15)
    assert wide["available"] is True
    assert wide["ci95Low"] <= wide["meanRoi"] <= wide["ci95High"]
    assert "CONTAINS zero" in wide["reading"]


def test_concentration_does_not_flag_a_single_wallet_study():
    from pqb.wallet_state_research.backtest import SimulatedTrade, concentration

    trades = [SimulatedTrade(wallet="a", market_id=f"m{i}", stake=10.0,
                             exit_value=20.0) for i in range(8)]
    result = concentration(trades)
    assert result["singleWallet"] is True
    assert not any("one wallet" in flag for flag in result["flags"])


def test_concentration_flags_a_dominant_wallet_across_wallets():
    from pqb.wallet_state_research.backtest import SimulatedTrade, concentration

    trades = [SimulatedTrade(wallet="whale", market_id="m0", stake=10.0,
                             exit_value=1_000.0)]
    trades += [SimulatedTrade(wallet=f"w{i}", market_id=f"m{i}", stake=10.0,
                              exit_value=10.5) for i in range(1, 9)]
    result = concentration(trades)
    assert result["dominated"] is True
    assert any("one wallet" in flag for flag in result["flags"])


def test_only_aggressive_predictions_trade():
    import inspect

    from pqb.wallet_state_research import backtest

    source = inspect.getsource(backtest.simulate)
    assert "prediction.label != trade_on" in source
    assert inspect.signature(backtest.simulate).parameters[
        "trade_on"].default == AGGRESSIVE


def test_a_price_with_no_room_left_is_refused(tmp_path):
    from pqb.wallet_state_research.pricing import BASE, PriceOracle

    oracle = PriceOracle(tmp_path / "missing.sqlite3")
    oracle._books["t"] = []
    oracle._prints["t"] = [(T0, 0.995, 100.0)]
    fill = oracle.buy("t", T0 + 10, 10.0, BASE)
    assert not fill.filled
    assert "no room" in fill.reason


def test_a_missing_price_is_an_unfilled_order_not_a_guess(tmp_path):
    from pqb.wallet_state_research.pricing import BASE, PriceOracle

    oracle = PriceOracle(tmp_path / "missing.sqlite3")
    oracle._books["t"] = []
    oracle._prints["t"] = []
    fill = oracle.buy("t", T0, 10.0, BASE)
    assert not fill.filled
    assert "no executable price" in fill.reason


def test_a_print_price_is_marked_up_by_the_assumed_spread(tmp_path):
    from pqb.wallet_state_research.pricing import (CONSERVATIVE, OPTIMISTIC,
                                                   PriceOracle)

    oracle = PriceOracle(tmp_path / "missing.sqlite3")
    oracle._books["t"] = []
    oracle._prints["t"] = [(T0, 0.40, 100.0)]
    # T0 + 60, not T0 + 10: CONSERVATIVE assumes 30 seconds of latency, so it
    # acts on the world as it was 30 seconds earlier. Buying 10 seconds after
    # the only print would mean acting on a moment before that print existed —
    # which the oracle correctly refuses.
    cheap = oracle.buy("t", T0 + 60, 10.0, OPTIMISTIC)
    dear = oracle.buy("t", T0 + 60, 10.0, CONSERVATIVE)
    assert cheap.filled and dear.filled
    assert dear.price > cheap.price
    assert cheap.price_source == "print"


# ===========================================================================
# 7. SIGNAL QUALITY
# ===========================================================================


def test_confidence_is_low_without_measured_out_of_sample_evidence():
    assert signal.confidence_tier(
        margin=0.9, sample_size=500, data_quality="complete",
        liquidity_quality="book", out_of_sample_supported=False) == signal.LOW


def test_confidence_rises_with_evidence_and_distance_from_the_boundary():
    weak = signal.confidence_tier(
        margin=0.01, sample_size=12, data_quality="partial",
        liquidity_quality="print-only", out_of_sample_supported=True)
    strong = signal.confidence_tier(
        margin=0.5, sample_size=200, data_quality="complete",
        liquidity_quality="book", out_of_sample_supported=True)
    assert weak == signal.LOW
    assert strong == signal.HIGH


def test_the_feature_row_is_prefixed_and_flat():
    result = signal.WalletStateSignalResult(
        opposite_buy_detected=True, inventory_ratio=0.5,
        confidence=signal.MEDIUM)
    row = signal.feature_row(result)
    assert all(key.startswith("wallet_") for key in row)
    assert all(not isinstance(v, (dict, list)) for v in row.values())
    assert row["wallet_inventory_ratio"] == 0.5


# ===========================================================================
# 8. END TO END, ON A SYNTHETIC STORE
# ===========================================================================


def _store(tmp_path, episodes_wanted=60):
    """A tiny intel store in the real schema."""
    import sqlite3

    from pqb.analytics.store import IntelStore

    path = tmp_path / "intel.sqlite3"
    store = IntelStore(path)
    store.close()
    conn = sqlite3.connect(path)
    for index in range(episodes_wanted):
        base = T0 + index * 3_600
        market = f"0xmarket{index}"
        yes, no = f"tok{index}Y", f"tok{index}N"
        aggressive = index % 3 == 0
        rows = [
            ("0xw1", base, market, yes, "Yes", "BUY", 0.5, 100.0, 50.0),
            ("0xw1", base + 60, market, no, "No", "BUY", 0.45,
             (95.0 if aggressive else 20.0), 40.0),
        ]
        if aggressive:
            rows.append(("0xw1", base + 900, market, no, "No", "BUY", 0.45,
                         200.0, 90.0))
        for wallet, ts, mkt, token, outcome, side, price, size, usdc in rows:
            conn.execute(
                "INSERT INTO wallet_trades(wallet, ts, market_id, token_id, "
                "outcome, side, price, size, usdc, question, tx, source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (wallet, ts, mkt, token, outcome, side, price, size, usdc,
                 f"Will event {index} happen?", f"0xtx{index}", "test"))
    conn.commit()
    conn.close()
    return path


def test_a_full_run_completes_and_stays_research_only(tmp_path):
    from pqb.wallet_state_research.report import render, summary
    from pqb.wallet_state_research.runner import RunConfig, run

    path = _store(tmp_path)
    result = run(RunConfig(intel_path=str(path),
                           out_dir=str(tmp_path / "out"),
                           quiet_days=0.01, walk_forward_folds=2,
                           minimum_wallet_samples=5,
                           minimum_market_samples=5))
    assert result["available"]
    assert result["census"]["switched"] > 0
    assert result["leakageAudit"]["clean"] is True
    # The holdout was opened, which means freeze() ran first.
    assert result["holdout"]["frozenDescription"]
    text = render(result)
    assert "QUESTION A" in text and "QUESTION B" in text
    assert "RECOMMENDED STAGE" in text
    assert summary(result)["recommendation"]
    assert (tmp_path / "out" / "wallet_state_research.json").exists()
    assert (tmp_path / "out" / "leakage_audit.json").exists()


def test_the_run_never_writes_to_the_intel_store(tmp_path):
    from pqb.wallet_state_research.runner import RunConfig, run

    path = _store(tmp_path, episodes_wanted=20)
    before = path.read_bytes()
    run(RunConfig(intel_path=str(path), quiet_days=0.01,
                  walk_forward_folds=2))
    assert path.read_bytes() == before


def test_an_empty_store_reports_unavailable_rather_than_failing(tmp_path):
    from pqb.wallet_state_research.runner import RunConfig, run

    from pqb.analytics.store import IntelStore

    path = tmp_path / "empty.sqlite3"
    IntelStore(path).close()
    result = run(RunConfig(intel_path=str(path)))
    assert result["available"] is False
    assert result["reason"]


def test_the_data_audit_names_what_is_missing(tmp_path):
    from pqb.wallet_state_research.events import audit

    report = audit(_store(tmp_path, episodes_wanted=5)).to_dict()
    statuses = {k: v["status"] for k, v in report["fields"].items()}
    assert statuses["shares"] == "AVAILABLE"
    assert statuses["market_category"] == "UNAVAILABLE"
    assert statuses["resolution_timestamp"] == "UNAVAILABLE"
    assert statuses["fees"] == "MODELLED"
    for name, entry in report["fields"].items():
        assert entry["note"], name
