"""
Wallet behavioral states: RN1's silence, measured without hindsight.

The operator's discovery, treated as a hypothesis about ONE wallet at a
time: a tracked wallet's BUY is not always a prediction. The candidate
directional state is a relatively HIGH first entry (the reported band:
60-79c) followed by sustained one-sided commitment; low entries, rapid
adds, and two-sided activity look like inventory management. The reported
numbers (V2 70% over 30, Candidate A 69.92% over 359) are HIS forward
results — everything here re-derives the effect from OUR settled tapes.

The two rules this module enforces above all others, from the spec:

* **No future information, ever.** Qualification uses only trades with a
  timestamp inside the checkpoint window (minute 3, minute 30). "The
  wallet never eventually switched sides" is a LABEL for studying the
  behavioral model — it is recorded, and it is never a feature.
* **The state must beat blind copying.** Every candidate is compared
  against the same wallet's ALL-first-buys baseline; a band or a
  persistence rule that doesn't improve on just following the wallet adds
  selection, not information.

Episodes are one per (wallet, market) — the market's two outcome tokens
are one bet, so same-side and opposite-side activity are read across both
tokens of the market, and independence is counted in MARKETS. Candidates
walk the standard library/frozen-OOS ladder (family "wallet-behavior")
and cannot vote in the live engine until an execution path is
deliberately built. The reported execution controls ($5 sizing, $15/$100
caps) ride in the rule for that future step; nothing trades now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Checkpoint grid (minutes) the study freezes features at — the operator's
# 3-minute quiet test and 30-minute persistence test, plus neighbors so a
# discovered region is a REGION, not one number.
QUIET_MINUTES = (3,)
PERSIST_MINUTES = (0, 10, 30)          # 0 = Candidate A (quiet test only)
PRICE_BANDS = ((0.55, 0.75), (0.60, 0.80), (0.65, 0.85))


@dataclass
class Episode:
    """One wallet's engagement with one market, from its first BUY."""
    wallet: str
    market: str
    token: str                  # the token of the FIRST buy (original side)
    first_ts: float
    first_price: float
    same_buys: list = field(default_factory=list)      # [(ts, usd)] incl. first
    opposite_buys: list = field(default_factory=list)  # [(ts, usd)]
    payout: float = 0.0         # settlement of the ORIGINAL side (label)

    def qualifies(self, price_lo: float, price_hi: float,
                  quiet_min: float, persist_min: float) -> bool:
        """Frozen-checkpoint qualification. Only trades whose timestamps
        fall INSIDE the windows are consulted — later activity must never
        retroactively change the answer."""
        if not price_lo <= self.first_price < price_hi:
            return False
        quiet_end = self.first_ts + quiet_min * 60.0
        same_in_quiet = sum(1 for ts, _ in self.same_buys
                            if self.first_ts <= ts <= quiet_end)
        opp_in_quiet = sum(1 for ts, _ in self.opposite_buys
                           if ts <= quiet_end)
        # Exactly the first buy, nothing else, no opposite: the quiet test.
        if same_in_quiet != 1 or opp_in_quiet != 0:
            return False
        if persist_min <= quiet_min:
            return True                       # Candidate A: quiet test only
        persist_end = self.first_ts + persist_min * 60.0
        # Between the checkpoints, ADDS to the original side are allowed;
        # ANY opposite-side buy through the persistence window rejects.
        return not any(ts <= persist_end for ts, _ in self.opposite_buys)

    def eventually_two_sided(self) -> bool:
        """LABEL ONLY — knowable only after the fact, never a feature."""
        return bool(self.opposite_buys)


def episodes_from_market(trades: list[dict], token_payouts: dict[str, float]
                         ) -> list[Episode]:
    """Per-wallet episodes for ONE market's tape (both outcome tokens).

    ``trades`` rows need wallet, token_id, ts, price, usdc, side. Only BUYs
    participate — the hypothesis is about buying behavior.
    """
    by_wallet: dict[str, list[dict]] = {}
    for row in sorted(trades, key=lambda r: float(r.get("ts") or 0.0)):
        if str(row.get("side") or "BUY").upper().startswith("S"):
            continue
        wallet = str(row.get("wallet") or "").lower()
        if wallet:
            by_wallet.setdefault(wallet, []).append(row)
    out: list[Episode] = []
    for wallet, rows in by_wallet.items():
        first = rows[0]
        token = str(first.get("token_id") or "")
        if token not in token_payouts:
            continue
        episode = Episode(
            wallet=wallet, market=str(first.get("market_id") or ""),
            token=token, first_ts=float(first.get("ts") or 0.0),
            first_price=float(first.get("price") or 0.0),
            payout=float(token_payouts.get(token, 0.0)))
        for row in rows:
            entry = (float(row.get("ts") or 0.0),
                     float(row.get("usdc") or 0.0))
            if str(row.get("token_id") or "") == token:
                episode.same_buys.append(entry)
            else:
                episode.opposite_buys.append(entry)
        out.append(episode)
    return out


def study(store, wallets: list[str], *, cost: float = 0.03,
          premium: float = 0.03, min_markets: int = 12,
          top_n: int = 2) -> dict:
    """Re-derive the behavioral-state effect for each target wallet."""
    resolutions = store.resolutions()
    payouts = {t: (1.0 if float(p) >= 0.99 else 0.0)
               for t, p in resolutions.items()}
    targets = {w.lower() for w in wallets if w}
    funnel: dict[str, Any] = {"wallets": sorted(targets),
                              "episodes": 0, "settledEpisodes": 0}
    reject: dict[str, int] = {}
    if not targets:
        funnel["kept"] = 0
        funnel["rejectReasons"] = {"no target wallets configured": 1}
        return {"candidates": [], "funnel": funnel, "perWallet": {}}

    placeholders = ",".join("?" for _ in targets)
    rows = store.query(
        "SELECT wallet, market_id, token_id, ts, price, usdc, side "
        "FROM wallet_trades WHERE lower(wallet) IN (" + placeholders + ") "
        "ORDER BY ts", tuple(sorted(targets)))
    markets_of: dict[str, list[dict]] = {}
    for row in rows:
        markets_of.setdefault(str(row["market_id"]), []).append(row)

    episodes: list[Episode] = []
    for market_id, market_rows in markets_of.items():
        episodes.extend(episodes_from_market(market_rows, payouts))
    funnel["episodes"] = len(episodes)
    funnel["settledEpisodes"] = len(episodes)   # only settled tokens survive

    per_wallet: dict[str, dict] = {}
    candidates: list[dict] = []
    for wallet in sorted(targets):
        mine = [e for e in episodes if e.wallet == wallet]
        if not mine:
            per_wallet[wallet] = {"episodes": 0}
            continue
        # The control every candidate must beat: blindly copy EVERY first
        # buy of this wallet, at the same premium and costs.
        baseline_net = sum(e.payout - min(0.99, e.first_price + premium)
                           - cost for e in mine) / len(mine)
        report: dict[str, Any] = {
            "episodes": len(mine),
            "baselineWinRate": round(sum(1 for e in mine
                                         if e.payout >= 1.0) / len(mine), 4),
            "baselineNet": round(baseline_net, 4),
            "eventuallyTwoSided": sum(1 for e in mine
                                      if e.eventually_two_sided()),
            "cells": {},
        }
        for price_lo, price_hi in PRICE_BANDS:
            for quiet in QUIET_MINUTES:
                for persist in PERSIST_MINUTES:
                    qualified = [e for e in mine if e.qualifies(
                        price_lo, price_hi, quiet, persist)]
                    label = (f"{price_lo:.2f}-{price_hi:.2f}"
                             f"/q{quiet}m/p{persist}m")
                    if not qualified:
                        continue
                    wins = sum(1 for e in qualified if e.payout >= 1.0)
                    net = sum(e.payout
                              - min(0.99, e.first_price + premium) - cost
                              for e in qualified) / len(qualified)
                    cell = {"markets": len(qualified), "wins": wins,
                            "winRate": round(wins / len(qualified), 4),
                            "net": round(net, 4)}
                    report["cells"][label] = cell
                    if len(qualified) < min_markets:
                        reject["insufficient sample (markets)"] = \
                            reject.get("insufficient sample (markets)", 0) + 1
                        continue
                    if net <= 0:
                        reject["cannot clear costs"] = \
                            reject.get("cannot clear costs", 0) + 1
                        continue
                    if net <= baseline_net:
                        reject["no better than blindly copying the wallet"] \
                            = reject.get(
                                "no better than blindly copying the wallet",
                                0) + 1
                        continue
                    candidates.append({
                        "type": "wallet_state", "wallet": wallet,
                        "price_lo": price_lo, "price_hi": price_hi,
                        "quiet_minutes": quiet,
                        "persist_minutes": persist,
                        "max_premium": premium, "side": "follow",
                        "direction": "up",
                        "markets": len(qualified),
                        "netExpectancy": round(net, 6),
                        "baselineNet": round(baseline_net, 6),
                        "winRate": round(wins / len(qualified), 4),
                    })
        per_wallet[wallet] = report

    candidates.sort(key=lambda c: -c["netExpectancy"])
    candidates = candidates[:top_n]
    funnel.update({"kept": len(candidates),
                   "rejectReasons": dict(sorted(reject.items(),
                                                key=lambda kv: -kv[1]))})
    return {"candidates": candidates, "funnel": funnel,
            "perWallet": per_wallet}


def frozen_replay(market_trades: list[dict], rule: dict,
                  token_payouts: dict[str, float], cost: float) -> dict:
    """One FROZEN wallet-state rule against one unseen market's tape.

    The target wallet's episode is reconstructed from the tape, qualified
    with checkpoint-frozen information exactly as discovered, and scored
    as one observation for this market."""
    wallet = str(rule.get("wallet") or "").lower()
    episodes = [e for e in episodes_from_market(market_trades, token_payouts)
                if e.wallet == wallet]
    if not episodes:
        return {"trades": 0}
    episode = episodes[0]
    if not episode.qualifies(float(rule.get("price_lo") or 0.0),
                             float(rule.get("price_hi") or 1.0),
                             float(rule.get("quiet_minutes") or 3),
                             float(rule.get("persist_minutes") or 0)):
        return {"trades": 0}
    entry = min(0.99, episode.first_price
                + float(rule.get("max_premium") or 0.0))
    pnl = episode.payout - entry - cost
    return {"trades": 1, "wins": 1 if pnl > 0 else 0, "pnl": pnl,
            "expectancy": pnl, "drawdown": max(0.0, -pnl), "sharpe": 0.0}


def describe(rule: dict) -> str:
    persist = float(rule.get("persist_minutes") or 0)
    mode = (f"one-sided through {persist:.0f}m" if persist
            else f"quiet at {float(rule.get('quiet_minutes') or 3):.0f}m")
    return (f"WALLET {str(rule.get('wallet', ''))[:10]}…: follow first BUY "
            f"{float(rule.get('price_lo', 0)):.0%}-"
            f"{float(rule.get('price_hi', 0)):.0%}, {mode}, "
            "hold to resolution")
