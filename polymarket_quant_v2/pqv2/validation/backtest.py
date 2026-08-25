"""Strategy evaluation against settled outcomes, with realistic execution.

What this models:
  * you pay the TAPE price at `delay_secs` after the wallet acted, not the
    wallet's own fill. If nothing printed inside the window the copy is
    recorded UNFILLED and earns nothing -- never silently filled at the
    wallet's price. That is the single most common way a copy backtest
    manufactures edge, and it is the one line that separates this from a
    fictional result.
  * slippage and fees on top of that price.
  * a per-trade notional cap, because you are not the whale you are following.
  * exit models other than hold-to-settlement, priced off the same tape.
  * optional compounding, so equity path and drawdown are real.

What it does NOT model, stated plainly rather than buried: partial fills, book
depth, and the market impact of the copy itself. On a venue where the wallet's
own print is often the only print in the window, those would be guesses. The
UNFILLED accounting is the conservative substitute -- an opportunity that
cannot be priced earns nothing.

Early-exit results carry a lower evidentiary weight than settlement results and
say so: see `Result.exit_confidence`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from ..config import Settings
from ..substrate.data import PriceTape
from ..substrate.state import Observation
from ..strategy_b.strategy import (CopyStrategy, EXIT_PARTIAL, EXIT_SETTLEMENT,
                                   EXIT_STOP, EXIT_TARGET, EXIT_TIME, EXIT_TRAIL)


@dataclass
class Fill:
    """One completed round trip. The unit of the winner/loser decomposition."""

    ts: int
    token_id: str
    market_id: str
    wallet: str
    entry: float
    exit_price: float
    stake: float
    ret: float
    pnl: float
    won: bool
    hold_secs: int
    exit_reason: str
    price_band: str = ""
    rel_notional: float = 0.0
    secs_to_settle: int = 0
    market_prints: int = 0


@dataclass
class Result:
    """Outcome of running one strategy over one set of observations."""

    n_admitted: int = 0
    n_filled: int = 0
    n_unfilled: int = 0
    n_rejected: int = 0
    n_wins: int = 0
    stake: float = 0.0
    pnl: float = 0.0
    returns: list = field(default_factory=list)
    fills: list = field(default_factory=list)
    tokens: set = field(default_factory=set)
    markets: set = field(default_factory=set)
    first_ts: int = 0
    last_ts: int = 0
    equity: list = field(default_factory=list)
    reasons: dict = field(default_factory=dict)
    _by_market: dict = field(default_factory=dict)
    exit_confidence: str = "exact"     # exact | modelled

    # --------------------------------------------------------------- metrics
    @property
    def roi(self) -> float:
        return self.pnl / self.stake if self.stake > 0 else 0.0

    @property
    def win_rate(self) -> float:
        return self.n_wins / self.n_filled if self.n_filled else 0.0

    @property
    def expectancy(self) -> float:
        """Mean per-trade return on capital -- the economically honest number.

        Deliberately NOT win rate. A 40% win rate with 3:1 asymmetry beats a
        70% win rate with 1:4, and only this number sees the difference.
        """
        return sum(self.returns) / len(self.returns) if self.returns else 0.0

    @property
    def fill_rate(self) -> float:
        d = self.n_filled + self.n_unfilled
        return self.n_filled / d if d else 0.0

    def t_stat(self) -> float:
        n = len(self.returns)
        if n < 5:
            return 0.0
        m = sum(self.returns) / n
        var = sum((r - m) ** 2 for r in self.returns) / (n - 1)
        return m / math.sqrt(var / n) if var > 0 else 0.0

    def sharpe_like(self) -> float:
        """Per-trade return over its dispersion. Not annualised -- trade clocks
        differ per wallet and annualising them would invent precision."""
        n = len(self.returns)
        if n < 5:
            return 0.0
        m = sum(self.returns) / n
        var = sum((r - m) ** 2 for r in self.returns) / (n - 1)
        return m / math.sqrt(var) if var > 0 else 0.0

    def max_drawdown(self) -> float:
        peak = worst = 0.0
        for e in self.equity:
            peak = max(peak, e)
            worst = min(worst, e - peak)
        return abs(worst)

    def max_drawdown_pct(self, starting: float) -> float:
        if starting <= 0:
            return 0.0
        peak = starting
        worst = 0.0
        for e in self.equity:
            level = starting + e
            peak = max(peak, level)
            worst = min(worst, (level - peak) / peak)
        return abs(worst)

    def concentration(self) -> float:
        """Share of gross profit from the single best market.

        1.0 means the entire result is one market -- the most common way a copy
        strategy is fake, and the failure the V1 adversarial battery was built
        to catch. Kept because it is still the right test.
        """
        gains = [v for v in self._by_market.values() if v > 0]
        tot = sum(gains)
        return (max(gains) / tot) if tot > 0 else 0.0

    # -- winner / loser asymmetry -------------------------------------------
    def asymmetry(self) -> dict:
        wins = sorted(r for r in self.returns if r > 0)
        losses = sorted(r for r in self.returns if r < 0)
        med = lambda xs: (xs[len(xs) // 2] if len(xs) % 2 else
                          (xs[len(xs) // 2 - 1] + xs[len(xs) // 2]) / 2) if xs else 0.0
        gross_win = sum(wins)
        gross_loss = -sum(losses)
        return {
            "avg_win": sum(wins) / len(wins) if wins else 0.0,
            "median_win": med(wins),
            "avg_loss": sum(losses) / len(losses) if losses else 0.0,
            "median_loss": med(losses),
            "largest_win": wins[-1] if wins else 0.0,
            "largest_loss": losses[0] if losses else 0.0,
            "win_loss_ratio": (
                (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
                if wins and losses else 0.0),
            "profit_factor": gross_win / gross_loss if gross_loss > 0 else 0.0,
            # Tail loss: mean of the worst 5%. Risk of ruin lives in this
            # number, not in the average.
            "tail_loss_p05": (
                sum(losses[:max(1, len(losses) // 20)])
                / max(1, len(losses) // 20)) if losses else 0.0,
        }

    def summary(self, starting: float = 0.0) -> dict:
        a = self.asymmetry()
        return {
            "n_admitted": self.n_admitted, "n_filled": self.n_filled,
            "n_unfilled": self.n_unfilled, "fill_rate": round(self.fill_rate, 4),
            "n_markets": len(self.markets), "n_tokens": len(self.tokens),
            "stake": round(self.stake, 2), "pnl": round(self.pnl, 2),
            "roi": round(self.roi, 5), "expectancy": round(self.expectancy, 5),
            "win_rate": round(self.win_rate, 4),
            "t_stat": round(self.t_stat(), 3),
            "sharpe_like": round(self.sharpe_like(), 4),
            "max_drawdown": round(self.max_drawdown(), 2),
            "max_drawdown_pct": round(self.max_drawdown_pct(starting), 4)
            if starting else 0.0,
            "concentration": round(self.concentration(), 4),
            "profit_factor": round(a["profit_factor"], 3),
            "avg_win": round(a["avg_win"], 4), "avg_loss": round(a["avg_loss"], 4),
            "win_loss_ratio": round(a["win_loss_ratio"], 3),
            "tail_loss_p05": round(a["tail_loss_p05"], 4),
            "exit_confidence": self.exit_confidence,
            "first_ts": self.first_ts, "last_ts": self.last_ts,
        }


def _band(p: float) -> str:
    for lo in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9):
        if p < lo + 0.1:
            return f"{lo:.1f}-{lo + 0.1:.1f}"
    return "0.9-1.0"


def _resolve_exit(strategy: CopyStrategy, o: Observation, entry: float,
                  tape: PriceTape) -> tuple[float, int, str]:
    """Where the position ends: (exit price, hold seconds, reason).

    Settlement is exact. Every other model is priced off tape prints, which are
    real prices someone paid but are not a continuous path -- so a target
    between two prints is filled at the first print PAST it, never at the
    target itself. Optimistic fills here would invent the whole result.
    """
    rule = strategy.exit
    settle_ts = o.trade.settled_ts or (o.trade.ts + o.secs_to_settle)
    hold_to_settle = max(0, settle_ts - o.trade.ts)

    if rule.model == EXIT_SETTLEMENT:
        return o.trade.resolution, hold_to_settle, "settlement"

    start = o.trade.ts + strategy.delay_secs
    end = settle_ts if settle_ts > start else start + 86_400
    if rule.max_hold_secs:
        end = min(end, start + rule.max_hold_secs)
    path = tape.path(o.trade.token_id, start + 1, end)

    peak = entry
    for ts, px in path:
        ret = (px - entry) / entry
        peak = max(peak, px)
        if rule.model in (EXIT_TARGET, EXIT_PARTIAL) and ret >= rule.target_return:
            return px, ts - o.trade.ts, "target"
        if rule.model == EXIT_STOP and ret <= rule.stop_return:
            return px, ts - o.trade.ts, "stop"
        if rule.model == EXIT_TRAIL and peak > entry:
            if (px - peak) / peak <= -rule.trail_return:
                return px, ts - o.trade.ts, "trail"
        if rule.model == EXIT_TIME and rule.max_hold_secs \
                and ts - o.trade.ts >= rule.max_hold_secs:
            return px, ts - o.trade.ts, "time"
    # Nothing triggered: the position runs to settlement. This is the correct
    # default -- an exit rule that never fired did not lose you the trade.
    return o.trade.resolution, hold_to_settle, "settlement"


def run(strategy: CopyStrategy, observations: list, st: Settings,
        tape: PriceTape, *, equity: float = 0.0,
        compound: bool = False, collect_fills: bool = True) -> Result:
    """Evaluate `strategy` over `observations`. Pure: no I/O beyond the tape."""
    res = Result()
    costs = st.costs
    if strategy.exit.model != EXIT_SETTLEMENT:
        res.exit_confidence = "modelled"
    eq = 0.0
    live_equity = equity or st.compounding.starting_capital

    for o in observations:
        if o.trade.wallet != strategy.wallet:
            continue
        if collect_fills:
            ok, why = strategy.admits(o)
            if not ok:
                res.n_rejected += 1
                key = " ".join(why.split(" ", 3)[:3])
                res.reasons[key] = res.reasons.get(key, 0) + 1
                continue
        elif not strategy.admits_fast(o):
            # Sweep path: the reason strings are never read here, and building
            # them cost 18% of sweep runtime when measured. See admits_fast.
            res.n_rejected += 1
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

        stake = min(strategy.stake_for(o, live_equity), costs.max_notional)
        if compound:
            stake = min(stake, max(0.0, live_equity) * 0.25)
        if stake <= 0:
            res.n_unfilled += 1
            continue

        exit_price, hold, reason = _resolve_exit(strategy, o, entry, tape)
        if reason != "settlement":
            # Selling also crosses the spread, in the other direction.
            exit_price *= (1.0 - costs.slippage_bps / 10_000.0)
        if strategy.exit.model == EXIT_PARTIAL and reason == "target":
            # Bank `partial_fraction` at the target, ride the rest to
            # settlement. The asymmetry lever the brief asks for, priced.
            f = strategy.exit.partial_fraction
            exit_price = f * exit_price + (1 - f) * o.trade.resolution
            reason = "partial+settlement"

        ret = (exit_price - entry) / entry
        pnl = stake * ret

        res.n_filled += 1
        res.n_wins += int(ret > 0)
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
        if compound:
            live_equity += pnl
        if collect_fills:
            res.fills.append(Fill(
                ts=o.trade.ts, token_id=o.trade.token_id,
                market_id=mk, wallet=o.trade.wallet, entry=entry,
                exit_price=exit_price, stake=stake, ret=ret, pnl=pnl,
                won=ret > 0, hold_secs=hold, exit_reason=reason,
                price_band=_band(entry), rel_notional=o.rel_notional,
                secs_to_settle=o.secs_to_settle,
                market_prints=o.market_recent_prints))

    return res
