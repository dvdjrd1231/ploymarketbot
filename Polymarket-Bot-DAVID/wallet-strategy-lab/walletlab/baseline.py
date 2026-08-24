"""The population control — separating wallet skill from market-wide bias (§46).

This module exists because of a measurement, not a theory. Calibrating every
settled trade in the database against its outcome shows a large, systematic
favourite–longshot bias:

    price band     n        mean price   actual win rate   gap
    0.10-0.20      17,477   0.148        0.080            -0.069
    0.30-0.40       9,994   0.345        0.257            -0.088
    0.60-0.70      11,200   0.650        0.742            +0.092
    0.70-0.80      13,389   0.748        0.840            +0.092
    0.80-0.90      20,532   0.847        0.900            +0.053

Favourites are underpriced by ~9 points in the middle-high band; longshots are
overpriced by ~8. So *any* rule of the form "buy between 0.6 and 0.9" earns a
large positive expectancy on this dataset regardless of whose trade it copies.

That is the trap this engine is built to avoid. Without a control, a search over
1,728 price-band transformations will "discover" that bias once per wallet,
report 122 independent validated strategies, and every one of them will be the
same market-wide effect wearing a different wallet's name.

    wallet_alpha = expectancy(strategy on this wallet)
                 - expectancy(same filter, same window, ALL wallets)

Only wallet_alpha answers the question the project actually asks: does *this
wallet* know something? A strategy with strong P&L and zero alpha is not a
copy-trading strategy — it is a market-structure trade, and it should be traded
directly rather than by following anyone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from functools import lru_cache

from .backtest import Result
from .config import Settings
from .data import DECISION_EVENT
from .strategy import CopyStrategy


@dataclass
class PopulationBaseline:
    n: int
    expectancy: float
    win_rate: float
    mean_price: float

    def as_dict(self) -> dict:
        return {
            "n": self.n,
            "expectancy": round(self.expectancy, 5),
            "win_rate": round(self.win_rate, 4),
            "mean_price": round(self.mean_price, 4),
        }


def population_expectancy(
    st: Settings,
    *,
    ts_from: int,
    ts_to: int,
    min_price: float,
    max_price: float,
    exclude_wallet: str | None = None,
) -> PopulationBaseline:
    """What the same price band paid to *everyone* over the same window.

    Excludes the wallet under test so the control cannot contain the thing it
    is controlling for.
    """
    sql = """
        SELECT t.price, r.price
          FROM wallet_trades t
          JOIN resolutions  r ON t.token_id = r.token_id
         WHERE t.event_type = ? AND t.side = 'BUY'
           AND t.usdc >= 1
           AND r.price IN (0.0, 1.0)
           AND t.ts >= ? AND t.ts <= ?
           AND t.price >= ? AND t.price <= ?
    """
    params: list = [DECISION_EVENT, ts_from, ts_to, min_price, max_price]
    if exclude_wallet:
        sql += " AND t.wallet <> ?"
        params.append(exclude_wallet)

    conn = sqlite3.connect(f"file:{st.data_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    if not rows:
        return PopulationBaseline(0, 0.0, 0.0, 0.0)

    rets, wins, prices = [], 0, 0.0
    for p, res in rows:
        entry = st.costs.fill_price(float(p))
        if not (st.costs.min_price < entry < st.costs.max_price):
            continue
        rets.append((float(res) - entry) / entry)
        wins += int(float(res) > 0.5)
        prices += float(p)
    if not rets:
        return PopulationBaseline(0, 0.0, 0.0, 0.0)
    n = len(rets)
    return PopulationBaseline(
        n=n,
        expectancy=sum(rets) / n,
        win_rate=wins / n,
        mean_price=prices / n,
    )


def wallet_alpha(
    st: Settings, strategy: CopyStrategy, test: Result
) -> tuple[float, PopulationBaseline]:
    """Strategy expectancy minus the matched population expectancy.

    Matching is on the two dimensions that carry the market-wide bias: the
    price band the strategy trades, and the time window it traded in. Anything
    left over is attributable to the wallet (or to a dimension not controlled,
    which is why this is reported, never hidden).
    """
    if test.n_filled == 0 or not test.first_ts:
        return 0.0, PopulationBaseline(0, 0.0, 0.0, 0.0)

    lo = strategy.min_price if strategy.min_price is not None else st.costs.min_price
    hi = strategy.max_price if strategy.max_price is not None else st.costs.max_price

    base = population_expectancy(
        st,
        ts_from=test.first_ts,
        ts_to=test.last_ts,
        min_price=lo,
        max_price=hi,
        exclude_wallet=strategy.wallet,
    )
    if base.n < 30:
        # Not enough control data to make the comparison; refuse to guess.
        return 0.0, base
    return test.expectancy - base.expectancy, base
