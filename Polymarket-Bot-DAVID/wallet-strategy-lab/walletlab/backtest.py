"""Strategy evaluation against settled outcomes, with realistic execution.

What this models (§29):
  * you pay the *tape* price at `delay_secs` after the wallet acted, not the
    wallet's own fill (§30). If nothing printed inside the window, the copy is
    recorded as UNFILLED rather than silently filled at the wallet's price —
    the single most common way a copy backtest manufactures edge.
  * slippage and fees on top of that price.
  * a per-trade notional cap, because you are not the whale you are following.

What it does not model, stated plainly rather than buried: partial fills, book
depth, and the market impact of the copy itself. On a venue where the wallet's
own print is often the only print in the window, those would be guesses. The
UNFILLED accounting is the conservative substitute — an opportunity that cannot
be priced earns nothing.

Holding is to resolution. Early exits need a price path this dataset does not
reliably have; see docs/AUDIT.md "what this cannot answer yet".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import Settings
from .data import PriceTape
from .state import Observation
from .strategy import CopyStrategy


@dataclass
class Result:
    """Outcome of running one strategy over one set of observations."""

    n_admitted: int = 0
    n_filled: int = 0
    n_unfilled: int = 0
    n_wins: int = 0
    stake: float = 0.0
    pnl: float = 0.0
    returns: list[float] = field(default_factory=list)
    tokens: set[str] = field(default_factory=set)
    markets: set[str] = field(default_factory=set)
    first_ts: int = 0
    last_ts: int = 0
    equity: list[float] = field(default_factory=list)

    # --------------------------------------------------------------- metrics
    @property
    def roi(self) -> float:
        return self.pnl / self.stake if self.stake > 0 else 0.0

    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_filled if self.n_filled else 0.0

    @property
    def expectancy(self) -> float:
        """Mean per-trade return on capital — the economically honest number."""
        return sum(self.returns) / len(self.returns) if self.returns else 0.0

    @property
    def fill_rate(self) -> float:
        d = self.n_filled + self.n_unfilled
        return self.n_filled / d if d else 0.0

    def t_stat(self) -> float:
        """Is the mean per-trade return distinguishable from zero?"""
        n = len(self.returns)
        if n < 5:
            return 0.0
        m = sum(self.returns) / n
        var = sum((r - m) ** 2 for r in self.returns) / (n - 1)
        if var <= 0:
            return 0.0
        return m / math.sqrt(var / n)

    def sharpe_like(self) -> float:
        """Per-trade return / its dispersion. Not annualised — trade clocks
        differ per wallet and annualising them would invent precision."""
        n = len(self.returns)
        if n < 5:
            return 0.0
        m = sum(self.returns) / n
        var = sum((r - m) ** 2 for r in self.returns) / (n - 1)
        return m / math.sqrt(var) if var > 0 else 0.0

    def max_drawdown(self) -> float:
        peak = 0.0
        worst = 0.0
        for e in self.equity:
            peak = max(peak, e)
            worst = min(worst, e - peak)
        return abs(worst)

    def concentration(self) -> float:
        """Share of gross profit contributed by the single best market.

        1.0 means the entire result is one market — the failure mode the old
        engine's adversarial battery was built to catch, kept here because it
        is the most common way a copy strategy is fake.
        """
        if not self._by_market:
            return 0.0
        gains = [v for v in self._by_market.values() if v > 0]
        tot = sum(gains)
        return (max(gains) / tot) if tot > 0 else 0.0

    _by_market: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "n_admitted": self.n_admitted, "n_filled": self.n_filled,
            "n_unfilled": self.n_unfilled, "fill_rate": round(self.fill_rate, 4),
            "n_markets": len(self.markets), "n_tokens": len(self.tokens),
            "stake": round(self.stake, 2), "pnl": round(self.pnl, 2),
            "roi": round(self.roi, 5), "expectancy": round(self.expectancy, 5),
            "win_rate": round(self.win_rate, 4), "t_stat": round(self.t_stat(), 3),
            "sharpe_like": round(self.sharpe_like(), 4),
            "max_drawdown": round(self.max_drawdown(), 2),
            "concentration": round(self.concentration(), 4),
            "first_ts": self.first_ts, "last_ts": self.last_ts,
        }


def run(
    strategy: CopyStrategy,
    observations: list[Observation],
    st: Settings,
    tape: PriceTape,
) -> Result:
    """Evaluate `strategy` over `observations`. Pure: no I/O beyond the tape."""
    res = Result()
    costs = st.costs
    eq = 0.0

    for o in observations:
        if o.trade.wallet != strategy.wallet:
            continue
        if not strategy.admits(o):
            continue
        res.n_admitted += 1

        # Execution price: what the tape says you could pay after the delay.
        if strategy.delay_secs <= 0:
            entry = o.price
        else:
            got = tape.price_at(o.trade.token_id, o.trade.ts + strategy.delay_secs)
            if got is None:
                res.n_unfilled += 1
                continue
            entry = got

        entry = costs.fill_price(entry)
        if not (costs.min_price < entry < costs.max_price):
            res.n_unfilled += 1
            continue

        stake = min(strategy.stake_for(o), costs.max_notional)
        # Payoff of holding one share bought at `entry` to resolution.
        ret = (o.trade.resolution - entry) / entry
        pnl = stake * ret

        res.n_filled += 1
        res.n_wins += int(o.trade.resolution > 0.5)
        res.stake += stake
        res.pnl += pnl
        res.returns.append(ret)
        res.tokens.add(o.trade.token_id)
        mk = o.trade.market_id or o.trade.token_id
        res.markets.add(mk)
        res._by_market[mk] = res._by_market.get(mk, 0.0) + pnl
        res.first_ts = res.first_ts or o.trade.ts
        res.last_ts = o.trade.ts
        eq += pnl
        res.equity.append(eq)

    return res
