"""Trade forensics: reconstruction, attribution, counterfactuals, restraint.

The behaviour under test that matters most is the REFUSAL. A diagnostic layer
that always finds something to change is a curve fitter with a report
generator attached, so several of these tests assert that nothing is proposed:
on a thin sample, on a protective exit, and on an account whose losses are its
own variance.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from pqb.analytics import forensics
from pqb.journal import _SCHEMA as JOURNAL_SCHEMA

BASE = 1_700_000_000.0


def _journal(tmp_path, trades, cycles=True):
    """A journal with the given closed trades, in the real schema."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "journal.sqlite3"
    conn = sqlite3.connect(path)
    conn.executescript(JOURNAL_SCHEMA)
    for i, spec in enumerate(trades, start=1):
        entry_ts = spec.get("entry_ts", BASE + i * 3_600)
        hold = spec.get("hold", 900.0)
        entry_price = spec.get("entry_price", 0.50)
        exit_price = spec.get("exit_price", 0.45)
        size = spec.get("size", 10.0)
        pnl = spec.get("pnl", (exit_price - entry_price) * size)
        decision_id = 1000 + i
        conn.execute(
            "INSERT INTO decisions(id, ts, action, market_id, token_id, "
            "outcome, question, score, confidence, reason, features) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, entry_ts, "BUY", spec.get("market", f"mk{i}"),
             spec.get("token", f"tk{i}"), f"out{i}", f"q{i}",
             spec.get("score", 0.7), 0.5, "entered",
             json.dumps({"price": entry_price})))
        conn.execute(
            "INSERT INTO lifecycles(token_id, market_id, outcome, question, "
            "status, entry_decision_id, entry_ts, entry_price, entry_size, "
            "entry_cost, peak_price, trough_price, exit_ts, exit_price, "
            "exit_size, exit_reason, exit_style, realized_pnl, return_pct, "
            "hold_seconds, category, liquidity_bucket, ttr_bucket, "
            "wallet_influence, mode) "
            "VALUES(?,?,?,?,'CLOSED',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (spec.get("token", f"tk{i}"), spec.get("market", f"mk{i}"),
             f"out{i}", f"q{i}", decision_id, entry_ts, entry_price, size,
             entry_price * size,
             spec.get("peak", entry_price * 1.1),
             spec.get("trough", entry_price * 0.9),
             entry_ts + hold, exit_price, size, "because",
             spec.get("style", "stop"), pnl,
             pnl / (entry_price * size), hold,
             spec.get("category", "sports"), "deep", "hours",
             spec.get("wallet", ""), "dry_run"))
        life_id = conn.execute("SELECT MAX(id) FROM lifecycles").fetchone()[0]
        conn.execute(
            "INSERT INTO executions(ts, decision_id, lifecycle_id, order_id, "
            "token_id, side, requested_size, limit_price, filled_size, "
            "avg_price, fee, status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry_ts, decision_id, life_id, f"o{i}",
             spec.get("token", f"tk{i}"), "BUY", size, entry_price, size,
             entry_price, spec.get("fee", 0.01), "FILLED"))
    if cycles:
        for i in range(len(trades) + 2):
            conn.execute(
                "INSERT INTO cycles(cycle_id, ts, markets, positions, "
                "portfolio_value, balance) VALUES(?,?,?,?,?,?)",
                (f"c{i}", BASE + i * 3_600, 25, min(i, 3), 100.0 - i, 50.0))
    conn.commit()
    conn.close()
    return path


def _intel(tmp_path, series):
    """An intel store carrying research_rows for the counterfactual."""
    from pqb.analytics.store import IntelStore

    store = IntelStore(tmp_path / "intel.sqlite3")
    store.record_research_rows(
        [(ts, token, "mk", "out", "sports", {"price": price, "bid": price})
         for token, points in series.items() for ts, price in points])
    store.close()
    return tmp_path / "intel.sqlite3"


# -- 1. reconstruction -------------------------------------------------------


def test_every_closed_trade_is_reconstructed(tmp_path):
    path = _journal(tmp_path, [{}] * 7)
    trades = forensics.reconstruct(path)
    assert len(trades) == 7
    assert all(t.lifecycle_id for t in trades)
    assert all(t.entry_price > 0 and t.exit_ts > 0 for t in trades)


def test_fees_and_portfolio_context_are_joined_in(tmp_path):
    path = _journal(tmp_path, [{"fee": 0.05}])
    trade = forensics.reconstruct(path)[0]
    assert trade.fees == pytest.approx(0.05)
    assert trade.fills == 1
    assert trade.equity_at_entry > 0
    assert trade.size_pct_of_equity > 0
    # Net is what the journal recorded; gross adds our own costs back.
    assert trade.gross_pnl == pytest.approx(trade.realized_pnl + 0.05)


def test_missing_fields_are_named_not_defaulted(tmp_path):
    """A zero fee and an unrecorded fee lead to opposite conclusions."""
    path = _journal(tmp_path, [{"fee": 0.0}], cycles=False)
    trade = forensics.reconstruct(path)[0]
    assert "venue_fees" in trade.unavailable
    assert "portfolio_context" in trade.unavailable
    assert trade.equity_at_entry == 0.0
    # And the gap is reported for the operator to instrument.
    data = forensics.report(path)
    gaps = data["instrumentationGaps"]
    assert "portfolio_context" in gaps["counts"]
    assert gaps["instrumentNext"]["portfolio_context"]


def test_an_empty_journal_reports_unavailable_not_zero(tmp_path):
    path = _journal(tmp_path, [])
    data = forensics.report(path)
    assert data["available"] is False
    assert "no closed trades" in data["reason"]


# -- 2. attribution ----------------------------------------------------------


def test_exit_attribution_splits_loss_and_profit_shares(tmp_path):
    path = _journal(tmp_path, [
        {"style": "stop", "pnl": -3.0},
        {"style": "stop", "pnl": -2.0},
        {"style": "take_profit", "pnl": +4.0},
        {"style": "edge_gone", "pnl": -1.0},
    ])
    data = forensics.exit_attribution(forensics.reconstruct(path))
    styles = data["byExitStyle"]
    assert styles["stop"]["shareOfTotalLoss"] == pytest.approx(5.0 / 6.0, abs=1e-4)
    assert styles["take_profit"]["shareOfTotalProfit"] == pytest.approx(1.0)
    assert data["destroyingMostValue"] == "stop"
    assert data["preservingMostValue"] == "take_profit"


def test_shares_are_computed_against_loss_not_against_a_negative_net(tmp_path):
    """Against a negative net, a share would flip sign and reverse the
    ranking — the biggest loser would read as the biggest profit source."""
    path = _journal(tmp_path, [{"style": "stop", "pnl": -10.0},
                               {"style": "wallet", "pnl": +1.0}])
    data = forensics.exit_attribution(forensics.reconstruct(path))
    assert data["byExitStyle"]["stop"]["shareOfTotalLoss"] == pytest.approx(1.0)
    assert data["byExitStyle"]["stop"]["shareOfTotalProfit"] == 0.0


def test_every_bucket_carries_its_sample_size(tmp_path):
    path = _journal(tmp_path, [{"style": "stop"}] * 3)
    data = forensics.report(path)
    for stats in data["exitAttribution"]["byExitStyle"].values():
        assert "trades" in stats
        assert stats["claimable"] is False       # 3 trades is an anecdote


# -- 3. counterfactuals ------------------------------------------------------


def test_counterfactual_finds_a_premature_exit(tmp_path):
    """Exited at 0.45; the market went to 0.60 half an hour later."""
    path = _journal(tmp_path, [
        {"token": "tk1", "entry_price": 0.50, "exit_price": 0.45,
         "style": "stop", "hold": 600.0}])
    trade = forensics.reconstruct(path)[0]
    intel = _intel(tmp_path, {"tk1": [(trade.exit_ts + 1_800.0, 0.60)]})
    history = forensics.PriceHistory(intel)
    try:
        data = forensics.counterfactuals(trade and [trade], history)
    finally:
        history.close()
    horizon = data["byHorizon"]["+30m"]
    assert horizon["answered"] == 1
    assert horizon["holdingWouldHaveBeenBetter"] == 1
    assert horizon["totalDelta"] > 0


def test_counterfactual_finds_a_protective_exit(tmp_path):
    path = _journal(tmp_path, [
        {"token": "tk1", "entry_price": 0.50, "exit_price": 0.45,
         "style": "stop", "hold": 600.0}])
    trade = forensics.reconstruct(path)[0]
    intel = _intel(tmp_path, {"tk1": [(trade.exit_ts + 1_800.0, 0.10)]})
    history = forensics.PriceHistory(intel)
    try:
        data = forensics.counterfactuals([trade], history)
    finally:
        history.close()
    assert data["byHorizon"]["+30m"]["holdingWouldHaveBeenWorse"] == 1
    assert data["byHorizon"]["+30m"]["totalDelta"] < 0


def test_a_horizon_with_no_data_is_not_available_not_zero(tmp_path):
    path = _journal(tmp_path, [{"token": "tk1"}])
    trade = forensics.reconstruct(path)[0]
    intel = _intel(tmp_path, {"tk1": [(trade.exit_ts + 1_800.0, 0.6)]})
    history = forensics.PriceHistory(intel)
    try:
        data = forensics.counterfactuals([trade], history)
    finally:
        history.close()
    # +1m has no snapshot within tolerance; it must say so.
    assert data["byHorizon"]["+1m"]["answered"] == 0
    assert data["byHorizon"]["+1m"]["notAvailable"] == 1
    assert data["byHorizon"]["+1m"]["claimable"] is False


def test_counterfactuals_never_touch_realised_pnl(tmp_path):
    path = _journal(tmp_path, [{"token": "tk1", "pnl": -2.0}])
    trade = forensics.reconstruct(path)[0]
    intel = _intel(tmp_path, {"tk1": [(trade.exit_ts + 1_800.0, 0.99)]})
    data = forensics.report(path, intel, starting_balance=100.0)
    # The counterfactual is enormous...
    assert data["counterfactual"]["byHorizon"]["+30m"]["totalDelta"] > 0
    # ...and the account's realised record is untouched by it.
    assert data["account"]["realisedPnl"] == pytest.approx(-2.0)
    # ...and the journal itself is unchanged.
    conn = sqlite3.connect(path)
    stored = conn.execute(
        "SELECT realized_pnl FROM lifecycles").fetchone()[0]
    conn.close()
    assert stored == pytest.approx(-2.0)


def test_the_module_never_writes_to_the_journal(tmp_path):
    path = _journal(tmp_path, [{}] * 5)
    before = path.read_bytes()
    forensics.report(path, starting_balance=100.0)
    assert path.read_bytes() == before


# -- 4. sizing, clustering, costs -------------------------------------------


def test_sizing_is_not_blamed_when_per_dollar_returns_do_not_worsen(tmp_path):
    """The trap this test exists to stop: 'the account is down, so trade
    smaller' — proposed without evidence that size is the mechanism."""
    specs = []
    for i in range(30):
        specs.append({"size": 4.0 if i % 2 else 40.0, "pnl": -0.5 if i % 2
                      else -5.0, "entry_price": 0.5})
    path = _journal(tmp_path, specs)
    data = forensics.sizing_forensics(forensics.reconstruct(path))
    assert data["sizingIsAmplifying"] is False
    assert "not its sizing" in data["reading"]


def test_correlated_positions_are_not_counted_as_independent(tmp_path):
    """Three overlapping positions in one market are one bet held thrice."""
    specs = [{"entry_ts": BASE, "hold": 10_000.0, "market": "same",
              "category": "sports"} for _ in range(3)]
    path = _journal(tmp_path, specs)
    data = forensics.correlated_exposure(forensics.reconstruct(path))
    assert data["simultaneousPairs"] == 3
    assert data["correlatedPairs"] == 3
    assert data["correlatedShare"] == pytest.approx(1.0)


def test_cost_drag_is_distinguished_from_a_dead_strategy(tmp_path):
    drag = _journal(tmp_path / "a", [{"pnl": -0.02, "fee": 0.05}] * 10)
    data = forensics.cost_analysis(forensics.reconstruct(drag))
    assert data["classification"].startswith("COST_DRAG")

    dead = _journal(tmp_path / "b", [{"pnl": -2.0, "fee": 0.001}] * 10)
    data = forensics.cost_analysis(forensics.reconstruct(dead))
    assert data["classification"].startswith("NEGATIVE_GROSS")


def test_loss_clusters_distinguish_concentration_from_variance(tmp_path):
    concentrated = _journal(tmp_path / "a", (
        [{"market": "bad", "pnl": -5.0}] * 6
        + [{"market": f"ok{i}", "pnl": -0.2} for i in range(6)]))
    data = forensics.loss_clusters(forensics.reconstruct(concentrated))
    assert data["worstMarketShareOfLoss"] > 0.35
    assert "CONCENTRATED" in data["verdict"]

    dispersed = _journal(tmp_path / "b",
                         [{"market": f"m{i}", "pnl": -1.0} for i in range(12)])
    data = forensics.loss_clusters(forensics.reconstruct(dispersed))
    assert "DISPERSED" in data["verdict"]


def test_losing_streaks_are_measured(tmp_path):
    path = _journal(tmp_path, [{"pnl": -1.0}] * 4 + [{"pnl": +1.0}]
                    + [{"pnl": -1.0}] * 2)
    data = forensics.loss_clusters(forensics.reconstruct(path))
    assert data["longestLosingStreak"] == 4


# -- 5. winner preservation --------------------------------------------------


def test_upside_capture_and_the_winner_tail_are_reported(tmp_path):
    specs = [{"pnl": -0.5, "peak": 0.52, "trough": 0.44} for _ in range(9)]
    specs.append({"pnl": +9.0, "peak": 0.90, "trough": 0.49})
    path = _journal(tmp_path, specs)
    data = forensics.upside_capture(forensics.reconstruct(path))
    assert data["available"]
    assert data["topFiveWinnerShareOfProfit"] == pytest.approx(1.0)
    assert len(data["topFiveWinners"]) == 5


def test_excursion_analysis_reports_both_sides_of_the_stop_tradeoff(tmp_path):
    path = _journal(tmp_path, [
        {"pnl": +2.0, "peak": 0.70, "trough": 0.40,
         "style": "take_profit"},
        {"pnl": -2.0, "peak": 0.51, "trough": 0.30, "style": "stop"}])
    data = forensics.excursion_analysis(forensics.reconstruct(path))
    assert data["available"]
    assert data["winners"]["sample"] == 1
    assert data["stopped"]["sample"] == 1
    assert "also lets every loser run further" in data["note"]


# -- 6. restraint: the hypotheses ------------------------------------------


def test_no_hypothesis_below_the_sample_floor(tmp_path):
    path = _journal(tmp_path, [{"pnl": -5.0, "style": "stop"}] * 10)
    data = forensics.report(path, starting_balance=100.0)
    assert data["hypotheses"] == []
    assert "No change recommended" in forensics.daily_report(data)


def test_a_protective_exit_produces_a_no_change_finding(tmp_path):
    specs = [{"token": f"tk{i}", "style": "wallet", "pnl": -0.2,
              "entry_price": 0.50, "exit_price": 0.48}
             for i in range(45)]
    path = _journal(tmp_path, specs)
    trades = forensics.reconstruct(path)
    # After every wallet exit, the market collapsed.
    intel = _intel(tmp_path, {t.token_id: [(t.exit_ts + 1_800.0, 0.05)]
                              for t in trades})
    data = forensics.report(path, intel, starting_balance=100.0)
    proposals = data["hypotheses"]
    wallet = [h for h in proposals if h["key"] == "exit-keep::wallet"]
    assert wallet, proposals
    assert wallet[0]["status"] == "NO_CHANGE"
    # And nothing proposes loosening it.
    assert not [h for h in proposals if h["key"] == "exit-later::wallet"]


def test_a_premature_exit_produces_a_hypothesis_with_a_test_and_a_risk(
        tmp_path):
    specs = [{"token": f"tk{i}", "style": "edge_gone", "pnl": -0.3,
              "entry_price": 0.50, "exit_price": 0.47}
             for i in range(45)]
    path = _journal(tmp_path, specs)
    trades = forensics.reconstruct(path)
    intel = _intel(tmp_path, {t.token_id: [(t.exit_ts + 1_800.0, 0.75)]
                              for t in trades})
    data = forensics.report(path, intel, starting_balance=100.0)
    found = [h for h in data["hypotheses"] if h["key"] == "exit-later::edge_gone"]
    assert found
    proposal = found[0]
    assert proposal["status"] == "PROPOSED"
    # It is a QUESTION with an address, not an instruction.
    assert "new risk-policy version" in proposal["proposal"].lower() \
        or "NEW risk-policy version" in proposal["proposal"]
    assert "not used to find it" in proposal["test"]
    assert proposal["risk"]
    # It proposes changing the RISK POLICY, never the validated signal.
    assert "signal" in proposal["proposal"]


def test_hypotheses_never_carry_an_applied_flag(tmp_path):
    """Nothing in the running system reads these, and the shape says so."""
    proposal = forensics.PolicyHypothesis(
        key="k", title="t", evidence="e", proposal="p", test="x").to_dict()
    assert "applied" not in proposal
    assert "enabled" not in proposal
    assert proposal["status"] == "PROPOSED"


def test_the_daily_report_states_that_nothing_was_applied(tmp_path):
    path = _journal(tmp_path, [{}] * 5)
    text = forensics.daily_report(forensics.report(path, starting_balance=100.0))
    assert "Nothing in this report has been applied" in text
    assert "cannot alter an exit rule" in text


def test_trade_quality_ignores_the_outcome(tmp_path):
    """Conditions at entry only, so 'bad conditions' stays distinguishable
    from 'bad luck'."""
    winner = forensics.TradeRecord(entry_score=0.8, entry_cost=10.0,
                                   realized_pnl=+50.0, hold_seconds=600)
    loser = forensics.TradeRecord(entry_score=0.8, entry_cost=10.0,
                                  realized_pnl=-50.0, hold_seconds=600)
    assert forensics.trade_quality(winner) == forensics.trade_quality(loser)


def test_the_full_report_runs_end_to_end(tmp_path):
    specs = [{"token": f"tk{i}", "style": ["stop", "edge_gone", "wallet",
                                           "take_profit"][i % 4],
              "pnl": (-1.0 if i % 3 else +2.0),
              "market": f"mk{i % 5}", "category": ["sports", "crypto"][i % 2]}
             for i in range(60)]
    path = _journal(tmp_path, specs)
    trades = forensics.reconstruct(path)
    intel = _intel(tmp_path, {t.token_id: [(t.exit_ts + 300.0, 0.5)]
                              for t in trades})
    data = forensics.report(path, intel, starting_balance=100.0)
    assert data["available"]
    for key in ("account", "exitAttribution", "byHoldingPeriod",
                "entryQuality", "excursions", "sizing", "lossClusters",
                "correlatedExposure", "costs", "upsideCapture",
                "counterfactual", "contributors", "tradeQuality",
                "hypotheses", "instrumentationGaps"):
        assert key in data, key
    text = forensics.daily_report(data)
    for heading in ("ACCOUNT STATE", "DRAWDOWN STATE", "EXIT ANALYSIS",
                    "COUNTERFACTUAL FINDINGS", "HOLDING-PERIOD ANALYSIS",
                    "POSITION-SIZING ANALYSIS", "CORRELATED-EXPOSURE ANALYSIS",
                    "COST ANALYSIS", "TOP LOSS / PROFIT CONTRIBUTORS",
                    "WINNER PRESERVATION", "RESEARCH HYPOTHESES GENERATED"):
        assert heading in text, heading
