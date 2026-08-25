"""Wallet decomposition: turning a wallet's trades into a behavioural profile.

The brief's central instruction, and the thing that makes this transferable:

    Do not learn "RN1 bought YES at 63 cents."
    Learn  "RN1 tends to enter this type of position when this collection of
            measurable conditions exists."

So nothing here records prices or tokens. Everything is a DISTRIBUTION over
conditions that were measurable at the moment the wallet acted, which is what
lets the same profile be matched against a wallet nobody has ever seen.

Every statistic is computed from the point-in-time observation stream, so a
profile built over a training window contains nothing that was not knowable
inside that window.

A wallet is not assumed to run one strategy. `split_families` looks for
distinct behavioural clusters inside a single wallet and reports them
separately when the evidence supports it -- and says so when it does not.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field, asdict


def _pct(xs, q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(q * (len(s) - 1))))
    return s[i]


def _summarise(xs) -> dict:
    if not xs:
        return {"n": 0, "mean": 0.0, "p10": 0.0, "p25": 0.0, "p50": 0.0,
                "p75": 0.0, "p90": 0.0, "std": 0.0}
    n = len(xs)
    mean = sum(xs) / n
    var = sum((x - mean) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    return {"n": n, "mean": mean, "p10": _pct(xs, 0.10), "p25": _pct(xs, 0.25),
            "p50": _pct(xs, 0.50), "p75": _pct(xs, 0.75), "p90": _pct(xs, 0.90),
            "std": math.sqrt(max(var, 0.0))}


@dataclass
class EntryModel:
    """When and where the wallet chooses to enter."""

    price: dict = field(default_factory=dict)
    notional: dict = field(default_factory=dict)
    rel_notional: dict = field(default_factory=dict)
    secs_to_settle: dict = field(default_factory=dict)
    market_prints: dict = field(default_factory=dict)
    market_move: dict = field(default_factory=dict)
    price_vs_norm: dict = field(default_factory=dict)
    hour_histogram: list = field(default_factory=lambda: [0] * 24)
    opening_entry_share: float = 0.0        # vs adding to an existing token
    new_market_share: float = 0.0


@dataclass
class SizingModel:
    """How conviction is expressed in stake."""

    mean_notional: float = 0.0
    median_notional: float = 0.0
    max_notional: float = 0.0
    dispersion: float = 0.0                 # std / mean; is size a signal at all?
    conviction_ratio: float = 0.0           # p90 / p50
    size_predicts_win: float = 0.0          # win rate of big vs small entries
    escalates_after_win: float = 0.0        # size ratio after a win vs after a loss


@dataclass
class HoldModel:
    """How long positions stay open, and whether they run to settlement.

    NOTE: reconstructed from BUY-side data only. SELL and REDEEM events exist
    in the tape (149,080 SELLs, 132,082 REDEEMs) but position-state
    reconstruction across MERGE/SPLIT/CONVERSION is not built, so
    `settlement_share` is an upper bound. Recorded honestly rather than
    presented as measured.
    """

    secs_to_settle: dict = field(default_factory=dict)
    settlement_share: float = 1.0
    settlement_share_confidence: str = "upper_bound"


@dataclass
class RiskModel:
    """What the wallet does around losses -- the loss-control signature."""

    max_consec_losses: int = 0
    mean_consec_losses: float = 0.0
    size_after_loss_ratio: float = 1.0      # <1 means it de-risks after losses
    pause_after_loss_secs: float = 0.0
    trades_per_day: float = 0.0
    market_concentration: float = 0.0       # HHI over markets


@dataclass
class OutcomeProfile:
    """The winner/loser shape of the wallet, hold-to-resolution."""

    n: int = 0
    win_rate: float = 0.0
    expectancy: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    tail_loss_p05: float = 0.0


@dataclass
class WalletProfile:
    """The complete behavioural reconstruction of one wallet."""

    wallet: str
    n_observations: int = 0
    first_ts: int = 0
    last_ts: int = 0
    entry: EntryModel = field(default_factory=EntryModel)
    sizing: SizingModel = field(default_factory=SizingModel)
    hold: HoldModel = field(default_factory=HoldModel)
    risk: RiskModel = field(default_factory=RiskModel)
    outcome: OutcomeProfile = field(default_factory=OutcomeProfile)
    top_markets: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    # Share of observations that had any settled evidence behind them. Low
    # values mean the wallet-state axes of the search are inert; see notes.
    pit_evidence_share: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    # The vector the similarity engine compares. Chosen so that two wallets
    # running the same idea on different markets score alike, and two wallets
    # with the same P&L but different methods do not.
    def signature(self) -> dict:
        return {
            "price_p50": self.entry.price.get("p50", 0.0),
            "price_p10": self.entry.price.get("p10", 0.0),
            "price_p90": self.entry.price.get("p90", 0.0),
            "price_std": self.entry.price.get("std", 0.0),
            "notional_p50": math.log1p(max(0.0, self.sizing.median_notional)),
            "size_dispersion": self.sizing.dispersion,
            "conviction_ratio": min(self.sizing.conviction_ratio, 10.0),
            "horizon_p50": math.log1p(max(0.0, self.hold.secs_to_settle.get("p50", 0.0))),
            "opening_share": self.entry.opening_entry_share,
            "new_market_share": self.entry.new_market_share,
            "trades_per_day": math.log1p(self.risk.trades_per_day),
            "market_hhi": self.risk.market_concentration,
            "size_after_loss": self.risk.size_after_loss_ratio,
            "chases": self.entry.market_move.get("mean", 0.0),
            "thin_tape_tolerance": -math.log1p(
                max(0.0, self.entry.market_prints.get("p50", 0.0))),
        }


def build_profile(wallet: str, observations: list) -> WalletProfile:
    """Reconstruct one wallet from its point-in-time observation stream.

    `observations` must come from `substrate.state.stream_observations`, which
    guarantees every field was knowable when the wallet acted. Passing raw
    trades here would silently reintroduce look-ahead.
    """
    obs = [o for o in observations if o.trade.wallet == wallet]
    p = WalletProfile(wallet=wallet, n_observations=len(obs))
    if not obs:
        p.notes.append("no observations")
        return p

    p.first_ts, p.last_ts = obs[0].trade.ts, obs[-1].trade.ts
    span_days = max((p.last_ts - p.first_ts) / 86400.0, 1e-6)

    prices = [o.price for o in obs]
    notionals = [o.notional for o in obs]
    rels = [o.rel_notional for o in obs]
    horizons = [o.secs_to_settle for o in obs if o.secs_to_settle > 0]

    p.entry.price = _summarise(prices)
    p.entry.notional = _summarise(notionals)
    p.entry.rel_notional = _summarise(rels)
    p.entry.secs_to_settle = _summarise(horizons)
    p.entry.market_prints = _summarise([o.market_recent_prints for o in obs])
    p.entry.market_move = _summarise([o.market_price_move for o in obs])
    p.entry.price_vs_norm = _summarise([o.price_vs_wallet_norm for o in obs])
    for o in obs:
        p.entry.hour_histogram[o.hour_of_day] += 1
    p.entry.opening_entry_share = sum(1 for o in obs if not o.w_token_repeat) / len(obs)
    p.entry.new_market_share = sum(1 for o in obs if not o.w_market_repeat) / len(obs)

    # --- sizing ---
    mean_n = p.entry.notional["mean"]
    p.sizing.mean_notional = mean_n
    p.sizing.median_notional = p.entry.notional["p50"]
    p.sizing.max_notional = max(notionals)
    p.sizing.dispersion = (p.entry.notional["std"] / mean_n) if mean_n > 0 else 0.0
    p.sizing.conviction_ratio = (
        p.entry.notional["p90"] / p.entry.notional["p50"]
        if p.entry.notional["p50"] > 0 else 0.0)

    # Does a big bet actually win more often? This is the empirical test of
    # whether size means anything for this wallet -- and it is the input Win
    # Expansion is allowed to use. If it is ~0 the wallet's size carries no
    # information and expansion must not key off it.
    big = [o for o in obs if o.rel_notional >= 1.5]
    small = [o for o in obs if o.rel_notional < 1.5]
    if big and small:
        bw = sum(1 for o in big if o.trade.won) / len(big)
        sw = sum(1 for o in small if o.trade.won) / len(small)
        p.sizing.size_predicts_win = bw - sw

    after_win = [o.notional for o in obs if o.w_consec_wins > 0]
    after_loss = [o.notional for o in obs if o.w_consec_losses > 0]
    if after_win and after_loss:
        aw = sum(after_win) / len(after_win)
        al = sum(after_loss) / len(after_loss)
        p.sizing.escalates_after_win = aw / al if al > 0 else 0.0
        p.risk.size_after_loss_ratio = al / aw if aw > 0 else 1.0

    # --- hold / settlement ---
    p.hold.secs_to_settle = _summarise(horizons)

    # --- risk ---
    p.risk.max_consec_losses = max((o.w_consec_losses for o in obs), default=0)
    losses = [o.w_consec_losses for o in obs if o.w_consec_losses > 0]
    p.risk.mean_consec_losses = sum(losses) / len(losses) if losses else 0.0
    gaps = [o.w_secs_since_prev for o in obs
            if o.w_consec_losses > 0 and o.w_secs_since_prev > 0]
    p.risk.pause_after_loss_secs = sum(gaps) / len(gaps) if gaps else 0.0
    p.risk.trades_per_day = len(obs) / span_days

    market_counts = Counter(o.trade.market_id or o.trade.token_id for o in obs)
    total = sum(market_counts.values())
    p.risk.market_concentration = sum((c / total) ** 2 for c in market_counts.values())
    p.top_markets = market_counts.most_common(10)

    # --- outcome, hold-to-resolution, gross of copy costs ---
    rets = [o.trade.gross_return() for o in obs]
    wins = [r for r in rets if r > 0]
    lose = [r for r in rets if r <= 0]
    p.outcome.n = len(rets)
    p.outcome.win_rate = len(wins) / len(rets)
    p.outcome.expectancy = sum(rets) / len(rets)
    p.outcome.avg_win = sum(wins) / len(wins) if wins else 0.0
    p.outcome.avg_loss = sum(lose) / len(lose) if lose else 0.0
    gl = -sum(lose)
    p.outcome.profit_factor = sum(wins) / gl if gl > 0 else 0.0
    p.outcome.largest_win = max(wins) if wins else 0.0
    p.outcome.largest_loss = min(lose) if lose else 0.0
    lose_sorted = sorted(lose)
    k = max(1, len(lose_sorted) // 20)
    p.outcome.tail_loss_p05 = sum(lose_sorted[:k]) / k if lose_sorted else 0.0

    if len(obs) < 30:
        p.notes.append(f"only {len(obs)} observations - profile is indicative, "
                       "not evidential")

    # Data-quality check, not a statistic. If almost no observation had any
    # settled evidence behind it, the wallet-state features (win rate, streaks,
    # edge t) are structurally empty rather than measured, and any strategy
    # keyed on them is untestable on this substrate. This is the visible
    # consequence of resolutions.settled_ts being 0 in all 8,116 rows: outcomes
    # arrive at observation time, so most trades appear to settle at once.
    with_evidence = sum(1 for o in obs if o.w_settled_n > 0)
    p.pit_evidence_share = with_evidence / len(obs)
    if p.pit_evidence_share < 0.25:
        p.notes.append(
            f"only {p.pit_evidence_share:.0%} of this wallet's trades had ANY "
            "settled track record available at the moment they were placed. "
            "Streak, win-rate and edge-t filters are therefore near-constant "
            "here and cannot be evaluated - this is the settled_ts gap, not a "
            "property of the wallet.")
    if p.risk.market_concentration > 0.25:
        p.notes.append(
            f"market HHI {p.risk.market_concentration:.2f}: this wallet's "
            "behaviour is concentrated, so its profile may describe a market "
            "rather than a method")
    return p


def split_families(profile: WalletProfile, observations: list,
                   min_cluster: int = 25) -> list:
    """Look for distinct behavioural regimes inside ONE wallet.

    A wallet may run several strategies. The split here is deliberately crude
    and interpretable -- price regime crossed with conviction -- rather than a
    latent clustering nobody can audit. A cluster is only reported when it is
    both large enough and genuinely different from the wallet's overall
    behaviour, because "this wallet has three strategies" is an easy thing to
    claim and a hard thing to demonstrate.
    """
    obs = [o for o in observations if o.trade.wallet == profile.wallet]
    if len(obs) < min_cluster * 2:
        return []

    p50 = profile.entry.price.get("p50", 0.5)
    buckets: dict = {}
    for o in obs:
        band = "low" if o.price < min(0.4, p50) else (
            "high" if o.price > max(0.6, p50) else "mid")
        conv = "big" if o.rel_notional >= 1.5 else "normal"
        buckets.setdefault((band, conv), []).append(o)

    overall = profile.outcome.expectancy
    out = []
    for (band, conv), members in sorted(buckets.items()):
        if len(members) < min_cluster:
            continue
        rets = [o.trade.gross_return() for o in members]
        exp = sum(rets) / len(rets)
        wins = sum(1 for r in rets if r > 0) / len(rets)
        # A family must differ from the wallet as a whole; otherwise it is a
        # slice, not a strategy.
        if abs(exp - overall) < 0.02:
            continue
        out.append({
            "family": f"{profile.wallet[:10]}::{band}_{conv}",
            "band": band, "conviction": conv, "n": len(members),
            "expectancy": round(exp, 5), "win_rate": round(wins, 4),
            "delta_vs_wallet": round(exp - overall, 5),
            "price_p50": round(_pct([o.price for o in members], 0.5), 4),
        })
    return sorted(out, key=lambda d: -abs(d["delta_vs_wallet"]))
