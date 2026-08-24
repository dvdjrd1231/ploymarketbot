"""
Dynamic wallet ranking (step 5a of the analytical pipeline).

Wallets are ranked by *measured* historical predictive performance. Nothing in
the config asserts that a wallet is good; the ordering is derived, it moves as
evidence accumulates, and it is recomputed from the full observed population
every pass.

**Shrinkage is the load-bearing idea.** A wallet with two lucky trades has a
100% win rate, and a naive ranking puts it first — permanently, because it never
trades again to be proven wrong. So each wallet's raw score is pulled toward the
population mean in proportion to how little evidence stands behind it:

    score = (raw * n + prior * K) / (n + K)

With ``K`` at 25, a wallet needs on the order of 25 scored trades before its own
record outweighs the prior. ``confidence`` reports that trade-off explicitly as
``n / (n + K)``, so an engine can tell "probably good" from "certainly good"
rather than having to infer it from the score alone.

The top-N cohort is a **label, not a filter**. Every wallet keeps its profile,
its identity and its place in the context handed to the engine; membership of
the cohort is one more feature, and the deliberate reason for that is so a
signal from outside the cohort remains discoverable.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from ..models import WalletIntel
from .features import WalletProfile

# Trades of evidence at which a wallet's own record carries equal weight with
# the population prior.
SHRINKAGE_K = 25.0

# A wallet needs at least this many scored trades to be ranked at all. Below it
# the wallet is still tracked, still profiled and still handed to the engine —
# it simply has no rank, which is honest rather than a rank of "unknown".
MIN_RANKED_SAMPLE = 5

# Scores decay toward the prior once a wallet goes quiet. Half-life in days:
# skill is persistent but circumstances are not, and a wallet last seen three
# months ago should not outrank one performing now.
STALENESS_HALF_LIFE_DAYS = 45.0


def _logistic(x: float) -> float:
    """Squash an unbounded edge into 0-1, centred so that 0 edge -> 0.5."""
    if x < -30:
        return 0.0
    if x > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def breadth_factor(markets: int, saturate_at: int = 20) -> float:
    """How much INDEPENDENT market coverage a record has, 0.35-1.0.

    Never zero: a narrow record is weak evidence, not no evidence. It
    saturates because twenty independent markets is already broad and two
    hundred is not ten times broader.
    """
    return 0.35 + 0.65 * min(1.0, max(0, markets) / float(saturate_at))


def raw_score(profile: WalletProfile) -> float:
    """A wallet's unshrunk quality, 0-1, from what it actually did.

    Three terms, because any one of them alone is gameable by an accident:
    average edge says how *right* the wallet was, win rate says how *reliably*,
    and breadth says across how much of the market — a wallet that is only ever
    right about one recurring market may be well-informed about that market
    rather than skilled at pricing.
    """
    if not profile.scores:
        return 0.5

    # Edge, scaled so a +25% average return per trade lands around 0.85.
    edge = _logistic(profile.avg_return * 7.5)

    # Reliability. A prediction market's base rate is not 50% — buying anything
    # at 0.20 wins 20% of the time if the market is efficient — so the win rate
    # is used as-is rather than recentred, and the edge term above is what
    # carries the magnitude.
    reliability = _clamp(profile.win_rate)

    # Breadth: saturating, so 20 markets is credible and 200 is not 10x more so.
    breadth = _clamp(profile.markets / 20.0)

    return _clamp(0.55 * edge + 0.30 * reliability + 0.15 * breadth)


def staleness_factor(last_seen: int, now: float) -> float:
    """1.0 for a wallet active now, decaying with a fixed half-life."""
    if not last_seen:
        return 1.0
    days = max(0.0, (now - last_seen) / 86400.0)
    return 0.5 ** (days / STALENESS_HALF_LIFE_DAYS)


def rank_wallets(profiles: dict[str, WalletProfile],
                 pinned: Optional[dict[str, str]] = None,
                 cohort_size: int = 25,
                 now: Optional[float] = None) -> dict[str, WalletIntel]:
    """Score, shrink, decay and rank every observed wallet.

    ``pinned`` maps lowercase address -> label for wallets named in the config.
    Pinning affects labelling and the evidence floor in
    :attr:`~pqb.models.WalletIntel.weight`; it does not affect the ranking,
    because an operator's interest in a wallet is not evidence about it.
    """
    now = time.time() if now is None else now
    pinned = {k.lower(): v for k, v in (pinned or {}).items()}

    # The prior is the population's own average, weighted by evidence, so it
    # reflects what a typical *informative* wallet looks like rather than being
    # dragged to 0.5 by the long tail of two-trade addresses.
    scored = [(p, raw_score(p)) for p in profiles.values() if p.scores]
    weight_total = sum(p.sample for p, _ in scored)
    prior = (sum(r * p.sample for p, r in scored) / weight_total
             if weight_total else 0.5)

    intel: dict[str, WalletIntel] = {}
    for wallet, profile in profiles.items():
        raw = raw_score(profile)
        n = profile.sample
        shrunk = (raw * n + prior * SHRINKAGE_K) / (n + SHRINKAGE_K)
        # Decay pulls toward the prior, not toward zero: going quiet is not
        # evidence of being bad, only of being less currently informative.
        decay = staleness_factor(profile.last_seen, now)
        score = prior + (shrunk - prior) * decay

        intel[wallet] = WalletIntel(
            wallet=wallet,
            label=pinned.get(wallet) or _short(wallet),
            score=round(_clamp(score), 6),
            raw_score=round(raw, 6),
            confidence=round(n / (n + SHRINKAGE_K), 6),
            sample=n,
            trades=profile.trades,
            markets=profile.markets,
            win_rate=round(profile.win_rate, 6),
            avg_return=round(profile.avg_return, 6),
            realized_usdc=round(profile.realized_usdc, 4),
            avg_usdc=round(profile.avg_usdc, 4),
            std_usdc=round(profile.std_usdc, 4),
            max_usdc=round(profile.max_usdc, 4),
            trades_per_day=round(profile.trades_per_day, 4),
            hedge_rate=round(profile.hedge_rate, 4),
            first_seen=profile.first_seen,
            last_seen=profile.last_seen,
            pinned=wallet in pinned,
        )

    # Rank only wallets with enough evidence to be ordered meaningfully. The
    # rest keep rank 0 and lose nothing else.
    #
    # ORDERING IS EVIDENCE-WEIGHTED, not by the point estimate alone (the
    # operator's audit): shrinkage alone still let a spectacular eight-trade
    # run outrank a three-hundred-trade record, because a posterior mean says
    # how good a wallet looks, not how much of that is known. Ranking on
    # score x confidence x market breadth answers the question the board is
    # actually asking — "how sure are we this wallet is good?" — so evidence
    # has to be earned across INDEPENDENT MARKETS, never from one hot streak.
    # ``score`` itself is untouched and still displayed as the estimate.
    rankable = [w for w in intel.values() if w.sample >= MIN_RANKED_SAMPLE]
    for wallet_intel in rankable:
        wallet_intel.rank_score = round(
            wallet_intel.score * wallet_intel.confidence
            * breadth_factor(wallet_intel.markets), 6)
    rankable.sort(key=lambda w: (w.rank_score, w.sample), reverse=True)
    for position, wallet_intel in enumerate(rankable, start=1):
        wallet_intel.rank = position
        wallet_intel.in_cohort = position <= max(0, cohort_size)
    return intel


def cohort(intel: dict[str, WalletIntel]) -> set[str]:
    return {w.wallet for w in intel.values() if w.in_cohort}


def _short(wallet: str) -> str:
    # ASCII only: these labels are printed to the console, and a Windows code
    # page renders a unicode ellipsis as a replacement character.
    return f"{wallet[:6]}..{wallet[-4:]}" if len(wallet) > 12 else wallet
