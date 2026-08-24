"""Point-in-time wallet state, and the feature vector built from it.

The single most common way a copy-trading backtest lies to itself:

    "this wallet has a 68% win rate, so follow its next trade"

computed over the wallet's *whole* history, including trades that had not
resolved yet at the moment of the signal. §43 forbids exactly this, and it is
not a small effect — on this dataset a wallet's rank is unstable for weeks
after its trades are placed, because a prediction market pays out only at
resolution.

So the rule enforced here is mechanical rather than promised:

    a trade's outcome enters wallet state at `settled_ts`, never at `ts`.

`stream_features` walks the tape forward and keeps unsettled trades in a heap.
A trade's contribution is folded into its wallet's running statistics only once
the clock passes its settlement. A feature vector emitted at time T is
therefore reproducible from data a live system would have held at time T.

`test_causality.py` asserts this against a deliberately constructed case.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Iterator

from .config import Settings
from .data import SettledTrade, iter_settled


@dataclass
class WalletState:
    """Everything known about a wallet from *settled* evidence only."""

    settled_n: int = 0
    settled_wins: int = 0
    settled_stake: float = 0.0
    settled_pnl: float = 0.0

    # Rolling window over the last N settled outcomes (recency matters more
    # than lifetime record for a wallet that changed behaviour).
    recent: list[int] = field(default_factory=list)
    recent_returns: list[float] = field(default_factory=list)

    # Observed-at-trade-time facts. These need no settlement, so they update
    # immediately — knowing a wallet just traded is not look-ahead.
    seen_n: int = 0
    last_ts: int = 0
    open_notional: float = 0.0
    tokens_seen: set[str] = field(default_factory=set)
    consecutive_losses: int = 0
    consecutive_wins: int = 0

    ROLL = 50

    def fold_settled(self, won: bool, gross_ret: float, stake: float) -> None:
        self.settled_n += 1
        self.settled_wins += int(won)
        self.settled_stake += stake
        self.settled_pnl += stake * gross_ret
        self.recent.append(int(won))
        self.recent_returns.append(gross_ret)
        if len(self.recent) > self.ROLL:
            self.recent.pop(0)
            self.recent_returns.pop(0)
        if won:
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self.consecutive_wins = 0
        self.open_notional = max(0.0, self.open_notional - stake)

    @property
    def win_rate(self) -> float:
        return self.settled_wins / self.settled_n if self.settled_n else 0.0

    @property
    def roi(self) -> float:
        return self.settled_pnl / self.settled_stake if self.settled_stake > 0 else 0.0

    @property
    def rolling_win_rate(self) -> float:
        return sum(self.recent) / len(self.recent) if self.recent else 0.0

    @property
    def rolling_roi(self) -> float:
        n = len(self.recent_returns)
        return sum(self.recent_returns) / n if n else 0.0

    def edge_t_stat(self) -> float:
        """How much of this wallet's ROI is distinguishable from noise.

        A t-like statistic on per-trade returns. Used for point-in-time
        ranking (§10) so that a wallet with 4 lucky trades cannot outrank one
        with 300 consistent ones.
        """
        n = len(self.recent_returns)
        if n < 5:
            return 0.0
        mean = sum(self.recent_returns) / n
        var = sum((r - mean) ** 2 for r in self.recent_returns) / (n - 1)
        if var <= 0:
            return 0.0
        return mean / math.sqrt(var / n)


@dataclass(frozen=True, slots=True)
class Observation:
    """A candidate copy opportunity, with only causally-available context."""

    trade: SettledTrade
    # wallet context, settled-only
    w_settled_n: int
    w_win_rate: float
    w_roi: float
    w_roll_win_rate: float
    w_roll_roi: float
    w_edge_t: float
    w_consec_losses: int
    w_consec_wins: int
    # observed-at-time context
    w_seen_n: int
    w_secs_since_prev: int
    w_open_notional: float
    w_token_repeat: bool
    # trade context
    price: float
    notional: float
    rel_notional: float          # this trade vs wallet's recent average
    # market context
    secs_to_settle: int          # NOTE: horizon only, never the outcome

    def as_dict(self) -> dict:
        return {
            "wallet": self.trade.wallet, "ts": self.trade.ts,
            "token_id": self.trade.token_id, "market_id": self.trade.market_id,
            "price": self.price, "notional": self.notional,
            "w_settled_n": self.w_settled_n, "w_win_rate": self.w_win_rate,
            "w_roi": self.w_roi, "w_roll_win_rate": self.w_roll_win_rate,
            "w_roll_roi": self.w_roll_roi, "w_edge_t": self.w_edge_t,
            "w_consec_losses": self.w_consec_losses,
            "w_consec_wins": self.w_consec_wins,
            "w_seen_n": self.w_seen_n,
            "w_secs_since_prev": self.w_secs_since_prev,
            "rel_notional": self.rel_notional,
            "secs_to_settle": self.secs_to_settle,
            "resolution": self.trade.resolution,
        }


def stream_features(
    st: Settings,
    *,
    wallets: list[str] | None = None,
    min_notional: float = 1.0,
) -> Iterator[Observation]:
    """Walk the tape forward, emitting one Observation per copyable trade.

    Memory is bounded by the number of *simultaneously unsettled* trades, not
    by the size of the tape (§22).
    """
    states: dict[str, WalletState] = {}
    pending: list[tuple[int, int, str, bool, float, float]] = []
    seq = 0
    recent_notional: dict[str, list[float]] = {}

    for tr in iter_settled(st, wallets=wallets, min_notional=min_notional):
        # 1. Advance the settlement clock to this trade's timestamp. Anything
        #    that resolved at or before now is knowable now, and only now.
        while pending and pending[0][0] <= tr.ts:
            _, _, w, won, gret, stake = heapq.heappop(pending)
            states.setdefault(w, WalletState()).fold_settled(won, gret, stake)

        s = states.setdefault(tr.wallet, WalletState())

        hist = recent_notional.setdefault(tr.wallet, [])
        avg_notional = sum(hist) / len(hist) if hist else tr.usdc
        rel = tr.usdc / avg_notional if avg_notional > 0 else 1.0

        yield Observation(
            trade=tr,
            w_settled_n=s.settled_n,
            w_win_rate=s.win_rate,
            w_roi=s.roi,
            w_roll_win_rate=s.rolling_win_rate,
            w_roll_roi=s.rolling_roi,
            w_edge_t=s.edge_t_stat(),
            w_consec_losses=s.consecutive_losses,
            w_consec_wins=s.consecutive_wins,
            w_seen_n=s.seen_n,
            w_secs_since_prev=(tr.ts - s.last_ts) if s.last_ts else -1,
            w_open_notional=s.open_notional,
            w_token_repeat=tr.token_id in s.tokens_seen,
            price=tr.price,
            notional=tr.usdc,
            rel_notional=rel,
            secs_to_settle=max(0, tr.settled_ts - tr.ts) if tr.settled_ts else -1,
        )

        # 2. Only *after* emitting do we record that this trade happened.
        s.seen_n += 1
        s.last_ts = tr.ts
        s.open_notional += tr.usdc
        s.tokens_seen.add(tr.token_id)
        hist.append(tr.usdc)
        if len(hist) > WalletState.ROLL:
            hist.pop(0)

        # 3. Queue its outcome for the moment it settles — not before.
        settle_at = tr.settled_ts if tr.settled_ts else tr.ts + 10**9
        seq += 1
        heapq.heappush(
            pending, (settle_at, seq, tr.wallet, tr.won, tr.gross_return(), tr.usdc)
        )
