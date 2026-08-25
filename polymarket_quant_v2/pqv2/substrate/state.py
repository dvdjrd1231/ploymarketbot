"""Point-in-time wallet state, and the observation vector built from it.

The single most common way a copy-trading backtest lies to itself:

    "this wallet has a 68% win rate, so follow its next trade"

computed over the wallet's whole history, including trades that had not
resolved at the moment of the signal. Rule 7 forbids it, and on a prediction
market it is not a small effect -- payoff happens at resolution, not at trade,
so a wallet's rank stays unstable for weeks after it acts.

The rule is enforced mechanically rather than promised:

    a trade's outcome enters wallet state at settled_ts, never at ts.

`stream_observations` walks the tape forward and holds unsettled trades in a
heap; a trade's contribution folds into its wallet's statistics only once the
clock passes its settlement. An observation emitted at time T is therefore
reproducible from data a live system would have held at T.

`tests/test_causality.py` asserts this against a constructed case, and against
a second case that would pass a naive implementation.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from typing import Iterator, Sequence

from ..config import Settings
from .data import SettledTrade, iter_settled


@dataclass
class WalletState:
    """Everything known about a wallet from SETTLED evidence only."""

    settled_n: int = 0
    settled_wins: int = 0
    settled_stake: float = 0.0
    settled_pnl: float = 0.0

    # Rolling window: recency matters more than lifetime record for a wallet
    # that changed behaviour.
    recent: list = field(default_factory=list)
    recent_returns: list = field(default_factory=list)

    # Observed-at-trade-time facts. These need no settlement, so they update
    # immediately -- knowing that a wallet just traded is not look-ahead.
    seen_n: int = 0
    last_ts: int = 0
    open_notional: float = 0.0
    tokens_seen: set = field(default_factory=set)
    markets_seen: set = field(default_factory=set)
    consecutive_losses: int = 0
    consecutive_wins: int = 0
    recent_notional: list = field(default_factory=list)
    recent_prices: list = field(default_factory=list)
    hour_counts: list = field(default_factory=lambda: [0] * 24)

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

    def observe_trade(self, tr: SettledTrade) -> None:
        self.seen_n += 1
        self.last_ts = tr.ts
        self.open_notional += tr.usdc
        self.tokens_seen.add(tr.token_id)
        if tr.market_id:
            self.markets_seen.add(tr.market_id)
        self.recent_notional.append(tr.usdc)
        self.recent_prices.append(tr.price)
        if len(self.recent_notional) > self.ROLL:
            self.recent_notional.pop(0)
            self.recent_prices.pop(0)
        self.hour_counts[(tr.ts // 3600) % 24] += 1

    # -- derived -------------------------------------------------------------
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

    @property
    def avg_notional(self) -> float:
        h = self.recent_notional
        return sum(h) / len(h) if h else 0.0

    @property
    def avg_price(self) -> float:
        h = self.recent_prices
        return sum(h) / len(h) if h else 0.0

    def edge_t_stat(self) -> float:
        """How much of this wallet's ROI is distinguishable from noise.

        Used for point-in-time ranking so a wallet with 4 lucky trades cannot
        outrank one with 300 consistent ones. Rule 30 in one function.
        """
        n = len(self.recent_returns)
        if n < 5:
            return 0.0
        mean = sum(self.recent_returns) / n
        var = sum((r - mean) ** 2 for r in self.recent_returns) / (n - 1)
        return mean / math.sqrt(var / n) if var > 0 else 0.0


@dataclass(frozen=True, slots=True)
class Observation:
    """A candidate copy opportunity with only causally-available context.

    Adding a field to this class is the one place look-ahead can enter the
    system, so every field carries a note on when it becomes knowable.
    """

    trade: SettledTrade

    # wallet context -- settled evidence only, folded at settled_ts
    w_settled_n: int
    w_win_rate: float
    w_roi: float
    w_roll_win_rate: float
    w_roll_roi: float
    w_edge_t: float
    w_consec_losses: int
    w_consec_wins: int

    # observed-at-trade-time context -- knowable the instant the wallet acts
    w_seen_n: int
    w_secs_since_prev: int
    w_open_notional: float
    w_token_repeat: bool
    w_market_repeat: bool
    w_avg_notional: float
    w_avg_price: float

    # this trade
    price: float
    notional: float
    size: float
    rel_notional: float           # this stake vs the wallet's recent norm
    price_vs_wallet_norm: float   # this price vs the wallet's recent norm
    hour_of_day: int

    # market context -- horizon only, NEVER the outcome
    secs_to_settle: int
    market_recent_prints: int
    market_price_move: float      # move over the last hour of tape, pre-trade
    market_velocity: float        # move per hour
    tape_price_gap: float         # this print vs the previous print

    def as_dict(self) -> dict:
        d = {k: getattr(self, k) for k in self.__slots__ if k != "trade"}
        d.update(wallet=self.trade.wallet, ts=self.trade.ts,
                 token_id=self.trade.token_id, market_id=self.trade.market_id,
                 resolution=self.trade.resolution)
        return d

    # Convenience for the behaviour matcher / feature evaluators.
    def feature(self, name: str, default: float = 0.0) -> float:
        return float(getattr(self, name, default) or 0.0)


class _MarketTape:
    """Rolling per-token print history, built forward as the stream advances.

    Deliberately built from the SAME forward pass rather than queried: querying
    a token's prints would return prints from the future.
    """

    def __init__(self, window: int = 3600) -> None:
        self.window = window
        self._prints: dict[str, list] = {}

    def context(self, token_id: str, ts: int, price: float) -> tuple:
        hist = self._prints.get(token_id) or []
        cut = ts - self.window
        recent = [(t, p) for t, p in hist if t >= cut]
        if recent:
            move = price - recent[0][1]
            span_h = max((ts - recent[0][0]) / 3600.0, 1e-6)
            velocity = move / span_h
        else:
            move = velocity = 0.0
        gap = price - hist[-1][1] if hist else 0.0
        return len(recent), move, velocity, gap

    def add(self, token_id: str, ts: int, price: float) -> None:
        hist = self._prints.setdefault(token_id, [])
        hist.append((ts, price))
        if len(hist) > 400:
            del hist[:200]


def stream_observations(st: Settings, *, wallets: Sequence[str] | None = None,
                        min_notional: float = 1.0, ts_from: int = 0,
                        ts_to: int = 0) -> Iterator[Observation]:
    """Walk the tape forward, emitting one Observation per copyable trade.

    Memory is bounded by the number of SIMULTANEOUSLY UNSETTLED trades, not by
    the size of the tape.
    """
    states: dict[str, WalletState] = {}
    pending: list = []            # (settle_at, seq, wallet, won, ret, stake)
    tape = _MarketTape()
    seq = 0

    for tr in iter_settled(st, wallets=wallets, min_notional=min_notional,
                           ts_from=ts_from, ts_to=ts_to):
        # 1. Advance the settlement clock. Anything that resolved at or before
        #    now is knowable now, and only now.
        while pending and pending[0][0] <= tr.ts:
            _, _, w, won, gret, stake = heapq.heappop(pending)
            states.setdefault(w, WalletState()).fold_settled(won, gret, stake)

        s = states.setdefault(tr.wallet, WalletState())
        avg_n = s.avg_notional or tr.usdc
        avg_p = s.avg_price or tr.price
        prints, move, velocity, gap = tape.context(tr.token_id, tr.ts, tr.price)

        yield Observation(
            trade=tr,
            w_settled_n=s.settled_n, w_win_rate=s.win_rate, w_roi=s.roi,
            w_roll_win_rate=s.rolling_win_rate, w_roll_roi=s.rolling_roi,
            w_edge_t=s.edge_t_stat(), w_consec_losses=s.consecutive_losses,
            w_consec_wins=s.consecutive_wins, w_seen_n=s.seen_n,
            w_secs_since_prev=(tr.ts - s.last_ts) if s.last_ts else -1,
            w_open_notional=s.open_notional,
            w_token_repeat=tr.token_id in s.tokens_seen,
            w_market_repeat=bool(tr.market_id) and tr.market_id in s.markets_seen,
            w_avg_notional=avg_n, w_avg_price=avg_p,
            price=tr.price, notional=tr.usdc, size=tr.size,
            rel_notional=tr.usdc / avg_n if avg_n > 0 else 1.0,
            price_vs_wallet_norm=tr.price - avg_p,
            hour_of_day=(tr.ts // 3600) % 24,
            secs_to_settle=max(0, tr.settled_ts - tr.ts) if tr.settled_ts else -1,
            market_recent_prints=prints, market_price_move=move,
            market_velocity=velocity, tape_price_gap=gap,
        )

        # 2. Only AFTER emitting do we record that this trade happened.
        s.observe_trade(tr)
        tape.add(tr.token_id, tr.ts, tr.price)

        # 3. Queue its outcome for the moment it settles -- not before. A trade
        #    with no settlement clock is parked past the end of time rather
        #    than folded in early.
        seq += 1
        settle_at = tr.settled_ts if tr.settled_ts else tr.ts + 10 ** 9
        heapq.heappush(pending,
                       (settle_at, seq, tr.wallet, tr.won, tr.gross_return(),
                        tr.usdc))


def collect(st: Settings, *, wallets: Sequence[str] | None = None,
            min_notional: float = 1.0, ts_from: int = 0,
            ts_to: int = 0, limit: int = 0) -> list:
    out = []
    for o in stream_observations(st, wallets=wallets, min_notional=min_notional,
                                 ts_from=ts_from, ts_to=ts_to):
        out.append(o)
        if limit and len(out) >= limit:
            break
    return out
