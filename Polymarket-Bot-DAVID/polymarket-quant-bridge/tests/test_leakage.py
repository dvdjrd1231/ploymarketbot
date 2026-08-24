"""No future data reaches a historical row. Three channels, all closed.

The audit named all three. Each is subtle in the same way: the leaked value is
plausible, the series still builds, the backtest still runs, and the discovered
rule looks unusually good — because it is reading, at every row, a summary of
what had not happened yet.
"""

from __future__ import annotations

from pqb.analytics.history_series import _series_for, build_series
from pqb.analytics.store import IntelStore
from pqb.models import WalletTrade


def _tape(n=300, start=1_700_000_000, step=120):
    rows = []
    price = 0.40
    for i in range(n):
        price = min(0.95, price + 0.001)
        rows.append({"ts": start + i * step, "price": price, "size": 10.0,
                     "usdc": price * 10.0,
                     "side": "BUY" if i % 3 else "SELL",
                     "wallet": f"0xw{i % 9}"})
    return rows


def _trade(wallet, ts, token, market, side, price):
    return WalletTrade(wallet=wallet, ts=ts, market_id=market, token_id=token,
                       outcome="Yes", side=side, price=price, size=10.0,
                       usdc=price * 10.0, question="Q?", tx="",
                       source="backfill")


# -- 1. wallet outcome leakage ------------------------------------------------

def test_wallet_skill_columns_are_never_populated_from_settled_outcomes(
        tmp_path):
    """Requirement 7. Wallet ranks are computed FROM how markets settled —
    including the markets being replayed — so they can never be a feature on a
    historical row."""
    store = IntelStore(tmp_path / "intel.sqlite3")
    trades = [_trade(f"0xw{i % 9}", 1_700_000_000 + i * 120, "T1", "M1",
                     "BUY" if i % 3 else "SELL", 0.40 + i * 0.001)
              for i in range(300)]
    store.record_trades(trades)
    store.record_resolution("T1", "M1", 1.0)

    series = build_series(store, min_rows=50, max_tokens=3)[0]["series"]

    assert all(row["wallet_weighted"] == 0.0 for row in series)
    assert all(row["wallet_best_score"] == 0.0 for row in series)
    # Activity is a tape fact and survives — the leak was skill, not presence.
    assert any(row["wallet_entries"] > 0 for row in series)
    assert any(row["wallet_exits"] > 0 for row in series)


# -- 2. end-of-tape leakage ---------------------------------------------------

def test_countdown_uses_published_settlement_not_the_end_of_the_tape():
    """Requirement 8. `last_ts - ts` is the distance to the final print, and
    nobody knows where the final print is until it happens."""
    tape = _tape()
    tape_end = tape[-1]["ts"]
    # Settlement published for a week AFTER the tape stops trading.
    settled = tape_end + 7 * 86_400

    series = _series_for("tok", tape, scores={}, settled_ts=settled)

    # Against the tape's end the first row would read ~10 hours. Against the
    # real settlement it is ~178. The distinction is the whole point.
    assert series[0]["hours_to_resolution"] > 100
    assert series[-1]["hours_to_resolution"] > 100
    assert series[0]["hours_to_resolution"] > series[-1]["hours_to_resolution"]


def test_unknown_settlement_leaves_the_countdown_at_zero_not_guessed():
    """An honest unknown. Constant across the series, so the feature-validity
    domain refuses any rule that leans on it."""
    series = _series_for("tok", _tape(), scores={}, settled_ts=0.0)

    assert {row["hours_to_resolution"] for row in series} == {0.0}
    assert {row["lifecycle_pct"] for row in series} == {0.0}


def test_lifecycle_pct_is_not_computed_from_the_eventual_span():
    """`(ts - first) / span` told a row two hours in that it was 12% through
    a market whose length was not yet decided."""
    tape = _tape()
    settled = tape[-1]["ts"] + 7 * 86_400

    series = _series_for("tok", tape, scores={}, settled_ts=settled)

    assert series[0]["lifecycle_pct"] < series[-1]["lifecycle_pct"]
    # The tape covers ~10 hours of a ~178-hour market, so the last row is
    # nowhere near the end. Under the old formula it was exactly 1.0.
    assert series[-1]["lifecycle_pct"] < 0.2


# -- 3. real settlement timestamps --------------------------------------------

def test_settlement_time_is_the_markets_not_the_databases(tmp_path):
    """Requirement 9. `resolutions.ts` is when we wrote the row; a backfill
    run today stamps a market that closed last year with today's date."""
    store = IntelStore(tmp_path / "intel.sqlite3")
    real = 1_699_000_000.0

    store.record_resolution("T1", "M1", 1.0, settled_ts=real,
                            settled_source="gamma_closed")

    when, source = store.settlement_times()["T1"]
    assert when == real
    assert source == "gamma_closed"
    # The bookkeeping timestamp still exists, and is emphatically not this.
    row = store.query("SELECT ts FROM resolutions WHERE token_id='T1'")[0]
    assert row["ts"] > real


def test_a_known_settlement_time_is_never_overwritten(tmp_path):
    store = IntelStore(tmp_path / "intel.sqlite3")
    store.record_resolution("T1", "M1", 1.0, settled_ts=1_699_000_000.0,
                            settled_source="gamma_closed")
    store.record_resolution("T1", "M1", 1.0, settled_ts=1_500_000_000.0,
                            settled_source="gamma_end")

    assert store.settlement_times()["T1"] == (1_699_000_000.0, "gamma_closed")


def test_a_missing_settlement_time_can_be_filled_in_later(tmp_path):
    """Learning the real date is new information; a changed price would be a
    contradiction. The two are treated differently on purpose."""
    store = IntelStore(tmp_path / "intel.sqlite3")
    store.record_resolution("T1", "M1", 1.0)
    assert "T1" not in store.settlement_times()

    store.record_resolution("T1", "M1", 1.0, settled_ts=1_699_000_000.0,
                            settled_source="gamma_end")
    assert store.settlement_times()["T1"][0] == 1_699_000_000.0


def test_last_trade_backfill_is_labelled_as_an_estimate(tmp_path):
    store = IntelStore(tmp_path / "intel.sqlite3")
    store.record_trades([_trade("0xa", 1_700_000_500, "T1", "M1", "BUY", 0.5)])
    store.record_resolution("T1", "M1", 1.0)

    assert store.backfill_settlement_times() == 1
    when, source = store.settlement_times()["T1"]
    assert when == 1_700_000_500
    assert source == "last_trade"          # named as the estimate it is


def test_gamma_settlement_fields_are_parsed_in_preference_order():
    from pqb.analytics.backfill import _settlement_moment

    closed = {"closedTime": "2025-03-01T12:00:00Z",
              "endDate": "2025-04-01T00:00:00Z"}
    assert _settlement_moment(closed)[1] == "gamma_closed"

    scheduled = {"endDate": "2025-04-01T00:00:00Z"}
    when, source = _settlement_moment(scheduled)
    assert source == "gamma_end" and when > 1_700_000_000

    # Epoch milliseconds are recognised as milliseconds, not year 55000.
    assert _settlement_moment({"closedTime": 1_740_000_000_000})[0] == \
        1_740_000_000.0
    # Nothing on record is zero with an empty source, never a plausible guess.
    assert _settlement_moment({"question": "?"}) == (0.0, "")
