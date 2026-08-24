"""
Anomaly detection (step 5b of the analytical pipeline).

Six detectors, one per pattern the brief names:

===================  =========================================================
``size_spike``       a wallet suddenly betting far outside its own usual size
``lead_lag``         a wallet that repeatedly enters before better-ranked ones
``convergence``      several wallets independently converging on one outcome
``behaviour_drift``  a wallet behaving unlike its own history
``market_flow``      a market taking unusual capital against its own baseline
``combo_edge``       a wallet+category+timing combination with a proven record
===================  =========================================================

Two rules shape all of them.

**Unusual is always relative to the right baseline.** A $5,000 trade is enormous
for one wallet and routine for another; $50k of flow is a flood in one market
and a quiet hour in another. Every detector therefore compares a subject against
*its own* history, never against a global constant. A single global threshold
would only ever fire on the largest wallets and the busiest markets, which is
exactly where the least information is.

**A detection is evidence, not an instruction.** Each signal carries the numbers
behind it — the z-score, the sample it was measured against, the wallets
involved — and every one is persisted. The engine decides what, if anything, an
anomaly is worth; the discovery layer decides which kinds have measurable
predictive value at all. Nothing here can cause an order.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Optional

from ..models import AnomalySignal, MarketIntel, WalletIntel, ttr_bucket
from .features import WalletProfile, _mean, _median, _stdev

KINDS = ("size_spike", "lead_lag", "convergence", "behaviour_drift",
         "market_flow", "combo_edge")


@dataclass
class AnomalyConfig:
    """Every threshold, in one place and all config-driven.

    Defaults are deliberately conservative: a detector that fires constantly
    tells the engine nothing, and the journal is what proves whether a kind is
    worth its threshold.
    """

    enabled: bool = True
    recent_seconds: int = 3_600          # what counts as "now"
    lookback_days: float = 30.0          # what counts as "history"

    size_z: float = 3.0                  # sigmas above a wallet's own median
    size_min_usdc: float = 25.0          # ignore dust spikes on tiny wallets

    lead_min_events: int = 3             # times it led before it counts
    lead_window_seconds: int = 86_400    # how long a "lead" may be
    lead_min_seconds: int = 60           # below this it is the same event

    convergence_min_wallets: int = 4
    convergence_window_seconds: int = 1_800
    convergence_min_usdc: float = 250.0

    drift_z: float = 2.5
    drift_min_history: int = 20          # trades needed to have a "normal"

    flow_z: float = 3.0
    flow_min_usdc: float = 1_000.0

    combo_min_samples: int = 8
    combo_min_return: float = 0.08       # mean edge worth calling an edge
    combo_min_t: float = 2.0             # and it must clear the noise

    max_per_cycle: int = 60              # bound the per-cycle emission


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _strength(z: float, threshold: float) -> float:
    """Map a z-score onto 0-1 so strengths are comparable across detectors.

    Saturating rather than linear: past a point, "more standard deviations" is
    not more actionable, and a linear scale would let one extreme outlier
    dominate any downstream blend.
    """
    if threshold <= 0:
        return 0.0
    excess = max(0.0, z - threshold)
    return _clamp(1.0 - math.exp(-excess / max(threshold, 1e-6)))


# --------------------------------------------------------------------------
# 1. size_spike — "a normally mediocre wallet suddenly making unusually large
#    bets". The wallet's rank and score travel in the detail rather than being
#    used to filter, so "mediocre wallet, huge bet" and "top wallet, huge bet"
#    are both emitted and the analytical layer decides which predicts anything.
# --------------------------------------------------------------------------

def detect_size_spikes(recent: list[dict], profiles: dict[str, WalletProfile],
                       intel: dict[str, WalletIntel],
                       cfg: AnomalyConfig) -> list[AnomalySignal]:
    out: list[AnomalySignal] = []
    for row in recent:
        wallet = str(row.get("wallet") or "").lower()
        usdc = float(row.get("usdc") or 0.0)
        if usdc < cfg.size_min_usdc:
            continue
        profile = profiles.get(wallet)
        if profile is None or profile.trades < 3:
            continue
        z = profile.size_z(usdc)
        if z < cfg.size_z:
            continue
        who = intel.get(wallet)
        out.append(AnomalySignal(
            kind="size_spike", subject=wallet, wallet=wallet,
            label=who.label if who else wallet[:10],
            market_id=str(row.get("market_id") or ""),
            token_id=str(row.get("token_id") or ""),
            outcome=str(row.get("outcome") or ""),
            z=round(z, 3), strength=_strength(z, cfg.size_z),
            ts=float(row.get("ts") or time.time()),
            detail={
                "usdc": round(usdc, 2),
                "walletMedianUsdc": round(profile.median_usdc, 2),
                "walletStdUsdc": round(profile.std_usdc, 2),
                "walletTrades": profile.trades,
                "walletRank": who.rank if who else 0,
                "walletScore": round(who.score, 4) if who else None,
                "inCohort": bool(who.in_cohort) if who else False,
                "side": str(row.get("side") or ""),
                "price": float(row.get("price") or 0.0),
            },
        ))
    return out


# --------------------------------------------------------------------------
# 2. lead_lag — "a lower-ranked wallet repeatedly entering before highly ranked
#    wallets". Repetition is the whole signal: one wallet happening to be early
#    once is noise, and only a sustained pattern distinguishes an informed
#    early mover from a lucky one.
# --------------------------------------------------------------------------

def detect_lead_lag(history: list[dict], intel: dict[str, WalletIntel],
                    cfg: AnomalyConfig) -> list[AnomalySignal]:
    # token -> chronological buys, so a "lead" is a strict ordering fact rather
    # than an inference from aggregate timing.
    by_token: dict[str, list[tuple[int, str]]] = {}
    for row in history:
        if str(row.get("side") or "").upper() != "BUY":
            continue
        token = str(row.get("token_id") or "")
        wallet = str(row.get("wallet") or "").lower()
        if token and wallet:
            by_token.setdefault(token, []).append(
                (int(row.get("ts") or 0), wallet))

    leads: dict[str, list[dict]] = {}
    for token, events in by_token.items():
        events.sort()
        seen_first: dict[str, int] = {}
        for ts, wallet in events:
            seen_first.setdefault(wallet, ts)
        # Compare each wallet's first entry with every wallet that arrived
        # later. Only first entries count — a wallet adding to a position it
        # already holds is not leading anyone into it.
        for leader, lead_ts in seen_first.items():
            leader_intel = intel.get(leader)
            for follower, follow_ts in seen_first.items():
                if follower == leader:
                    continue
                gap = follow_ts - lead_ts
                if gap < cfg.lead_min_seconds or gap > cfg.lead_window_seconds:
                    continue
                follower_intel = intel.get(follower)
                if follower_intel is None or not follower_intel.in_cohort:
                    continue
                # Only count it as a lead if the follower is genuinely better
                # ranked; a top wallet being followed by a worse one is the
                # ordinary case, not an anomaly.
                if leader_intel is not None and leader_intel.rank:
                    if leader_intel.rank <= follower_intel.rank:
                        continue
                leads.setdefault(leader, []).append({
                    "token": token, "follower": follower,
                    "followerRank": follower_intel.rank, "gapSeconds": gap,
                })

    out: list[AnomalySignal] = []
    for wallet, events in leads.items():
        if len(events) < cfg.lead_min_events:
            continue
        gaps = [e["gapSeconds"] for e in events]
        who = intel.get(wallet)
        # Strength grows with how often it happened, not how early — a huge
        # median gap usually means the two wallets were reacting to different
        # things days apart, which is weaker evidence, not stronger.
        strength = _clamp(len(events) / (cfg.lead_min_events * 4.0))
        out.append(AnomalySignal(
            kind="lead_lag", subject=wallet, wallet=wallet,
            label=who.label if who else wallet[:10],
            z=round(len(events) / max(1.0, cfg.lead_min_events), 3),
            strength=strength,
            detail={
                "leadEvents": len(events),
                "distinctFollowers": len({e["follower"] for e in events}),
                "distinctTokens": len({e["token"] for e in events}),
                "medianGapSeconds": int(_median([float(g) for g in gaps])),
                "walletRank": who.rank if who else 0,
                "inCohort": bool(who.in_cohort) if who else False,
                "examples": events[:5],
            },
        ))
    return out


# --------------------------------------------------------------------------
# 3. convergence — "several wallets independently moving toward the same
#    outcome unusually quickly". Distinct wallets is the unit, not trades: one
#    wallet buying twenty times is conviction, twenty wallets buying once each
#    is consensus, and only the second is what this detector is for.
# --------------------------------------------------------------------------

def detect_convergence(recent: list[dict], intel: dict[str, WalletIntel],
                       cfg: AnomalyConfig) -> list[AnomalySignal]:
    by_token: dict[str, list[dict]] = {}
    for row in recent:
        if str(row.get("side") or "").upper() != "BUY":
            continue
        token = str(row.get("token_id") or "")
        if token:
            by_token.setdefault(token, []).append(row)

    out: list[AnomalySignal] = []
    for token, rows in by_token.items():
        rows.sort(key=lambda r: int(r.get("ts") or 0))
        wallets = {str(r.get("wallet") or "").lower() for r in rows}
        wallets.discard("")
        if len(wallets) < cfg.convergence_min_wallets:
            continue
        gross = sum(float(r.get("usdc") or 0.0) for r in rows)
        if gross < cfg.convergence_min_usdc:
            continue
        span = int(rows[-1].get("ts") or 0) - int(rows[0].get("ts") or 0)
        if span > cfg.convergence_window_seconds:
            continue

        members = [intel[w] for w in wallets if w in intel]
        cohort_members = [w for w in members if w.in_cohort]
        # More wallets, tighter window, and more of them well-ranked all make
        # the cluster more meaningful; each is capped so no single term can
        # carry the score on its own.
        crowd = _clamp(len(wallets) / (cfg.convergence_min_wallets * 3.0))
        speed = _clamp(1.0 - span / max(1.0, cfg.convergence_window_seconds))
        quality = _clamp(len(cohort_members) / 3.0)
        out.append(AnomalySignal(
            kind="convergence", subject=token, token_id=token,
            market_id=str(rows[0].get("market_id") or ""),
            outcome=str(rows[0].get("outcome") or ""),
            z=round(len(wallets) / max(1.0, cfg.convergence_min_wallets), 3),
            strength=_clamp(0.5 * crowd + 0.25 * speed + 0.25 * quality),
            detail={
                "wallets": len(wallets),
                "cohortWallets": len(cohort_members),
                "spanSeconds": span,
                "grossUsdc": round(gross, 2),
                "avgPrice": round(_mean([float(r.get("price") or 0.0)
                                         for r in rows]), 4),
                "participants": [w.label for w in members[:8]],
            },
        ))
    return out


# --------------------------------------------------------------------------
# 4. behaviour_drift — "a wallet's current behaviour deviating significantly
#    from its historical behaviour". Three independent axes, because a wallet
#    can change what it risks, how often it acts, or where it looks — and any
#    of the three is a change of behaviour.
# --------------------------------------------------------------------------

def detect_behaviour_drift(recent: list[dict],
                           profiles: dict[str, WalletProfile],
                           intel: dict[str, WalletIntel],
                           cfg: AnomalyConfig,
                           now: Optional[float] = None) -> list[AnomalySignal]:
    now = time.time() if now is None else now
    by_wallet: dict[str, list[dict]] = {}
    for row in recent:
        wallet = str(row.get("wallet") or "").lower()
        if wallet:
            by_wallet.setdefault(wallet, []).append(row)

    out: list[AnomalySignal] = []
    for wallet, rows in by_wallet.items():
        profile = profiles.get(wallet)
        if profile is None or profile.trades < cfg.drift_min_history:
            continue

        # -- axis 1: size --------------------------------------------------
        sizes = [float(r.get("usdc") or 0.0) for r in rows]
        size_z = 0.0
        if profile.std_usdc > 0:
            size_z = abs(_mean(sizes) - profile.median_usdc) / profile.std_usdc

        # -- axis 2: cadence -----------------------------------------------
        window_days = max(cfg.recent_seconds, 1) / 86400.0
        current_rate = len(rows) / window_days
        baseline_rate = max(profile.trades_per_day, 1e-6)
        rate_ratio = current_rate / baseline_rate
        # Log-scaled and symmetric: going quiet is as much a change as a burst.
        rate_z = abs(math.log(max(rate_ratio, 1e-6))) / math.log(3.0)

        # -- axis 3: where it is looking -----------------------------------
        historical_markets = {s.market_id for s in profile.scores}
        current_markets = {str(r.get("market_id") or "") for r in rows} - {""}
        novelty = (len(current_markets - historical_markets)
                   / max(1, len(current_markets)))

        z = max(size_z, rate_z, novelty * cfg.drift_z)
        if z < cfg.drift_z:
            continue
        who = intel.get(wallet)
        out.append(AnomalySignal(
            kind="behaviour_drift", subject=wallet, wallet=wallet,
            label=who.label if who else wallet[:10],
            market_id=str(rows[-1].get("market_id") or ""),
            token_id=str(rows[-1].get("token_id") or ""),
            z=round(z, 3), strength=_strength(z, cfg.drift_z),
            detail={
                "sizeZ": round(size_z, 3),
                "rateZ": round(rate_z, 3),
                "marketNovelty": round(novelty, 3),
                "recentTrades": len(rows),
                "recentAvgUsdc": round(_mean(sizes), 2),
                "baselineMedianUsdc": round(profile.median_usdc, 2),
                "baselineTradesPerDay": round(profile.trades_per_day, 3),
                "currentTradesPerDay": round(current_rate, 3),
                "walletRank": who.rank if who else 0,
            },
        ))
    return out


# --------------------------------------------------------------------------
# 5. market_flow — "a market receiving unusual capital/volume relative to its
#    normal baseline". The z-score is computed in features.market_intel against
#    the market's own hourly history; this turns it into a signal.
# --------------------------------------------------------------------------

def detect_market_flow(markets: dict[str, MarketIntel],
                       cfg: AnomalyConfig) -> list[AnomalySignal]:
    out: list[AnomalySignal] = []
    for market_id, market in markets.items():
        if market.gross_usdc < cfg.flow_min_usdc:
            continue
        if market.flow_z < cfg.flow_z:
            continue
        out.append(AnomalySignal(
            kind="market_flow", subject=market_id, market_id=market_id,
            z=round(market.flow_z, 3),
            strength=_strength(market.flow_z, cfg.flow_z),
            detail={
                "grossUsdc": round(market.gross_usdc, 2),
                "netUsdc": round(market.net_usdc, 2),
                "baselineUsdc": round(market.baseline_usdc, 2),
                "walletsActive": market.wallets_active,
                "cohortWallets": market.cohort_wallets,
                "cohortNetUsdc": round(market.cohort_net_usdc, 2),
                "trades": market.trades,
            },
        ))
    return out


# --------------------------------------------------------------------------
# 6. combo_edge — "a particular combination of wallet + market + timing having
#    historically strong outcomes". Built from settled history, then matched
#    against what is happening now, so the signal names a live token rather
#    than merely reporting a statistic.
# --------------------------------------------------------------------------

@dataclass
class Combo:
    wallet: str
    category: str
    timing: str
    returns: list[float] = field(default_factory=list)
    usdc: float = 0.0

    @property
    def key(self) -> str:
        return f"{self.wallet}|{self.category}|{self.timing}"

    @property
    def mean(self) -> float:
        return _mean(self.returns)

    @property
    def t_stat(self) -> float:
        """Mean over standard error — does this edge clear its own noise?

        Without it, eight trades averaging +9% look identical whether they were
        all +9% or ranged from -80% to +100%, and only one of those is an edge.
        """
        n = len(self.returns)
        if n < 2:
            return 0.0
        sd = _stdev(self.returns)
        if sd <= 0:
            # Zero dispersion is the *most* consistent an edge can be, not the
            # least — returning 0 here would score a combination that has won
            # by the same margin every single time as indistinguishable from
            # noise. There is no standard error to divide by, so significance
            # is taken from the sample size, which is what it rests on.
            return float(n) if self.mean > 0 else 0.0
        return self.mean / (sd / math.sqrt(n))


def build_combos(profiles: dict[str, WalletProfile],
                 categories: dict[str, str],
                 end_times: dict[str, int]) -> dict[str, Combo]:
    """Aggregate settled trade outcomes by wallet x category x timing bucket."""
    combos: dict[str, Combo] = {}
    for wallet, profile in profiles.items():
        for score in profile.scores:
            if not score.resolved:
                # Only settled outcomes build the record. A mark-to-market
                # estimate would let an unrealised paper gain create a "proven"
                # combination that has never actually paid out.
                continue
            category = categories.get(score.market_id, "") or "uncategorised"
            end_ts = end_times.get(score.market_id)
            timing = ttr_bucket(end_ts - score.ts if end_ts else None)
            combo = combos.setdefault(
                f"{wallet}|{category}|{timing}",
                Combo(wallet=wallet, category=category, timing=timing))
            combo.returns.append(score.ret)
            combo.usdc += score.usdc
    return combos


def detect_combo_edge(recent: list[dict], combos: dict[str, Combo],
                      categories: dict[str, str], end_times: dict[str, int],
                      intel: dict[str, WalletIntel],
                      cfg: AnomalyConfig) -> list[AnomalySignal]:
    strong = {
        key: combo for key, combo in combos.items()
        if len(combo.returns) >= cfg.combo_min_samples
        and combo.mean >= cfg.combo_min_return
        and combo.t_stat >= cfg.combo_min_t
    }
    if not strong:
        return []

    out: list[AnomalySignal] = []
    seen: set[str] = set()
    for row in recent:
        if str(row.get("side") or "").upper() != "BUY":
            continue
        wallet = str(row.get("wallet") or "").lower()
        market_id = str(row.get("market_id") or "")
        category = categories.get(market_id, "") or "uncategorised"
        end_ts = end_times.get(market_id)
        ts = int(row.get("ts") or 0)
        timing = ttr_bucket(end_ts - ts if end_ts else None)
        combo = strong.get(f"{wallet}|{category}|{timing}")
        if combo is None:
            continue
        token = str(row.get("token_id") or "")
        dedupe = f"{wallet}|{token}"
        if dedupe in seen:
            continue
        seen.add(dedupe)
        who = intel.get(wallet)
        out.append(AnomalySignal(
            kind="combo_edge", subject=combo.key, wallet=wallet,
            label=who.label if who else wallet[:10],
            market_id=market_id, token_id=token,
            outcome=str(row.get("outcome") or ""),
            z=round(combo.t_stat, 3),
            strength=_clamp(0.4 + 0.6 * _clamp(combo.t_stat / 6.0)),
            ts=float(ts or time.time()),
            detail={
                "combo": combo.key, "category": category, "timing": timing,
                "historicalTrades": len(combo.returns),
                "historicalMeanReturn": round(combo.mean, 4),
                "tStat": round(combo.t_stat, 3),
                "historicalUsdc": round(combo.usdc, 2),
                "walletRank": who.rank if who else 0,
                "usdc": round(float(row.get("usdc") or 0.0), 2),
            },
        ))
    return out


# --------------------------------------------------------------------------
# the pass
# --------------------------------------------------------------------------

def detect_all(recent: list[dict], history: list[dict],
               profiles: dict[str, WalletProfile],
               intel: dict[str, WalletIntel],
               markets: dict[str, MarketIntel],
               categories: dict[str, str], end_times: dict[str, int],
               cfg: AnomalyConfig,
               now: Optional[float] = None) -> list[AnomalySignal]:
    """Run every detector and return the strongest signals for this cycle."""
    if not cfg.enabled:
        return []
    combos = build_combos(profiles, categories, end_times)
    found: list[AnomalySignal] = []
    found += detect_size_spikes(recent, profiles, intel, cfg)
    found += detect_lead_lag(history, intel, cfg)
    found += detect_convergence(recent, intel, cfg)
    found += detect_behaviour_drift(recent, profiles, intel, cfg, now=now)
    found += detect_market_flow(markets, cfg)
    found += detect_combo_edge(recent, combos, categories, end_times, intel, cfg)

    # Strongest first, then bounded. A cycle that trips 400 detections is a
    # threshold problem, and passing all 400 to the engine would bury the few
    # that matter rather than surfacing them.
    found.sort(key=lambda a: a.strength, reverse=True)
    return found[:max(1, cfg.max_per_cycle)]
