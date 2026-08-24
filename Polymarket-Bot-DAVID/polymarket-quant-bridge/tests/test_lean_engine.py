"""The Quant-Bridge-backed decision engine."""

from __future__ import annotations

import json
import time

import pytest

from pqb.bridge.lean_engine import LeanDecisionEngine
from pqb.config import Config
from pqb.models import (
    AccountState, Action, AnomalySignal, BridgeContext, MarketFeatures,
    MarketStatus, OutcomeQuote, PositionView, WalletIntel,
)
from pqb.research import DiscoveredStrategy, save, signature_of


def rule(entry_feature="flow_z", entry_op=">", entry_threshold=1.0,
         direction="long", filter_feature=None, filter_op=None,
         filter_threshold=None):
    return {
        "id": "S0001", "direction": direction, "entry_feature": entry_feature,
        "entry_op": entry_op, "entry_threshold": entry_threshold,
        "stop_pct": 10.0, "target_pct": 20.0, "time_exit_bars": 0,
        "contracts": 1, "filter_feature": filter_feature,
        "filter_op": filter_op, "filter_threshold": filter_threshold,
    }


def discovered(score=0.8, accepted_on=3, status="validated",
               **kw) -> DiscoveredStrategy:
    """A discovered rule the engine may USE — i.e. one that passed OOS.

    Status defaults to validated because these tests model the voting/scoring
    mechanics of usable rules; the only-validated-may-trade gate has its own
    tests in test_oos_validation.py.
    """
    r = rule(**kw)
    s = DiscoveredStrategy(rule=r, signature=signature_of(r),
                           tokens=["a", "b", "c"][:accepted_on],
                           accepted_on=accepted_on, score=score,
                           describe="test rule")
    s.status = status
    return s


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config()
    c.root = tmp_path
    c.storage.data_dir = "state"
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return c


def engine_with(cfg, strategies, journal=None) -> LeanDecisionEngine:
    save(cfg.data_dir / "strategies.json", strategies)
    return LeanDecisionEngine(cfg.engine, config=cfg, journal=journal)


def quote(**kw) -> OutcomeQuote:
    base = dict(token_id="T1", outcome="Yes", bid=0.44, ask=0.46, mid=0.45,
                spread=0.02, bid_depth=900.0, ask_depth=900.0,
                source="stream", updated_ts=time.time())
    base.update(kw)
    return OutcomeQuote(**base)


def market(**kw) -> MarketFeatures:
    base = dict(market_id="M1", question="Will it?", category="Politics",
                status=MarketStatus.ACTIVE, end_ts=int(time.time()) + 172_800,
                liquidity=60_000.0, volume_total=400_000.0, volume_24h=60_000.0)
    base.update(kw)
    m = MarketFeatures(**base)
    m.quotes = {"T1": quote()}
    return m


def context(markets=None, positions=None, **kw) -> BridgeContext:
    base = dict(cycle_id="c1", ts=time.time(),
                account=AccountState(balance=1_000.0, position_value=0.0),
                markets=markets if markets is not None else {"M1": market()},
                positions=positions or [], min_trade_size=0.19,
                tracked_wallets=5)
    base.update(kw)
    return BridgeContext(**base)


# --- day one: no research has run yet ---------------------------------------

def test_falls_back_to_baseline_when_nothing_is_discovered(cfg):
    from pqb.bridge.baseline_engine import BaselineDecisionEngine
    engine = engine_with(cfg, [])
    ctx = context()
    engine.evaluate(ctx)                   # it still decides, and says why
    assert engine.evaluate(ctx)

    score, parts, _ = engine._entry_score(ctx.markets["M1"], quote(), "T1",
                                          ctx, time.time())
    assert parts["rulesTotal"] == 0
    assert "fallback" in parts
    # With no rules the score is exactly the baseline's, not a degraded one.
    baseline = BaselineDecisionEngine(cfg.engine)
    expected, _p, _i = baseline._entry_score(ctx.markets["M1"], quote(), "T1",
                                             ctx, time.time())
    assert abs(score - expected) < 1e-9


def test_a_missing_strategy_file_is_not_fatal(cfg):
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    assert engine.strategies == []
    assert engine.evaluate(context()) is not None


def test_a_corrupt_strategy_file_is_not_fatal(cfg):
    (cfg.data_dir / "strategies.json").write_text("{not json", encoding="utf-8")
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    assert engine.strategies == []


# --- discovered rules drive the score ---------------------------------------

def test_a_firing_long_rule_raises_the_score(cfg):
    engine = engine_with(cfg, [discovered(entry_feature="flow_z",
                                          entry_threshold=1.0)])
    from pqb.models import MarketIntel
    quiet = context()
    loud = context(market_intel={"M1": MarketIntel(market_id="M1", flow_z=5.0)})

    quiet_score = engine._entry_score(quiet.markets["M1"], quote(), "T1",
                                      quiet, time.time())[0]
    loud_score = engine._entry_score(loud.markets["M1"], quote(), "T1",
                                     loud, time.time())[0]
    assert loud_score > quiet_score


def test_a_firing_short_rule_lowers_the_score(cfg):
    """There is no borrow on a prediction market.

    A short signal is a statement that the outcome is overpriced, which is
    evidence against buying it — not a tradable direction of its own.
    """
    from pqb.models import MarketIntel
    engine = engine_with(cfg, [discovered(direction="short",
                                          entry_feature="flow_z",
                                          entry_threshold=1.0)])
    ctx = context(market_intel={"M1": MarketIntel(market_id="M1", flow_z=5.0)})
    fired = engine._entry_score(ctx.markets["M1"], quote(), "T1", ctx,
                                time.time())[0]
    quiet = context()
    idle = engine._entry_score(quiet.markets["M1"], quote(), "T1", quiet,
                               time.time())[0]
    assert fired < idle


def test_a_rules_filter_must_also_pass(cfg):
    from pqb.models import MarketIntel
    engine = engine_with(cfg, [discovered(
        entry_feature="flow_z", entry_threshold=1.0,
        filter_feature="liquidity", filter_op=">", filter_threshold=1e9)])
    ctx = context(market_intel={"M1": MarketIntel(market_id="M1", flow_z=9.0)})
    _score, parts, _ = engine._entry_score(ctx.markets["M1"], quote(), "T1",
                                           ctx, time.time())
    assert parts["rulesFired"] == 0


def test_conviction_is_normalised_by_the_whole_board(cfg):
    """One weak rule firing must not read as total conviction."""
    from pqb.models import MarketIntel
    ctx = context(market_intel={"M1": MarketIntel(market_id="M1", flow_z=9.0)})

    alone = engine_with(cfg, [discovered(entry_feature="flow_z")])
    one_of_five = engine_with(cfg, [
        discovered(entry_feature="flow_z"),
        *[discovered(entry_feature="spread", entry_op="<",
                     entry_threshold=-1.0) for _ in range(4)],
    ])
    a = alone._entry_score(ctx.markets["M1"], quote(), "T1", ctx, time.time())[0]
    b = one_of_five._entry_score(ctx.markets["M1"], quote(), "T1", ctx,
                                 time.time())[0]
    assert a > b


def test_a_stale_rule_dilutes_rather_than_votes(cfg):
    """A rule naming a column this build no longer produces is stale, not false.

    It must stay in the denominator — otherwise a stale strategy file would
    silently concentrate conviction into whichever rules still parse.
    """
    from pqb.models import MarketIntel
    ctx = context(market_intel={"M1": MarketIntel(market_id="M1", flow_z=9.0)})
    engine = engine_with(cfg, [
        discovered(entry_feature="flow_z"),
        discovered(entry_feature="a_column_that_no_longer_exists"),
    ])
    _score, parts, _ = engine._entry_score(ctx.markets["M1"], quote(), "T1",
                                           ctx, time.time())
    assert parts["rulesFired"] == 1
    assert parts["rulesTotal"] == 2
    assert parts["ruleNet"] < 1.0


def test_rule_weight_scales_with_cross_token_confirmation(cfg):
    weak = discovered(score=0.8, accepted_on=1)
    strong = discovered(score=0.8, accepted_on=3)
    engine = engine_with(cfg, [])
    assert engine._weight(strong) > engine._weight(weak)


# --- the analytical layer reaches the rules as ordinary columns -------------

def test_anomaly_columns_are_addressable_by_a_discovered_rule(cfg):
    engine = engine_with(cfg, [discovered(entry_feature="anomaly_convergence",
                                          entry_threshold=0.5)])
    plain = context()
    flagged = context(anomalies=[AnomalySignal(kind="convergence",
                                               token_id="T1", strength=0.9)])
    quiet = engine._entry_score(plain.markets["M1"], quote(), "T1", plain,
                                time.time())[0]
    loud = engine._entry_score(flagged.markets["M1"], quote(), "T1", flagged,
                               time.time())[0]
    assert loud > quiet


def test_wallet_rank_is_addressable_by_a_discovered_rule(cfg):
    from pqb.models import WalletSignal
    engine = engine_with(cfg, [discovered(entry_feature="wallet_best_score",
                                          entry_threshold=0.5)])
    signals = [WalletSignal(wallet="0xa", action="ENTRY", token_id="T1",
                            usdc=500.0)]
    strong = context(wallet_signals=signals,
                     wallet_intel={"0xa": WalletIntel(wallet="0xa", score=0.9,
                                                      sample=80,
                                                      confidence=0.75)})
    weak = context(wallet_signals=signals,
                   wallet_intel={"0xa": WalletIntel(wallet="0xa", score=0.1,
                                                    sample=80,
                                                    confidence=0.75)})
    a = engine._entry_score(strong.markets["M1"], quote(), "T1", strong,
                            time.time())[0]
    b = engine._entry_score(weak.markets["M1"], quote(), "T1", weak,
                            time.time())[0]
    assert a > b


# --- structure inherited from the baseline survives -------------------------

def test_exit_precedence_is_unchanged(cfg):
    engine = engine_with(cfg, [discovered()])
    position = PositionView(token_id="T1", market_id="M1", size=100.0,
                            avg_price=0.40, cur_price=0.20, peak_price=0.40)
    decisions = engine.evaluate(context(positions=[position]))
    verdict = next(d for d in decisions if d.token_id == "T1")
    assert verdict.action is Action.EXIT
    assert verdict.exit_style == "stop"


def test_flatten_still_beats_everything(cfg):
    engine = engine_with(cfg, [discovered()])
    position = PositionView(token_id="T1", market_id="M1", size=100.0,
                            avg_price=0.40, cur_price=0.60, peak_price=0.60)
    decisions = engine.evaluate(context(positions=[position], flattening=True,
                                        flatten_reason="doubling"))
    verdict = next(d for d in decisions if d.token_id == "T1")
    assert verdict.action is Action.EXIT
    assert verdict.exit_style == "doubling"


def test_every_position_is_adjudicated_every_cycle(cfg):
    engine = engine_with(cfg, [discovered()])
    positions = [
        PositionView(token_id=f"T{i}", market_id="M1", size=10.0,
                     avg_price=0.40, cur_price=0.41, peak_price=0.41)
        for i in range(4)
    ]
    decisions = engine.evaluate(context(positions=positions))
    adjudicated = {d.token_id for d in decisions if d.token_id.startswith("T")}
    assert {f"T{i}" for i in range(4)}.issubset(adjudicated)


# --- feedback moves the wallet-exit bar -------------------------------------

def test_override_threshold_is_the_config_value_without_evidence(cfg, journal):
    engine = engine_with(cfg, [], journal=journal)
    assert engine._wallet_override_threshold(context()) == \
        cfg.engine.exits.wallet_exit_override_score


def test_override_threshold_moves_toward_what_paid(cfg, journal):
    from test_feedback import close, override_decision
    close(journal, style="wallet", ret=-0.20, n=12)
    close(journal, style="take_profit", ret=0.40, n=12)
    for row in journal.query(
            "SELECT id FROM lifecycles WHERE exit_style='take_profit'"):
        override_decision(journal, row["id"])

    engine = engine_with(cfg, [], journal=journal)
    engine._refresh_feedback(force=True)
    moved = engine._wallet_override_threshold(context())
    # Overriding paid better, so overriding should be easier — a lower bar.
    assert moved < cfg.engine.exits.wallet_exit_override_score
    assert 0.05 <= moved <= 0.95


def test_a_broken_journal_costs_the_tilt_not_the_decision(cfg):
    class Broken:
        mode = "test"

        def query(self, *_a, **_k):
            raise RuntimeError("journal is unreadable")

    engine = engine_with(cfg, [discovered()], journal=Broken())
    assert engine.memory.active is False
    assert engine.evaluate(context()) is not None


# --- persistence round trip -------------------------------------------------

def test_strategies_round_trip(cfg):
    original = [discovered(score=0.7), discovered(entry_feature="spread",
                                                  entry_op="<", score=0.6)]
    path = cfg.data_dir / "strategies.json"
    save(path, original)
    from pqb.research import load_strategies
    loaded = load_strategies(path)
    assert len(loaded) == 2
    assert loaded[0].signature == original[0].signature
    assert loaded[0].rule["entry_feature"] == "flow_z"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert "featureColumns" in payload


# --- engineered features: the rules are discovered over these ---------------

def _seed_history(store, token="T1", rows=120, market="M1"):
    import time as _t
    from pqb.features import FEATURE_NAMES
    start = _t.time() - rows * 60
    payload = []
    for i in range(rows):
        feats = {name: 0.0 for name in FEATURE_NAMES}
        feats.update({"price": 0.40 + (i % 7) * 0.01, "flow_z": (i % 5) - 2.0,
                      "liquidity": 50_000.0, "is_active": 1.0})
        payload.append((start + i * 60, token, market, "Yes", "Politics", feats))
    store.record_research_rows(payload)


def test_engineered_columns_reach_the_rule(cfg, intel_store):
    """Discovery searches the bridge's ENGINEERED features, not the raw ones.

    A rule names `flow_z_z`, not `flow_z`. Without live engineering the lookup
    misses, every rule counts as stale, and the researched view is silently
    switched off with nothing in the logs to say so.
    """
    pytest.importorskip("pandas")
    from pqb.quant import available
    ok, why = available()
    if not ok:
        pytest.skip(f"Quant Bridge unavailable: {why.splitlines()[0]}")

    _seed_history(intel_store)
    save(cfg.data_dir / "strategies.json", [discovered(entry_feature="flow_z_z")])
    engine = LeanDecisionEngine(cfg.engine, config=cfg,
                                intel_store=intel_store)
    ctx = context()
    engine.evaluate(ctx)

    row = engine._features_for(ctx.markets["M1"], quote(), ctx)
    assert "flow_z_z" in row, "engineered columns are missing from the live row"
    # And the raw columns remain addressable alongside them.
    assert "flow_z" in row and "price" in row


def test_without_history_it_degrades_to_the_raw_row(cfg, intel_store):
    from pqb.features import FEATURE_NAMES
    save(cfg.data_dir / "strategies.json", [discovered(entry_feature="flow_z_z")])
    engine = LeanDecisionEngine(cfg.engine, config=cfg,
                                intel_store=intel_store)
    ctx = context()
    row = engine._features_for(ctx.markets["M1"], quote(), ctx)
    # No captured history yet: the raw contract still holds, and the rule that
    # needs an engineered column counts as stale rather than as "did not fire".
    assert set(row) == set(FEATURE_NAMES)
    _score, parts, _ = engine._entry_score(ctx.markets["M1"], quote(), "T1",
                                           ctx, time.time())
    assert parts["rulesFired"] == 0
    assert parts["rulesTotal"] == 1


def test_engineering_is_cached_within_a_cycle(cfg, intel_store):
    pytest.importorskip("pandas")
    from pqb.quant import available
    if not available()[0]:
        pytest.skip("Quant Bridge unavailable")

    _seed_history(intel_store)
    save(cfg.data_dir / "strategies.json", [discovered(entry_feature="flow_z_z")])
    engine = LeanDecisionEngine(cfg.engine, config=cfg,
                                intel_store=intel_store)
    ctx = context()
    engine.evaluate(ctx)
    engine._features_for(ctx.markets["M1"], quote(), ctx)
    assert "T1" in engine.live_features._cache

    engine.live_features.begin_cycle("a-new-cycle")
    assert engine.live_features._cache == {}


# --- column reduction: the live path has to fit inside a cycle --------------

def test_required_columns_covers_bases_and_cross_column_inputs():
    from pqb.bridge.live_features import required_columns
    needed = required_columns({"flow_z_z", "ask_vel", "bidask_imbalance_vel",
                               "px_velocity_5", "wallet_weighted_expansion"})
    # Bases resolved by longest-prefix match.
    assert {"flow_z", "ask", "wallet_weighted"} <= needed
    # price is unconditional: the bridge's global features derive from it.
    assert "price" in needed
    # EVERY bid/ask-tagged column, because the engineer's cross-column families
    # aggregate all of them — a subset would leave `bidask_imbalance_vel` named
    # the same while changing what it measures.
    assert {"bid", "ask", "bid_depth", "ask_depth"} <= needed
    # And it is a genuine reduction, not the whole vector.
    assert len(needed) < len(FEATURE_NAMES_LEN)


FEATURE_NAMES_LEN = __import__("pqb.features", fromlist=["x"]).FEATURE_NAMES


def test_longest_prefix_wins():
    from pqb.bridge.live_features import required_columns
    # `spread_rel_vel` must map to `spread_rel`, not to `spread`.
    needed = required_columns({"spread_rel_vel"})
    assert "spread_rel" in needed


def test_engine_declares_entry_and_filter_features(cfg):
    engine = engine_with(cfg, [
        discovered(entry_feature="flow_z_z", filter_feature="ask_vel",
                   filter_op=">", filter_threshold=0.0),
    ])
    assert engine.referenced_features == {"flow_z_z", "ask_vel"}


def test_reduction_is_abandoned_if_it_changes_a_value(cfg, intel_store):
    """The guard, not the optimisation, is the load-bearing part.

    If a reduced frame ever changed a referenced feature, the rule would keep
    its name and quietly measure something else. That must cost speed, never
    correctness.
    """
    pytest.importorskip("pandas")
    from pqb.quant import available
    if not available()[0]:
        pytest.skip("Quant Bridge unavailable")
    from pqb.bridge.live_features import LiveFeatureEngineer

    _seed_history(intel_store)
    eng = LiveFeatureEngineer(intel_store)
    eng.set_referenced_features({"flow_z_z"})
    assert eng._columns is not None                 # reduction proposed

    # Force a mismatch by claiming a feature the reduced frame cannot produce.
    eng._verified = False
    eng._referenced = {"a_feature_only_the_full_frame_has"}
    history = intel_store.research_series("T1")
    eng._verify_reduction(history, {n: 0.5 for n in FEATURE_NAMES_LEN})
    # Either it verified cleanly (the name is absent from both, so not ours) or
    # it fell back — never a silent partial.
    assert eng._columns is None or eng._verified


def test_changing_the_strategy_set_reclears_the_cache(cfg, intel_store):
    from pqb.bridge.live_features import LiveFeatureEngineer
    eng = LiveFeatureEngineer(intel_store)
    eng.set_referenced_features({"flow_z_z"})
    eng._cache["T1"] = {"x": 1.0}
    eng.set_referenced_features({"ask_vel"})
    assert eng._cache == {}
    assert eng._verified is False


# --- learning mode (the operator's capital-preservation rule) -----------------

def test_learning_mode_blocks_entries_until_strategies_exist(tmp_path):
    """No validated strategies -> no entries, exits unaffected."""
    from pqb.config import Config

    cfg = Config()
    cfg.root = tmp_path
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    assert engine.strategies == []
    reason = engine._entry_block_reason(None, 0)
    assert "Learning mode" in reason


def test_learning_mode_off_restores_normal_gating(tmp_path):
    from pqb.config import Config
    from test_engine import context

    cfg = Config()
    cfg.root = tmp_path
    cfg.engine.entry.require_strategies = False
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    ctx = context(balance=100.0)
    assert "Learning mode" not in (engine._entry_block_reason(ctx, 0) or "")
