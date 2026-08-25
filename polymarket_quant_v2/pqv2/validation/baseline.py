"""Wallet alpha: the control that stops the engine discovering market structure
and calling it a strategy.

The measurement that makes this module non-optional, taken on the client's own
database over all 116,923 settled copyable trades:

    price band   n        mean price   actual win rate   gap
    0.30-0.40    9,994    0.345        0.257             -0.088
    0.60-0.70    11,200   0.650        0.742             +0.092
    0.70-0.80    13,389   0.748        0.840             +0.092

That is a large favourite-longshot bias. Any rule of the form "buy between 0.6
and 0.9" earns roughly +20% expectancy while copying nobody in particular. A
search over price-band transformations across 60 wallets will find this once
per wallet and report ~60 "independent validated strategies" -- all the same
market-wide effect, all worthless, and it would be the most impressive-looking
output the system has ever produced.

So every candidate is scored against the SAME price band and the SAME time
window across all OTHER wallets. If the wallet contributed nothing above that,
the strategy is `NO_WALLET_ALPHA` regardless of profit, and cannot promote.

This control does not exist anywhere in the V1 engine.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..substrate.data import connect, DECISION_EVENT


@dataclass
class PopulationEdge:
    """What the whole market did in a band and window, excluding one wallet."""

    n: int
    mean_return: float
    win_rate: float

    @property
    def usable(self) -> bool:
        # Below this the "population" is a handful of trades and subtracting it
        # would add noise rather than remove bias.
        return self.n >= 200


class BaselineBook:
    """Population expectancy by (price band, time bucket), computed once.

    Cached in memory: the sweep asks this millions of times and it must not
    become the bottleneck that pushes anyone toward skipping the control.
    """

    BAND = 0.05
    BUCKET_SECS = 7 * 86_400

    def __init__(self, st: Settings) -> None:
        self.st = st
        self._cells: dict[tuple, list] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        conn = connect(self.st.data_db)
        try:
            rows = conn.execute(
                "SELECT t.wallet, t.ts, t.price, r.price"
                "  FROM wallet_trades t JOIN resolutions r"
                "    ON t.token_id = r.token_id"
                " WHERE t.event_type = ? AND t.side = 'BUY'"
                "   AND t.price > ? AND t.price < ? AND r.price IN (0.0, 1.0)",
                (DECISION_EVENT, self.st.costs.min_price,
                 self.st.costs.max_price)).fetchall()
        finally:
            conn.close()
        for wallet, ts, price, res in rows:
            key = self._key(float(price), int(ts))
            cell = self._cells.setdefault(key, [])
            cell.append((wallet, (float(res) - float(price)) / float(price),
                         float(res) > 0.5))
        self._loaded = True

    def _key(self, price: float, ts: int) -> tuple:
        return (int(price / self.BAND), int(ts // self.BUCKET_SECS))

    def population(self, price: float, ts: int,
                   exclude_wallet: str) -> PopulationEdge:
        """What everyone ELSE earned in the same band and week.

        Excluding the wallet matters: including it would let a wallet with many
        trades in one cell define its own benchmark and always show zero alpha.
        """
        self._load()
        cell = self._cells.get(self._key(price, ts)) or []
        rets = [r for w, r, _ in cell if w != exclude_wallet]
        wins = [win for w, _, win in cell if w != exclude_wallet]
        n = len(rets)
        if n == 0:
            return PopulationEdge(0, 0.0, 0.0)
        return PopulationEdge(n, sum(rets) / n, sum(wins) / n)

    def alpha_for(self, fills, wallet: str) -> dict:
        """Strategy expectancy minus the matched population expectancy.

        Returns `alpha`, and the coverage that says how much of the result the
        control could actually see. Low coverage is reported, not hidden --
        an uncontrolled result must never be silently presented as controlled.
        """
        matched = 0
        strat_sum = 0.0
        pop_sum = 0.0
        for f in fills:
            pe = self.population(f.entry, f.ts, wallet)
            if not pe.usable:
                continue
            matched += 1
            strat_sum += f.ret
            pop_sum += pe.mean_return
        if matched == 0:
            return {"alpha": 0.0, "matched": 0, "coverage": 0.0,
                    "population_edge": 0.0, "strategy_edge": 0.0,
                    "controlled": False}
        se = strat_sum / matched
        pe_mean = pop_sum / matched
        return {"alpha": se - pe_mean, "matched": matched,
                "coverage": matched / max(1, len(fills)),
                "population_edge": pe_mean, "strategy_edge": se,
                "controlled": matched / max(1, len(fills)) >= 0.50}


def calibration_table(st: Settings, bands: int = 10) -> list[dict]:
    """The favourite-longshot measurement itself, so the docs can be re-checked
    rather than believed."""
    conn = connect(st.data_db)
    try:
        rows = conn.execute(
            "SELECT t.price, r.price FROM wallet_trades t"
            "  JOIN resolutions r ON t.token_id = r.token_id"
            " WHERE t.event_type = ? AND t.side = 'BUY'"
            "   AND t.price > ? AND t.price < ? AND r.price IN (0.0, 1.0)",
            (DECISION_EVENT, st.costs.min_price, st.costs.max_price)).fetchall()
    finally:
        conn.close()
    buckets: dict = {}
    for price, res in rows:
        b = min(bands - 1, int(float(price) * bands))
        acc = buckets.setdefault(b, [0, 0.0, 0])
        acc[0] += 1
        acc[1] += float(price)
        acc[2] += int(float(res) > 0.5)
    out = []
    for b in sorted(buckets):
        n, psum, wins = buckets[b]
        mean_p = psum / n
        win = wins / n
        out.append({"band": f"{b / bands:.2f}-{(b + 1) / bands:.2f}", "n": n,
                    "mean_price": round(mean_p, 4),
                    "win_rate": round(win, 4),
                    "gap": round(win - mean_p, 4)})
    return out
