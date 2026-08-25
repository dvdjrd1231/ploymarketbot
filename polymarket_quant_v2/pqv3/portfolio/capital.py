"""The $100 capital model.

The brief's hardest constraint is not a feature, it is an arithmetic collision:

    equity                       $100.00
    reserve (10%)              - $ 10.00
    deployable                   $ 90.00
    max per trade (5%)           $  5.00
    venue minimum order          $  1.00
    minimum shares (5) @ $0.80   $  4.00

At $100 those numbers still fit, barely. At $100 with a 2% per-trade cap they
do not: $2.00 of budget cannot buy 5 shares at $0.80. An institutional backtest
never meets this wall because $2.00 of *its* budget is $2,000. This is why
`CAPITAL_INFEASIBLE` is a first-class outcome here and not a rounding step —
a strategy that only works above a size we cannot deploy is not a strategy we
have, and reporting it as one is the most flattering lie available.

Eleven quantities the brief asks to be distinguished, and where each lives:

    ACCOUNT BALANCE      Account.balance          cash + position value
    AVAILABLE CASH       Account.available_cash   cash - reserved
    RESERVED CAPITAL     Account.reserved         reserve + pending orders
    POSITION VALUE       Account.position_value   marked at last print
    UNREALIZED PNL       Account.unrealized_pnl
    REALIZED PNL         Account.realized_pnl
    RISK CAPITAL         Account.risk_capital     deployable after reserve
    PER-TRADE CAPITAL    Sizing.budget
    MARKET EXPOSURE      Exposure.by_market
    CORRELATED EXPOSURE  Exposure.by_correlation
    WALLET-COPY EXPOSURE Exposure.by_wallet
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from enum import Enum

from ..config import CapitalConfig, CostConfig, Settings


class Feasibility(str, Enum):
    OK = "OK"
    CAPITAL_INFEASIBLE = "CAPITAL_INFEASIBLE"
    LIQUIDITY_INFEASIBLE = "LIQUIDITY_INFEASIBLE"
    PRICE_OUT_OF_RANGE = "PRICE_OUT_OF_RANGE"
    NO_CASH = "NO_CASH"
    POSITION_LIMIT = "POSITION_LIMIT"
    EXPOSURE_LIMIT = "EXPOSURE_LIMIT"

    @property
    def tradeable(self) -> bool:
        return self is Feasibility.OK


@dataclass
class Exposure:
    by_market: dict = field(default_factory=dict)
    by_correlation: dict = field(default_factory=dict)
    by_wallet: dict = field(default_factory=dict)
    by_strategy: dict = field(default_factory=dict)
    gross: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Account:
    """The bankroll, in the eleven distinct senses above."""

    starting_capital: float = 100.00
    cash: float = 100.00
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    position_value: float = 0.0
    reserved: float = 0.0
    peak_equity: float = 100.00
    exposure: Exposure = field(default_factory=Exposure)
    open_positions: int = 0

    @property
    def equity(self) -> float:
        return self.cash + self.position_value

    @property
    def balance(self) -> float:
        return self.equity

    @property
    def available_cash(self) -> float:
        return max(0.0, self.cash - self.reserved)

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def return_pct(self) -> float:
        s = self.starting_capital
        return (self.equity - s) / s if s > 0 else 0.0

    @property
    def drawdown(self) -> float:
        p = max(self.peak_equity, self.starting_capital)
        return max(0.0, (p - self.equity) / p) if p > 0 else 0.0

    def risk_capital(self, cfg: CapitalConfig) -> float:
        """Deployable equity after the untouchable reserve.

        Reserve is a fraction of EQUITY, not of starting capital: a system that
        has lost 40% should also shrink the absolute size of its buffer, or the
        buffer eventually becomes the whole account and nothing can trade.
        """
        return min(self.available_cash, cfg.deployable(self.equity))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(equity=round(self.equity, 4),
                 available_cash=round(self.available_cash, 4),
                 total_pnl=round(self.total_pnl, 4),
                 return_pct=round(self.return_pct, 6),
                 drawdown=round(self.drawdown, 6))
        return d


@dataclass
class SizingResult:
    """A fully-costed trade proposal, or a written reason there isn't one."""

    feasibility: Feasibility
    reason: str = ""
    budget: float = 0.0                 # capital we were willing to commit
    size_usdc: float = 0.0              # capital actually committed
    size_shares: float = 0.0
    entry_price: float = 0.0            # after slippage + fees
    signal_price: float = 0.0
    fees: float = 0.0
    slippage_cost: float = 0.0
    max_loss: float = 0.0
    expected_gain: float = 0.0
    expected_value: float = 0.0
    prob_success: float = 0.0
    prob_adverse: float = 0.0
    kelly_fraction: float = 0.0
    fill_probability: float = 0.0
    available_liquidity: float = 0.0
    correlation_with_book: float = 0.0
    remaining_bankroll: float = 0.0
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.feasibility.tradeable and self.size_usdc > 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["feasibility"] = self.feasibility.value
        return d


class CapitalEngine:
    """Turns (probability, price, liquidity, portfolio) into a size or a refusal."""

    def __init__(self, st: Settings) -> None:
        self.st = st
        self.cfg: CapitalConfig = st.capital
        self.costs: CostConfig = st.costs

    # -- helpers ------------------------------------------------------------
    def entry_price(self, signal_price: float) -> float:
        bps = self.costs.slippage_bps + self.costs.fee_bps
        return min(0.999, signal_price * (1.0 + bps / 10_000.0))

    def kelly(self, p: float, price: float) -> float:
        """Fractional Kelly for a binary contract bought at `price`.

        Payoff is 1 on a win and 0 on a loss, so net odds are (1-price)/price.
        Always multiplied by `kelly_fraction` — full Kelly on a probability we
        estimated ourselves is a bet on our own calibration being exact, which
        it is not.
        """
        if not (0 < price < 1) or not (0 < p < 1):
            return 0.0
        b = (1.0 - price) / price
        f = (p * b - (1.0 - p)) / b
        return max(0.0, f) * self.cfg.kelly_fraction

    # -- the main entry point ----------------------------------------------
    def size(self, *, account: Account, probability: float,
             signal_price: float, available_liquidity: float,
             confidence: float = 1.0, correlation_key: str = "",
             wallet_followed: str = "", strategy_id: str = "") -> SizingResult:
        """Size one candidate trade, or refuse it with a reason.

        The order of checks is deliberate: cheap structural refusals first, so
        the expensive portfolio arithmetic only runs on candidates that could
        still become trades.
        """
        r = SizingResult(feasibility=Feasibility.OK, signal_price=signal_price)
        r.prob_success = probability
        r.prob_adverse = 1.0 - probability
        r.available_liquidity = available_liquidity

        if not (self.costs.min_price <= signal_price <= self.costs.max_price):
            r.feasibility = Feasibility.PRICE_OUT_OF_RANGE
            r.reason = (f"price {signal_price:.3f} outside tradeable band "
                        f"[{self.costs.min_price}, {self.costs.max_price}]")
            return r

        if account.open_positions >= self.cfg.max_open_positions:
            r.feasibility = Feasibility.POSITION_LIMIT
            r.reason = (f"{account.open_positions} open positions is at the "
                        f"limit of {self.cfg.max_open_positions}")
            return r

        risk_capital = account.risk_capital(self.cfg)
        r.remaining_bankroll = round(risk_capital, 4)
        if risk_capital < self.cfg.min_order_usdc:
            r.feasibility = Feasibility.NO_CASH
            r.reason = (f"risk capital ${risk_capital:.2f} is below the venue "
                        f"minimum order of ${self.cfg.min_order_usdc:.2f}")
            return r

        # --- budget: the tightest of Kelly, the per-trade cap, and what is left
        entry = self.entry_price(signal_price)
        r.entry_price = round(entry, 6)
        k = self.kelly(probability, entry)
        r.kelly_fraction = round(k, 6)

        cap_trade = account.equity * self.cfg.max_fraction_per_trade
        kelly_budget = account.equity * k * max(0.0, min(1.0, confidence))
        budget = min(cap_trade, kelly_budget if k > 0 else cap_trade,
                     risk_capital)

        # --- exposure caps. Each is a separate refusal reason, never merged:
        # "exposure limit" without saying WHICH one is unactionable.
        caps = (
            ("market", correlation_key or "",
             account.exposure.by_market.get(correlation_key, 0.0),
             self.cfg.max_fraction_per_market),
            ("correlated", correlation_key or "",
             account.exposure.by_correlation.get(correlation_key, 0.0),
             self.cfg.max_fraction_correlated),
            ("wallet-copy", wallet_followed or "",
             account.exposure.by_wallet.get(wallet_followed, 0.0),
             self.cfg.max_fraction_one_wallet_copy),
        )
        for name, key, used, frac in caps:
            if not key:
                continue
            room = account.equity * frac - used
            if room <= 0:
                r.feasibility = Feasibility.EXPOSURE_LIMIT
                r.reason = (f"{name} exposure for {key[:16]} is ${used:.2f}, "
                            f"already at the {frac:.0%} cap "
                            f"(${account.equity * frac:.2f})")
                r.detail["binding_cap"] = name
                return r
            budget = min(budget, room)

        r.budget = round(budget, 4)

        # --- venue minimums. THE $100 collision.
        if budget < self.cfg.min_order_usdc:
            r.feasibility = Feasibility.CAPITAL_INFEASIBLE
            r.reason = (
                f"budget ${budget:.2f} is below the ${self.cfg.min_order_usdc:.2f} "
                f"venue minimum. At ${account.equity:.2f} equity a "
                f"{self.cfg.max_fraction_per_trade:.0%} per-trade cap is "
                f"${cap_trade:.2f}; this strategy needs more capital or a "
                f"larger per-trade fraction, and raising the fraction raises "
                f"concentration risk. Not executable as configured.")
            return r

        shares = budget / entry
        if shares < self.cfg.min_shares:
            need = self.cfg.min_shares * entry
            r.feasibility = Feasibility.CAPITAL_INFEASIBLE
            r.reason = (
                f"budget ${budget:.2f} buys {shares:.2f} shares at "
                f"${entry:.3f}; venue minimum is {self.cfg.min_shares:g} shares "
                f"(${need:.2f}). Would need {need / account.equity:.1%} of "
                f"equity, above the {self.cfg.max_fraction_per_trade:.0%} cap.")
            r.detail["shares_needed"] = self.cfg.min_shares
            r.detail["usdc_needed"] = round(need, 4)
            return r

        # --- liquidity. We never assume we get all of the visible size.
        takeable = available_liquidity * self.costs.fill_ratio_assumption
        if available_liquidity <= 0:
            # Unmeasured liquidity is not infinite liquidity. Without a book we
            # cannot claim a fill, so this refuses rather than guesses.
            r.feasibility = Feasibility.LIQUIDITY_INFEASIBLE
            r.reason = ("no liquidity measurement available; refusing to assume "
                        "a fill. Order-book capture must be running.")
            return r
        if takeable < self.cfg.min_order_usdc:
            r.feasibility = Feasibility.LIQUIDITY_INFEASIBLE
            r.reason = (f"only ${takeable:.2f} takeable of ${available_liquidity:.2f} "
                        f"visible at the assumed "
                        f"{self.costs.fill_ratio_assumption:.0%} fill ratio")
            return r
        if takeable < budget:
            budget = takeable
            shares = budget / entry
            r.detail["reduced_by_liquidity"] = True
            if shares < self.cfg.min_shares:
                r.feasibility = Feasibility.LIQUIDITY_INFEASIBLE
                r.reason = (f"liquidity caps the order at {shares:.2f} shares, "
                            f"below the {self.cfg.min_shares:g}-share minimum")
                return r

        # --- round to the venue tick, downward. Rounding up would silently
        # breach the cap we just enforced.
        shares = math.floor(shares * 100) / 100.0
        size_usdc = shares * entry
        if size_usdc < self.cfg.min_order_usdc:
            r.feasibility = Feasibility.CAPITAL_INFEASIBLE
            r.reason = (f"after tick rounding the order is ${size_usdc:.2f}, "
                        f"below the ${self.cfg.min_order_usdc:.2f} minimum")
            return r

        # --- costs and outcome arithmetic
        r.size_shares = round(shares, 4)
        r.size_usdc = round(size_usdc, 4)
        r.slippage_cost = round(shares * (entry - signal_price), 6)
        r.fees = round(size_usdc * self.costs.fee_bps / 10_000.0, 6)
        r.max_loss = round(size_usdc, 4)             # binary: it goes to zero
        r.expected_gain = round(shares * (1.0 - entry), 4)
        r.expected_value = round(probability * r.expected_gain
                                 - (1.0 - probability) * r.max_loss, 4)
        r.fill_probability = round(min(1.0, takeable / max(size_usdc, 1e-9)), 4)
        r.remaining_bankroll = round(risk_capital - size_usdc, 4)
        r.correlation_with_book = round(
            account.exposure.by_correlation.get(correlation_key, 0.0)
            / account.equity, 4) if account.equity > 0 else 0.0
        r.detail.update(
            equity=round(account.equity, 4),
            per_trade_cap=round(cap_trade, 4),
            kelly_budget=round(kelly_budget, 4),
            risk_capital=round(risk_capital, 4))
        return r


def account_from_store(store, st: Settings, mode: str) -> Account:
    """Rebuild the account from persisted fills and positions.

    Derived, never cached as a running total. A stored balance that drifts from
    its own ledger is the classic way a paper system reports a return it did
    not earn.
    """
    acct = Account(starting_capital=st.capital.starting_capital,
                   cash=st.capital.starting_capital)

    closed = store.query(
        "SELECT realized_pnl FROM positions WHERE mode=? AND status!='OPEN'",
        (mode,))
    acct.realized_pnl = round(sum(float(r["realized_pnl"] or 0.0)
                                  for r in closed), 6)

    open_rows = store.query(
        "SELECT position_id, market_id, size_usdc, unrealized_pnl, "
        "       correlation_key, wallet_followed, strategy_id "
        "  FROM positions WHERE mode=? AND status='OPEN'", (mode,))
    invested = sum(float(r["size_usdc"] or 0.0) for r in open_rows)
    acct.unrealized_pnl = round(sum(float(r["unrealized_pnl"] or 0.0)
                                    for r in open_rows), 6)
    acct.position_value = round(invested + acct.unrealized_pnl, 6)
    acct.open_positions = len(open_rows)
    acct.cash = round(st.capital.starting_capital + acct.realized_pnl
                      - invested, 6)

    exp = Exposure(gross=round(invested, 4))
    for r in open_rows:
        sz = float(r["size_usdc"] or 0.0)
        exp.by_market[r["market_id"]] = exp.by_market.get(r["market_id"], 0.0) + sz
        ck = r["correlation_key"] or r["market_id"]
        exp.by_correlation[ck] = exp.by_correlation.get(ck, 0.0) + sz
        if r["wallet_followed"]:
            exp.by_wallet[r["wallet_followed"]] = \
                exp.by_wallet.get(r["wallet_followed"], 0.0) + sz
        if r["strategy_id"]:
            exp.by_strategy[r["strategy_id"]] = \
                exp.by_strategy.get(r["strategy_id"], 0.0) + sz
    acct.exposure = exp

    peak = store.get_meta(f"peak_equity_{mode}", "")
    acct.peak_equity = max(float(peak) if peak else 0.0,
                           acct.equity, st.capital.starting_capital)
    return acct
