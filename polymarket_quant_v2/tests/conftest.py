"""Synthetic fixtures. Every test in this suite runs OFFLINE.

A test suite that needs the client's 2.6 GB database is a test suite nobody
runs. These fixtures build a small SQLite file with the same schema and the
same pathologies as the real one -- including the favourite-longshot bias --
so the controls that matter can be asserted rather than described.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pqv2.config import Settings                                    # noqa: E402
from pqv2.substrate.data import SettledTrade                        # noqa: E402
from pqv2.substrate.state import Observation                        # noqa: E402

SCHEMA = """
CREATE TABLE wallet_trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT, wallet TEXT NOT NULL,
    ts INTEGER NOT NULL, market_id TEXT, token_id TEXT, outcome TEXT,
    side TEXT, price REAL DEFAULT 0, size REAL DEFAULT 0, usdc REAL DEFAULT 0,
    question TEXT, tx TEXT, source TEXT, event_type TEXT DEFAULT 'TRADE');
CREATE TABLE resolutions (
    token_id TEXT PRIMARY KEY, market_id TEXT, price REAL NOT NULL,
    ts REAL NOT NULL, settled_ts REAL DEFAULT 0, settled_source TEXT DEFAULT '');
CREATE INDEX ix_wt_token ON wallet_trades(token_id);
CREATE INDEX ix_wt_wallet ON wallet_trades(wallet);
"""

T0 = 1_700_000_000


def build_db(path: Path, *, wallets: int = 6, tokens: int = 60,
             trades_per_wallet: int = 120, seed: int = 7,
             favourite_bias: float = 0.09) -> Path:
    """A tape with a known, planted structure.

    Planted deliberately:
      * a favourite-longshot bias of `favourite_bias`, so `baseline.py` has
        something real to control for
      * one wallet ("0xedge") with genuine skill INDEPENDENT of price band, so
        a wallet-alpha test can distinguish it from the bias
      * settlement times that are always after the trade, so causality tests
        have a valid clock
    """
    rng = random.Random(seed)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    # A token resolves ONCE, for everyone. So the bias has to be planted in the
    # PRICE relative to a token's true probability, not in a per-trade outcome
    # draw: otherwise wallets buying the same token at different prices need
    # contradictory outcomes and the structure is destroyed by the single
    # resolution.
    token_ids = [f"tok{i:04d}" for i in range(tokens)]
    truth: dict = {}
    for i, tid in enumerate(token_ids):
        p_true = rng.uniform(0.08, 0.94)
        won = 1.0 if rng.random() < p_true else 0.0
        settle = T0 + 86_400 * (10 + i % 40)
        truth[tid] = (p_true, won)
        conn.execute(
            "INSERT INTO resolutions (token_id, market_id, price, ts, settled_ts)"
            " VALUES (?,?,?,?,?)",
            (tid, f"mkt{i % 25:03d}", won, settle, 0.0))

    names = [f"0xw{i:03d}" for i in range(wallets - 1)] + ["0xedge"]
    rows = []
    for w in names:
        for k in range(trades_per_wallet):
            tid = rng.choice(token_ids)
            p_true, _ = truth[tid]
            ts = T0 + 3600 * (k * 3 + rng.randint(0, 2)) + rng.randint(0, 600)
            # Favourites are UNDERPRICED and long shots are OVERPRICED, which
            # is exactly the favourite-longshot bias: a 0.75 favourite is quoted
            # at 0.75 - bias and wins 75% of the time, so buying it earns.
            skew = favourite_bias if p_true > 0.55 else -favourite_bias
            price = p_true - skew + rng.gauss(0, 0.02)
            price = round(max(0.05, min(0.95, price)), 3)
            # The edge wallet is skilful in a way ORTHOGONAL to price: it buys
            # the same tokens, but preferentially the ones that won. That is a
            # real wallet edge and it must survive the price-band control.
            if w == "0xedge" and truth[tid][1] < 0.5 and rng.random() < 0.45:
                tid2 = rng.choice(token_ids)
                if truth[tid2][1] > 0.5:
                    tid = tid2
                    p_true = truth[tid][0]
                    skew = favourite_bias if p_true > 0.55 else -favourite_bias
                    price = round(max(0.05, min(0.95,
                                                p_true - skew)), 3)
            rows.append((w, ts, f"mkt{token_ids.index(tid) % 25:03d}", tid,
                         "Yes", "BUY", price, 100.0,
                         round(rng.uniform(20, 400), 2), "q?", "TRADE"))

    conn.executemany(
        "INSERT INTO wallet_trades (wallet, ts, market_id, token_id, outcome,"
        " side, price, size, usdc, question, event_type)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def db(tmp_path) -> Path:
    return build_db(tmp_path / "intel.sqlite3")


@pytest.fixture
def st(db, tmp_path) -> Settings:
    s = Settings()
    s.data_db = db
    s.work_dir = tmp_path / "var"
    s.journal_db = tmp_path / "nope-journal.sqlite3"
    s.library_db = tmp_path / "nope-library.sqlite3"
    s.strategy_b.min_wallet_trades = 20
    s.strategy_b.min_oos_fills = 10
    s.strategy_b.min_oos_markets = 2
    return s.ensure_dirs()


def make_trade(**kw) -> SettledTrade:
    base = dict(wallet="0xw000", ts=T0, token_id="tok0001", market_id="mkt001",
                outcome="Yes", price=0.5, size=100.0, usdc=100.0,
                resolution=1.0, settled_ts=T0 + 86_400, question="q")
    base.update(kw)
    return SettledTrade(**base)


def make_obs(**kw) -> Observation:
    trade_kw = {k: kw.pop(k) for k in list(kw)
                if k in ("wallet", "ts", "token_id", "market_id", "price",
                         "size", "usdc", "resolution", "settled_ts")}
    tr = make_trade(**trade_kw)
    base = dict(
        trade=tr, w_settled_n=50, w_win_rate=0.6, w_roi=0.1,
        w_roll_win_rate=0.6, w_roll_roi=0.1, w_edge_t=2.0, w_consec_losses=0,
        w_consec_wins=1, w_seen_n=50, w_secs_since_prev=3600,
        w_open_notional=0.0, w_token_repeat=False, w_market_repeat=False,
        w_avg_notional=100.0, w_avg_price=0.5, price=tr.price,
        notional=tr.usdc, size=tr.size, rel_notional=1.0,
        price_vs_wallet_norm=0.0, hour_of_day=12,
        secs_to_settle=max(0, tr.settled_ts - tr.ts), market_recent_prints=10,
        market_price_move=0.01, market_velocity=0.01, tape_price_gap=0.0)
    base.update(kw)
    return Observation(**base)
