"""The consistency / loss-minimisation layer (surgical patch v2).

What these pin is not the thresholds — those are meant to move as research
moves them — but the four PROMISES the patch makes, each of which is a way the
layer could quietly become the thing it was built to avoid:

  1. it cannot reach a take-profit, a stop, or any other Layer 1 exit;
  2. shadow mode never changes a decision, however severe its finding;
  3. a losing position whose thesis is intact is held, and a position inside
     the range winners routinely use is held even when the thesis has failed;
  4. nothing is promoted out of shadow on win rate, on a small sample, or
     without out-of-sample evidence.

The first three are structural and are tested against the engine. The fourth
is the research layer's, and is tested against the promotion gate directly —
because a gate that can be talked into promoting something is the failure that
ends the account, and it is the one failure no live test would ever catch.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from pqb import consistency
from pqb.analytics import consistency_research as cr
from pqb.bridge.baseline_engine import BaselineDecisionEngine
from pqb.config import ConsistencyConfig, EngineConfig
from pqb.models import (
    AccountState, Action, BridgeContext, MarketFeatures, MarketStatus,
    OutcomeQuote, PositionView, WalletSignal,
)

NOW = time.time()


# --- fixtures ---------------------------------------------------------------

def market(market_id="m1", *, bid=0.50, ask=0.51, liquidity=50_000.0,
           token_id="tok1", spread=0.01) -> MarketFeatures:
    return MarketFeatures(
        market_id=market_id, question="Will it?", category="Test",
        status=MarketStatus.ACTIVE, end_ts=int(NOW + 86_400),
        quotes={token_id: OutcomeQuote(
            token_id=token_id, outcome="Yes", bid=bid, ask=ask,
            mid=(bid + ask) / 2, spread=spread, source="stream",
            bid_depth=1_000.0, ask_depth=1_000.0, updated_ts=NOW)},
        liquidity=liquidity, volume_24h=30_000.0, volume_total=100_000.0)


def position(*, entry=0.50, mark=0.50, peak=0.0, trough=0.0, size=100.0,
             held=3_600, token_id="tok1") -> PositionView:
    view = PositionView(
        token_id=token_id, market_id="m1", outcome="Yes", question="Will it?",
        size=size, avg_price=entry, cur_price=mark,
        opened_ts=int(NOW - held), end_ts=int(NOW + 86_400),
        peak_price=peak or max(entry, mark),
        trough_price=trough or min(entry, mark), lifecycle_id=1)
    setattr(view, "reduced_count", 0)
    return view


def thesis(**overrides) -> consistency.EntryThesis:
    """An entry with several checkable conditions, all healthy by default."""
    base = dict(score=0.70, market_score=0.70, wallet_score=0.60,
                wallet_influence="whale-1", price=0.50, liquidity=50_000.0,
                spread=0.01, market_state="3", ts=NOW - 3_600)
    base.update(overrides)
    return consistency.EntryThesis(**base)


def context(positions, markets, *, balance=100.0, equity=100.0,
            signals=None, market_state=None) -> BridgeContext:
    account = AccountState(balance=balance, position_value=equity - balance)
    return BridgeContext(
        cycle_id="c1", ts=NOW, account=account,
        markets={m.market_id: m for m in markets}, positions=positions,
        wallet_signals=signals or [], min_trade_size=0.19,
        market_state=market_state or {})


def engine(*, mode="shadow", **consistency_overrides) -> BaselineDecisionEngine:
    cfg = EngineConfig()
    cfg.consistency = ConsistencyConfig(mode=mode, **consistency_overrides)
    return BaselineDecisionEngine(cfg)


def attach(view: PositionView, entry_thesis, *, state="", streak=0):
    setattr(view, "entry_thesis", entry_thesis)
    setattr(view, "thesis_state", state)
    setattr(view, "thesis_streak", streak)
    return view


# ===========================================================================
# PROMISE 1 — Layer 2 cannot reach any Layer 1 exit
# ===========================================================================

def test_take_profit_is_untouchable_even_in_enforce_mode():
    """The engine that produced +30.8% average take-profits is the asset.

    A position at +40% with a totally failed thesis must still exit as a
    take-profit, because Layer 2 is never consulted on a decision that is not
    a HOLD — and take-profit is not a HOLD.
    """
    view = attach(position(entry=0.50, mark=0.70, peak=0.70),
                  thesis(liquidity=500_000.0, spread=0.001),
                  state=consistency.INVALIDATED, streak=99)
    decisions = engine(mode="enforce", min_adverse_room_pct=0.01,
                       confirm_cycles=1, grace_seconds=0).evaluate(
        context([view], [market(bid=0.70, ask=0.71, liquidity=10.0,
                                spread=0.40)]))
    exit_decision = next(d for d in decisions if d.token_id == "tok1")
    assert exit_decision.action is Action.EXIT
    assert exit_decision.exit_style == "take_profit"


def test_the_stop_is_untouchable_even_in_enforce_mode():
    view = attach(position(entry=0.50, mark=0.30, trough=0.30), thesis(),
                  state=consistency.INVALIDATED, streak=99)
    decisions = engine(mode="enforce", min_adverse_room_pct=0.01,
                       confirm_cycles=1, grace_seconds=0).evaluate(
        context([view], [market(bid=0.30, ask=0.31)]))
    exit_decision = next(d for d in decisions if d.token_id == "tok1")
    assert exit_decision.action is Action.EXIT
    assert exit_decision.exit_style == "stop"      # not safety_*


def test_layer_two_only_ever_converts_a_hold():
    """The structural guarantee, stated as a property of the method."""
    eng = engine(mode="enforce", min_adverse_room_pct=0.01, confirm_cycles=1,
                 grace_seconds=0)
    view = attach(position(entry=0.50, mark=0.30, trough=0.30), thesis())
    ctx = context([view], [market(bid=0.30, ask=0.31)])
    baseline = eng._baseline_position_verdict(view, ctx)
    final = eng._evaluate_position(view, ctx)
    # Layer 1 wanted an EXIT; the final decision is Layer 1's, unmodified.
    assert baseline.action is Action.EXIT
    assert final.exit_style == baseline.exit_style


# ===========================================================================
# PROMISE 2 — shadow mode changes nothing
# ===========================================================================

def _failed_setup():
    """A position whose thesis has comprehensively failed and is deep red."""
    view = attach(
        position(entry=0.50, mark=0.34, trough=0.34),
        thesis(liquidity=100_000.0, spread=0.01, market_state="3"),
        state=consistency.INVALIDATED, streak=5)
    # liquidity collapsed, spread blown out, market state changed
    return view, [market(bid=0.34, ask=0.40, liquidity=1_000.0, spread=0.06)]


def test_shadow_mode_records_the_verdict_and_changes_nothing():
    view, markets = _failed_setup()
    eng = engine(mode="shadow", min_adverse_room_pct=0.05, confirm_cycles=1,
                 grace_seconds=0)
    # Stop is disabled so Layer 1 genuinely holds and Layer 2 is consulted.
    eng.cfg.exits.stop_loss_pct = 0.0
    eng.cfg.exits.edge_exit_floor = 0.0
    decision = eng._evaluate_position(view, context([view], markets))

    assert decision.action is Action.HOLD
    verdict = decision.rationale["consistency"]
    assert verdict["triggered"] is True         # it had an opinion...
    assert verdict["enforced"] is False         # ...and did not act on it
    assert verdict["health"] == consistency.INVALIDATED


def test_enforce_mode_acts_on_the_same_verdict():
    """Shadow and enforce must compute the IDENTICAL verdict, so the A/B is
    a comparison of one rule in two modes rather than of two rules."""
    view, markets = _failed_setup()
    eng = engine(mode="enforce", min_adverse_room_pct=0.05, confirm_cycles=1,
                 grace_seconds=0)
    eng.cfg.exits.stop_loss_pct = 0.0
    eng.cfg.exits.edge_exit_floor = 0.0
    decision = eng._evaluate_position(view, context([view], markets))

    assert decision.action is Action.EXIT
    assert decision.exit_style == "safety_thesis_invalidated"
    assert decision.size_shares == view.size
    assert decision.rationale["consistency"]["enforced"] is True


@pytest.mark.parametrize("entry,mark,peak,trough,held", [
    (0.50, 0.50, 0.50, 0.50, 60),        # flat, fresh
    (0.50, 0.72, 0.72, 0.48, 7_200),     # take-profit territory
    (0.50, 0.30, 0.55, 0.30, 7_200),     # stop territory
    (0.50, 0.45, 0.62, 0.44, 3_600),     # trailing territory
    (0.50, 0.49, 0.51, 0.40, 90_000),    # long and going nowhere
    (0.20, 0.14, 0.21, 0.13, 1_800),     # cheap token, deep red
    (0.90, 0.93, 0.94, 0.88, 600),       # expensive token, small gain
])
def test_shadow_mode_is_a_no_op_across_the_decision_space(
        entry, mark, peak, trough, held):
    """The whole A/B rests on this: a shadow build must decide exactly what an
    unpatched build decides, for every position, not just for the one case a
    single test happened to construct."""
    markets = [market(bid=mark, ask=round(mark + 0.01, 4), liquidity=800.0,
                      spread=0.05)]

    def decide(mode):
        view = attach(position(entry=entry, mark=mark, peak=peak,
                               trough=trough, held=held),
                      thesis(liquidity=100_000.0),
                      state=consistency.INVALIDATED, streak=9)
        eng = engine(mode=mode, min_adverse_room_pct=0.01, confirm_cycles=1,
                     grace_seconds=0)
        return eng._evaluate_position(view, context([view], markets))

    shadow, off = decide("shadow"), decide("off")
    assert shadow.action is off.action
    assert shadow.exit_style == off.exit_style
    assert shadow.reason == off.reason
    assert shadow.size_shares == off.size_shares


def test_mode_off_computes_nothing_at_all():
    view, markets = _failed_setup()
    eng = engine(mode="off")
    eng.cfg.exits.stop_loss_pct = 0.0
    eng.cfg.exits.edge_exit_floor = 0.0
    decision = eng._evaluate_position(view, context([view], markets))
    assert "consistency" not in decision.rationale


def test_the_shipped_default_cannot_fire():
    """A build nobody has configured must behave exactly as it did before."""
    cfg = ConsistencyConfig()
    assert cfg.mode == "shadow"
    assert cfg.min_adverse_room_pct == 0.0     # blocks the thesis exit outright
    assert cfg.loss_tail_enabled is False
    assert cfg.profit_floor_arm_pct == 0.0

    view, markets = _failed_setup()
    eng = engine(mode="enforce")               # enforce, but default thresholds
    eng.cfg.exits.stop_loss_pct = 0.0
    eng.cfg.exits.edge_exit_floor = 0.0
    decision = eng._evaluate_position(view, context([view], markets))
    assert decision.action is Action.HOLD


# ===========================================================================
# PROMISE 3 — red is not the same as wrong
# ===========================================================================

def test_a_losing_position_with_an_intact_thesis_is_held():
    """Module 4, the central claim of the whole patch."""
    view = attach(position(entry=0.50, mark=0.40, trough=0.40), thesis())
    state = consistency.build_state(
        view, view.entry_thesis, conviction=0.65, now=NOW,
        market=market(bid=0.40, ask=0.41), quote=None, market_state_now="3")
    health = consistency.thesis_health(ConsistencyConfig(), state,
                                       view.entry_thesis)
    assert health.state == consistency.HEALTHY
    assert state.unrealized_pct < 0            # ...and it is losing


def test_pnl_is_not_an_input_to_thesis_health():
    """A winning position with failed conditions is INVALIDATED, and a losing
    one with intact conditions is HEALTHY. If the detector ever starts
    agreeing with the P&L it has become a stop with extra steps."""
    cfg = ConsistencyConfig()
    winner = position(entry=0.50, mark=0.75, peak=0.75)
    entry = thesis(liquidity=100_000.0)
    state = consistency.build_state(
        winner, entry, conviction=0.10, now=NOW,
        market=market(bid=0.75, ask=0.90, liquidity=1_000.0, spread=0.15),
        quote=None, wallet_exited=True, market_state_now="5")
    assert consistency.thesis_health(cfg, state, entry).state \
        == consistency.INVALIDATED
    assert state.unrealized_pct > 0


def test_winner_room_blocks_the_exit_inside_the_normal_adverse_range():
    """Module 6, enforced in code: below the distance a real winner routinely
    travels against us, Layer 2 does not act however certain it is."""
    view, markets = _failed_setup()          # position is about -32%
    eng = engine(mode="enforce", min_adverse_room_pct=0.50,   # winners need 50%
                 confirm_cycles=1, grace_seconds=0)
    eng.cfg.exits.stop_loss_pct = 0.0
    eng.cfg.exits.edge_exit_floor = 0.0
    decision = eng._evaluate_position(view, context([view], markets))
    assert decision.action is Action.HOLD
    verdict = decision.rationale["consistency"]
    assert verdict["triggered"] is False
    assert any("winner" in note for note in verdict["notes"])


def test_unmeasured_winner_room_blocks_the_exit_entirely():
    """0 means "we have not measured it", and an unmeasured distance must
    disable the rule rather than default it to something."""
    view, markets = _failed_setup()
    eng = engine(mode="enforce", min_adverse_room_pct=0.0, confirm_cycles=1,
                 grace_seconds=0)
    eng.cfg.exits.stop_loss_pct = 0.0
    eng.cfg.exits.edge_exit_floor = 0.0
    decision = eng._evaluate_position(view, context([view], markets))
    assert decision.action is Action.HOLD
    assert any("research" in n for n in
               decision.rationale["consistency"]["notes"])


def test_unknown_never_triggers_anything():
    """A position with no recorded thesis has no checkable conditions."""
    cfg = ConsistencyConfig(mode="enforce", min_adverse_room_pct=0.01,
                            confirm_cycles=1, grace_seconds=0)
    empty = consistency.EntryThesis()
    state = consistency.build_state(
        position(entry=0.50, mark=0.10, trough=0.10), empty,
        conviction=0.0, now=NOW)
    verdict = consistency.evaluate(cfg, state, empty)
    assert verdict.health == consistency.UNKNOWN
    assert verdict.triggered is False


def test_the_confirmation_streak_resets_on_any_other_reading():
    """A flickering condition must not accumulate its way to an exit."""
    cfg = ConsistencyConfig(mode="enforce", min_adverse_room_pct=0.01,
                            confirm_cycles=3, grace_seconds=0)
    entry = thesis()
    healthy = consistency.build_state(
        position(entry=0.50, mark=0.40, trough=0.40), entry,
        conviction=0.65, now=NOW, market=market(liquidity=50_000.0),
        market_state_now="3")
    verdict = consistency.evaluate(cfg, healthy, entry,
                                   prior_state=consistency.INVALIDATED,
                                   prior_streak=2)
    assert verdict.health == consistency.HEALTHY
    assert verdict.streak == 0
    assert verdict.triggered is False


def test_the_grace_window_silences_a_cold_first_cycle():
    view, markets = _failed_setup()
    eng = engine(mode="enforce", min_adverse_room_pct=0.01, confirm_cycles=1,
                 grace_seconds=86_400)          # longer than the hold
    eng.cfg.exits.stop_loss_pct = 0.0
    eng.cfg.exits.edge_exit_floor = 0.0
    decision = eng._evaluate_position(view, context([view], markets))
    assert decision.action is Action.HOLD
    assert decision.rationale["consistency"]["triggered"] is False


# ===========================================================================
# Module 7 / 8 — the two independent guards
# ===========================================================================

def test_the_loss_tail_guard_binds_on_the_account_not_the_position():
    cfg = ConsistencyConfig(mode="enforce", loss_tail_enabled=True,
                            max_single_trade_loss_pct=0.10, grace_seconds=0)
    entry = thesis()
    state = consistency.build_state(
        position(entry=0.50, mark=0.40, trough=0.40, size=100.0), entry,
        conviction=0.65, now=NOW, market=market(liquidity=50_000.0),
        market_state_now="3")
    # -$10 on a $50 account is 20% of equity; on a $1,000 account it is 1%.
    assert consistency.evaluate(cfg, state, entry, equity=50.0).triggered
    assert not consistency.evaluate(cfg, state, entry, equity=1_000.0).triggered


def test_the_loss_tail_guard_is_off_by_default():
    cfg = ConsistencyConfig(mode="enforce", grace_seconds=0)
    entry = thesis()
    state = consistency.build_state(
        position(entry=0.50, mark=0.10, trough=0.10), entry,
        conviction=0.65, now=NOW, market=market(), market_state_now="3")
    assert not consistency.evaluate(cfg, state, entry, equity=20.0).triggered


def test_the_profit_floor_will_not_stop_a_thirty_percent_winner_at_five():
    """The explicit requirement in Module 8, as an executable statement."""
    cfg = ConsistencyConfig(mode="enforce", profit_floor_arm_pct=0.25,
                            profit_floor_keep_fraction=0.5, grace_seconds=0)
    entry = thesis()
    # Ran to +10% only: below the arm level, so no floor exists at all.
    running = consistency.build_state(
        position(entry=0.50, mark=0.55, peak=0.55), entry,
        conviction=0.7, now=NOW, market=market(), market_state_now="3")
    assert not consistency.evaluate(cfg, running, entry).triggered

    # Ran to +30% and still holding +20%: above the 15% floor. Held.
    holding = consistency.build_state(
        position(entry=0.50, mark=0.60, peak=0.65), entry,
        conviction=0.7, now=NOW, market=market(), market_state_now="3")
    assert not consistency.evaluate(cfg, holding, entry).triggered

    # Ran to +30% and given it all back: below the floor. Banked.
    given_back = consistency.build_state(
        position(entry=0.50, mark=0.505, peak=0.65), entry,
        conviction=0.7, now=NOW, market=market(), market_state_now="3")
    assert consistency.evaluate(cfg, given_back, entry).style == "profit_floor"


# ===========================================================================
# PROMISE 4 — the promotion gate refuses by default
# ===========================================================================

def _metrics(**kw) -> cr.Metrics:
    base = dict(n=100, net=100.0, expectancy=1.0, win_rate=0.6,
                profit_factor=1.8, avg_winner=5.0, avg_loser=-3.0,
                largest_loser=-12.0, largest_winner=20.0, p95_loss=8.0,
                max_drawdown=20.0)
    base.update(kw)
    return cr.Metrics(**base)


def _candidate(parameters=2) -> cr.Candidate:
    return cr.Candidate("X", "test candidate", parameters=parameters)


def _forward(stable=4, total=5) -> dict:
    return {"available": True, "folds": [], "stable": stable, "total": total}


def test_a_better_win_rate_with_worse_expectancy_is_rejected():
    """Module 17's example, verbatim: 60% / +$100 vs 80% / -$20."""
    baseline = _metrics(win_rate=0.60, expectancy=100.0)
    candidate = _metrics(win_rate=0.80, expectancy=-20.0)
    guard = cr.win_rate_guard(candidate, baseline)
    assert guard["verdict"] == "reject"

    verdict = cr.promotion_verdict(
        _candidate(), candidate, baseline, _forward(),
        cr.composite_score(candidate, baseline, 2, 4, 5))
    assert verdict["promote"] is False


def test_a_worse_win_rate_with_better_expectancy_is_an_acceptance_candidate():
    """The other half of Module 17: 58% / +$145 with lower drawdown."""
    baseline = _metrics(win_rate=0.60, expectancy=100.0, max_drawdown=50.0)
    candidate = _metrics(win_rate=0.58, expectancy=145.0, max_drawdown=30.0)
    guard = cr.win_rate_guard(candidate, baseline)
    assert guard["verdict"] == "ok"
    assert "more money" in guard["reading"]

    verdict = cr.promotion_verdict(
        _candidate(), candidate, baseline, _forward(),
        cr.composite_score(candidate, baseline, 2, 4, 5))
    assert verdict["promote"] is True


def test_nothing_is_promoted_without_out_of_sample_evidence():
    baseline = _metrics()
    candidate = _metrics(expectancy=2.0)
    verdict = cr.promotion_verdict(
        _candidate(), candidate, baseline,
        {"available": False, "stable": 0, "total": 0},
        cr.composite_score(candidate, baseline, 2, 0, 0))
    assert verdict["promote"] is False
    assert any("out-of-sample" in f for f in verdict["failed"])


def test_nothing_is_promoted_on_a_sixteen_trade_sample():
    """The absolute rule at the top of the brief."""
    baseline = _metrics(n=16)
    candidate = _metrics(n=16, expectancy=5.0, max_drawdown=1.0)
    verdict = cr.promotion_verdict(
        _candidate(), candidate, baseline, _forward(),
        cr.composite_score(candidate, baseline, 2, 4, 5))
    assert verdict["promote"] is False
    assert any("16 trades" in f for f in verdict["failed"])


def test_a_candidate_that_shrinks_the_winners_is_rejected_however_good():
    """Module 22: the take-profit engine is what makes the money."""
    baseline = _metrics(avg_winner=10.0)
    candidate = _metrics(avg_winner=3.0, expectancy=5.0, max_drawdown=1.0,
                         p95_loss=0.5, largest_loser=-1.0)
    score = cr.composite_score(candidate, baseline, 1, 5, 5)
    verdict = cr.promotion_verdict(_candidate(1), candidate, baseline,
                                   _forward(5, 5), score)
    assert verdict["promote"] is False
    assert any("destroys profitable trades" in f for f in verdict["failed"])


def test_complexity_is_penalised_and_capped():
    baseline = _metrics()
    candidate = _metrics(expectancy=1.5)
    simple = cr.composite_score(candidate, baseline, 1, 4, 5)["score"]
    complex_ = cr.composite_score(candidate, baseline, 6, 4, 5)["score"]
    assert simple > complex_

    verdict = cr.promotion_verdict(_candidate(6), candidate, baseline,
                                   _forward(), cr.composite_score(
                                       candidate, baseline, 6, 4, 5))
    assert any("too many" in f for f in verdict["failed"])


def test_the_score_ignores_win_rate_entirely():
    """Two candidates identical but for win rate must score identically."""
    baseline = _metrics()
    a = _metrics(win_rate=0.10)
    b = _metrics(win_rate=0.95)
    assert cr.composite_score(a, baseline, 2, 4, 5)["score"] \
        == cr.composite_score(b, baseline, 2, 4, 5)["score"]


def test_an_extreme_improvement_on_a_tiny_sample_cannot_win():
    """A 40x expectancy improvement on four trades is a small sample."""
    baseline = _metrics()
    tiny = _metrics(n=4, expectancy=40.0)
    honest = _metrics(n=200, expectancy=1.3)
    assert cr.composite_score(honest, baseline, 1, 5, 5)["score"] \
        > cr.composite_score(tiny, baseline, 1, 1, 1)["score"]


# ===========================================================================
# Module 6 / 14 / 24 — the research measurements
# ===========================================================================

class _Trade:
    """A minimal TradeRecord stand-in for the distribution functions."""

    def __init__(self, pnl, entry=0.50, trough=0.40, peak=0.60, ts=0.0,
                 size=100.0, wallet="", hold=600.0):
        self.realized_pnl = pnl
        self.entry_price = entry
        self.entry_size = size
        self.entry_cost = entry * size
        self.entry_ts = ts
        self.exit_ts = ts + hold
        self.exit_price = entry + pnl / size
        self.hold_seconds = hold
        self.peak_price = peak
        self.trough_price = trough
        self.return_pct = pnl / (entry * size)
        self.unavailable = []
        self.wallet_influence = wallet
        self.category = "Test"
        self.liquidity_bucket = "deep"
        self.ttr_bucket = "day"
        self.exit_style = "stop" if pnl < 0 else "take_profit"
        self.token_id = "tok1"
        self.market_id = "m1"
        self.lifecycle_id = int(ts)
        self.fees = 0.0

    @property
    def mae(self):
        return self.trough_price / self.entry_price - 1.0

    @property
    def mfe(self):
        return self.peak_price / self.entry_price - 1.0


def test_winner_room_reports_what_a_tight_stop_would_have_cost():
    # Winners that routinely dip 20% before coming good.
    trades = [_Trade(5.0, trough=0.40, peak=0.70, ts=i) for i in range(10)]
    trades += [_Trade(-5.0, trough=0.30, peak=0.52, ts=100 + i)
               for i in range(6)]
    room = cr.winner_room(trades)
    assert room["available"]
    assert room["winners"]["median"] == pytest.approx(0.20, abs=0.01)

    tight = next(r for r in room["killedByStop"] if r["stopDistance"] == 0.15)
    assert tight["shareOfWinners"] == 1.0        # a 15% stop kills them all
    assert tight["profitDestroyed"] == pytest.approx(50.0)

    wide = next(r for r in room["killedByStop"] if r["stopDistance"] == 0.30)
    assert wide["shareOfWinners"] == 0.0


def test_winner_room_says_so_when_the_distributions_overlap():
    """The honest answer when no stop distance can separate the two."""
    trades = [_Trade(5.0, trough=0.30, peak=0.70, ts=i) for i in range(10)]
    trades += [_Trade(-5.0, trough=0.35, peak=0.52, ts=100 + i)
               for i in range(10)]
    assert "OVERLAP" in cr.winner_room(trades)["reading"]


def test_the_walk_forward_validates_strictly_forward_in_time():
    paths = [cr.TradePath(trade=_Trade(1.0, ts=float(i))) for i in range(60)]
    result = cr.walk_forward(cr.Candidate("A", "baseline"), paths, 0.0,
                             folds=4)
    assert result["available"]
    previous_end = -1.0
    for fold in result["folds"]:
        assert fold["from"] > previous_end     # each block is strictly later
        previous_end = fold["to"]


def test_the_walk_forward_refuses_a_sample_too_small_to_fold():
    paths = [cr.TradePath(trade=_Trade(1.0, ts=float(i))) for i in range(8)]
    result = cr.walk_forward(cr.Candidate("A", "baseline"), paths, 0.0)
    assert result["available"] is False
    assert result["total"] == 0


def test_a_candidate_that_never_fires_reproduces_the_baseline_exactly():
    """The replay's most important property: it changes only what it fires on."""
    paths = [cr.TradePath(trade=_Trade(1.0 if i % 2 else -2.0, ts=float(i)),
                          rows=[(float(i), 0.5, {}), (float(i) + 1, 0.5, {})],
                          replayable=True) for i in range(20)]
    outcomes = cr.replay(cr.Candidate("A", "baseline", rule=None), paths, 0.0)
    assert not any(o.changed for o in outcomes)
    assert cr.measure(outcomes).net == pytest.approx(
        sum(p.trade.realized_pnl for p in paths))


def test_a_candidate_firing_after_the_real_exit_is_not_credited():
    """Otherwise a rule gets the benefit of exits it did not make."""
    trade = _Trade(-5.0, ts=0.0, hold=100.0)
    path = cr.TradePath(trade=trade, rows=[(500.0, 0.90, {})],
                        replayable=True)
    fired = cr.Candidate("X", "always", rule=lambda p: (p.rows[0], "x"))
    assert cr.replay(fired, [path], 0.0)[0].changed is False


def test_wallet_shrinkage_will_not_let_a_five_trade_wallet_top_the_ranking():
    """Module 12, verbatim: not a 5-trade wallet over a 500-trade one."""
    trades = [_Trade(20.0, ts=float(i), wallet="lucky") for i in range(5)]
    trades += [_Trade(3.0 if i % 3 else -1.0, ts=100.0 + i, wallet="proven")
               for i in range(300)]
    result = cr.wallet_diagnostics(trades)
    lucky = next(w for w in result["wallets"] if w["wallet"] == "lucky")
    proven = next(w for w in result["wallets"] if w["wallet"] == "proven")

    assert lucky["rawExpectancy"] > proven["rawExpectancy"]   # raw: lucky wins
    assert lucky["trustworthy"] is False
    assert lucky["confidence"] < proven["confidence"]
    assert lucky["shrunkExpectancy"] < lucky["rawExpectancy"]


def test_protected_growth_measures_what_survived_the_drawdown():
    trades = [_Trade(10.0, ts=1), _Trade(10.0, ts=2), _Trade(-15.0, ts=3)]
    outcomes = [cr._actual(t) for t in trades]
    growth = cr.protected_growth(outcomes, starting_balance=100.0)
    assert growth["peakEquity"] == pytest.approx(120.0)
    assert growth["currentEquity"] == pytest.approx(105.0)
    assert growth["maxDrawdown"] == pytest.approx(15.0)
    assert growth["retainedFromPeak"] == pytest.approx(105.0 / 120.0)


def test_the_three_system_comparison_keeps_b_when_c_is_not_better():
    paths = [cr.TradePath(trade=_Trade(1.0, ts=float(i))) for i in range(20)]
    comparison = cr.compare_systems(paths, None, 0.0, 100.0)
    assert comparison["B_riskPatch"]["identicalToA"] is True
    assert cr.keep_or_reject(comparison)["decision"] == "KEEP B"


def test_c_is_rejected_when_it_shrinks_the_winners():
    comparison = {
        "B_riskPatch": {"metrics": {"expectancy": 1.0, "net": 100.0,
                                    "avgWinner": 10.0, "maxDrawdown": 20.0,
                                    "p95Loss": 8.0}},
        "C_consistency": {"metrics": {"expectancy": 1.1, "net": 110.0,
                                      "avgWinner": 4.0, "maxDrawdown": 5.0,
                                      "p95Loss": 2.0}},
    }
    decision = cr.keep_or_reject(comparison)
    assert decision["decision"] == "KEEP B"
    assert any("winners materially smaller" in r for r in decision["reasons"])


def test_the_loss_distribution_reports_the_tail_not_just_the_average():
    outcomes = [cr._actual(_Trade(-1.0, ts=i)) for i in range(19)]
    outcomes.append(cr._actual(_Trade(-50.0, ts=99)))
    losses = cr.loss_distribution(outcomes)
    assert losses["median"] == pytest.approx(1.0)
    assert losses["max"] == pytest.approx(50.0)
    assert losses["p95"] > losses["median"]      # the tail is visible


def test_coverage_names_what_could_not_be_replayed():
    paths = [cr.TradePath(trade=_Trade(1.0, ts=float(i)),
                          replayable=i < 6) for i in range(10)]
    result = cr.coverage(paths)
    assert result["replayable"] == 6
    assert result["unreplayable"] == 4
    assert result["share"] == pytest.approx(0.6)


# ===========================================================================
# persistence
# ===========================================================================

def test_the_journal_migrates_a_database_that_predates_the_patch(tmp_path):
    """An existing install must gain the new columns, not fail every write."""
    from pqb.journal import Journal

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(str(path))
    old.execute("CREATE TABLE lifecycles (id INTEGER PRIMARY KEY, "
                "token_id TEXT, status TEXT DEFAULT 'OPEN')")
    old.execute("INSERT INTO lifecycles(token_id) VALUES('tok1')")
    old.commit()
    old.close()

    journal = Journal(path)
    try:
        columns = {r[1] for r in journal._conn.execute(
            "PRAGMA table_info(lifecycles)")}
        assert {"thesis_state", "thesis_streak", "thesis_ts"} <= columns
        journal.record_thesis(1, consistency.WEAKENING, 2)
        row = journal._conn.execute(
            "SELECT thesis_state, thesis_streak FROM lifecycles "
            "WHERE id=1").fetchone()
        assert row["thesis_state"] == consistency.WEAKENING
        assert row["thesis_streak"] == 2
    finally:
        journal.close()


def test_the_shadow_record_is_written_once_at_the_moment_of_the_proposal(
        tmp_path):
    """Module 20 compares the proposal against what happened next, which only
    works if the row is the FIRST trigger and not the latest one."""
    from pqb.journal import Journal

    journal = Journal(tmp_path / "j.sqlite3")
    try:
        first = {"triggered": True, "style": "thesis_invalidated",
                 "reason": "first", "health": consistency.INVALIDATED,
                 "streak": 3, "enforced": False,
                 "state": {"price": 0.40, "returnPct": -0.20}}
        later = dict(first, reason="later", state={"price": 0.10,
                                                   "returnPct": -0.80})
        journal.record_consistency(1, "tok1", first)
        journal.record_consistency(1, "tok1", later)
        rows = journal.consistency_rows()
        assert len(rows) == 1
        assert rows[0]["reason"] == "first"
        assert rows[0]["price"] == pytest.approx(0.40)
    finally:
        journal.close()


def test_the_dashboard_panel_survives_an_empty_install(tmp_path):
    """§26's block must render on a bot that has never traded, not raise."""
    from pqb.config import Config
    from pqb.gui.reader import Reader

    cfg = Config()
    cfg.root = tmp_path
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    reader = Reader(cfg)

    data = reader.consistency()
    assert data["available"] is False
    assert data.get("reason")                 # says WHY, never just blank
    census = reader.thesis_census()
    assert census == {"HEALTHY": 0, "WEAKENING": 0, "INVALIDATED": 0,
                      "UNKNOWN": 0}


def test_the_thesis_census_counts_only_open_positions(tmp_path):
    """The panel answers "what am I holding", so a closed position must not
    appear in it however recently it closed."""
    from pqb.config import Config
    from pqb.gui.reader import Reader
    from pqb.journal import Journal

    cfg = Config()
    cfg.root = tmp_path
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    journal = Journal(cfg.journal_path)
    try:
        journal._conn.execute(
            "INSERT INTO lifecycles(token_id, status, thesis_state) "
            "VALUES('a','OPEN',?)", (consistency.WEAKENING,))
        journal._conn.execute(
            "INSERT INTO lifecycles(token_id, status, thesis_state) "
            "VALUES('b','CLOSED',?)", (consistency.INVALIDATED,))
        journal._conn.execute(
            "INSERT INTO lifecycles(token_id, status) VALUES('c','OPEN')")
        journal._conn.commit()
    finally:
        journal.close()

    census = Reader(cfg).thesis_census()
    assert census["WEAKENING"] == 1
    assert census["INVALIDATED"] == 0          # closed, so not in the book
    assert census["UNKNOWN"] == 1              # never read yet


# The §26 dashboard block is driven through the REAL Dashboard in
# tests/test_gui_flows.py, which owns the offscreen Qt harness. Building a
# second QApplication here crashes the interpreter, and two harnesses for one
# toolkit is how a suite ends up with a flaky module nobody can bisect.


def test_shadow_review_nets_avoided_loss_against_sacrificed_profit(tmp_path):
    """A rule that avoids $5 and sacrifices $20 must read as net negative."""
    from pqb.journal import Journal

    path = tmp_path / "j.sqlite3"
    journal = Journal(path)
    try:
        # Proposal 1: would have exited a loser early — avoids loss.
        journal.record_consistency(1, "tok1", {
            "triggered": True, "style": "thesis_invalidated", "health": "X",
            "state": {"price": 0.45}})
        # Proposal 2: would have exited a winner early — sacrifices profit.
        journal.record_consistency(2, "tok2", {
            "triggered": True, "style": "thesis_invalidated", "health": "X",
            "state": {"price": 0.55}})
    finally:
        journal.close()

    loser = _Trade(-10.0, ts=1)
    loser.lifecycle_id, loser.entry_price, loser.entry_size = 1, 0.50, 100.0
    winner = _Trade(20.0, ts=2)
    winner.lifecycle_id, winner.entry_price, winner.entry_size = 2, 0.50, 100.0

    review = cr.shadow_review(path, [loser, winner])
    assert review["available"]
    assert review["lossAvoided"] == pytest.approx(5.0)     # -5 instead of -10
    assert review["profitSacrificed"] == pytest.approx(15.0)  # +5 instead of +20
    assert review["net"] == pytest.approx(-10.0)
    assert review["winnersInterrupted"] == 1
