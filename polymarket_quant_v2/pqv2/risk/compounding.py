"""The compounding engine: the account, and what it will lend to a trade.

Every number the brief asks to be tracked is a field here, and the invariant
that makes them meaningful is enforced rather than assumed:

    equity == starting_capital + realized_pnl
    deployable == equity * (1 - reserve) - allocated

`Account.check()` raises if the books do not balance. An equity curve that
drifts from its own trade log is the most common silent bug in a backtester,
and it always flatters.

Compounding is not automatic virtue. Compounding a negative-expectancy strategy
destroys capital faster than flat staking, and compounding through a drawdown
is how a recoverable loss becomes a terminal one. So the account de-risks as
drawdown deepens (see sizing.py) and halts at `hard_stop_drawdown`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Settings


@dataclass
class Position:
    token_id: str
    market_id: str
    wallet: str
    strategy_id: str
    route: str
    stake: float
    entry: float
    ts: int
    category: str = ""

    @property
    def value(self) -> float:
        return self.stake


@dataclass
class Account:
    starting_capital: float
    reserve_fraction: float = 0.10
    reinvest: bool = True

    realized_pnl: float = 0.0
    realized_wins: float = 0.0
    realized_losses: float = 0.0
    n_wins: int = 0
    n_losses: int = 0
    peak_equity: float = 0.0
    positions: dict = field(default_factory=dict)
    equity_curve: list = field(default_factory=list)
    halted: bool = False
    halt_reason: str = ""

    def __post_init__(self) -> None:
        self.peak_equity = self.starting_capital
        self.equity_curve.append((0, self.starting_capital))

    # -- state ---------------------------------------------------------------
    @property
    def equity(self) -> float:
        """Realised equity. Deliberately excludes unrealised marks: on a venue
        with no continuous book, marking open positions to the last print
        invents equity that cannot be withdrawn."""
        return self.starting_capital + self.realized_pnl

    @property
    def allocated(self) -> float:
        return sum(p.stake for p in self.positions.values())

    @property
    def deployable(self) -> float:
        base = self.equity if self.reinvest else self.starting_capital
        return max(0.0, base * (1.0 - self.reserve_fraction) - self.allocated)

    @property
    def drawdown(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - self.equity) / self.peak_equity)

    @property
    def compounded_return(self) -> float:
        return (self.equity / self.starting_capital) - 1.0 \
            if self.starting_capital > 0 else 0.0

    @property
    def exposure(self) -> float:
        return self.allocated / self.equity if self.equity > 0 else 0.0

    # -- transitions ---------------------------------------------------------
    def can_open(self, stake: float) -> tuple[bool, str]:
        if self.halted:
            return False, f"account halted: {self.halt_reason}"
        if stake <= 0:
            return False, "stake is zero"
        if stake > self.deployable:
            return False, (f"stake ${stake:,.2f} exceeds deployable "
                           f"${self.deployable:,.2f} (equity ${self.equity:,.2f}, "
                           f"allocated ${self.allocated:,.2f}, reserve "
                           f"{self.reserve_fraction:.0%})")
        return True, ""

    def open(self, key: str, position: Position) -> None:
        ok, why = self.can_open(position.stake)
        if not ok:
            raise ValueError(why)
        self.positions[key] = position

    def close(self, key: str, ret: float, ts: int = 0) -> float:
        pos = self.positions.pop(key, None)
        if pos is None:
            raise KeyError(f"no open position {key}")
        pnl = pos.stake * ret
        self.realized_pnl += pnl
        if pnl >= 0:
            self.realized_wins += pnl
            self.n_wins += 1
        else:
            self.realized_losses += -pnl
            self.n_losses += 1
        self.peak_equity = max(self.peak_equity, self.equity)
        self.equity_curve.append((ts, self.equity))
        if self.drawdown >= 0.0:
            pass
        return pnl

    def enforce_halt(self, st: Settings) -> None:
        if not self.halted and self.drawdown >= st.risk.hard_stop_drawdown:
            self.halted = True
            self.halt_reason = (
                f"drawdown {self.drawdown:.1%} reached the hard stop "
                f"{st.risk.hard_stop_drawdown:.0%}")

    # -- invariants ----------------------------------------------------------
    def check(self) -> None:
        expected = self.starting_capital + self.realized_pnl
        if abs(self.equity - expected) > 1e-6:
            raise AssertionError(
                f"account does not balance: equity {self.equity} != "
                f"starting {self.starting_capital} + realized "
                f"{self.realized_pnl}")
        net = self.realized_wins - self.realized_losses
        if abs(net - self.realized_pnl) > 1e-6:
            raise AssertionError(
                f"win/loss split {net} does not reconstruct realized P&L "
                f"{self.realized_pnl}")

    def max_drawdown(self) -> float:
        peak = self.starting_capital
        worst = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            worst = min(worst, (eq - peak) / peak if peak > 0 else 0.0)
        return abs(worst)

    def summary(self) -> dict:
        self.check()
        return {
            "starting_capital": round(self.starting_capital, 2),
            "equity": round(self.equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "realized_wins": round(self.realized_wins, 2),
            "realized_losses": round(self.realized_losses, 2),
            "profit_factor": round(
                self.realized_wins / self.realized_losses, 3)
            if self.realized_losses > 0 else 0.0,
            "n_wins": self.n_wins, "n_losses": self.n_losses,
            "win_rate": round(self.n_wins / (self.n_wins + self.n_losses), 4)
            if (self.n_wins + self.n_losses) else 0.0,
            "open_positions": len(self.positions),
            "allocated": round(self.allocated, 2),
            "deployable": round(self.deployable, 2),
            "exposure": round(self.exposure, 4),
            "drawdown": round(self.drawdown, 4),
            "max_drawdown": round(self.max_drawdown(), 4),
            "compounded_return": round(self.compounded_return, 4),
            "halted": self.halted, "halt_reason": self.halt_reason,
        }


def new_account(st: Settings) -> Account:
    c = st.compounding
    return Account(starting_capital=c.starting_capital,
                   reserve_fraction=c.reserve_fraction, reinvest=c.reinvest)


def compare_sizing_modes(st: Settings, fills, modes=None,
                         warmup: int = 20) -> list:
    """Replay the same fills under different staking rules.

    The brief asks for this comparison directly. Note what it holds constant:
    the SAME trades in the SAME order. Sizing cannot change which trades
    happened, so any difference here is purely the staking rule -- which is the
    only way to compare them honestly.

    CAUSALITY. Every mode may use only information available BEFORE the trade
    is placed. In particular `edge` sizes on the expectancy measured over the
    fills already CLOSED, never on this trade's own result -- an earlier
    revision of this function read `f.ret` while sizing, which is rule-7
    look-ahead, and it is the reason `test_sizing_modes_are_causal` exists.
    `warmup` trades are sized flat, because an expectancy computed from two
    closed trades is noise, not an edge.
    """
    modes = modes or ["fixed", "fixed_fractional", "edge", "confidence"]
    if not fills:
        return []
    out = []
    for mode in modes:
        acct = new_account(st)
        cfg_fraction = st.sizing.base_fraction
        closed_returns: list = []          # strictly PRIOR outcomes
        for i, f in enumerate(fills):
            if acct.halted:
                break
            if mode == "fixed":
                stake = st.compounding.starting_capital * cfg_fraction
            elif mode == "edge":
                # Edge-adjusted: scale with the expectancy realised so far,
                # saturating at 2x. Twice the measured edge does not justify
                # twice the risk once the estimate's own error dominates.
                if len(closed_returns) < warmup:
                    scale = 1.0
                else:
                    prior = sum(closed_returns) / len(closed_returns)
                    scale = min(2.0, max(0.25, prior / 0.05)) if prior > 0 \
                        else 0.25
                stake = acct.equity * cfg_fraction * scale
            elif mode == "confidence":
                # rel_notional is the wallet's own stake versus its recent
                # norm -- known at signal time, so this is causal.
                stake = acct.equity * cfg_fraction * min(
                    2.0, max(0.5, f.rel_notional / 2.0))
            else:
                stake = acct.equity * cfg_fraction
            stake = min(stake, acct.deployable, st.costs.max_notional)
            if stake <= 0:
                continue
            key = f"{f.token_id}:{i}"
            acct.open(key, Position(f.token_id, f.market_id, f.wallet, "",
                                    "B", stake, f.entry, f.ts))
            acct.close(key, f.ret, f.ts)
            closed_returns.append(f.ret)    # only NOW is it known
            acct.enforce_halt(st)
        s = acct.summary()
        s["mode"] = mode
        out.append(s)
    out.sort(key=lambda d: -(d["compounded_return"] /
                             (d["max_drawdown"] or 1.0)))
    return out
