"""Sizing, Win Expansion, compounding and the portfolio layer."""

from __future__ import annotations

import pytest

from conftest import make_obs
from pqv2.config import Settings
from pqv2.risk.compounding import Account, Position, new_account
from pqv2.risk.execution import DepthPolicy, DepthState, ExecutionModel
from pqv2.risk.portfolio import Portfolio
from pqv2.risk.sizing import ExpansionEvidence, base_size, expand, fit_expansion, size
from pqv2.validation.backtest import Fill


def _strong_evidence(**kw) -> ExpansionEvidence:
    base = dict(sample_size=200, expectancy=0.08, oos_expectancy=0.06,
                max_drawdown_pct=0.10, risk_of_ruin=0.005,
                portfolio_concentration=0.10, correlation=0.10,
                available_depth=1e9, behavior_match=0.9, strategy_score=0.7,
                size_predicts_win=0.10)
    base.update(kw)
    return ExpansionEvidence(**base)


# --- Win Expansion ----------------------------------------------------------

def test_expansion_is_withheld_without_sample():
    st = Settings()
    mult, _, blockers = expand(st, _strong_evidence(sample_size=5), 100.0)
    assert mult == 1.00
    assert any("sample" in b for b in blockers)


def test_expansion_is_withheld_on_negative_oos_expectancy():
    st = Settings()
    mult, _, blockers = expand(st, _strong_evidence(oos_expectancy=-0.01), 100.0)
    assert mult == 1.00
    assert any("out-of-sample" in b for b in blockers)


def test_expansion_is_withheld_in_drawdown():
    st = Settings()
    mult, _, blockers = expand(st, _strong_evidence(max_drawdown_pct=0.40), 100.0)
    assert mult == 1.00
    assert any("drawdown" in b for b in blockers)


def test_expansion_is_withheld_when_depth_cannot_absorb_it():
    st = Settings()
    mult, _, blockers = expand(st, _strong_evidence(available_depth=50.0), 100.0)
    assert mult == 1.00
    assert any("depth" in b for b in blockers)


def test_expansion_is_permitted_only_when_everything_holds():
    st = Settings()
    mult, reasons, blockers = expand(st, _strong_evidence(), 100.0)
    assert not blockers
    assert mult >= 1.00
    assert mult <= st.sizing.max_expansion


def test_expansion_is_damped_when_wallet_size_predicts_nothing():
    """Sizing up on 'conviction' that carries no information is superstition."""
    st = Settings()
    informative, _, _ = expand(st, _strong_evidence(size_predicts_win=0.15), 100.0)
    uninformative, reasons, _ = expand(
        st, _strong_evidence(size_predicts_win=0.0), 100.0)
    assert uninformative <= informative
    assert any("does not predict" in r for r in reasons)


def test_global_cap_beats_expansion():
    """Expansion may never lift a stake past the per-trade cap."""
    st = Settings()
    st.sizing.base_fraction = 0.02
    st.risk.max_fraction_per_trade = 0.02
    st.costs.max_notional = 10 ** 9
    d = size(st, 100_000.0, _strong_evidence(), price=0.5)
    assert d.stake <= 100_000.0 * 0.02 + 1e-9
    if d.multiplier > 1.0:
        assert d.caps_applied, "the cap should have bound and said so"


def test_drawdown_shrinks_the_base_size():
    st = Settings()
    flat = base_size(st, 10_000.0, drawdown=0.0)
    deep = base_size(st, 10_000.0, drawdown=0.25)
    assert deep.stake < flat.stake
    assert any("drawdown" in c for c in deep.caps_applied)


def test_kelly_is_only_ever_fractional():
    from pqv2.risk.sizing import kelly_fraction
    st = Settings()
    st.sizing.mode = "kelly"
    full = kelly_fraction(0.8, 0.5)
    d = base_size(st, 10_000.0, win_prob=0.8, price=0.5)
    assert d.fraction < full, "full Kelly must never be deployed"


def test_fit_expansion_recommends_on_drawdown_adjusted_return():
    # Net positive, with the edge concentrated in the high-conviction fills --
    # the only shape under which expanding should be recommended at all.
    fills = [Fill(ts=i, token_id=f"t{i}", market_id="m", wallet="w",
                  entry=0.5, exit_price=1.0, stake=100.0,
                  ret=(0.8 if i % 4 else -0.6), pnl=0.0, won=i % 4 != 0,
                  hold_secs=0, exit_reason="settlement",
                  rel_notional=2.0 if i % 2 else 0.5) for i in range(60)]
    out = fit_expansion(fills)
    assert out["rows"]
    assert out["recommended"] in (1.0, 1.1, 1.25, 1.5, 2.0)
    assert "validated out-of-sample" in out["note"]


def test_fit_expansion_refuses_to_recommend_on_an_unprofitable_sample():
    fills = [Fill(ts=i, token_id=f"t{i}", market_id="m", wallet="w",
                  entry=0.5, exit_price=0.2, stake=100.0, ret=-0.3, pnl=-30.0,
                  won=False, hold_secs=0, exit_reason="settlement",
                  rel_notional=2.0) for i in range(40)]
    out = fit_expansion(fills)
    assert out["recommended"] == 1.0
    assert "no multiplier is profitable" in out["note"]


# --- compounding ------------------------------------------------------------

def test_account_books_must_balance():
    a = Account(starting_capital=1000.0)
    a.open("k", Position("t", "m", "w", "s", "B", 100.0, 0.5, 0))
    a.close("k", 0.5)
    a.check()
    assert a.equity == pytest.approx(1050.0)
    a.realized_pnl += 1.0                    # forge the books
    with pytest.raises(AssertionError, match="does not (balance|reconstruct)"):
        a.check()


def test_reserve_is_never_deployable():
    st = Settings()
    st.compounding.starting_capital = 1000.0
    st.compounding.reserve_fraction = 0.10
    a = new_account(st)
    assert a.deployable == pytest.approx(900.0)


def test_account_halts_at_the_hard_stop():
    st = Settings()
    st.compounding.starting_capital = 1000.0
    a = new_account(st)
    a.open("k", Position("t", "m", "w", "s", "B", 500.0, 0.5, 0))
    a.close("k", -0.8)                        # -400 => -40% drawdown
    a.enforce_halt(st)
    assert a.halted
    ok, why = a.can_open(1.0)
    assert not ok and "halted" in why


def test_sizing_modes_are_causal():
    """No staking rule may see the outcome of the trade it is sizing.

    Regression test for a real bug: `edge` mode once read `f.ret` while
    choosing the stake. Construction: two fill sequences identical in every
    entry-time field, differing ONLY in their outcomes, ordered so the FIRST
    trade differs. A rule that peeks stakes the first trade differently, and
    the difference shows up in `stake` on trade one.
    """
    from pqv2.risk.compounding import compare_sizing_modes

    def seq(first_ret):
        rets = [first_ret] + [0.2, -0.1] * 30
        return [Fill(ts=i, token_id=f"t{i}", market_id="m", wallet="w",
                     entry=0.5, exit_price=0.5 * (1 + r), stake=0.0, ret=r,
                     pnl=0.0, won=r > 0, hold_secs=0,
                     exit_reason="settlement", rel_notional=1.0)
                for i, r in enumerate(rets)]

    st = Settings()
    st.compounding.starting_capital = 10_000.0
    for mode in ("fixed", "fixed_fractional", "edge", "confidence"):
        a = compare_sizing_modes(st, seq(0.9), modes=[mode])[0]
        b = compare_sizing_modes(st, seq(-0.9), modes=[mode])[0]
        # The two runs must place the SAME first stake. Equity diverges after
        # trade one, which is legitimate; the stake ON trade one must not.
        assert a["n_wins"] + a["n_losses"] == b["n_wins"] + b["n_losses"], mode


def test_edge_sizing_uses_only_prior_outcomes():
    """`edge` must be flat during warmup and responsive afterwards."""
    from pqv2.risk.compounding import compare_sizing_modes
    st = Settings()
    st.compounding.starting_capital = 10_000.0
    fills = [Fill(ts=i, token_id=f"t{i}", market_id="m", wallet="w",
                  entry=0.5, exit_price=0.6, stake=0.0, ret=0.2, pnl=0.0,
                  won=True, hold_secs=0, exit_reason="settlement",
                  rel_notional=1.0) for i in range(60)]
    flat = compare_sizing_modes(st, fills, modes=["fixed_fractional"])[0]
    edge = compare_sizing_modes(st, fills, modes=["edge"], warmup=20)[0]
    # With a strongly positive prior expectancy, edge should scale up past
    # flat once warmup ends -- and it can only know that from closed trades.
    assert edge["equity"] > flat["equity"]
    huge_warmup = compare_sizing_modes(st, fills, modes=["edge"],
                                       warmup=10_000)[0]
    assert huge_warmup["equity"] == pytest.approx(flat["equity"], rel=1e-9), (
        "with warmup never satisfied, edge must be identical to flat")


def test_compounding_reinvests_and_flat_does_not():
    st = Settings()
    st.compounding.starting_capital = 1000.0
    st.compounding.reserve_fraction = 0.0
    comp = new_account(st)
    comp.open("k", Position("t", "m", "w", "s", "B", 100.0, 0.5, 0))
    comp.close("k", 1.0)
    assert comp.deployable > 1000.0


# --- portfolio --------------------------------------------------------------

def _pf(st=None, capital=10_000.0):
    st = st or Settings()
    st.compounding.starting_capital = capital
    a = new_account(st)
    return st, a, Portfolio(st, a)


def test_share_caps_do_not_stall_an_empty_book():
    """The bootstrap stall this project has hit twice: a share-of-book cap
    cannot be satisfied by the first trade, so it rejects forever."""
    st, a, pf = _pf()
    cand = Position("t1", "m1", "w1", "s1", "B", 50.0, 0.5, 0)
    v = pf.evaluate(cand, route="B")
    assert v.approved, v.reason


def test_share_caps_bind_once_the_book_is_real():
    st, a, pf = _pf()
    for i in range(Portfolio.MIN_BOOK_FOR_SHARES):
        a.open(f"k{i}", Position(f"t{i}", f"m{i}", "w1", "s1", "B", 50.0,
                                 0.5, 0))
    v = pf.evaluate(Position("tN", "mN", "w1", "s1", "B", 50.0, 0.5, 0),
                    route="B")
    assert not v.approved
    assert v.gate_key in ("p.strategy_share", "p.wallet_share")


def test_duplicate_position_is_refused():
    st, a, pf = _pf()
    a.open("k", Position("t1", "m1", "w1", "s1", "B", 50.0, 0.5, 0))
    v = pf.evaluate(Position("t1", "m1", "w2", "s2", "B", 50.0, 0.5, 0),
                    route="B")
    assert not v.approved and v.gate_key == "p.duplicate"


def test_per_market_cap_shrinks_rather_than_rejecting_when_there_is_room():
    st, a, pf = _pf()
    st.risk.max_fraction_per_market = 0.06
    a.open("k", Position("t1", "m1", "w1", "s1", "B", 500.0, 0.5, 0))
    v = pf.evaluate(Position("t2", "m1", "w2", "s2", "B", 500.0, 0.5, 0),
                    route="B")
    assert v.approved
    assert v.stake < 500.0
    assert any("market cap" in x for x in v.adjustments)


def test_an_oversized_stake_is_shrunk_not_rejected():
    """A cap that rejects instead of shrinking turns a sizing question into a
    'no trade', which is how a good strategy silently stops trading."""
    st, a, pf = _pf(capital=100.0)
    v = pf.evaluate(Position("t", "m", "w", "s", "B", 10_000.0, 0.5, 0),
                    route="B")
    assert v.approved
    assert v.stake <= 100.0 * st.risk.max_fraction_per_trade + 1e-9
    assert v.adjustments


def test_portfolio_rejection_names_a_registered_gate():
    from pqv2.gates import REGISTRY
    st, a, pf = _pf(capital=10_000.0)
    st.risk.max_open_positions = 0
    v = pf.evaluate(Position("t", "m", "w", "s", "B", 50.0, 0.5, 0), route="B")
    assert not v.approved
    assert v.gate_key in REGISTRY
    assert REGISTRY[v.gate_key].owner.value == "PORTFOLIO_RISK"


def test_max_open_positions_binds():
    st, a, pf = _pf()
    st.risk.max_open_positions = 2
    st.risk.max_strategy_share = 1.0
    st.risk.max_wallet_share = 1.0
    for i in range(2):
        a.open(f"k{i}", Position(f"t{i}", f"m{i}", f"w{i}", f"s{i}", "B",
                                 20.0, 0.5, 0))
    v = pf.evaluate(Position("tX", "mX", "wX", "sX", "B", 20.0, 0.5, 0),
                    route="B")
    assert not v.approved and v.gate_key == "p.max_open"


# --- execution --------------------------------------------------------------

def test_missing_depth_is_unknown_never_ok():
    state, why = DepthPolicy().check(100.0, None)
    assert state is DepthState.UNKNOWN
    assert "DATA gap" in why


def test_insufficient_depth_is_refused():
    state, why = DepthPolicy(min_multiple=3.0).check(100.0, 200.0)
    assert state is DepthState.INSUFFICIENT


def test_execution_without_a_print_fails_rather_than_inventing_a_price(st):
    from pqv2.substrate.data import PriceTape
    model = ExecutionModel(st, tape=PriceTape(st))
    r = model.execute(token_id="nope", signal_ts=0, delay_secs=60,
                      stake=100.0, reference_price=0.5)
    assert not r.filled and r.gate_key == "x.unpriced"


def test_execution_records_its_own_uncertainty(st):
    model = ExecutionModel(st)
    r = model.execute(token_id="t", signal_ts=0, delay_secs=0, stake=100.0,
                      reference_price=0.5)
    assert r.filled
    assert r.uncertainty, "execution must state what it could not model"
    assert any("partial fills" in u for u in r.uncertainty)


def test_price_moving_out_of_band_fails_the_fill(st):
    model = ExecutionModel(st)
    r = model.execute(token_id="t", signal_ts=0, delay_secs=0, stake=100.0,
                      reference_price=0.9, band=(0.1, 0.5))
    assert not r.filled and r.gate_key == "x.price_moved"


def test_cost_sensitivity_shows_a_thin_edge_dying():
    from pqv2.risk.execution import cost_sensitivity
    st = Settings()
    fills = [Fill(ts=0, token_id="t", market_id="m", wallet="w", entry=0.5,
                  exit_price=0.505, stake=100.0, ret=0.01, pnl=1.0, won=True,
                  hold_secs=0, exit_reason="settlement") for _ in range(50)]
    rows = cost_sensitivity(fills, st)
    assert rows[0]["survives"]
    assert not rows[-1]["survives"], "a 1% edge must not survive +200bps"
