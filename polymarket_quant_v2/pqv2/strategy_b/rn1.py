"""RN1: the reference wallet, and the honest handling of where it came from.

RN1 is the INITIAL reference, not an unquestioned truth (rule 28). Two ways it
can be set, and they have very different evidential standing:

  1. NOMINATED BY THE OPERATOR (`--wallet 0x...`, or PQV2_RN1).
     This is external information. It costs no statistical power, because the
     choice was not made by looking at this dataset.

  2. NOMINATED FROM DATA (`identify_candidates`).
     This is a SELECTION, and selection is a hypothesis test. Picking the most
     profitable wallet from 28,034 and then reporting how profitable it is, is
     the oldest error in quantitative finance. So:

       * selection runs on the IN-SAMPLE window only, and
       * every downstream number is measured on the OOS window, and
       * the selection itself is counted in the multiple-testing budget
         (`selection_penalty`), and
       * the fact of selection is recorded in the research log.

     A wallet chosen this way carries `provenance="data_selected"` and the
     ladder is told, so nobody reads a data-selected wallet's result as though
     someone had named it in advance.

If RN1 turns out to be unremarkable once controlled, that is a finding and the
engine reports it. The goal was never to copy RN1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..config import Settings
from ..substrate import data as sdata
from ..substrate.state import collect
from .decompose import WalletProfile, build_profile, split_families


@dataclass
class Reference:
    """A reference wallet and the provenance of the choice."""

    wallet: str
    provenance: str                  # operator_nominated | data_selected
    in_sample_stats: dict = field(default_factory=dict)
    selection_pool: int = 0
    note: str = ""

    @property
    def costs_power(self) -> bool:
        return self.provenance == "data_selected"

    def selection_penalty(self) -> int:
        """How many implicit hypotheses the selection consumed.

        Handed to the BH budget so a data-selected reference is held to a
        stricter threshold than a nominated one. This is the difference between
        controlling for selection and merely mentioning it.
        """
        return self.selection_pool if self.costs_power else 0

    def to_dict(self) -> dict:
        return {"wallet": self.wallet, "provenance": self.provenance,
                "selection_pool": self.selection_pool, "note": self.note,
                "in_sample_stats": self.in_sample_stats}


def from_operator(wallet: str) -> Reference:
    return Reference(wallet=wallet, provenance="operator_nominated",
                     note="named externally; costs no statistical power")


def identify_candidates(st: Settings, *, top: int = 10,
                        min_trades: int | None = None) -> list:
    """Rank wallets on the IN-SAMPLE window only.

    Ranked by a robustness-weighted score, never by total profit. Total profit
    ranks whales and lottery winners; what a copier needs is a wallet whose
    per-trade edge is large relative to its own noise and spread across many
    markets.
    """
    cfg = st.strategy_b
    min_trades = min_trades or cfg.min_wallet_trades
    split = sdata.oos_split_ts(st)

    counts = dict(sdata.wallet_trade_counts(st, min_trades=min_trades))
    if not counts:
        return []

    # One forward pass over the in-sample window, accumulating per wallet.
    acc: dict = {}
    for tr in sdata.iter_settled(st, ts_to=split):
        if tr.wallet not in counts:
            continue
        a = acc.setdefault(tr.wallet, {"rets": [], "markets": set(),
                                       "stake": 0.0, "pnl": 0.0})
        r = tr.gross_return()
        a["rets"].append(r)
        a["markets"].add(tr.market_id or tr.token_id)
        a["stake"] += tr.usdc
        a["pnl"] += tr.usdc * r

    import math
    out = []
    for wallet, a in acc.items():
        rets = a["rets"]
        n = len(rets)
        if n < min_trades // 2:
            continue
        mean = sum(rets) / n
        var = sum((r - mean) ** 2 for r in rets) / (n - 1) if n > 1 else 0.0
        sd = math.sqrt(max(var, 0.0))
        t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
        breadth = min(1.0, len(a["markets"]) / 40.0)
        # Score: edge distinguishable from noise, spread across markets, with
        # enough evidence to be worth studying. Profit is reported, not ranked.
        score = t * (0.5 + 0.5 * breadth) * min(1.0, math.log1p(n) / math.log1p(300))
        out.append({
            "wallet": wallet, "n_in_sample": n, "markets": len(a["markets"]),
            "expectancy": round(mean, 5), "t_stat": round(t, 3),
            "roi": round(a["pnl"] / a["stake"], 5) if a["stake"] > 0 else 0.0,
            "pnl": round(a["pnl"], 2), "score": round(score, 4),
        })
    out.sort(key=lambda d: -d["score"])
    return out[:top]


def select_reference(st: Settings, wallet: str | None = None) -> Reference:
    """Resolve the reference wallet, recording how the choice was made."""
    wallet = wallet or os.environ.get("PQV2_RN1", "").strip()
    if wallet:
        return from_operator(wallet)

    pool = identify_candidates(st, top=10)
    if not pool:
        raise RuntimeError(
            "no wallet carries enough in-sample evidence to serve as a "
            "reference. Lower strategy_b.min_wallet_trades or backfill more "
            "resolutions.")
    best = pool[0]
    total_pool = len(sdata.wallet_trade_counts(st, st.strategy_b.min_wallet_trades))
    return Reference(
        wallet=best["wallet"], provenance="data_selected",
        in_sample_stats=best, selection_pool=total_pool,
        note=(f"selected from {total_pool} eligible wallets on the in-sample "
              "window only. This selection is counted in the multiple-testing "
              "budget; every number reported for this wallet is out-of-sample."))


@dataclass
class Reconstruction:
    """The full RN1 answer: profile, families, and what could not be answered."""

    reference: Reference
    profile: WalletProfile
    families: list = field(default_factory=list)
    limits: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"reference": self.reference.to_dict(),
                "profile": self.profile.to_dict(),
                "families": self.families, "limits": self.limits}


def reconstruct(st: Settings, reference: Reference,
                *, in_sample_only: bool = True) -> Reconstruction:
    """Reconstruct the reference wallet's behaviour.

    `in_sample_only` defaults True and should stay True for anything that will
    later be validated: a profile built over the OOS window and then tested on
    the OOS window is not a test.
    """
    split = sdata.oos_split_ts(st) if in_sample_only else 0
    obs = collect(st, wallets=[reference.wallet], ts_to=split)
    profile = build_profile(reference.wallet, obs)
    families = split_families(profile, obs)

    limits = [
        "Exit behaviour is reconstructed from BUY-side data only. 149,080 SELL "
        "rows and 132,082 REDEEM rows exist in the tape but position-state "
        "reconstruction across MERGE/SPLIT/CONVERSION is not built, so "
        "'holds to settlement' is an upper bound, not a measurement.",
        "resolutions.settled_ts is 0 in all 8,116 rows, so the settlement "
        "clock falls back to observation time. Point-in-time wallet track "
        "record is therefore blunt over most of the tape - safe (it can only "
        "delay information, never advance it) but weak.",
        "No historical order book exists, so depth, spread and partial-fill "
        "behaviour cannot be reconstructed for any historical trade. Any "
        "claim about them would be invented.",
    ]
    if reference.costs_power:
        limits.insert(0, (
            "This wallet was SELECTED from the data. Its in-sample statistics "
            "are biased upward by that selection and are reported for context "
            "only. Judge it on the out-of-sample numbers."))
    if profile.n_observations < 60:
        limits.append(
            f"Only {profile.n_observations} in-sample observations. The "
            "profile is a description, not yet evidence.")
    return Reconstruction(reference=reference, profile=profile,
                          families=families, limits=limits)
