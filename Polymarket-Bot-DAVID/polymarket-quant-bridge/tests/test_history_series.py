"""Discovery must study wallet HISTORY, not only rows captured after boot.

David's correction: the bridge's value is studying the historical closed trades
of wallets. These tests lock in that settled markets' backfilled tapes become
research series discovery can run over — and that unsettled markets never do,
because history is only useful when the outcome is known.
"""

from __future__ import annotations

from pqb.analytics.history_series import build_series
from pqb.analytics.store import IntelStore
from pqb.models import WalletTrade


def _trade(wallet, ts, token, market, side, price, size):
    return WalletTrade(wallet=wallet, ts=ts, market_id=market, token_id=token,
                       outcome="Yes", side=side, price=price, size=size,
                       usdc=price * size, question="Will X win?", tx="",
                       source="backfill")


def _store(tmp_path) -> IntelStore:
    return IntelStore(tmp_path / "intel.sqlite3")


def _fill(store, token="T1", market="M1", n=300, resolve=True):
    trades = []
    for i in range(n):
        price = 0.40 + (i / n) * 0.5          # drifts 0.40 -> 0.90
        trades.append(_trade(f"0xw{i % 7}", 1_000_000 + i * 120, token, market,
                             "BUY" if i % 3 else "SELL", price, 10.0))
    store.record_trades(trades)
    if resolve:
        # A published settlement time, an hour after the tape stops. Without
        # one the countdown columns are honestly zero — see test_leakage.
        store.record_resolution(token, market, 1.0,
                                settled_ts=1_000_000 + n * 120 + 3600,
                                settled_source="gamma_closed")


def test_settled_tape_becomes_a_series(tmp_path):
    store = _store(tmp_path)
    _fill(store)
    entries = build_series(store, min_rows=50, max_tokens=5)
    assert len(entries) == 1
    series = entries[0]["series"]
    assert len(series) >= 50
    # Price is real and moves with the tape; the book columns are neutral.
    assert series[0]["price"] > 0
    assert series[-1]["price"] > series[0]["price"]      # the drift survived
    assert series[0]["bid"] == series[0]["price"]        # pinned, no fake book
    assert series[0]["quote_is_live"] == 0.0


def test_unsettled_markets_are_excluded(tmp_path):
    """History is only trainable when the outcome is known."""
    store = _store(tmp_path)
    _fill(store, resolve=False)
    assert build_series(store, min_rows=50) == []


def test_time_to_resolution_counts_down(tmp_path):
    store = _store(tmp_path)
    _fill(store)
    series = build_series(store, min_rows=50)[0]["series"]
    assert series[0]["hours_to_resolution"] > series[-1]["hours_to_resolution"]
    assert series[-1]["hours_to_resolution"] >= 0.0


def test_thin_tapes_are_refused(tmp_path):
    store = _store(tmp_path)
    _fill(store, n=20)          # far too few trades to mean anything
    assert build_series(store, min_rows=200) == []


def test_resolution_adapts_to_trade_density():
    """A deep tape gets fine bars; a thin tape is not stretched into empty
    buckets that would repeat the last price as a fake flat stretch."""
    from pqb.analytics.history_series import TARGET_ROWS, rows_for

    assert rows_for(100_000) == TARGET_ROWS       # dense: full resolution
    assert rows_for(900) == 300                    # sparse: 3 trades per row
    # A thin tape is NOT padded up to a floor. 10 prints buy 3 rows, and 3
    # rows is below the useful minimum, so the tape is rejected with a
    # reason rather than stretched into 200 mostly-empty buckets.
    assert rows_for(10) == 3


def test_short_dense_market_is_admitted():
    """The audit's headline bottleneck: a market that settled in minutes but
    traded thousands of times used to be discarded for being 'short'. Wall
    clock is a backstop on bucket width now, never an admission test."""
    from pqb.analytics.history_series import plan_series

    # 11 minutes, 4,000 prints — one of the densest tapes in the store.
    plan = plan_series(trade_count=4_000, span_seconds=660, min_rows=80)
    assert plan.admitted
    assert plan.rows >= 80
    assert plan.bucket_seconds < 60          # the old floor would have barred it


def test_thin_tape_is_rejected_with_a_reason_not_stretched():
    from pqb.analytics.history_series import MIN_BUCKET_SECONDS, plan_series

    nothing = plan_series(trade_count=6, span_seconds=86_400, min_rows=80)
    assert not nothing.admitted
    assert nothing.reason == "insufficient_trades"

    # 90 prints over 8 days buys 30 rows. Asked for 80, it is refused —
    # never padded to 80 by repeating the last price.
    sparse = plan_series(trade_count=90, span_seconds=691_200, min_rows=80)
    assert not sparse.admitted
    assert sparse.reason == "insufficient_observations"
    assert sparse.rows == 30

    assert MIN_BUCKET_SECONDS == 1           # the clock's resolution, no more


def test_every_rejection_is_counted_with_its_reason(tmp_path):
    """Section 2: expose total settled, admitted, and the reason for every
    rejection — so 'the pool is small' is always answerable with 'because'."""
    store = _store(tmp_path)
    _fill(store, token="THICK", market="MTHICK", n=300)
    _fill(store, token="THIN", market="MTHIN", n=12)

    stats: dict = {}
    entries = build_series(store, min_rows=50, max_tokens=5, stats=stats)

    assert [e["tokenId"] for e in entries] == ["THICK"]
    assert stats["settledMarkets"] == 2
    assert stats["considered"] == 2
    assert stats["admitted"] == 1
    assert stats["rejected"] == 1
    assert stats["rejectedBy"] == {"insufficient_trades": 1}


def test_historical_export_carries_the_tapes_clock(tmp_path):
    """The walk-forward split orders markets by lastTs. Historical exports
    without one sorted at epoch zero — silently un-newest-ing the holdout."""
    from pqb.research import export_historical_series

    store = _store(tmp_path)
    _fill(store)
    written = export_historical_series(store, tmp_path / "exports",
                                       min_rows=50, max_tokens=5)
    assert len(written) == 1
    entry = written[0]
    assert entry["lastTs"] and float(entry["lastTs"]) > 1_000_000
    assert float(entry["firstTs"]) < float(entry["lastTs"])
