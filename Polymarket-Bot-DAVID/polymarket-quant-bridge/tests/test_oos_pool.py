"""The persistent OOS pool: settled series built once, breadth unlimited.

The bottleneck this removes: validation could only draw from the ~48
series exported per pass while the store held hundreds of settled markets.
Pinned: the cache builds incrementally and NEVER rebuilds (settled series
are immutable), thin tapes are remembered as ineligible forever, the pool
survives restarts, and the per-strategy market ledger makes every
validation decision reconstructible (§14).
"""

from __future__ import annotations

import json

import pytest

from pqb.analytics.store import IntelStore
from pqb.config import Config
from pqb.models import WalletTrade
from pqb.research import ensure_oos_pool


def _trade(wallet, ts, token, market, price):
    return WalletTrade(wallet=wallet, ts=ts, market_id=market, token_id=token,
                       outcome="Yes", side="BUY", price=price, size=10.0,
                       usdc=price * 10.0, question="q?", tx="",
                       source="backfill")


def _fill_market(store, token, market, n=400, base=0.45):
    trades = [_trade(f"0xw{i % 5}", 1_000_000 + i * 120, token, market,
                     base + 0.10 * ((i % 20) - 10) / 10.0)
              for i in range(n)]
    store.record_trades(trades)
    store.record_resolution(token, market, 1.0)


@pytest.fixture()
def project(tmp_path):
    cfg = Config()
    cfg.root = tmp_path
    cfg.research.min_rows = 50
    store = IntelStore(tmp_path / "intel.sqlite3")
    yield cfg, store, tmp_path / "pool"
    store.close()


def test_pool_builds_and_persists(project):
    cfg, store, pool_root = project
    for i in range(3):
        _fill_market(store, f"tok{i}", f"M{i}")
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 3
    assert len(entries) == 3
    assert {e["marketId"] for e in entries} == {"M0", "M1", "M2"}
    assert all((pool_root / f"pool_tok{i}" / "features.csv").exists()
               for i in range(3))
    index = json.loads((pool_root / "pool-index.json").read_text())
    assert all(index[f"tok{i}"]["eligible"] for i in range(3))


def test_settled_series_are_built_exactly_once(project):
    """Immutability is the whole economy: pass two builds NOTHING new."""
    cfg, store, pool_root = project
    for i in range(3):
        _fill_market(store, f"tok{i}", f"M{i}")
    ensure_oos_pool(store, pool_root, cfg)
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 0
    assert len(entries) == 3


def test_pool_fills_incrementally_richest_first(project):
    cfg, store, pool_root = project
    cfg.research.oos_pool_build_per_pass = 2
    _fill_market(store, "rich", "MR", n=600)
    _fill_market(store, "mid", "MM", n=400)
    _fill_market(store, "small", "MS", n=300)
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 2
    assert {e["marketId"] for e in entries} == {"MR", "MM"}   # richest first
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 1                                          # catches up
    assert len(entries) == 3


def test_thin_tapes_marked_ineligible_forever(project):
    cfg, store, pool_root = project
    _fill_market(store, "thin", "MT", n=20)      # far below min_rows
    _fill_market(store, "good", "MG", n=400)
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 1
    assert {e["marketId"] for e in entries} == {"MG"}
    index = json.loads((pool_root / "pool-index.json").read_text())
    assert index["thin"]["eligible"] is False
    # And it is never retried:
    _, built2, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built2 == 0


def test_new_settlements_join_later_passes(project):
    cfg, store, pool_root = project
    _fill_market(store, "tok0", "M0")
    ensure_oos_pool(store, pool_root, cfg)
    _fill_market(store, "tok1", "M1")            # settles later
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 1
    assert len(entries) == 2


def test_disabled_build_still_serves_cache(project):
    cfg, store, pool_root = project
    _fill_market(store, "tok0", "M0")
    ensure_oos_pool(store, pool_root, cfg)
    cfg.research.oos_pool_build_per_pass = 0
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 0 and len(entries) == 1


def test_lower_pool_floor_retries_thin_marks(project):
    """A tape marked thin under a stricter floor is re-tried when the
    eligibility floor drops — evidence supply widens without touching any
    validation threshold."""
    cfg, store, pool_root = project
    cfg.research.oos_pool_min_rows = 300      # strict: everything is thin
    _fill_market(store, "tok0", "M0", n=400)
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 0 and entries == []
    cfg.research.oos_pool_min_rows = 60       # floor drops -> retry
    entries, built, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built == 1
    assert len(entries) == 1
    # And a mark made under the SAME floor is never retried.
    _, built2, _stats = ensure_oos_pool(store, pool_root, cfg)
    assert built2 == 0


def test_reopen_single_market_rejections(tmp_path):
    """The operator's re-audit: a rejection carried by ONE market's
    evidence re-opens; one carried by two or more stands. Idempotent."""
    from pqb.library import StrategyLibrary

    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    try:
        solo = lib.upsert_candidate("solo", {"entry_feature": "x"}, "r")
        lib.record_validation(solo, "M1", trades=40, wins=5, pnl=-9.0,
                              drawdown=1.0)
        lib.set_status(solo, "rejected", "old-era rejection")
        broad = lib.upsert_candidate("broad", {"entry_feature": "y"}, "r")
        lib.record_validation(broad, "M1", trades=20, wins=3, pnl=-5.0,
                              drawdown=1.0)
        lib.record_validation(broad, "M2", trades=20, wins=2, pnl=-6.0,
                              drawdown=1.0)
        lib.set_status(broad, "rejected", "real failure across markets")

        assert lib.reopen_single_market_rejections() == 1
        statuses = {s["id"]: s["status"] for s in lib.all_strategies()}
        assert statuses[solo] == "validating"
        assert statuses[broad] == "rejected"       # two markets: stands
        reopened_row = next(s for s in lib.all_strategies()
                            if s["id"] == solo)
        assert "re-opened" in reopened_row["retired_reason"]
        assert lib.reopen_single_market_rejections() == 0   # idempotent
    finally:
        lib.close()


# -- §14: the market ledger --------------------------------------------------

def test_market_ledger_reconstructs_the_decision(tmp_path):
    from pqb.library import StrategyLibrary

    lib = StrategyLibrary(tmp_path / "library.sqlite3")
    try:
        sid = lib.upsert_candidate("sig", {"entry_feature": "x"}, "r")
        lib.record_validation(sid, "M1", trades=10, wins=6, pnl=2.0,
                              drawdown=0.3, period="2026-01")
        lib.record_validation(sid, "M2", trades=5, wins=2, pnl=-1.0,
                              drawdown=0.5, period="2026-02")
        ledger = lib.market_ledger(sid)
        assert [row["market_id"] for row in ledger] == ["M1", "M2"]
        assert ledger[0]["trades"] == 10 and ledger[1]["pnl"] == -1.0
        # The ledger sums to exactly the cumulative record — one source
        # of truth, reconstructible.
        cumulative = lib.cumulative(sid)
        assert cumulative["trades"] == sum(r["trades"] for r in ledger)
        assert cumulative["pnl"] == pytest.approx(
            sum(r["pnl"] for r in ledger))
    finally:
        lib.close()
