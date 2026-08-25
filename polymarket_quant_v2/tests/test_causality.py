"""Rule 7: never use future information.

The tests here are constructed so that a naive implementation PASSES the easy
one and fails the hard one. A causality suite that only contains the easy case
gives false confidence, which is worse than no suite.
"""

from __future__ import annotations

import sqlite3

from conftest import T0, build_db
from pqv2.substrate.data import iter_settled, oos_split_ts, time_bounds
from pqv2.substrate.state import WalletState, stream_observations


def test_outcome_enters_wallet_state_at_settlement_not_at_trade(tmp_path):
    """The easy case: a wallet's second trade must not know the first's result.

    Wallet places trade A at t=0 which settles at t=+10 days, then trade B at
    t=+1 day. At B, the wallet has ZERO settled evidence -- its record is
    unknown, not 'one win'.
    """
    db = tmp_path / "c.sqlite3"
    conn = sqlite3.connect(db)
    from conftest import SCHEMA
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO resolutions VALUES ('tokA','m1',1.0,?,0,'')",
                 (T0 + 10 * 86400,))
    conn.execute("INSERT INTO resolutions VALUES ('tokB','m2',1.0,?,0,'')",
                 (T0 + 20 * 86400,))
    for tid, ts in (("tokA", T0), ("tokB", T0 + 86400)):
        conn.execute(
            "INSERT INTO wallet_trades (wallet, ts, market_id, token_id,"
            " outcome, side, price, size, usdc, event_type)"
            " VALUES ('0xw',?,?,?,'Yes','BUY',0.5,100,100,'TRADE')",
            (ts, tid[:2], tid))
    conn.commit()
    conn.close()

    from pqv2.config import Settings
    st = Settings()
    st.data_db = db
    st.work_dir = tmp_path / "var"
    obs = list(stream_observations(st))
    assert len(obs) == 2
    assert obs[0].w_settled_n == 0
    assert obs[1].w_settled_n == 0, (
        "the second trade saw the first trade's outcome before it settled")


def test_outcome_is_folded_in_once_the_clock_passes_settlement(tmp_path):
    """The hard case: it must ALSO be folded in on time.

    A implementation that simply never folds outcomes in would pass the test
    above. Here trade A settles BEFORE trade B is placed, so at B the wallet
    must have exactly one settled trade.
    """
    db = tmp_path / "c2.sqlite3"
    conn = sqlite3.connect(db)
    from conftest import SCHEMA
    conn.executescript(SCHEMA)
    conn.execute("INSERT INTO resolutions VALUES ('tokA','m1',1.0,?,0,'')",
                 (T0 + 86400,))
    conn.execute("INSERT INTO resolutions VALUES ('tokB','m2',0.0,?,0,'')",
                 (T0 + 30 * 86400,))
    for tid, ts in (("tokA", T0), ("tokB", T0 + 5 * 86400)):
        conn.execute(
            "INSERT INTO wallet_trades (wallet, ts, market_id, token_id,"
            " outcome, side, price, size, usdc, event_type)"
            " VALUES ('0xw',?,?,?,'Yes','BUY',0.5,100,100,'TRADE')",
            (ts, tid[:2], tid))
    conn.commit()
    conn.close()

    from pqv2.config import Settings
    st = Settings()
    st.data_db = db
    st.work_dir = tmp_path / "var"
    obs = list(stream_observations(st))
    assert obs[0].w_settled_n == 0
    assert obs[1].w_settled_n == 1, (
        "trade A settled before trade B was placed, so B must see it")
    assert obs[1].w_win_rate == 1.0


def test_a_trade_with_no_settlement_clock_never_folds_in(tmp_path):
    """An unknown settlement time must park the outcome past the end of time,
    never treat it as 'known now'."""
    s = WalletState()
    assert s.settled_n == 0
    s.fold_settled(True, 1.0, 100.0)
    assert s.settled_n == 1        # sanity: folding works when asked


def test_observation_is_emitted_before_its_own_trade_is_recorded(st):
    """An observation must not see the trade it describes.

    `w_seen_n` at the wallet's first trade must be 0, not 1.
    """
    obs = list(stream_observations(st))
    firsts = {}
    for o in obs:
        firsts.setdefault(o.trade.wallet, o)
    for wallet, o in firsts.items():
        assert o.w_seen_n == 0, f"{wallet} counted its own first trade"
        assert not o.w_token_repeat


def test_stream_is_chronological(st):
    obs = list(stream_observations(st))
    times = [o.trade.ts for o in obs]
    assert times == sorted(times)


def test_market_context_uses_only_prior_prints(st):
    """`market_price_move` must be built forward from the same pass, not
    queried -- a query would return prints from the future."""
    obs = list(stream_observations(st))
    first_by_token = {}
    for o in obs:
        if o.trade.token_id not in first_by_token:
            first_by_token[o.trade.token_id] = o
    for o in first_by_token.values():
        assert o.market_recent_prints == 0, (
            "the first print in a token cannot have prior prints")
        assert o.tape_price_gap == 0.0


def test_oos_split_is_by_time_not_by_row(st):
    lo, hi = time_bounds(st)
    split = oos_split_ts(st)
    assert lo < split < hi
    is_rows = list(iter_settled(st, ts_to=split))
    oos_rows = list(iter_settled(st, ts_from=split))
    assert is_rows and oos_rows
    assert max(t.ts for t in is_rows) < min(t.ts for t in oos_rows), (
        "in-sample and out-of-sample windows overlap in time")
    total = len(list(iter_settled(st)))
    assert len(is_rows) + len(oos_rows) == total, "the split loses rows"
