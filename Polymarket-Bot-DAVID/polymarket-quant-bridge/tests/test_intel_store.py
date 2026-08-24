"""The intel store: ingestion, de-duplication, rollups and retention."""

from __future__ import annotations

import time

from conftest import trade


def test_records_and_deduplicates(intel_store):
    rows = [trade("0xAAA", 1000), trade("0xBBB", 1001)]
    assert intel_store.record_trades(rows) == 2
    # The same observation arriving again from the other sweep must not become
    # a second trade: it would double every wallet's apparent activity.
    assert intel_store.record_trades(rows) == 0
    assert intel_store.stats()["trades"] == 2


def test_wallet_identity_is_lowercased_not_lost(intel_store):
    intel_store.record_trades([trade("0xAbCdEf", 1000)])
    summaries = intel_store.wallet_summaries()
    assert [s["wallet"] for s in summaries] == ["0xabcdef"]
    assert summaries[0]["trades"] == 1


def test_same_wallet_different_trades_both_kept(intel_store):
    intel_store.record_trades([
        trade("0xAAA", 1000, usdc=100.0),
        trade("0xAAA", 1000, usdc=250.0),      # same second, different size
        trade("0xAAA", 1001, usdc=100.0),      # same size, different second
    ])
    assert intel_store.stats()["trades"] == 3


def test_resolution_first_write_wins(intel_store):
    intel_store.record_resolution("T1", "M1", 1.0)
    intel_store.record_resolution("T1", "M1", 0.0)
    # Settlement is a fact every historical wallet score was computed against;
    # a later transient read must not silently rewrite it.
    assert intel_store.resolutions() == {"T1": 1.0}


def test_rollup_is_idempotent(intel_store):
    base = int(time.time()) - 7200
    intel_store.record_trades([trade("0xAAA", base + i, usdc=50.0)
                               for i in range(10)])
    intel_store.rollup()
    first = intel_store.query("SELECT gross_usdc FROM market_flow")
    intel_store.rollup()
    second = intel_store.query("SELECT gross_usdc FROM market_flow")
    assert first == second
    assert sum(r["gross_usdc"] for r in first) == 500.0


def test_prune_rolls_up_before_deleting(intel_store):
    old = int(time.time()) - 20 * 86400
    intel_store.record_trades([trade("0xAAA", old + i * 60, usdc=40.0)
                               for i in range(5)])
    hour = int(time.time() // 3600)

    removed = intel_store.prune(max_age_days=1.0)
    assert removed == 5
    assert intel_store.stats()["trades"] == 0
    # The whole point: the flow baseline outlives the raw rows it came from.
    baseline = intel_store.market_baseline("M1", before_hour=hour, hours=1000)
    assert baseline and sum(baseline) == 200.0


def test_market_baseline_excludes_the_current_hour(intel_store):
    now = time.time()
    hour = int(now // 3600)
    intel_store.record_trades([trade("0xAAA", int(now), usdc=9_999.0)])
    intel_store.rollup()
    # A spike must never be compared against a window that already contains it.
    assert intel_store.market_baseline("M1", before_hour=hour) == []


def test_research_rows_round_trip(intel_store):
    now = time.time()
    intel_store.record_research_rows([
        (now + i, "T1", "M1", "Yes", "Politics", {"price": 0.4 + i / 100})
        for i in range(5)
    ])
    assert intel_store.research_tokens(min_rows=5)[0]["rows"] == 5
    assert intel_store.research_tokens(min_rows=6) == []
    series = intel_store.research_series("T1")
    assert len(series) == 5
    assert series[0]["price"] == 0.4


def test_scores_round_trip(intel_store):
    from pqb.models import WalletIntel
    intel_store.save_scores([
        WalletIntel(wallet="0xaaa", label="alpha", rank=1, score=0.8,
                    confidence=0.5, sample=25, in_cohort=True),
    ])
    loaded = intel_store.load_scores()
    assert loaded["0xaaa"].rank == 1
    assert loaded["0xaaa"].in_cohort is True
    # Re-saving updates in place rather than duplicating the wallet.
    intel_store.save_scores([WalletIntel(wallet="0xaaa", rank=7, score=0.2)])
    assert intel_store.load_scores()["0xaaa"].rank == 7
    assert len(intel_store.load_scores()) == 1


# --- O(1) dashboard stats -----------------------------------------------------

def test_meta_roundtrips(intel_store):
    intel_store.set_meta("observed_wallets", 42_031.0)
    assert intel_store.get_meta("observed_wallets") == 42_031.0
    intel_store.set_meta("observed_wallets", 50_000.0)   # upsert
    assert intel_store.get_meta("observed_wallets") == 50_000.0
    assert intel_store.get_meta("missing") is None


def test_a_documented_column_cannot_break_startup(tmp_path):
    """A permanent guard on a real outage.

    The migration derives wanted columns from the schema TEXT. A column
    carrying a trailing inline comment produced
    `ALTER TABLE t ADD COLUMN foo TEXT DEFAULT '' -- note`, where the comment
    swallows the statement terminator; SQLite refused it with "incomplete
    input", the store failed to open, and the whole analytical layer went
    down at startup — because of a comment.

    Build the database at the PREVIOUS shape (current schema minus the newest
    columns), open it with the current code, and require them to arrive.
    """
    import re
    import sqlite3

    from pqb.analytics.store import _SCHEMA, IntelStore

    new_columns = ("settled_ts", "settled_source")
    kept = []
    for line in _SCHEMA.splitlines():
        stripped = line.split("--")[0].strip()
        if not stripped:
            continue                       # comment-only or blank
        if any(stripped.startswith(c) for c in new_columns):
            continue                       # the columns being migrated in
        kept.append(stripped)
    # Dropping the last column in a table leaves the one before it with a
    # dangling comma. Tidy it, or the fixture fails for a reason that has
    # nothing to do with what is under test.
    old_schema = re.sub(r",(\s*\);)", r"\1", "\n".join(kept))

    path = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(path))
    conn.executescript(old_schema)
    conn.commit()
    before = {r[1] for r in conn.execute("PRAGMA table_info(resolutions)")}
    conn.close()
    assert not set(new_columns) & before, "fixture did not build an OLD shape"

    store = IntelStore(path)                       # must not raise
    try:
        after = {r["name"] for r in
                 store.query("PRAGMA table_info(resolutions)")}
        assert set(new_columns) <= after, f"did not migrate: {after}"

        # ...and the migrated columns actually work end to end.
        store.record_resolution("T1", "M1", 1.0, settled_ts=1_699_000_000.0,
                                settled_source="gamma_closed")
        assert store.settlement_times()["T1"] == (1_699_000_000.0,
                                                  "gamma_closed")
    finally:
        store.close()
