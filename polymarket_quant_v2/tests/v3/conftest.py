"""Test fixtures. Offline, no network, no real database required.

Every test here runs against a temporary store and a synthetic tape. Tests that
would need the client's 2.6 GB `intel.sqlite3` are skipped rather than silently
passing — a test suite that quietly does nothing when its data is absent is
worse than no suite, because it reports green.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pqv3.config import Settings                            # noqa: E402
from pqv3.core.store import Store                           # noqa: E402


@pytest.fixture
def st(tmp_path) -> Settings:
    s = Settings()
    s.work_dir = tmp_path / "var"
    s.data_db = tmp_path / "absent.sqlite3"
    s.collectors.enabled = False
    return s.ensure_dirs()


@pytest.fixture
def store(st) -> Store:
    return Store(st)


def build_tape(path: Path, *, trades: list, resolutions: list) -> None:
    """Write a minimal V1-shaped database.

    Same column names and types as the real `intel.sqlite3`, so a test that
    passes here is testing the same SQL that runs in production.
    """
    c = sqlite3.connect(str(path))
    c.executescript("""
      CREATE TABLE wallet_trades(
        id INTEGER PRIMARY KEY, wallet TEXT, ts INTEGER, market_id TEXT,
        token_id TEXT, outcome TEXT, side TEXT, price REAL, size REAL,
        usdc REAL, question TEXT, tx TEXT, source TEXT, event_type TEXT);
      CREATE TABLE resolutions(
        token_id TEXT PRIMARY KEY, price REAL, ts INTEGER, settled_ts INTEGER);
      CREATE TABLE raw_events(id INTEGER PRIMARY KEY);
    """)
    c.executemany(
        "INSERT INTO wallet_trades(wallet,ts,market_id,token_id,outcome,side,"
        "price,size,usdc,question,event_type) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        trades)
    c.executemany(
        "INSERT INTO resolutions(token_id,price,ts,settled_ts) VALUES(?,?,?,?)",
        resolutions)
    c.commit()
    c.close()


@pytest.fixture
def tape(tmp_path, st) -> Settings:
    """A small synthetic tape: two wallets, two markets, known outcomes."""
    db = tmp_path / "intel.sqlite3"
    base = 1_700_000_000
    trades = []
    for i in range(120):
        ts = base + i * 300
        trades.append(("0xalpha", ts, "MKT_A", "TOK_A", "Yes", "BUY",
                       0.40 + (i % 5) * 0.01, 10.0, 25.0, "Will A happen?",
                       "TRADE"))
    for i in range(80):
        ts = base + 600 + i * 400
        trades.append(("0xbeta", ts, "MKT_B", "TOK_B", "Yes", "BUY",
                       0.70 + (i % 3) * 0.01, 10.0, 40.0, "Will B happen?",
                       "TRADE"))
    # A mechanical event well after the last real trade — the data clock must
    # not be fooled by it.
    trades.append(("0xalpha", base + 500_000, "MKT_A", "TOK_A", "Yes", "",
                   0.0, 0.0, 0.0, "Will A happen?", "REDEEM"))
    resolutions = [("TOK_A", 1.0, base + 200_000, 0),
                   ("TOK_B", 0.0, base + 210_000, 0)]
    build_tape(db, trades=trades, resolutions=resolutions)
    st.data_db = db
    return st
