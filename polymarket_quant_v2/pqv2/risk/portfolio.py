"""The portfolio layer: capital management, kept separate from strategy quality.

The rule that gives this module its shape:

    The portfolio layer may reject a trade for risk reasons.
    BUT it must NEVER erase the underlying signal.

So a portfolio rejection is recorded as PORTFOLIO_REJECTED with the cap that
bound, and the signal that produced it stays in the ledger with its strategy
verdict intact. That is what lets the diagnostic answer two different
questions:

    "is this strategy any good?"        -> strategy acceptance rate
    "can we afford to trade it?"        -> portfolio approval rate

Conflating them is how a good strategy gets retired for being unaffordable, and
how a bad one survives because the portfolio was full whenever it fired.

Strategy A and Strategy B both pass through here, and neither can see the
other's gates -- portfolio caps are owned by PORTFOLIO_RISK, which may block
both, which is exactly what a portfolio layer is for.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..config import Settings
from ..gates import Owner, assert_may_block
from .compounding import Account, Position


@dataclass
class PortfolioVerdict:
    approved: bool
    gate_key: str = ""
    reason: str = ""
    stake: float = 0.0
    adjustments: list = None

    def to_dict(self) -> dict:
        return {"approved": self.approved, "gate_key": self.gate_key,
                "reason": self.reason, "stake": round(self.stake, 2),
                "adjustments": self.adjustments or []}


class Portfolio:
    """Exposure accounting across both routes."""

    # Below this many open positions, share-of-book caps cannot be satisfied by
    # any trade and are not applied. See the note in `evaluate`.
    MIN_BOOK_FOR_SHARES = 4

    def __init__(self, st: Settings, account: Account) -> None:
        self.st = st
        self.account = account
        self.correlation_groups: dict = {}   # token -> group key

    # -- views ---------------------------------------------------------------
    def by(self, attr: str) -> dict:
        out: dict = defaultdict(float)
        for p in self.account.positions.values():
            out[getattr(p, attr, "") or ""] += p.stake
        return dict(out)

    def share(self, attr: str, value: str) -> float:
        total = self.account.allocated
        if total <= 0:
            return 0.0
        return self.by(attr).get(value, 0.0) / total

    def market_exposure(self, market_id: str) -> float:
        return self.by("market_id").get(market_id, 0.0)

    def correlated_exposure(self, group: str) -> float:
        total = self.account.allocated
        if total <= 0 or not group:
            return 0.0
        same = sum(p.stake for p in self.account.positions.values()
                   if self.correlation_groups.get(p.token_id) == group)
        return same / total

    # -- the gate ------------------------------------------------------------
    def evaluate(self, candidate: Position, *, route: str,
                 correlation_group: str = "") -> PortfolioVerdict:
        """Approve, shrink, or reject. Never silently modify."""
        st = self.st
        adjustments: list = []
        stake = candidate.stake

        # GLOBAL_SAFETY first. These apply to both routes and carry evidence in
        # gates.py; a strategy cannot opt out of them.
        for key in ("g.drawdown_halt", "g.per_trade_cap", "g.per_market_cap"):
            assert_may_block(key, route)

        self.account.enforce_halt(st)
        if self.account.halted:
            return PortfolioVerdict(False, "g.drawdown_halt",
                                    self.account.halt_reason)

        hard = self.account.equity * st.risk.max_fraction_per_trade
        if stake > hard:
            adjustments.append(
                f"per-trade cap {st.risk.max_fraction_per_trade:.1%}: "
                f"${stake:,.2f} -> ${hard:,.2f}")
            stake = hard

        market_cap = self.account.equity * st.risk.max_fraction_per_market
        already = self.market_exposure(candidate.market_id)
        if already + stake > market_cap:
            room = max(0.0, market_cap - already)
            if room < 1.0:
                return PortfolioVerdict(
                    False, "g.per_market_cap",
                    f"market {candidate.market_id[:12]} already carries "
                    f"${already:,.2f} of a ${market_cap:,.2f} cap "
                    f"({st.risk.max_fraction_per_market:.0%} of equity)")
            adjustments.append(f"market cap: ${stake:,.2f} -> ${room:,.2f}")
            stake = room

        # PORTFOLIO_RISK. These reject the TRADE, never the signal.
        if any(p.token_id == candidate.token_id
               for p in self.account.positions.values()):
            return PortfolioVerdict(False, "p.duplicate",
                                    f"already holding {candidate.token_id[:12]}")

        if len(self.account.positions) >= st.risk.max_open_positions:
            return PortfolioVerdict(
                False, "p.max_open",
                f"{len(self.account.positions)} open positions at the cap of "
                f"{st.risk.max_open_positions}")

        # Concentration caps are shares of the book, so they are arithmetically
        # unsatisfiable while the book is nearly empty: the first position is
        # always 100% of exposure and would be rejected by any share cap below
        # 1.0, forever. That is a bootstrap stall, and it is the same class of
        # error as V1's learning-mode deadlock -- a rule that can never be
        # satisfied reads exactly like a rule that is never satisfied.
        #
        # So share caps bind only once the book is large enough for a share to
        # mean something. Below that, the per-trade and per-market caps in
        # GLOBAL_SAFETY above are what bound risk, and they are absolute
        # fractions of equity rather than shares of the book.
        total_after = self.account.allocated + stake
        share_caps_apply = len(self.account.positions) >= self.MIN_BOOK_FOR_SHARES
        if total_after > 0 and share_caps_apply:
            strat_share = (self.by("strategy_id").get(candidate.strategy_id, 0.0)
                           + stake) / total_after
            if strat_share > st.risk.max_strategy_share:
                return PortfolioVerdict(
                    False, "p.strategy_share",
                    f"strategy {candidate.strategy_id} would be "
                    f"{strat_share:.0%} of exposure, cap "
                    f"{st.risk.max_strategy_share:.0%}")
            wallet_share = (self.by("wallet").get(candidate.wallet, 0.0)
                            + stake) / total_after
            if wallet_share > st.risk.max_wallet_share:
                return PortfolioVerdict(
                    False, "p.wallet_share",
                    f"following {candidate.wallet[:12]} would be "
                    f"{wallet_share:.0%} of exposure, cap "
                    f"{st.risk.max_wallet_share:.0%}. Copying one wallet with "
                    "many positions is one bet, not many.")

        if correlation_group:
            self.correlation_groups[candidate.token_id] = correlation_group
            corr = self.correlated_exposure(correlation_group)
            if corr > st.risk.max_correlated_share:
                return PortfolioVerdict(
                    False, "p.correlated",
                    f"correlated group {correlation_group} already "
                    f"{corr:.0%} of exposure")

        ok, why = self.account.can_open(stake)
        if not ok:
            return PortfolioVerdict(False, "p.insufficient_capital", why)

        return PortfolioVerdict(True, stake=stake, adjustments=adjustments)

    def correlation_matrix(self, returns_by_strategy: dict) -> list:
        """Pairwise return correlation between strategies.

        Two strategies with 0.9 correlation are one strategy with two names,
        and sizing them independently doubles the real risk. Reported on the
        dashboard because it is invisible in per-strategy statistics.
        """
        import math
        keys = sorted(returns_by_strategy)
        out = []
        for i, a in enumerate(keys):
            for b in keys[i + 1:]:
                xa, xb = returns_by_strategy[a], returns_by_strategy[b]
                n = min(len(xa), len(xb))
                if n < 10:
                    continue
                xa, xb = xa[:n], xb[:n]
                ma, mb = sum(xa) / n, sum(xb) / n
                va = sum((x - ma) ** 2 for x in xa)
                vb = sum((x - mb) ** 2 for x in xb)
                cov = sum((xa[k] - ma) * (xb[k] - mb) for k in range(n))
                if va <= 0 or vb <= 0:
                    continue
                out.append({"a": a, "b": b, "n": n,
                            "correlation": round(cov / math.sqrt(va * vb), 4)})
        out.sort(key=lambda d: -abs(d["correlation"]))
        return out
