"""
Strategy discovery on Polymarket data, run through the Quant Bridge (step 4).

This is where the brief's central instruction is actually satisfied — *"I don't
want us to hard-code a specific strategy and assume that it will work"*. Rules
are not written here. Captured Polymarket features are handed to the existing
LEAN research platform, which does what it already does: engineer features,
search the rule space, walk-forward and Monte-Carlo the survivors, rank them,
and reject what does not hold up. What comes back is what the live engine then
evaluates.

**Each token is its own instrument, and that is why the export is per-token.**
The bridge's backtester models one price series: it reads consecutive rows as
consecutive bars and measures stop and target as percentage moves between them.
Stacking every token into one file would put a false price jump at each token
boundary — a position opened on the last bar of one outcome would be stopped out
against the first bar of an unrelated one, and those fabricated trades would be
scored as if they were real. So each token is exported to its own directory and
researched on its own, which is also what it is: a separate instrument with its
own resolution date.

**Then the cross-check.** A rule discovered on a single token is a rule fitted
to a single token. What survives here is what was ranked as acceptable on at
least ``min_tokens`` of them independently, which is the cheapest real defence
against curve-fitting available — and the reason the aggregation step exists at
all rather than simply returning the best-scoring rule found anywhere.
"""

from __future__ import annotations

import contextlib
import csv
import datetime as _dt
import hashlib
import json
import random as _random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import adversarial
from . import motif as motif_mod
from .analytics.store import IntelStore
from .config import Config
from .feature_domain import FeatureDomain, quarantine_incompatible
from .features import FEATURE_NAMES
from .sources import (SOURCE_INVERSE_ADVERSARIAL, rule_complexity as
                      _rule_complexity, source_census, source_of)
from .quant import QuantBridgeNotFound, load as load_bridge

# Written next to the exports so a person can see exactly what discovery saw.
MANIFEST = "MANIFEST.json"

# A profit figure no run can reach, used to disable the caps that have no
# "0 = disabled" sentinel. Large but finite, so it cannot poison an arithmetic
# comparison the way an infinity could.
_UNREACHABLE = 1e12


class ReplayDataUnavailable(RuntimeError):
    """A replay could not run because the data it needs is missing.

    Distinct from a replay that ran and produced nothing: this candidate was
    never given a fair test in this market, and the operator should see the
    difference. Surfaces as a DATA_FAILURE blocker rather than vanishing into
    a bare `continue`.
    """


@dataclass
class DiscoveredStrategy:
    """One rule that survived, plus the evidence for keeping it."""

    rule: dict[str, Any]                 # the bridge's Strategy.to_dict()
    signature: str                       # identity across tokens
    tokens: list[str] = field(default_factory=list)
    accepted_on: int = 0
    score: float = 0.0
    sharpe: float = 0.0
    oos_sharpe: float = 0.0
    win_rate: float = 0.0
    trades: int = 0
    describe: str = ""
    halves: set = field(default_factory=set)   # market-split halves hit
    # -- TRUE out-of-sample: frozen replay on markets discovery never saw ----
    status: str = "candidate"
    confidence: float = 0.0            # Wilson lower bound on the OOS win rate
    oos_trades: int = 0
    oos_markets: int = 0
    oos_win: float = 0.0
    oos_expectancy: float = 0.0
    oos_drawdown: float = 0.0
    oos_period: str = ""
    version: int = 1
    last_validated_ts: float = 0.0
    # Composite evidence score (library.evidence_score): sample x breadth x
    # Wilson confidence x P&L diversification, 0..1. Ranks the board.
    evidence: float = 0.0
    # Research-only evidence layers (the operator's family spec): maturity
    # state, the named blocking condition, and the HYPOTHESIS-family
    # ledger (evidence across all versions, market-deduplicated). Version
    # evidence above stays the atomic record; these never replace it.
    maturity: str = ""
    blocking: str = ""
    family_markets: int = 0
    family_trades: int = 0
    family_expectancy: float = 0.0
    family_versions: int = 0
    # -- FAMILY & MOTIF INTELLIGENCE (research evidence, never validation) ---
    # The structural class this candidate belongs to, what that class's own
    # record looks like across INDEPENDENT candidates and non-overlapping
    # markets, and the two sentences explaining its place in the research
    # queue. `family_*` above is the hypothesis-family (one signature, all its
    # versions) ledger and keeps its original meaning; everything below is the
    # motif layer, which spans signatures. Neither can promote anything.
    motif: str = ""
    motif_weight: float = 1.0
    family_research_score: float = 0.0
    family_replication: float = 0.0
    family_independent_markets: int = 0
    family_independent_candidates: int = 0
    family_failure_motif: str = ""
    why_family_elevated: str = ""
    why_family_deprioritised: str = ""
    # §8 temporal independence, §12 divergence, §7 allocation — all
    # research-only; none feeds the trading gate.
    oos_periods: int = 0
    overfit_risk: float = 0.0
    priority: float = 0.0
    # §9 structured diagnostics: every active blocker with its target,
    # and the next-action state when market breadth is the blocker.
    blockers: list = field(default_factory=list)
    next_action: str = ""
    # §11 and §23: which research pathway proposed this, what it was derived
    # from, how complicated it is, and how much of its evidence is genuinely
    # walk-forward rather than merely unseen. All descriptive; none of it can
    # promote anything.
    source: str = ""
    parent_id: str = ""
    complexity: int = 0
    oos_forward_markets: int = 0
    # -- the autonomous research layer, all descriptive -----------------------
    # What deliberate attack found, how much of the battery could be run
    # against this candidate's evidence, and the sentence explaining its place
    # in the research queue. `tradable` reads `status` and nothing here; a
    # candidate that survived every attack is not thereby validated, and one
    # that broke is not thereby rejected — the ladder owns both verdicts.
    adversarial_verdict: str = ""
    robustness: float = 0.0
    adversarial_coverage: float = 0.0
    adversarial_failed: list = field(default_factory=list)
    research_reward: float = 0.0
    why_more_research: str = ""
    why_stopped: str = ""

    def to_dict(self) -> dict:
        return {
            "signature": self.signature, "rule": self.rule,
            "tokens": self.tokens, "acceptedOn": self.accepted_on,
            "score": round(self.score, 6), "sharpe": round(self.sharpe, 4),
            "oosSharpe": round(self.oos_sharpe, 4),
            "winRate": round(self.win_rate, 4), "trades": self.trades,
            "describe": self.describe,
            "status": self.status,
            "confidence": round(self.confidence, 4),
            "oosTrades": self.oos_trades, "oosMarkets": self.oos_markets,
            "oosWin": round(self.oos_win, 4),
            "oosExpectancy": round(self.oos_expectancy, 6),
            "oosDrawdown": round(self.oos_drawdown, 4),
            "oosPeriod": self.oos_period,
            "version": self.version,
            "lastValidatedTs": self.last_validated_ts,
            "evidence": round(self.evidence, 4),
            "maturity": self.maturity,
            "blocking": self.blocking,
            "familyMarkets": self.family_markets,
            "familyTrades": self.family_trades,
            "familyExpectancy": round(self.family_expectancy, 6),
            "familyVersions": self.family_versions,
            "motif": self.motif,
            "motifWeight": round(self.motif_weight, 4),
            "familyResearchScore": round(self.family_research_score, 4),
            "familyReplication": round(self.family_replication, 4),
            "familyIndependentMarkets": self.family_independent_markets,
            "familyIndependentCandidates": self.family_independent_candidates,
            "familyFailureMotif": self.family_failure_motif,
            "whyFamilyElevated": self.why_family_elevated,
            "whyFamilyDeprioritised": self.why_family_deprioritised,
            "oosPeriods": self.oos_periods,
            "overfitRisk": round(self.overfit_risk, 4),
            "priority": round(self.priority, 4),
            "blockers": list(self.blockers),
            "nextAction": self.next_action,
            "source": self.source,
            "parentId": self.parent_id,
            "complexity": self.complexity,
            "oosForwardMarkets": self.oos_forward_markets,
            "adversarialVerdict": self.adversarial_verdict,
            "robustness": round(self.robustness, 4),
            "adversarialCoverage": round(self.adversarial_coverage, 4),
            "adversarialFailed": list(self.adversarial_failed),
            "researchReward": round(self.research_reward, 4),
            "whyMoreResearch": self.why_more_research,
            "whyStopped": self.why_stopped,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DiscoveredStrategy":
        return cls(
            rule=data.get("rule") or {}, signature=str(data.get("signature", "")),
            tokens=list(data.get("tokens") or []),
            accepted_on=int(data.get("acceptedOn", 0)),
            score=float(data.get("score", 0.0)),
            sharpe=float(data.get("sharpe", 0.0)),
            oos_sharpe=float(data.get("oosSharpe", 0.0)),
            win_rate=float(data.get("winRate", 0.0)),
            trades=int(data.get("trades", 0)),
            describe=str(data.get("describe", "")),
            status=str(data.get("status", "candidate")),
            confidence=float(data.get("confidence", 0.0)),
            oos_trades=int(data.get("oosTrades", 0)),
            oos_markets=int(data.get("oosMarkets", 0)),
            oos_win=float(data.get("oosWin", 0.0)),
            oos_expectancy=float(data.get("oosExpectancy", 0.0)),
            oos_drawdown=float(data.get("oosDrawdown", 0.0)),
            oos_period=str(data.get("oosPeriod", "")),
            version=int(data.get("version", 1)),
            last_validated_ts=float(data.get("lastValidatedTs", 0.0)),
            evidence=float(data.get("evidence", 0.0)),
            maturity=str(data.get("maturity", "")),
            blocking=str(data.get("blocking", "")),
            family_markets=int(data.get("familyMarkets", 0)),
            family_trades=int(data.get("familyTrades", 0)),
            family_expectancy=float(data.get("familyExpectancy", 0.0)),
            family_versions=int(data.get("familyVersions", 1)),
            motif=str(data.get("motif", "")),
            motif_weight=float(data.get("motifWeight", 1.0)),
            family_research_score=float(data.get("familyResearchScore", 0.0)),
            family_replication=float(data.get("familyReplication", 0.0)),
            family_independent_markets=int(
                data.get("familyIndependentMarkets", 0)),
            family_independent_candidates=int(
                data.get("familyIndependentCandidates", 0)),
            family_failure_motif=str(data.get("familyFailureMotif", "")),
            why_family_elevated=str(data.get("whyFamilyElevated", "")),
            why_family_deprioritised=str(
                data.get("whyFamilyDeprioritised", "")),
            oos_periods=int(data.get("oosPeriods", 0)),
            overfit_risk=float(data.get("overfitRisk", 0.0)),
            priority=float(data.get("priority", 0.0)),
            blockers=list(data.get("blockers") or []),
            next_action=str(data.get("nextAction", "")),
            source=str(data.get("source", "")),
            parent_id=str(data.get("parentId", "")),
            complexity=int(data.get("complexity", 0)),
            oos_forward_markets=int(data.get("oosForwardMarkets", 0)),
            adversarial_verdict=str(data.get("adversarialVerdict", "")),
            robustness=float(data.get("robustness", 0.0)),
            adversarial_coverage=float(data.get("adversarialCoverage", 0.0)),
            adversarial_failed=list(data.get("adversarialFailed") or []),
            research_reward=float(data.get("researchReward", 0.0)),
            why_more_research=str(data.get("whyMoreResearch", "")),
            why_stopped=str(data.get("whyStopped", "")),
        )

    @property
    def tradable(self) -> bool:
        """Only OOS-proven strategies may drive money."""
        return self.status in ("validated", "high_confidence")


def wilson_lower_bound(wins: int, n: int, z: float = 1.96) -> float:
    """The 95% lower confidence bound on a win rate — sample size made honest.

    98% over 8 trades bounds near 0.75; 98% over 500 bounds near 0.96. This is
    the number that stops a tiny lucky sample from outranking real evidence.
    """
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) + z * z / (4 * n)) / n) ** 0.5
    return max(0.0, (centre - margin) / denom)


def split_markets_by_date(entries: list[dict],
                          oos_fraction: float) -> tuple[set, set]:
    """Market-level, TIME-ordered split: the newest markets are held out.

    Newest-as-holdout gives both separations the spec demands at once — the
    OOS markets are disjoint from discovery (no market on both sides) AND
    later in time (walk-forward: discover on the past, validate on what came
    after). Returns (discovery_market_ids, oos_market_ids).
    """
    by_market: dict[str, float] = {}
    for entry in entries:
        market = str(entry.get("marketId") or entry.get("tokenId"))
        last = float(entry.get("lastTs") or 0.0)
        by_market[market] = max(by_market.get(market, 0.0), last)
    ordered = sorted(by_market, key=lambda m: by_market[m])
    if len(ordered) < 2:
        return set(ordered), set()
    cut = max(1, int(len(ordered) * oos_fraction))
    return set(ordered[:-cut]), set(ordered[-cut:])


def assign_status(oos: dict, cfg) -> str:
    """The validation ladder, from frozen-OOS evidence alone.

    candidate -> oos_testing -> validated -> high_confidence, or failed_oos.
    A strategy is only VALIDATED by data it never saw; a tiny sample stays
    "oos_testing" however spectacular its win rate looks.
    """
    trades = int(oos.get("trades", 0))
    markets = int(oos.get("markets", 0))
    expectancy = float(oos.get("expectancy", 0.0))
    if trades == 0:
        return "candidate"
    if trades >= cfg.oos_min_trades and expectancy <= cfg.oos_min_expectancy:
        return "failed_oos"
    if trades < cfg.oos_min_trades or markets < cfg.oos_min_markets:
        return "oos_testing"
    if expectancy <= cfg.oos_min_expectancy:
        return "failed_oos"
    if trades >= cfg.oos_hc_trades and markets >= cfg.oos_hc_markets:
        return "high_confidence"
    return "validated"


def signature_of(rule: dict) -> str:
    """Rule identity across tokens.

    Thresholds are excluded on purpose. Discovery sets them from each token's
    own quantiles, so the same idea — "buy when cohort flow is unusually
    positive" — lands on a different number for every token. Keying on the
    threshold would make each token's version a distinct strategy and the
    cross-token check could never confirm anything.
    """
    if rule.get("type") == "sequence":
        # A chain's identity is its ordered kinds and direction; gaps and
        # holds are its thresholds, so a retimed chain is a new VERSION of
        # the same family — exactly like a rethresholded feature rule.
        return "seq|" + "|".join(rule.get("chain") or []) + \
            "|" + str(rule.get("direction", ""))
    if rule.get("type") == "sharp_move":
        # Identity is the condition cell and response direction; the hold
        # is its threshold, so a re-timed pattern is a new version.
        return "sharp|" + "|".join(str(rule.get(k, "")) for k in (
            "move_direction", "price_region", "liquidity", "direction"))
    if rule.get("type") == "longshot":
        # Identity is the category, band, and side; the capital floor is a
        # threshold, so a re-floored rule is a new version.
        return "longshot|" + "|".join(str(rule.get(k, "")) for k in (
            "category", "prob_lo", "prob_hi", "side"))
    if rule.get("type") == "wallet_state":
        # Identity is the wallet, band, and checkpoints; the premium is a
        # threshold, so a re-priced rule is a new version.
        return "wstate|" + "|".join(str(rule.get(k, "")) for k in (
            "wallet", "price_lo", "price_hi", "quiet_minutes",
            "persist_minutes"))
    if rule.get("type") == "wallet_behavior":
        # Identity is the BEHAVIOR — origin (fresh entry vs side switch),
        # band bucket, trigger, hold class, direction — never the
        # wallet(s) that inspired it, so a hundred wallets sharing one
        # behavior are one candidate. The discovered numeric edges are
        # thresholds, so a re-edged rule is a version. Origin is identity
        # because the switching and non-switching versions of one entry
        # condition are competing hypotheses (§5), never one record.
        return "wpat|" + str(rule.get("origin") or "entry") + "|" + \
            "|".join(str(rule.get(k, "")) for k in (
                "direction", "band", "trigger", "hold"))
    return "|".join(str(rule.get(k, "")) for k in (
        "direction", "entry_feature", "entry_op", "filter_feature", "filter_op"))


def family_of(rule: dict) -> str:
    """The FAMILY a rule belongs to — the underlying idea, not the variant.

    The operator's correction: the library was filling with near-identical
    twists on one phenomenon (short ask_z / short bid_z / short mid_z are
    one idea, not three). Registration is capped per family per pass, and a
    family that unseen data keeps refusing loses research priority — so
    the library measures whether PHENOMENA are real, not how many ways one
    can be spelled.
    """
    kind = rule.get("type")
    if kind == "sequence":
        return "sequence-event"
    if kind == "sharp_move":
        return "crash-recovery"
    if kind == "longshot":
        return "longshot-calibration"
    if kind == "wallet_state":
        return "wallet-behavior"
    if kind == "wallet_behavior":
        return "wallet-pattern"
    feature = str(rule.get("entry_feature") or "")
    if feature.startswith("liq_"):
        return "liquidation"
    if feature.startswith("np_"):
        return "book-structure"
    if feature.startswith("ms_"):
        return "market-state"
    if feature.startswith("regime_"):
        return "regime"
    if feature.startswith("wallet_"):
        return "wallet-flow"
    if "spread" in feature or "depth" in feature:
        return "order-book"
    # Price-derived features split by TRADE INTENT, not by which column
    # spelled the price: buying weakness / selling strength is mean
    # reversion; the opposite is momentum.
    direction = str(rule.get("direction") or "")
    op = str(rule.get("entry_op") or "")
    if (direction, op) in (("long", "<"), ("short", ">")):
        return "mean-reversion"
    if (direction, op) in (("long", ">"), ("short", "<")):
        return "momentum"
    return "other"


def uncertain_span(series: list[dict], lo: float, hi: float) -> list[dict]:
    """The stretch of a series where the outcome was still genuinely in doubt.

    Once a market decides, its price only converges on 0 or 1. A rule fitted to
    that stretch scores superbly and means nothing: research over backfilled
    settled markets returned rules with a 97.9% win rate and a Sharpe of 46,
    every one of which amounted to "a token trading at 0.998 reaches 0.999".
    91% of those series moved less than five cents from end to end.

    The **longest contiguous** run inside the band is taken rather than every
    in-band row, because dropping rows from the middle would splice two
    separated stretches into one series and invent price moves across the join
    that never happened.
    """
    best_start = best_len = run_start = run_len = 0
    for index, row in enumerate(series):
        price = _num(row.get("price"))
        if lo <= price <= hi:
            if run_len == 0:
                run_start = index
            run_len += 1
            if run_len > best_len:
                best_start, best_len = run_start, run_len
        else:
            run_len = 0
    return series[best_start:best_start + best_len]


# --------------------------------------------------------------------------
# export
# --------------------------------------------------------------------------

def export_token_series(store: IntelStore, out_root: Path,
                        min_rows: int = 200, max_tokens: int = 12,
                        log: Optional[Callable[[str], None]] = None,
                        lo: float = 0.0, hi: float = 1.0) -> list[dict]:
    """Write one CSV per token, each in its own directory.

    Returns the manifest entries actually written. Tokens with too little
    captured history are skipped and reported rather than exported thin — a
    50-row series produces a strategy that has learned 50 rows.
    """
    say = log or (lambda _m: None)
    out_root.mkdir(parents=True, exist_ok=True)
    candidates = store.research_tokens(min_rows=min_rows)
    # Counted before the cap is applied, so the manifest can state what was
    # left out. A run that quietly researched 12 of 40 eligible tokens and said
    # nothing would read afterwards as though it had covered everything.
    all_tokens = len(store.research_tokens(min_rows=1))
    below_floor = all_tokens - len(candidates)
    if not candidates:
        say(f"  no token has {min_rows}+ captured rows yet "
            f"({all_tokens} token(s) captured, all below the floor)")
        return []

    written: list[dict] = []
    trimmed_away = decided_out = 0
    for entry in candidates[:max_tokens]:
        token_id = entry["token_id"]
        series = store.research_series(token_id)
        captured = len(series)
        series = uncertain_span(series, lo, hi)
        trimmed_away += captured - len(series)
        if len(series) < min_rows:
            # Long enough as captured, too short once the settled tail is gone.
            if captured >= min_rows:
                decided_out += 1
            continue
        token_dir = out_root / _safe_name(token_id)
        token_dir.mkdir(parents=True, exist_ok=True)
        path = token_dir / "features.csv"
        _write_csv(path, series)
        written.append({
            "tokenId": token_id, "marketId": entry.get("market_id", ""),
            "outcome": entry.get("outcome", ""),
            "category": entry.get("category", ""),
            "rows": len(series), "path": str(path),
            "firstTs": entry.get("first_ts"), "lastTs": entry.get("last_ts"),
        })
        say(f"  {token_id[:14]}… {len(series):>6} rows -> {path.name}")

    capped_out = max(0, len(candidates) - len(written))
    if capped_out:
        say(f"  NOTE: {capped_out} eligible token(s) not researched this run "
            f"(max_tokens={max_tokens}); re-run to cover them")
    if below_floor:
        say(f"  {below_floor} token(s) captured but under the {min_rows}-row "
            "floor")
    # Never silent: a filter that quietly removes most of the input reads
    # afterwards as though discovery saw everything.
    if trimmed_away or decided_out:
        say(f"  trimmed {trimmed_away:,} row(s) outside the {lo:g}-{hi:g} "
            f"uncertainty band (already-decided price action)")
        if decided_out:
            say(f"  {decided_out} token(s) had enough rows but too few still "
                "in play - dropped")

    manifest = {
        "generatedTs": time.time(),
        "featureColumns": list(FEATURE_NAMES),
        "tokens": written,
        "tokensCaptured": all_tokens,
        "tokensEligible": len(candidates),
        "tokensExported": len(written),
        "cappedOut": capped_out,
        "belowMinRows": below_floor,
        "minRows": min_rows,
        "maxTokens": max_tokens,
        "uncertaintyBand": [lo, hi],
        "rowsTrimmedAsDecided": trimmed_away,
        "tokensDroppedAsDecided": decided_out,
    }
    (out_root / MANIFEST).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")
    return written


def export_historical_series(store: IntelStore, out_root: Path,
                             min_rows: int = 200, max_tokens: int = 12,
                             log: Optional[Callable[[str], None]] = None,
                             lo: float = 0.0, hi: float = 1.0) -> list[dict]:
    """Write research series built from settled markets' historical tapes.

    The correction behind this: discovery must study the wallets' PAST — the
    closed, settled trades backfill already pulled — not only rows captured
    after the bot starts. See :mod:`pqb.analytics.history_series` for what the
    tape can and cannot honestly provide.
    """
    from .analytics.history_series import build_series

    say = log or (lambda _m: None)
    out_root.mkdir(parents=True, exist_ok=True)
    # The cap is applied AFTER the band trim, so out-of-band series cannot
    # consume it. The busiest tapes are settled epilogues (legacy collection
    # pulled exactly those); ranked busiest-first they filled every slot and
    # were then dropped by the trim — the markets actually worth studying
    # never got considered.
    entries = build_series(store, min_rows=min_rows, max_tokens=max_tokens * 4)
    if not entries:
        say("  no settled market has a deep enough tape yet - run "
            "`backfill --markets 300 --trades 2000` to pull more history")
        return []

    written: list[dict] = []
    decided_out = 0
    for entry in entries:
        if len(written) >= max_tokens:
            break
        token_id = entry["tokenId"]
        # Settled tapes are the worst offenders: the trades a closed market has
        # most of are the ones after everyone already knew the answer.
        series = uncertain_span(entry["series"], lo, hi)
        if len(series) < min_rows:
            decided_out += 1
            continue
        token_dir = out_root / f"hist_{_safe_name(token_id)}"
        token_dir.mkdir(parents=True, exist_ok=True)
        path = token_dir / "features.csv"
        _write_csv(path, series)
        written.append({
            "tokenId": f"hist:{token_id}", "marketId": entry["marketId"],
            "outcome": entry["outcome"], "category": entry["category"],
            "rows": len(series), "path": str(path),
            "source": "history",
            # The tape's own clock — from the TRIMMED series, so the
            # walk-forward split orders markets by the data actually studied.
            # Without these, every historical market sorted at epoch zero and
            # "newest held out" quietly stopped being true.
            "firstTs": float(series[0]["ts"]) if series else None,
            "lastTs": float(series[-1]["ts"]) if series else None,
        })
        say(f"  hist {token_id[:14]}… {len(series):>6} rows "
            f"-> {path.parent.name}/{path.name}")
    if decided_out:
        say(f"  {decided_out} settled series dropped: too little of the tape "
            f"was inside the {lo:g}-{hi:g} band to study "
            "(price had already converged)")
    say(f"  {len(written)} historical series exported (settled markets, "
        "outcomes known)")
    return written


def _write_csv(path: Path, series: list[dict]) -> None:
    """Timestamp first, then every declared feature column, in a fixed order.

    Missing keys are written as 0.0 rather than blank: the bridge forward-fills
    blanks, which would carry a stale value across a gap and present it as a
    fresh observation.
    """
    # The non-print engine's structural columns are dynamic (the engine defines
    # its own snapshot keys), so the header is the fixed contract PLUS the
    # sorted union of np_ keys seen anywhere in this series. Without the union
    # they would be silently dropped from the very CSVs discovery studies —
    # the exact defeat of the integration.
    structural = sorted({key for row in series for key in row
                         if key.startswith("np_")})
    names = [*FEATURE_NAMES, *structural]
    columns = ["timestamp", *names]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for row in series:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.gmtime(float(row.get("ts", 0.0))))
            writer.writerow([stamp,
                             *(_num(row.get(name, 0.0)) for name in names)])


def _num(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return 0.0 if (out != out or out in (float("inf"), float("-inf"))) else out


def _safe_name(token_id: str) -> str:
    """Filesystem-safe, SHORT, and still unique.

    Token ids are 70+ digits; 48-character directory names stacked under
    nested research paths pushed deep installs past Windows' 260-char
    limit. 20 chars of the id plus an 8-char digest keeps names unique
    and cuts every nested path by 20 characters.
    """
    cleaned = "".join(c for c in str(token_id) if c.isalnum()) or "token"
    if len(cleaned) <= 28:
        return cleaned
    digest = hashlib.sha1(cleaned.encode()).hexdigest()[:8]
    return f"{cleaned[:20]}{digest}"


def directed_expansions(entry: dict, directive: str) -> list[tuple[dict, str]]:
    """The variant a PAST FAILURE asks for. §11's self-correction loop.

    `variant_expansions` below expands on success — a candidate doing well
    earns siblings that test whether its holding period or direction is
    load-bearing. This function is the other half, and it is the one the
    directive was actually written about: a candidate that FAILED, classified
    by `experiments.classify`, gets the specific follow-up its failure
    implies. Costs beat the move, so try holding longer. The edge lives at
    exactly one threshold, so try a coarser one. The rule never fired, so
    loosen what it waits for.

    What it deliberately does NOT do is retune toward a better result. Each
    variant is a different QUESTION registered as a fresh candidate with zero
    inherited evidence, not the same question asked again with the numbers
    nudged until the answer changes — and since the variant's evidence must be
    earned on markets its parent never touched, there is no path here that
    could turn a refuted idea into a validated one by repetition.
    """
    rule = entry.get("rule") or {}
    kind = str(rule.get("type") or "")
    if not directive or kind not in ("sequence", "sharp_move",
                                     "wallet_behavior"):
        return []
    out: list[tuple[dict, str]] = []

    def _emit(changes: dict, tag: str, why: str) -> None:
        variant = dict(rule)
        variant.update(changes)
        variant["variant_of"] = entry["id"]
        variant["variant"] = tag
        variant["directive"] = directive
        out.append((variant, f"{tag.upper()} of "
                             f"{entry.get('describe') or entry['id']} - "
                             f"{why}"[:120]))

    if directive == "LENGTHEN_HOLD":
        if kind == "wallet_behavior":
            if str(rule.get("hold") or "") != "resolution":
                _emit({"hold_seconds": float(rule.get("hold_seconds")
                                             or 3600.0) * 3.0},
                      "directed-longer-hold", "costs beat the move")
        else:
            _emit({"hold_bars": max(4, int(rule.get("hold_bars") or 15) * 3)},
                  "directed-longer-hold", "costs beat the move")
    elif directive == "SHORTEN_HOLD" and kind in ("sequence", "sharp_move"):
        _emit({"hold_bars": max(2, int(rule.get("hold_bars") or 15) // 3)},
              "directed-shorter-hold", "the path was unsurvivable")
    elif directive == "WIDEN_CONDITION" and kind == "sequence":
        _emit({"gap_bars": int(rule.get("gap_bars") or 15) * 2},
              "directed-wider-window",
              "the effect lived at exactly one threshold")
    elif directive == "LOOSEN_ENTRY" and kind == "sequence":
        chain = [str(c) for c in (rule.get("chain") or [])]
        if len(chain) > 2:
            # The cheapest way to loosen a chain is to ask less of it. A
            # shorter chain is also a SIMPLER hypothesis, which is the
            # direction complexity is supposed to move without evidence.
            _emit({"chain": chain[-2:]}, "directed-shorter-chain",
                  "the full chain never occurred")
    return out


def variant_expansions(library, entry: dict, cumulative: dict,
                       cfg, directive: str = "") -> list[tuple[dict, str]]:
    """Direction and hold as DISCOVERY VARIABLES (the operator's
    bidirectional spec): evidence-driven variant candidates for one
    library row. Returns [(rule, describe), ...] to register.

    * **Inverse variant** — when a candidate's own unseen-market record is
      decisively NEGATIVE (>=10 trades, expectancy beyond twice the cost),
      the OPPOSITE side becomes a hypothesis worth its own record. It
      inherits the parent's discovery-market exclusions (the shared signal
      was fitted there) and NOTHING else — zero evidence, zero validation,
      exactly as the spec demands.
    * **Hold variants** — when a candidate shows PROMISE (>=10 trades,
      positive expectancy) and no other version of its family exists yet,
      half and double holding windows register as sibling versions. The
      spec's own restraint applies: complexity expands only when evidence
      justifies it, so losers never spawn hold ladders.

    Bridge feature-rules are exempt: their search already explores both
    directions and a wall-clock hold ladder natively. NO-TRADE needs no
    variant — it is the ladder's default for everything unproven.
    """
    rule = entry.get("rule") or {}
    rule_type = rule.get("type")
    if rule_type not in ("sequence", "sharp_move", "longshot",
                         "wallet_behavior"):
        return []
    trades = int(cumulative.get("trades") or 0)
    expectancy = float(cumulative.get("expectancy") or 0.0)
    cost = float(getattr(cfg, "assumed_spread", 0.01))
    # The failure-directed variant comes FIRST, because the per-pass variant
    # budget is small and a question raised by a specific measured failure is
    # worth more than the generic ladder that would otherwise fill the slot.
    out: list[tuple[dict, str]] = directed_expansions(entry, directive)

    def _flip(direction: str) -> str:
        return "down" if str(direction) == "up" else "up"

    # ENGINE D — INVERSE / ADVERSARIAL (§9).
    #
    # The inverse used to be generated only for candidates whose record was
    # decisively NEGATIVE, on the reasoning that a rule losing money might be
    # right backwards. True, and not the whole question. §9 asks it of every
    # SUFFICIENTLY SUPPORTED candidate too, and for the opposite reason: a
    # rule that makes money is only interesting if its opposite does not. If
    # both directions pay, the edge is not directional and what we have found
    # is volatility, or a cost artefact, or nothing.
    #
    # The inverse is never assumed better. It is another hypothesis with its
    # own identity, its own version, its own evidence and its own trip
    # through the one validation pipeline.
    decisively_bad = trades >= 10 and expectancy <= -2.0 * cost
    well_supported = trades >= 30 and expectancy > 0
    if decisively_bad or well_supported:
        inverse = dict(rule)
        inverse["variant_of"] = entry["id"]
        inverse["variant"] = ("inverse" if decisively_bad
                              else "directionality-test")
        if rule_type == "longshot":
            inverse["side"] = ("high" if rule.get("side") == "low"
                               else "low")
        elif rule_type == "wallet_behavior":
            inverse["direction"] = ("short" if rule.get("direction",
                                                        "long") == "long"
                                    else "long")
        else:
            inverse["direction"] = _flip(rule.get("direction", "up"))
        flipped_sig = signature_of(inverse)
        already = any(s["signature"] == flipped_sig
                      for s in library.all_strategies())
        if not already:
            label = ("INVERSE" if decisively_bad else "DIRECTIONALITY TEST")
            out.append((inverse,
                        f"{label} of "
                        f"{entry.get('describe') or entry['id']}"[:120]))

    # TIMING ADVERSARIES (§9's delayed entry and early exit). An edge that
    # survives being entered a few bars late is a real relationship; one that
    # evaporates was a reaction to a single print, and knowing which is the
    # difference between a strategy and a latency race we would lose. Only
    # for candidates already carrying evidence — §8's staged complexity says
    # expand when the simpler thing has earned it, not before.
    if rule_type in ("sequence", "sharp_move") and well_supported:
        hold = int(rule.get("hold_bars") or 15)
        delayed = dict(rule)
        delayed["delay_bars"] = max(1, int(rule.get("delay_bars") or 0) + 2)
        delayed["variant_of"] = entry["id"]
        delayed["variant"] = "delayed-entry"
        out.append((delayed,
                    f"DELAYED ENTRY of "
                    f"{entry.get('describe') or entry['id']}"[:120]))
        if hold > 3:
            early = dict(rule)
            early["hold_bars"] = max(2, hold // 3)
            early["variant_of"] = entry["id"]
            early["variant"] = "early-exit"
            out.append((early,
                        f"EARLY EXIT of "
                        f"{entry.get('describe') or entry['id']}"[:120]))

    if rule_type in ("sequence", "sharp_move") and trades >= 10 \
            and expectancy > 0:
        family_versions = sum(
            1 for s in library.all_strategies()
            if s["signature"] == entry.get("signature"))
        if family_versions <= 1:
            hold = int(rule.get("hold_bars") or 15)
            for factor, tag in ((0.5, "half-hold"), (2.0, "double-hold")):
                variant = dict(rule)
                variant["hold_bars"] = max(2, int(hold * factor))
                variant["variant_of"] = entry["id"]
                variant["variant"] = tag
                out.append((variant,
                            f"{tag.upper()} of "
                            f"{entry.get('describe') or entry['id']}"[:120]))

    # Wallet-behavior hold discovery (the operator's §8): the mined hold
    # came from the wallets' own exits; a promising pattern earns its
    # half/double timed-hold siblings. Resolution holds have no dial to
    # turn — direction inversion above is their only variant.
    if rule_type == "wallet_behavior" and trades >= 10 and expectancy > 0 \
            and str(rule.get("hold") or "") != "resolution":
        family_versions = sum(
            1 for s in library.all_strategies()
            if s["signature"] == entry.get("signature"))
        if family_versions <= 1:
            hold_seconds = float(rule.get("hold_seconds") or 3600.0)
            for factor, tag in ((0.5, "half-hold"), (2.0, "double-hold")):
                variant = dict(rule)
                variant["hold_seconds"] = max(600.0, hold_seconds * factor)
                variant["variant_of"] = entry["id"]
                variant["variant"] = tag
                out.append((variant,
                            f"{tag.upper()} of "
                            f"{entry.get('describe') or entry['id']}"[:120]))
    return out


def _pattern_signature(rule: dict) -> str:
    """The rule's relationship identity, in the hypothesis layer's vocabulary.

    The join key between a candidate and whatever the convergence layer knows
    about the relationship it expresses. Cheap and pure; two engines' rules
    describing one phenomenon land on one string, which is the whole reason
    `hypothesis.normalize` exists.
    """
    from .hypothesis import normalize
    return normalize(rule or {}).signature()


def _convergence_priorities(root: Path, config: Config,
                            log: Optional[Callable[[str], None]] = None
                            ) -> dict[str, float]:
    """Last pass's convergence priority per relationship signature.

    LAST pass's, unavoidably and correctly: the hypothesis layer runs at the
    very end of a pass, over the finished library view, so at allocation time
    the newest figures do not exist yet. Using the previous pass's is not a
    staleness bug — convergence is a statement about which relationships
    several independent engines have landed on, which does not turn over
    hourly, and a one-pass lag on a research-priority hint costs nothing.

    Returns an empty dict on a first run or any read failure. Convergence is
    a bonus term with a hard ceiling (`reward.CONVERGENCE_MAX_BONUS`), so its
    absence changes an ordering and never a decision.
    """
    if not config.research.hypothesis_layer_enabled:
        return {}
    path = root / "hypotheses.sqlite3"
    if not path.exists():
        return {}
    from .hypothesis import HypothesisStore

    try:
        store = HypothesisStore(path)
    except Exception:                                     # noqa: BLE001
        return {}
    try:
        # Highest VERSION wins where several exist, not highest priority. A
        # re-versioned hypothesis supersedes its predecessor; taking the best
        # score across versions would let a superseded record go on steering
        # research forever, which is the versioning discipline defeated by
        # the reader rather than by the writer.
        best: dict[str, tuple[int, float]] = {}
        for hypothesis in store.all():
            signature = hypothesis.signature
            if not signature:
                continue
            version = int(hypothesis.version)
            if version >= best.get(signature, (-1, 0.0))[0]:
                best[signature] = (version, float(hypothesis.priority))
        return {sig: priority for sig, (_v, priority) in best.items()}
    except Exception:                                     # noqa: BLE001
        return {}
    finally:
        store.close()


def validation_domain(root: Path, log: Optional[Callable[[str], None]] = None
                      ) -> "FeatureDomain":
    """Measure — and cache — the feature validity domain of the OOS pool.

    Read from the pool cache rather than from this pass's fresh exports,
    because the gate has to run BEFORE registration and the pool is what
    candidates are actually validated against. On a first run the pool is
    empty, and the domain returned is permissive: there is nothing to measure
    yet, and refusing every candidate on the strength of no measurement would
    be worse than the problem.
    """
    from .feature_domain import FeatureDomain, build_domain

    say = log or (lambda _m: None)
    index_path = root / "pool" / "pool-index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        index = {}
    paths = [Path(meta["path"]) for meta in index.values()
             if meta.get("eligible") and Path(meta.get("path", "")).exists()]
    if not paths:
        say("  feature domain: no validation series yet — gate is permissive")
        return FeatureDomain(permissive=True)

    domain = build_domain(paths, FEATURE_NAMES)
    with contextlib.suppress(OSError):
        domain.save(root / "feature-domain.json")
    constant = domain.constant_columns()
    summary = domain.summary()
    say(f"  feature domain: {summary['featuresUsableInValidation']}"
        f"/{summary['featuresKnown']} features usable in validation data "
        f"({len(constant)} constant) across "
        f"{domain.series_sampled} series")
    if constant:
        say("    constant in validation data: "
            + ", ".join(constant[:8])
            + (f" (+{len(constant) - 8} more)" if len(constant) > 8 else ""))
    return domain


def ensure_oos_pool(store: IntelStore, pool_root: Path, config: Config,
                    log: Optional[Callable[[str], None]] = None
                    ) -> tuple[list[dict], int]:
    """The PERSISTENT OOS market pool: every eligible settled market, built
    once, reusable forever. Returns (eligible entries, built this pass).

    The bottleneck this removes (the operator's audit): validation could
    only draw breadth from the ~max_tokens series exported per pass, while
    the store held hundreds of settled markets candidates never reached.
    Settled series are IMMUTABLE — a tape that ended cannot change — so
    each is built exactly once (band-trimmed like every research series),
    cached to disk, and indexed. The cache fills incrementally
    (oos_pool_build_per_pass per pass, richest tapes first), and tokens
    whose tapes prove too thin are remembered as ineligible so they are
    never rebuilt. Per-candidate exclusions and the market-testifies-once
    rule gate USE of the pool exactly as before.
    """
    say = log or (lambda _m: None)
    pool_root.mkdir(parents=True, exist_ok=True)
    index_path = pool_root / "pool-index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        index = {}

    built = 0
    # The admission census for this pass's build batch. Section 2 asks for
    # total settled markets, how many were rejected as thin, how many for
    # insufficient trades, how many admitted, and the reason for every
    # rejection — reported rather than inferred from a small pool.
    build_census: dict = {}
    # Reported whether or not a build batch runs this pass. Derived from the
    # census alone, it read 0 on any pass with nothing left to build — which
    # is exactly the pass where the pool is FULLY processed, so the healthiest
    # possible state displayed as "no settled markets known".
    build_census["settledMarkets"] = len(store.resolutions())
    per_pass = int(config.research.oos_pool_build_per_pass)
    cap = int(config.research.oos_pool_max_series)
    pool_floor = int(getattr(config.research, "oos_pool_min_rows", 80))
    eligible_now = sum(1 for v in index.values() if v.get("eligible"))
    if per_pass > 0 and eligible_now < cap:
        resolutions = store.resolutions()
        counts = store.query(
            "SELECT token_id, COUNT(*) n FROM wallet_trades "
            "WHERE token_id != '' GROUP BY token_id")
        settled = sorted(
            (r for r in counts
             if str(r["token_id"]) in resolutions and int(r["n"]) >= 8),
            key=lambda r: -int(r["n"]))

        from .analytics.history_series import SERIES_BUILD_VERSION

        def _retryable(token: str) -> bool:
            # Unindexed tokens are always candidates; tokens marked thin
            # under a STRICTER floor deserve a retry when the floor drops.
            meta = index.get(token)
            if meta is None:
                return True
            if meta.get("eligible"):
                return False
            # A rejection is a verdict of the construction that made it. When
            # construction changes — as it did when wall-clock duration
            # stopped being an admission test — every previous "thin" verdict
            # is re-opened exactly once, so the pool rebuilds itself rather
            # than staying permanently narrowed by a rule no longer in force.
            if int(meta.get("recipe", 1)) < SERIES_BUILD_VERSION:
                return True
            return int(meta.get("minRows", 200)) > pool_floor

        missing = [str(r["token_id"]) for r in settled
                   if _retryable(str(r["token_id"]))][:per_pass]
        if missing:
            from .analytics.history_series import build_series

            lo = config.research.uncertain_min_price
            hi = config.research.uncertain_max_price
            entries = build_series(store, min_rows=pool_floor,
                                   max_tokens=per_pass,
                                   only_tokens=set(missing),
                                   stats=build_census)
            built_tokens = set()
            for entry in entries:
                token = str(entry["tokenId"])
                built_tokens.add(token)
                series = uncertain_span(entry["series"], lo, hi)
                if len(series) < pool_floor:
                    index[token] = {"eligible": False, "reason": "thin",
                                    "minRows": pool_floor,
                                    "recipe": SERIES_BUILD_VERSION}
                    continue
                token_dir = pool_root / f"pool_{_safe_name(token)}"
                token_dir.mkdir(parents=True, exist_ok=True)
                csv_path = token_dir / "features.csv"
                _write_csv(csv_path, series)
                index[token] = {
                    "eligible": True,
                    "marketId": str(entry.get("marketId") or token),
                    "path": str(csv_path), "rows": len(series),
                    "firstTs": float(series[0]["ts"]),
                    "lastTs": float(series[-1]["ts"]),
                    # Stored at build time because the builder is the only
                    # place that knows it: re-deriving a category later would
                    # mean a Gamma lookup per pool market per pass, and the
                    # information-gain ordering needs it on every one.
                    "category": str(entry.get("category") or ""),
                }
                built += 1
            # Attempted but not returned by the builder: tape too thin to
            # bucket at all. Remember that, or it would retry every pass.
            for token in missing:
                if token not in built_tokens \
                        and not index.get(token, {}).get("eligible"):
                    index[token] = {"eligible": False, "reason": "thin",
                                    "minRows": pool_floor,
                                    "recipe": SERIES_BUILD_VERSION}
            with contextlib.suppress(OSError):
                index_path.write_text(json.dumps(index), encoding="utf-8")

    entries_out = []
    for token, meta in index.items():
        if not meta.get("eligible"):
            continue
        if not Path(meta.get("path", "")).exists():
            continue                    # cache pruned by hand; rebuild later
        entries_out.append({
            "tokenId": f"pool:{token}",
            "marketId": str(meta.get("marketId") or token),
            "path": str(meta["path"]), "rows": int(meta.get("rows") or 0),
            "firstTs": meta.get("firstTs"), "lastTs": meta.get("lastTs"),
            "category": str(meta.get("category") or ""),
            "source": "pool",
        })
    ineligible = sum(1 for v in index.values() if not v.get("eligible"))
    remaining = 0
    if per_pass > 0:
        try:
            remaining = sum(1 for r in settled
                            if _retryable(str(r["token_id"])))
        except NameError:                        # build phase skipped
            pass
    say(f"  OOS pool: {len(entries_out)} cached settled series eligible "
        f"({built} built this pass, {ineligible} known-thin, "
        f"{remaining} still unprocessed)")
    rejected_by = dict(build_census.get("rejectedBy") or {})
    if rejected_by:
        say("  admission census this batch: "
            + f"{build_census.get('admitted', 0)} admitted, "
            + ", ".join(f"{n} {reason.replace('_', ' ')}"
                        for reason, n in sorted(rejected_by.items())))
    return entries_out, built, {
        "knownThin": ineligible,
        "unprocessed": remaining,
        # Section 2's required exposure. Reported straight from the builder,
        # so the funnel can name the bottleneck instead of implying one.
        "settledMarkets": int(build_census.get("settledMarkets") or 0),
        "consideredThisPass": int(build_census.get("considered") or 0),
        "admittedThisPass": int(build_census.get("admitted") or 0),
        "rejectedThisPass": int(build_census.get("rejected") or 0),
        "rejectedBy": rejected_by,
    }


# --------------------------------------------------------------------------
# research
# --------------------------------------------------------------------------

def median_of(csv_path: Path, column: str) -> float:
    """Median of one column in an exported series, 0.0 if unreadable.

    Read from the exported CSV rather than the store so both export paths -
    live capture and backfilled history - are measured the same way.
    """
    values: list[float] = []
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                value = _num(row.get(column))
                if value > 0:
                    values.append(value)
    except (OSError, csv.Error):
        return 0.0
    if not values:
        return 0.0
    values.sort()
    return values[len(values) // 2]


def median_price(csv_path: Path) -> float:
    """Typical share price in an exported series, used to size positions."""
    return median_of(csv_path, "price")


def median_bar_seconds(csv_path: Path) -> float:
    """How much wall-clock time one row of an exported series covers."""
    stamps: list[float] = []
    try:
        with open(csv_path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                raw = row.get("timestamp")
                if not raw:
                    continue
                try:
                    stamps.append(_dt.datetime.fromisoformat(str(raw)).timestamp())
                except ValueError:
                    stamps.append(_num(raw))
    except (OSError, csv.Error):
        return 0.0
    gaps = sorted(b - a for a, b in zip(stamps, stamps[1:]) if b > a)
    return gaps[len(gaps) // 2] if gaps else 0.0


def hold_ladder(bar_seconds: float) -> list[int]:
    """Time-exit choices, in bars, for a series at this resolution.

    The bridge's choices are counts of bars, so what they mean depends entirely
    on how long a bar is. Raising series resolution cuts bar duration and
    silently turns "hold up to 100 bars" from seventeen hours into four -
    every strategy starts timing out before its target can be reached. A
    holding period is a span of time, so it is expressed as one here and
    converted per series.
    """
    if bar_seconds <= 0:
        return [0, 10, 25, 50, 100]          # unknown cadence: leave as-is
    wanted = (1800.0, 7200.0, 21600.0, 86400.0)     # 30m, 2h, 6h, 24h
    bars = sorted({max(2, int(round(s / bar_seconds))) for s in wanted})
    return [0] + bars                        # 0 = no time exit


def median_spread(csv_path: Path) -> float:
    """Typical bid-ask spread in an exported series, in dollars per share.

    This is the real cost of trading a prediction market, and it is large:
    measured across the captured series it runs about $0.010 on a $0.51 share,
    a full 2% one way and 4% on a round trip.
    """
    return median_of(csv_path, "spread")


def effective_spread(measured: float, assumed: float) -> tuple[float, bool]:
    """The spread to charge, and whether it was measured. -> (spread, measured?)

    A series that cannot report its own spread is not a free one. Historical
    series are rebuilt from the trade tape, and no historical order book exists
    to rebuild the quotes from, so every row reports a spread of zero. Charging
    what the data says charges nothing, and a round trip costed at zero is not
    a cheap trade - it is a fictional one.
    """
    if measured > 0:
        return measured, True
    return max(0.0, assumed), False


def exit_ladders(spread: float, price: float) -> tuple[list[float], list[float]]:
    """Stop and target choices for one token, as percentages. -> (stops, targets)

    The bridge ships futures-scale exits - 0.05% to 0.6%. On a $0.51 share that
    is a quarter of a tick to three ticks, against a spread of ten. Every target
    the search could express was smaller than the cost of crossing the book, so
    no strategy it could find was tradeable even in principle: the profits were
    entirely the spread the backtest never charged.

    Exits therefore scale with the spread, because that is what they have to
    clear. A target below one round trip is not an ambitious target, it is a
    guaranteed loss.
    """
    price = max(float(price), 0.01)
    # Callers resolve an unmeasurable spread through `effective_spread` before
    # getting here, so a zero at this point is a programming error rather than
    # a free market. One tick keeps the ladder finite if one slips through.
    spread = max(float(spread), 0.001)
    round_trip = spread / price * 100.0        # cost of in-and-out, in percent
    # Stops from one round trip out to four: tighter than the spread and the
    # bid-ask bounce alone closes the position.
    stops = [round(round_trip * m, 4) for m in (1.0, 1.5, 2.5, 4.0)]
    # Targets must beat the round trip to make money at all.
    targets = [round(round_trip * m, 4) for m in (1.5, 2.0, 3.0, 5.0)]
    return stops, targets


def research_equity(config: Config) -> tuple[float, str]:
    """The account size discovery must size against. -> (equity, source).

    Research that sizes to a notional account is not research about this
    account. The bridge's futures default is $50,000 and this module used to
    pin $10,000; the real book here is $100. Every share count, expectancy,
    drawdown and rejection recorded under the wrong number is wrong by the
    ratio between them — a strategy is not rejected for losing $11,001, it is
    rejected for losing $110 a hundred times over, and only one of those two
    sentences is about a real account.

    Resolution order, most specific first:

    1. ``research.account_equity`` when set — an explicit "research a
       $5,000 account" instruction, and the only case where a notional
       figure is the correct answer.
    2. The engine's last recorded portfolio value, live or paper. This is
       the account as it actually stands, including whatever it has made or
       lost since it started, so research follows the book rather than its
       opening balance.
    3. ``mode.paper_starting_balance`` — the configured bankroll, which is
       what a bot that has not completed a cycle yet is going to trade.

    Whatever comes back is floored at ``research.min_account_equity``: below
    that the ladder degenerates (see :func:`position_ladder`) and a silently
    degenerate search is worse than an explicit floor.
    """
    floor = max(0.01, float(config.research.min_account_equity))

    explicit = float(getattr(config.research, "account_equity", 0.0) or 0.0)
    if explicit > 0:
        return max(explicit, floor), "research.account_equity"

    # The journal is the engine's own record of where the book stood. It is
    # read-only here and entirely optional: a fresh install has no cycles
    # table yet, and research must not depend on the bot having run.
    try:
        journal = config.journal_path
        if Path(journal).exists():
            import sqlite3
            conn = sqlite3.connect(f"file:{journal}?mode=ro", uri=True)
            try:
                row = conn.execute(
                    "SELECT portfolio_value FROM cycles "
                    "WHERE portfolio_value > 0 ORDER BY ts DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()
            if row and float(row[0]) > 0:
                live = float(row[0])
                source = ("live portfolio value" if config.mode.live
                          else "paper portfolio value")
                return max(live, floor), source
    except Exception:                                     # noqa: BLE001
        # A missing table, a locked database, an older schema — none of them
        # are reasons to fail a research pass. Fall through to the config.
        pass

    configured = float(config.mode.paper_starting_balance or 0.0)
    if configured > 0:
        return max(configured, floor), "mode.paper_starting_balance"
    return floor, "research.min_account_equity (no account figure found)"


def position_ladder(price: float, equity: float,
                    fraction: float) -> tuple[list[int], int]:
    """Share counts discovery may choose from, and the cap. -> (ladder, max).

    The bridge sizes in whole "contracts" and its default ladder is [1, 2, 3,
    5] - a futures position, where one contract controls thousands of dollars.
    One Polymarket share costs under a dollar, so that same ladder puts about
    $2 to work on a $10,000 account: every strategy then returns a fraction of
    a basis point and every equity curve is flat to the pixel. That is not the
    rule failing, it is the size being wrong.

    So size the way the live engine does - a share of the portfolio, per
    `engine.portfolio.max_position_fraction` - and let discovery pick among
    fractions of it rather than among literal counts.

    On a real $100 book the ladder stays usable but gets SHORT: at 25% per
    position a $0.95 share affords 26 of them, so the rungs are 3/6/13/26 and
    the bottom rung is $2.85. Nothing collapses to zero — every rung is
    floored at one share — but duplicate rungs are folded away, so a small
    enough account yields a two- or one-rung ladder and discovery loses size
    as a search dimension. That is a true statement about the account rather
    than a bug, and `research.min_account_equity` is where the line is drawn;
    below it research refuses to pretend.
    """
    # A near-zero price is a token nobody believes in; flooring it keeps the
    # share count finite rather than letting 0.0001 ask for ten million shares.
    price = max(float(price), 0.01)
    top = int(equity * max(0.0, min(1.0, fraction)) / price)
    top = max(1, top)
    ladder = sorted({max(1, top // 8), max(1, top // 4),
                     max(1, top // 2), top})
    return ladder, top


def _bridge_overrides(token_dir: Path, out_dir: Path, cfg: Config,
                      price: float = 0.0, spread: float = 0.0,
                      bar_seconds: float = 0.0) -> dict[str, Any]:
    """Point the bridge at our data, and disable what does not apply here.

    The bridge's prop-firm constraints — contract counts, 10:1 micro scaling,
    end-of-day trailing drawdown, a 10-second minimum hold — describe a futures
    prop account. Polymarket has no contracts, no trading day and no scaling
    rules, and its own risk envelope is the portfolio-doubling rule, which lives
    in :mod:`pqb.doubling` and is enforced on the live path. Leaving them on
    would have discovery reject strategies for violating rules that do not exist
    here, so they are opened up and the reason is recorded in the manifest.
    """
    equity, _source = research_equity(cfg)
    ladder, max_shares = position_ladder(
        price, equity, cfg.engine.portfolio.max_position_fraction)
    stops, targets = exit_ladders(spread, price)
    holds = hold_ladder(bar_seconds)
    return {
        "dataset.data_dir": str(token_dir),
        "dataset.price_column": "price",
        "dataset.timestamp_column": "timestamp",
        "output.strategies_dir": str(out_dir / "strategies"),
        "output.reports_dir": str(out_dir / "reports"),
        # One "contract" is one share of an outcome token, worth its price.
        "instrument.point_value": 1.0,
        # The cost of trading here is the spread, not a commission. The price
        # column is the ASK (pqb.features), so an uncharged backtest buys and
        # sells at the same side of the book and pockets the spread it never
        # crossed. The bridge charges `commission_per_contract` on entry AND
        # exit, so half the spread each way charges one full round trip.
        "instrument.commission_per_contract": max(0.0, spread) / 2.0,
        # The exchange fee is FLAT per fill, and charging it per contract (as
        # this did while it was folded into the commission) makes it a fixed
        # PERCENTAGE at every account size — which is precisely the cost that
        # is supposed to hurt a small book more. Charged per fill, $0.01 is
        # 0.7% of a round trip on the $3 bottom rung of a $100 account and
        # 0.003% on the $600 rung of a $10,000 one. That difference is the
        # whole reason sizing to the real account matters.
        "instrument.fee_per_fill": max(
            0.0, float(cfg.engine.portfolio.fee_per_trade_usdc)),
        "account.starting_equity": equity,
        # Sized from this token's own price, not the futures default - see
        # `position_ladder`. Without this every curve is flat by construction.
        "discovery.position_contracts_choices": ladder,
        # Exits scaled to what they must clear - see `exit_ladders`.
        "discovery.stop_pct_choices": stops,
        "discovery.target_pct_choices": targets,
        # Holding periods in wall-clock time, converted to this series' bars -
        # see `hold_ladder`. A bar count means nothing without a bar duration.
        "discovery.time_exit_bars_choices": holds,
        # Key names below are exactly those PropConstraints.from_config reads.
        # A near-miss here does not error — it silently leaves the futures
        # default in force, and a 1.5% account-drawdown halt on a prediction
        # market stops the backtest before it has taken a position.
        "prop_constraints.min_hold_seconds": 0.0,
        "prop_constraints.max_hold_seconds": 0.0,        # 0 = disabled
        "prop_constraints.eod_max_drawdown": 0.0,        # 0 = disabled
        "prop_constraints.max_position_contracts": max_shares,
        "prop_constraints.per_trade_max_dd_pct": 100.0,
        "prop_constraints.account_max_dd_pct": 100.0,
        "prop_constraints.min_win_rate": 0.0,
        # These two have NO "0 = disabled" sentinel: the backtester caps the
        # day as soon as `daily_pnl >= daily_profit_cap`, so a cap of zero is
        # satisfied on the first bar and blocks every entry for the whole run.
        # Disabling them means putting them out of reach, not zeroing them.
        "prop_constraints.daily_profit_cap": _UNREACHABLE,
        "prop_constraints.daily_profit_target": _UNREACHABLE,
    }


def _oos_context(bridge, token_dir: Path, out_dir: Path, config: Config,
                 price: float = 0.0, spread: float = 0.0,
                 bar_seconds: float = 0.0):
    """Phases 1-2 ONLY for a held-out token: data + engineered features.

    No discovery, no threshold fitting, no ranking ever touches this data —
    it exists solely so a FROZEN rule can be replayed against it. The same
    realistic costs charged in discovery are charged here: OOS may be harsher
    than discovery, never kinder.
    """
    cfg = bridge.config(_bridge_overrides(token_dir, out_dir, config,
                                          price, spread, bar_seconds))
    engine = bridge.ResearchEngine(cfg, logger=lambda _m: None)
    engine.run_data()
    engine.run_features()
    instr = cfg.section("instrument") if hasattr(cfg, "section") else {}
    acct = cfg.section("account") if hasattr(cfg, "section") else {}
    equity, _source = research_equity(config)
    return {
        "fs": engine.feature_set,
        "constraints": engine.constraints,
        "point_value": float(instr.get("point_value", 1.0)),
        # The replay must book P&L on the SAME account discovery searched
        # against. A rule discovered on a $100 book and replayed on a
        # $10,000 one is not the same rule: the prop-constraint drawdown
        # limits are dollar figures derived from starting equity, so the
        # holdout would enforce a different risk envelope than discovery.
        "starting_equity": float(acct.get("starting_equity", equity)),
        "fee_per_fill": float(instr.get("fee_per_fill", 0.0)),
    }


def _frozen_run(context: dict, rule: dict, fee: float):
    """One frozen rule against one unseen token. Returns per-market OOS stats.

    The rule dict is used EXACTLY as discovered — same thresholds, same
    stops, same sizing, and now the same ACCOUNT and the same cost model.

    Costs used to be assembled differently here than in discovery: this
    function took `max(flat_fee, spread/2)` as the per-contract commission
    while `_bridge_overrides` used `spread/2`, so with the default $0.01 fee
    and the 1c assumed spread the holdout charged double what discovery did
    on the per-share leg. "OOS may be harsher, never kinder" permitted that,
    but the two legs are different costs — crossing the book scales with
    size, an exchange fee does not — and adding them together made both
    wrong. They are now charged separately and identically on both sides:
    `spread/2` per contract per fill, plus the flat fee once per fill.
    """
    from core.backtester import Backtester, Strategy   # bridge path is loaded

    fs = context["fs"]
    bt = Backtester(features=fs.frame, price=fs.price,
                    constraints=context["constraints"],
                    point_value=context["point_value"],
                    commission=max(0.0, float(fee)),
                    starting_equity=float(
                        context.get("starting_equity") or 10_000.0),
                    fee_per_fill=float(context.get("fee_per_fill") or 0.0))
    result = bt.run(Strategy(**rule))
    pnl = [float(x) for x in getattr(result, "trade_pnl", [])]
    if not pnl:
        return {"trades": 0}
    equity_run = 0.0
    peak = 0.0
    drawdown = 0.0
    for x in pnl:
        equity_run += x
        peak = max(peak, equity_run)
        drawdown = max(drawdown, peak - equity_run)
    mean = sum(pnl) / len(pnl)
    if len(pnl) >= 10:
        var = sum((x - mean) ** 2 for x in pnl) / (len(pnl) - 1)
        sharpe = (mean / var ** 0.5 * len(pnl) ** 0.5) if var > 0 else 0.0
    else:
        sharpe = 0.0
    return {"trades": len(pnl), "wins": sum(1 for x in pnl if x > 0),
            "pnl": sum(pnl), "expectancy": mean, "drawdown": drawdown,
            "sharpe": sharpe}


@dataclass
class ResearchResult:
    tokens_researched: int = 0
    tokens_exported: int = 0
    candidates: int = 0
    accepted: int = 0
    strategies: list[DiscoveredStrategy] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_reason: str = ""
    # The discovery funnel: population at every stage, in pipeline order,
    # plus the first stage that went to zero and why. This is what turns
    # "14,000 trades and nothing on the board" from a mystery into a
    # sentence — the answer must be readable, never re-derived.
    funnel: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "tokensExported": self.tokens_exported,
            "tokensResearched": self.tokens_researched,
            "candidates": self.candidates,
            "accepted": self.accepted,
            "strategies": [s.to_dict() for s in self.strategies],
            "errors": self.errors,
            "skippedReason": self.skipped_reason,
            "funnel": self.funnel,
        }



# -- the adversarial probe ----------------------------------------------------

class ReplayProbe(adversarial.Probe):

    # Rule families whose replay is "enter, hold N bars, exit" against a
    # price series. Only these can be compared against a random entry of
    # the same shape. Longshots and wallet-state rules resolve at
    # settlement rather than after a hold, so a random-entry control
    # would be answering a different question; they decline and say so.
    _HELD = ("sequence", "sharp_move", "wallet_behavior")

    DRAWS = 200          # permutation draws; 200 resolves p to ~0.005
    P_FAIL = 0.10        # random beats real this often => not a signal
    MIN_MARKETS = 2
    MIN_TRADES = adversarial.MIN_TRADES_TO_ATTACK

    def __init__(self, pool: dict, charge, volumes, cfg):
        self.pool = pool          # market_id -> pool item (csv, token, ...)
        self.charge = charge      # market item -> per-share cost of one fill
        self.volumes = volumes    # [market_id] -> {market_id: traded value}
        self.cfg = cfg
        self._rows: dict[str, list] = {}

    def _series(self, market_id: str) -> list:
        if market_id not in self._rows:
            from .analytics import sequences as seq_mod
            item = self.pool[market_id]
            try:
                self._rows[market_id] = seq_mod.rows_from_csv(
                    item["csv"])
            except Exception:                         # noqa: BLE001
                self._rows[market_id] = []
        return self._rows[market_id]

    def placebo(self, entry, cumulative, ledger):
        """Random entries, same markets, same count, same hold.

        The control the whole battery was missing. Every other test asks
        WHERE the edge holds; this one asks whether the entry condition
        is doing anything at all. A chain that fires before a 15-bar hold
        in a market that drifted up will show a positive expectancy with
        no predictive content whatsoever, and nothing else here would
        catch it — leave-one-out, temporal split and dispersion would all
        report a broad, stable, replicated edge, because the drift is
        broad, stable and replicated.

        Comparing against the candidate's OWN per-market trade counts
        matters: drawing a flat number of entries per market would let a
        market where the rule rarely fired dominate the control and make
        any candidate look significant.
        """
        rule = entry.get("rule") or {}
        rtype = str(rule.get("type") or "threshold")
        if rtype not in self._HELD:
            return adversarial.NOT_RUN, (
                f"{rtype} rules do not enter-and-hold; a random-entry "
                "control would ask a different question")
        hold = int(rule.get("hold_bars") or 0)
        direction = str(rule.get("direction") or "up")
        if hold <= 0:
            return adversarial.NOT_RUN, "no hold length on the rule"

        # Only markets that are BOTH in this candidate's evidence and in
        # the current pool can be controlled against.
        usable = [(r, self._series(str(r["market_id"])))
                  for r in ledger
                  if int(r.get("trades") or 0) > 0
                  and str(r["market_id"]) in self.pool]
        usable = [(r, rows) for r, rows in usable if len(rows) > hold + 2]
        trades = sum(int(r.get("trades") or 0) for r, _ in usable)
        if len(usable) < self.MIN_MARKETS or trades < self.MIN_TRADES:
            return adversarial.NOT_RUN, (
                f"only {trades} trade(s) over {len(usable)} market(s) "
                "still in the pool")

        actual = float(cumulative.get("expectancy") or 0.0)
        # Seeded from the candidate id, so the same candidate gets the
        # same control on every pass (§14). An unseeded draw would make
        # this the one test whose verdict could be re-rolled.
        rng = _random.Random(
            int(hashlib.sha1(str(entry.get("id") or "")
                             .encode()).hexdigest()[:12], 16))
        beat = 0
        for _ in range(self.DRAWS):
            pnl = 0.0
            count = 0
            for row, rows in usable:
                cost = self.charge(self.pool[str(row["market_id"])])
                price = [x.get("price", 0.0) for x in rows]
                top = len(price) - hold - 1
                for _ in range(int(row.get("trades") or 0)):
                    i = rng.randint(0, top)
                    if price[i] <= 0:
                        continue
                    move = price[i + hold] - price[i]
                    pnl += (move if direction == "up" else -move) \
                        - cost * 2.0
                    count += 1
            if count and (pnl / count) >= actual:
                beat += 1
        p = (beat + 1) / (self.DRAWS + 1)
        detail = (f"random entries of the same shape beat {actual:+.4f} "
                  f"in {beat}/{self.DRAWS} draws (p={p:.3f}) across "
                  f"{len(usable)} market(s)")
        if p > self.P_FAIL:
            return adversarial.FAILED, detail + " - the hold, not the signal"
        return adversarial.SURVIVED, detail

    def liquidity_stress(self, entry, cumulative, ledger):
        """Does the edge survive where the book is actually thin?

        Not a re-run of `cost_stress`, which charges everyone more. This
        splits the candidate's own evidence markets at their median
        spread and asks where the P&L came from. The failure it catches
        is specific and nasty: an edge that exists ONLY in the widest
        books is an edge you cannot take, and it is exactly the shape a
        mis-modelled fill produces — the thinner the market, the more the
        replay's assumed spread flatters it.
        """
        rows = [r for r in ledger
                if int(r.get("trades") or 0) > 0
                and str(r["market_id"]) in self.pool]
        if len(rows) < adversarial.MIN_MARKETS_FOR_SUBSETS:
            return adversarial.NOT_RUN, (
                f"only {len(rows)} evidence market(s) still in the pool")

        # ONE depth metric for the whole split, never a mixture: ranking
        # some markets by spread and others by volume would produce an
        # ordering that means nothing. Spread is the more direct measure
        # and is preferred when every market can report one — but only
        # 13% of the pool can, because series rebuilt from the trade tape
        # have no order book to quote from. Traded value is the honest
        # fallback: it is available everywhere and it is what actually
        # decides whether a position can be taken at size.
        spreads = {}
        for row in rows:
            market_id = str(row["market_id"])
            measured, was_measured = effective_spread(
                median_spread(self.pool[market_id]["csv"]),
                self.cfg.assumed_spread)
            if was_measured:
                # Negated so that "larger key" means "thinner book" for
                # both metrics and the sort below reads the same way.
                spreads[market_id] = measured

        if len(spreads) == len(rows):
            metric, basis = spreads, "spread"
        else:
            volumes = self.volumes(
                [str(r["market_id"]) for r in rows])
            traded = {m: v for m, v in volumes.items() if v > 0}
            if len(traded) < adversarial.MIN_MARKETS_FOR_SUBSETS:
                return adversarial.NOT_RUN, (
                    f"{len(spreads)}/{len(rows)} market(s) quote a "
                    f"spread and {len(traded)} report traded value - "
                    "no single depth metric covers this evidence")
            # Low volume = thin, so negate to keep the ordering.
            metric = {m: -v for m, v in traded.items()}
            basis = "traded value"

        ranked = sorted((r for r in rows
                         if str(r["market_id"]) in metric),
                        key=lambda r: metric[str(r["market_id"])])
        mid = len(ranked) // 2
        deep = ranked[:mid]      # tightest books / most traded
        thin = ranked[mid:]      # widest books / least traded
        e_deep, t_deep = adversarial._expectancy(deep)
        e_thin, t_thin = adversarial._expectancy(thin)
        if min(t_deep, t_thin) < 3:
            return adversarial.NOT_RUN, (
                f"a depth half is too thin ({t_deep} / {t_thin} trades)")
        detail = (f"by {basis}: deep half {e_deep:+.4f} ({t_deep} "
                  f"trades) vs thin half {e_thin:+.4f} ({t_thin} trades)")
        if e_deep <= 0 < e_thin:
            return adversarial.FAILED, (
                detail + " - the edge is only where you cannot get filled")
        if e_deep <= 0 and e_thin <= 0:
            return adversarial.FAILED, detail + " - negative in both halves"
        if e_thin <= 0 < e_deep:
            # The GOOD asymmetry, and worth saying plainly: it pays where
            # it is takeable. Still only inconclusive, because half the
            # record just went negative and that is not a clean pass.
            return adversarial.INCONCLUSIVE, (
                detail + " - holds only in takeable books")
        return adversarial.SURVIVED, detail


def run(config: Config, store: IntelStore,
        log: Optional[Callable[[str], None]] = None,
        min_rows: int = 200, max_tokens: int = 12,
        min_tokens: int = 2) -> ResearchResult:
    """Export, research each token, and keep what held up on more than one."""
    say = log or (lambda _m: None)
    result = ResearchResult()

    try:
        bridge = load_bridge()
    except QuantBridgeNotFound as exc:
        result.skipped_reason = str(exc)
        say(f"  {exc}")
        return result

    root = config.data_dir / "research"
    exports = root / "exports"
    lo = config.research.uncertain_min_price
    hi = config.research.uncertain_max_price
    funnel = result.funnel
    try:
        stats = store.stats()
        funnel["rawTrades"] = int(stats.get("trades") or 0)
        funnel["settledMarkets"] = int(stats.get("resolutions") or 0)
    except Exception:                                    # noqa: BLE001
        pass
    # Say the account size out loud before anything is searched. Every
    # number this pass produces — expectancy, drawdown, the P&L that decides
    # a rejection — is denominated in it, and a pass that quietly sized to
    # the wrong account is indistinguishable from one that sized to the
    # right one until someone divides by a hundred.
    pass_equity, equity_source = research_equity(config)
    funnel["accountEquity"] = pass_equity
    funnel["accountEquitySource"] = equity_source
    say(f"  sizing this pass to ${pass_equity:,.2f} ({equity_source}); "
        f"at most {config.engine.portfolio.max_position_fraction:.0%} of it "
        "per position")

    say(f"[1/4] Exporting captured feature series -> {exports}")
    say(f"  studying only the {lo:g}-{hi:g} price band, where the outcome was "
        "still in doubt")
    written = export_token_series(store, exports, min_rows=min_rows,
                                  max_tokens=max_tokens, log=say,
                                  lo=lo, hi=hi)
    result.tokens_exported = len(written)

    # HISTORY, not just live capture. The backfilled tapes of already-settled
    # markets replay into research series that are weeks deep with known
    # outcomes — the study of wallet history the whole bridge exists for. Live
    # capture then keeps extending forward what history seeds.
    say("[1b] Exporting HISTORICAL series from settled markets' trade tapes")
    historical = export_historical_series(store, exports, min_rows=min_rows,
                                          max_tokens=max_tokens, log=say,
                                          lo=lo, hi=hi)
    written = written + historical
    result.tokens_exported = len(written)
    funnel["seriesExported"] = len(written)
    try:                       # what the band excluded, from the manifest
        manifest = json.loads((exports / MANIFEST).read_text(encoding="utf-8"))
        funnel["rowsTrimmedAsDecided"] = int(
            manifest.get("rowsTrimmedAsDecided") or 0)
        funnel["seriesDroppedAsDecided"] = int(
            manifest.get("tokensDroppedAsDecided") or 0)
    except Exception:                                    # noqa: BLE001
        pass

    if not written:
        result.skipped_reason = (
            f"No series has {min_rows}+ rows yet, live or historical. Run "
            "`backfill --markets 300 --trades 2000` for deeper history, or run "
            "the bridge longer for live capture.")
        say(f"  {result.skipped_reason}")
        return result

    # TRUE out-of-sample: the NEWEST markets are held out before any search.
    # Discovery never reads them; candidates are frozen and replayed against
    # them afterwards. Disjoint markets AND later in time, in one split.
    discovery_markets, oos_markets = split_markets_by_date(
        written, config.research.oos_fraction)
    discovery_entries = [e for e in written
                         if str(e.get("marketId") or e.get("tokenId"))
                         in discovery_markets]
    oos_entries = [e for e in written
                   if str(e.get("marketId") or e.get("tokenId"))
                   in oos_markets]
    say(f"[2/4] Researching {len(discovery_entries)} discovery series "
        f"({len(oos_entries)} newest series HELD OUT untouched)")
    say(f"  charging the spread as the cost of trading; series that cannot "
        f"report one are charged the assumed "
        f"${config.research.assumed_spread:.4f}, not zero")

    per_token: list[tuple[str, list]] = []
    market_of: dict[str, str] = {}
    assumed_for = 0
    for entry in discovery_entries:
        token_id = entry["tokenId"]
        market_of[token_id] = str(entry.get("marketId")
                                  or entry.get("market_id") or token_id)
        token_dir = Path(entry["path"]).parent
        out_dir = root / "out" / _safe_name(token_id)
        csv_path = Path(entry["path"])
        price = median_price(csv_path)
        spread, was_measured = effective_spread(
            median_spread(csv_path), config.research.assumed_spread)
        if not was_measured:
            assumed_for += 1
        bar_seconds = median_bar_seconds(csv_path)
        try:
            cfg = bridge.config(_bridge_overrides(token_dir, out_dir, config,
                                                  price, spread, bar_seconds))
            engine = bridge.ResearchEngine(cfg, logger=lambda m: say(f"    {m}"))
            engine.run_full(top_n_validate=40, top_n_export=5)
        except Exception as exc:                        # noqa: BLE001
            # One token's series failing is not the run failing. A token whose
            # book never moved produces a degenerate price series, and that
            # should cost that token rather than the whole research pass.
            message = f"{token_id[:14]}…: {type(exc).__name__}: {exc}"
            result.errors.append(message)
            say(f"    ! {message}")
            continue
        result.tokens_researched += 1
        per_token.append((token_id, list(engine.ranked)))

    if assumed_for:
        say(f"  {assumed_for} of {len(discovery_entries)} series had no "
            "order-book data; their cost is the assumed spread, so their "
            "results are weaker evidence than the live-captured ones")

    funnel["discoverySeries"] = len(discovery_entries)
    funnel["holdoutSeries"] = len(oos_entries)
    funnel["seriesResearched"] = result.tokens_researched
    funnel["rankedCandidates"] = sum(len(r) for _, r in per_token)
    # Informational only — bridge acceptance is futures-grade and no longer
    # gates research (see aggregate()); the count stays visible so a future
    # reader can see how harsh it is here.
    funnel["bridgeAccepted"] = sum(
        1 for _, reports in per_token for r in reports
        if getattr(r, "accepted", False))

    say("[3/4] Cross-checking rules across discovery tokens")
    result.strategies = aggregate(per_token, min_tokens=min_tokens)
    result.candidates = sum(len(r) for _, r in per_token)
    funnel["crossMarketCandidates"] = len(result.strategies)

    # ---- [3b] SEQUENTIAL / EVENT-CHAIN discovery (additive) ----------------
    # The second question, asked of the SAME discovery data: does the order
    # and timing of events predict what single events do not? Survivors are
    # ordinary candidates; the funnel explains every death.
    sequence_candidates: list[DiscoveredStrategy] = []
    if config.research.seq_enabled:
        from .analytics import sequences as seq_mod

        seq_series = []
        for entry in discovery_entries:
            rows = seq_mod.rows_from_csv(entry["path"])
            if rows:
                seq_series.append(
                    (str(entry.get("marketId") or entry.get("tokenId")),
                     rows))
        mined = seq_mod.mine(
            seq_series,
            max_len=config.research.seq_max_len,
            gap_bars=config.research.seq_gap_bars,
            hold_bars=config.research.seq_hold_bars,
            min_occurrences=config.research.seq_min_occurrences,
            min_markets=min(2, max(1, len(seq_series))),
            cost=config.research.assumed_spread,
            top_n=config.research.seq_register_top)
        funnel["sequences"] = mined["funnel"]
        say(f"[3b] Event chains: {mined['funnel']['eventsObserved']} events "
            f"({mined['funnel']['eventTypes']} kinds) -> "
            f"{mined['funnel']['chainsGenerated']} chains -> "
            f"{mined['funnel']['sufficientSample']} sampled -> "
            f"{mined['funnel']['netPositive']} net-positive -> "
            f"{mined['funnel']['kept']} kept (incremental over components)")
        for reason, count in list(
                mined["funnel"].get("rejectReasons", {}).items())[:3]:
            say(f"    chains rejected: {count} x {reason}")
        for candidate in mined["candidates"]:
            strategy = DiscoveredStrategy(
                rule=candidate, signature=signature_of(candidate),
                describe=seq_mod.describe(candidate),
                score=float(candidate.get("netExpectancy", 0.0)),
                trades=int(candidate.get("occurrences", 0)))
            sequence_candidates.append(strategy)

    # ---- [3c] SHARP-MOVE / CRASH-RECOVERY research (additive) --------------
    # Dislocation as a hypothesis: recovery, continuation, or nothing —
    # discovered per condition against a no-event drift control, never
    # assumed. Survivors are ordinary library candidates.
    if config.research.sharp_enabled:
        from .analytics import sequences as seq_mod2
        from .analytics import sharp_moves as sharp_mod

        sharp_series = []
        for entry in discovery_entries:
            rows = seq_mod2.rows_from_csv(entry["path"])
            if rows:
                sharp_series.append(
                    (str(entry.get("marketId") or entry.get("tokenId")),
                     rows))
        studied = sharp_mod.study(
            sharp_series,
            hold_bars=config.research.sharp_hold_bars,
            cost=config.research.assumed_spread,
            min_events=config.research.sharp_min_events,
            min_markets=min(2, max(1, len(sharp_series))),
            top_n=config.research.sharp_register_top)
        funnel["sharpMoves"] = studied["funnel"]
        sf = studied["funnel"]
        say(f"[3c] Sharp moves: {sf['sharpMovesDetected']} detected -> "
            f"{sf['usableEvents']} measured -> {sf['conditionCells']} "
            f"condition cells -> {sf['cellsWithSample']} sampled -> "
            f"{sf['netPositive']} beat costs AND drift -> "
            f"{sf['kept']} kept")
        if sf.get("responseClasses"):
            say("    responses: " + ", ".join(
                f"{v} {k}" for k, v in
                list(sf["responseClasses"].items())[:4]))
        for reason, count in list(sf.get("rejectReasons", {}).items())[:3]:
            say(f"    cells rejected: {count} x {reason}")
        for candidate in studied["candidates"]:
            strategy = DiscoveredStrategy(
                rule=candidate, signature=signature_of(candidate),
                describe=sharp_mod.describe(candidate),
                score=float(candidate.get("netExpectancy", 0.0)),
                trades=int(candidate.get("events", 0)))
            sequence_candidates.append(strategy)

    # ---- [3d] MILITARY-ATTACK LONGSHOT calibration (additive) --------------
    # The reported 52% is a hypothesis: re-derived from the settled tapes
    # with entry-time information only, against same-priced non-military
    # controls, clustered by geopolitical event. Honest "insufficient
    # sample" is the expected early answer.
    if config.research.longshot_enabled:
        from .analytics import longshot as longshot_mod

        shot = longshot_mod.study(
            store, cost=config.research.assumed_spread
            + float(config.engine.portfolio.fee_per_trade_usdc),
            min_events=config.research.longshot_min_events,
            top_n=config.research.longshot_register_top)
        funnel["longshot"] = shot["funnel"]
        lf = shot["funnel"]
        say(f"[3d] Longshot calibration: {lf['settledTapes']} settled tapes "
            f"-> {lf['observations']} entry-honest observations "
            f"({lf['militaryMarkets']} military markets, "
            f"{lf['controlMarkets']} control) -> {lf['kept']} kept")
        for reason, count in list(lf.get("rejectReasons", {}).items())[:3]:
            say(f"    cells rejected: {count} x {reason}")
        for candidate in shot["candidates"]:
            strategy = DiscoveredStrategy(
                rule=candidate, signature=signature_of(candidate),
                describe=longshot_mod.describe(candidate),
                score=float(candidate.get("netExpectancy", 0.0)),
                trades=int(candidate.get("events", 0)))
            sequence_candidates.append(strategy)

    # ---- [3e] WALLET BEHAVIORAL STATES (the operator's RN1 model) ----------
    # A tracked wallet's buy is not always a prediction: the candidate
    # signal is a high first entry followed by checkpoint-frozen one-sided
    # commitment. Re-derived per wallet from our tapes; every cell must
    # beat blindly copying that wallet.
    if config.research.wallet_state_enabled:
        from .analytics import wallet_states as wstate_mod

        targets = [w.address for w in config.wallets if w.address]
        targets += [str(a) for a in
                    config.research.wallet_state_targets if a]
        try:
            ranked = store.query(
                "SELECT wallet FROM wallet_scores WHERE rank > 0 "
                "ORDER BY rank LIMIT 3")
            targets += [str(r["wallet"]) for r in ranked]
        except Exception:                                # noqa: BLE001
            pass
        targets = list(dict.fromkeys(t.lower() for t in targets))[:5]
        states = wstate_mod.study(
            store, targets,
            cost=config.research.assumed_spread
            + float(config.engine.portfolio.fee_per_trade_usdc),
            premium=config.research.wallet_state_max_premium,
            min_markets=config.research.wallet_state_min_markets,
            top_n=config.research.wallet_state_register_top)
        funnel["walletStates"] = states["funnel"]
        wf = states["funnel"]
        say(f"[3e] Wallet states: {len(wf.get('wallets', []))} wallet(s) -> "
            f"{wf.get('episodes', 0)} settled episodes -> "
            f"{wf.get('kept', 0)} kept")
        for reason, count in list(wf.get("rejectReasons", {}).items())[:3]:
            say(f"    cells rejected: {count} x {reason}")
        for candidate in states["candidates"]:
            strategy = DiscoveredStrategy(
                rule=candidate, signature=signature_of(candidate),
                describe=wstate_mod.describe(candidate),
                score=float(candidate.get("netExpectancy", 0.0)),
                trades=int(candidate.get("markets", 0)))
            sequence_candidates.append(strategy)

    # ---- [3f] WALLET BEHAVIORAL DISCOVERY (operator's spec) ----------------
    # The third wallet question: not "which wallets look good" (ranking)
    # and not "does following THIS wallet work" (wallet states), but WHAT
    # repeating behavior explains the results — extracted as an explicit,
    # wallet-free market rule and tested where the wallet never traded.
    # The wallet is the source of hypotheses; it is never the proof.
    if config.research.wallet_behavior_enabled:
        from .analytics import wallet_behavior as wbehav_mod

        pinned_wallets = [w.address for w in config.wallets if w.address]
        pinned_wallets += [str(a) for a in
                           config.research.wallet_state_targets if a]
        behavior = wbehav_mod.study(
            store, pinned=pinned_wallets,
            cost=config.research.assumed_spread
            + float(config.engine.portfolio.fee_per_trade_usdc),
            min_trades=config.research.wallet_behavior_min_trades,
            min_markets=config.research.wallet_behavior_min_markets,
            min_repeat=config.research.wallet_behavior_min_repeat,
            min_wallet_trades=config.research.wallet_behavior_min_wallet_trades,
            max_wallets=config.research.wallet_behavior_max_wallets,
            top_n=config.research.wallet_behavior_register_top)
        funnel["walletBehavior"] = behavior["funnel"]
        bf = behavior["funnel"]
        # The Wallets screen's research overlay: per-wallet behavioral
        # fingerprints keyed by address. Written beside the other view
        # files so the dashboard reads persisted research rather than
        # recomputing it — and so the authoritative wallet_scores table
        # stays exactly as the ranking layer wrote it.
        try:
            (config.data_dir / "wallet_research.json").write_text(
                json.dumps({"wallets": behavior.get("perWallet") or {},
                            "twoSided": bf.get("twoSided") or {},
                            "ts": time.time()}), encoding="utf-8")
        except OSError:
            pass
        pooled_verdict = str((bf.get("twoSided") or {}).get("verdict") or "")
        if pooled_verdict:
            say(f"    two-sided test: {pooled_verdict}")
        say(f"[3f] Wallet behavior: {bf.get('walletsConsidered', 0)} "
            f"wallet(s) considered, {bf.get('walletsEligible', 0)} eligible "
            f"-> {bf.get('settledObservations', 0)} settled observations "
            f"-> {bf.get('cellsFormed', 0)} behavior cells -> "
            f"{bf.get('kept', 0)} kept "
            f"({bf.get('multiWallet', 0)} multi-wallet, "
            f"{bf.get('duplicatesMerged', 0)} duplicate(s) merged)")
        if bf.get("switchObservations"):
            say(f"    side switches: {bf.get('switchObservations', 0)} "
                f"reconstructed -> {bf.get('switchCells', 0)} conditional "
                f"pattern(s) -> {bf.get('keptSwitch', 0)} kept as "
                "hypotheses")
        for reason, count in list(bf.get("rejectReasons", {}).items())[:3]:
            say(f"    cells rejected: {count} x {reason}")
        for candidate in behavior["candidates"]:
            strategy = DiscoveredStrategy(
                rule=candidate, signature=signature_of(candidate),
                describe=wbehav_mod.describe(candidate),
                score=float(candidate.get("source_net", 0.0)),
                win_rate=float(candidate.get("source_win_rate", 0.0)),
                trades=int(candidate.get("source_trades", 0)))
            sequence_candidates.append(strategy)

    # ---- [4/4] THE PERSISTENT LIBRARY: additive, never resetting -----------
    # The pass registers new candidates, then challenges the WHOLE library —
    # old strategies and new alike — against the untouched markets. Evidence
    # accumulates per independent market; statuses move gradually; nothing is
    # ever erased. An hourly pass means "learn more", never "start over".
    from .library import StrategyLibrary, next_status

    library = StrategyLibrary(config.data_dir / "library.sqlite3")
    fee = float(config.engine.portfolio.fee_per_trade_usdc)

    # EXPERIMENT MEMORY, opened before anything is registered because its
    # first job is to be consulted at the door: a family that three
    # independent candidates have already died in, the same way, does not get
    # its usual registration slots this pass. Opened even when disabled would
    # create the file for nothing, so the handle is None and every use is
    # guarded — the pass must run identically with the memory switched off.
    from .experiments import ExperimentStore, from_candidate

    exp_store = None
    dead_ends: dict[str, list[str]] = {}
    directives: dict[str, str] = {}
    if config.research.experiment_memory_enabled:
        try:
            exp_store = ExperimentStore(root / "experiments.sqlite3")
            dead_ends = exp_store.dead_ends()
            directives = exp_store.latest_directives()
            if directives:
                say(f"  experiment memory: {len(directives)} open research "
                    "directive(s) from previous failures")
            if dead_ends:
                say(f"  experiment memory: {len(dead_ends)} family(ies) "
                    "throttled after repeated identical failures")
        except Exception as exc:                          # noqa: BLE001
            # Research memory is an optimisation of the SEARCH. Losing it
            # must never fail a pass that is otherwise producing evidence.
            say(f"  experiment memory unavailable: {type(exc).__name__}: {exc}")
            exp_store = None

    # A library holding evidence from before the sizing fix cannot be
    # reasoned about: correctly-sized rows get summed together with rows
    # booked at ~100x, and the cumulative record — which is what every
    # promotion and rejection reads — stays wrong no matter how many good
    # passes follow. Say so loudly every pass until it is dealt with; do
    # NOT clear it here, because deleting a client's evidence as a silent
    # side effect of running research is not this function's decision.
    if library.sizing_epoch() <= 0:
        stale = library.evidence_summary()
        if stale["validations"]:
            say("  WARNING: this library holds "
                f"{stale['validations']} validation row(s) recorded before "
                "the sizing fix, at the wrong account size. They will be "
                "summed with correctly-sized evidence and nothing will "
                "promote. Run `pqb resize-library --yes` once, then re-run "
                "this pass.")
            funnel["staleSizedValidations"] = stale["validations"]

    # Historical verdicts meet today's rules. Two things re-open one: it
    # was made on a single market's evidence (one market can neither
    # validate nor reject), or every figure behind it was measured before
    # the sizing epoch, on an account a hundred times larger than the real
    # one. The second is what unlocks the delivered library, where the
    # breadth rule matched nothing at all. Idempotent, and guarded so a
    # re-opened verdict cannot cycle. See `library.reopen_rejections`.
    reopened = library.reopen_rejections(
        max_reopens=int(config.research.max_verdict_reopens))
    if reopened.get("breadth"):
        say(f"  {reopened['breadth']} single-market rejection(s) re-opened "
            "under the breadth-symmetric rule")
    if reopened.get("sizing"):
        say(f"  {reopened['sizing']} rejection(s) re-opened: their evidence "
            "predates the sizing fix and describes no real account")
    if reopened.get("held_by_guard"):
        say(f"  {reopened['held_by_guard']} rejection(s) held closed by the "
            "re-open ceiling; those verdicts stand")
    funnel["rejectionsReopened"] = (int(reopened.get("breadth", 0))
                                    + int(reopened.get("sizing", 0)))
    funnel["rejectionsHeldByGuard"] = int(reopened.get("held_by_guard", 0))

    # Every market this pass searched joins the (re)discovered rules'
    # permanent exclusion lists — those markets may never validate them,
    # even if a live market's growing lastTs later drifts it into the
    # newest-30% holdout of a future pass.
    pass_markets = {str(e.get("marketId") or e.get("tokenId"))
                    for e in discovery_entries}
    rows_before = len(library.all_strategies())

    # Convergent discovery (operator's §15): a wallet-derived hypothesis
    # that a NON-wallet pathway independently landed on is stronger
    # research signal. Recorded as metadata on the rule — prioritization
    # color, never validation.
    for candidate in sequence_candidates:
        if (candidate.rule or {}).get("type") == "wallet_behavior":
            from .analytics import wallet_behavior as wbehav_mod
            matched = wbehav_mod.convergences(
                candidate.rule, library.all_strategies())
            if matched:
                candidate.rule["convergent_with"] = matched[:8]
                say(f"    convergent discovery: {candidate.signature} "
                    f"independently matches {len(matched)} quant rule(s)")
    # THE VALIDATION DOMAIN, measured before anything is registered against
    # it. Built from the OOS pool's own CSVs — the exact data every candidate
    # will be judged on — so "this feature exists in validation data" is a
    # measurement rather than an assumption.
    feature_domain = validation_domain(root, say)
    funnel.update(feature_domain.summary())
    if not feature_domain.permissive:
        quarantined = quarantine_incompatible(library, feature_domain, say)
        funnel["quarantinedThisPass"] = len(quarantined)
        if quarantined:
            say(f"  {len(quarantined)} legacy candidate(s) quarantined: "
                "their features do not exist in validation data")

    fresh_ids: set[str] = set()
    # Duplicate-correlation control (the operator's correction): rules
    # register per FAMILY, capped per pass, and a family that unseen data
    # keeps refusing loses its slots — the library measures whether
    # phenomena are real, not how many spellings one idea has.
    family_history = library.family_stats()
    family_cap = int(config.research.family_register_cap)
    family_counts: dict[str, int] = {}
    skipped_family = 0
    registrable: list[tuple] = []
    for candidate in (sorted(result.strategies, key=lambda s: -s.score)[:12]
                      + sequence_candidates):
        family = family_of(candidate.rule)
        history = family_history.get(family, {})
        refused = history.get("rejected", 0) + history.get("retired", 0)
        proven = history.get("validated", 0) + history.get(
            "high_confidence", 0)
        # Adaptive search: three refusals and zero successes shrink the
        # family to one exploratory slot until something changes.
        cap = 1 if (refused >= 3 and proven == 0) else family_cap
        # ...and the experiment memory shrinks it for a second, sharper
        # reason: not "this family has been refused" but "this family has
        # been refused the SAME WAY three times". A throttle rather than a
        # ban, because a family that keeps failing for one reason may still
        # be right under a condition nobody has stated yet — §18 asks for
        # maximum exploration freedom, and closing a branch outright is the
        # one thing that cannot be undone by later evidence.
        if family in dead_ends:
            cap = min(cap, int(config.research.dead_end_family_slots))
        if family_counts.get(family, 0) >= cap:
            skipped_family += 1
            continue
        family_counts[family] = family_counts.get(family, 0) + 1
        registrable.append((candidate, family))
    funnel["familiesThisPass"] = dict(family_counts)
    funnel["skippedAsDuplicateFamily"] = skipped_family
    if skipped_family:
        say(f"  {skipped_family} candidate(s) skipped as near-duplicates of "
            "a family already holding its slots this pass")
    # FEATURE VALIDITY GATE (§5). A rule whose columns are constant or absent
    # in the validation data cannot fire there, so registering it would
    # manufacture a candidate that is permanently untestable and permanently
    # reports as merely untested. Refused at the door, and counted — the
    # refusal is a finding about the DATA, not about the rule.
    skipped_feature = 0
    for candidate, family in registrable:
        admitted, problems = feature_domain.admits(candidate.rule or {})
        if not admitted:
            skipped_feature += 1
            say(f"  not registered ({candidate.signature[:40]}): "
                f"{problems[0]}")
            continue
        # A candidate mined from history beyond this pass (wallet
        # behavior) excludes its SOURCE markets too — the markets whose
        # trades shaped the rule may never testify for it (§11).
        source_markets = set(
            (candidate.rule or {}).get("source_markets_list") or [])
        fresh_ids.add(library.upsert_candidate(
            candidate.signature, candidate.rule, candidate.describe,
            in_score=candidate.score, in_win=candidate.win_rate,
            in_sharpe=candidate.sharpe,
            discovery_markets=pass_markets | source_markets,
            family=family))
    funnel["skippedFeatureIncompatible"] = skipped_feature
    funnel["registeredThisPass"] = len(fresh_ids)
    funnel["registeredNew"] = len(library.all_strategies()) - rows_before

    # THE OOS MARKET POOL (master fix §2-§4): EVERY exported series is
    # eligible evidence for any candidate whose own exclusion list permits
    # it — not merely this pass's newest-holdout slice. A candidate
    # registered THIS pass excludes its whole discovery set, so it still
    # validates only on the holdout, exactly as before; a candidate frozen
    # in an earlier pass never trained on today's discovery markets, so
    # the full pool is legitimate unseen evidence for it. Disjointness is
    # per-candidate and permanent (discovery + already-tested markets),
    # every replay is entry-time-prospective, and a market still testifies
    # exactly once per candidate.
    eval_entries: list[dict] = []
    stamps: list[float] = []
    for entry in written:
        eval_entries.append({
            "market": str(entry.get("marketId") or entry.get("tokenId")),
            "csv": Path(entry["path"]),
            "token": str(entry.get("tokenId") or "").removeprefix(
                "hist:").removeprefix("pool:"),
            "tokenRaw": str(entry.get("tokenId") or ""),
            # Carried through for the eligibility service's walk-forward
            # classification — a market's own clock, not this pass's.
            "firstTs": float(entry.get("firstTs") or 0.0),
            "lastTs": float(entry.get("lastTs") or 0.0),
            "rows": int(entry.get("rows") or 0),
            # §7's information-gain ordering asks "is this a meaningfully
            # different environment?", and category is the strongest cheap
            # answer available. Exports have carried it since the manifest
            # was written; it was simply never passed this far.
            "category": str(entry.get("category") or ""),
            "source": "export",
        })
        stamps.append(float(entry.get("lastTs") or 0.0))

    # THE PERSISTENT POOL joins the pass's own exports: every cached
    # settled market not already exported this pass becomes eligible
    # unseen evidence. This is what turns "12 markets in the pool" into
    # "every settled market ever collected".
    pool_built = 0
    if config.research.oos_pool_enabled:
        pool_entries, pool_built, pool_stats = ensure_oos_pool(
            store, root / "pool", config, say)
        funnel["poolKnownThin"] = int(pool_stats.get("knownThin") or 0)
        funnel["poolUnprocessed"] = int(pool_stats.get("unprocessed") or 0)
        funnel["settledMarkets"] = int(pool_stats.get("settledMarkets") or 0)
        funnel["seriesConsidered"] = int(
            pool_stats.get("consideredThisPass") or 0)
        funnel["seriesAdmitted"] = int(pool_stats.get("admittedThisPass") or 0)
        funnel["seriesRejected"] = int(pool_stats.get("rejectedThisPass") or 0)
        funnel["seriesRejectedBy"] = dict(pool_stats.get("rejectedBy") or {})
        known_markets = {e["market"] for e in eval_entries}
        pool_added = 0
        for entry in pool_entries:
            market = str(entry.get("marketId") or entry.get("tokenId"))
            if market in known_markets:
                continue
            known_markets.add(market)
            eval_entries.append({
                "market": market,
                "csv": Path(entry["path"]),
                "token": str(entry.get("tokenId") or "").removeprefix(
                    "hist:").removeprefix("pool:"),
                "tokenRaw": str(entry.get("tokenId") or ""),
                "firstTs": float(entry.get("firstTs") or 0.0),
                "lastTs": float(entry.get("lastTs") or 0.0),
                "rows": int(entry.get("rows") or 0),
                "source": "pool",
            })
            stamps.append(float(entry.get("lastTs") or 0.0))
            pool_added += 1
        funnel["poolCached"] = len(pool_entries)
        funnel["poolBuiltThisPass"] = pool_built
        funnel["poolAddedBeyondExports"] = pool_added
    funnel["oosPoolSeries"] = len(eval_entries)
    period = ""
    if stamps:
        period = (time.strftime("%Y-%m-%d", time.gmtime(min(stamps)))
                  + " .. "
                  + time.strftime("%Y-%m-%d", time.gmtime(max(stamps))))

    # Per-market costs are cheap and cached; bridge feature-contexts are
    # EXPENSIVE and built lazily on the first bridge-rule replay of that
    # market, so a pass evaluating only chain/tape rules never pays for
    # feature engineering it does not use.
    charge_cache: dict[str, float] = {}
    bridge_cache: dict[str, dict] = {}

    def _crossing_for(item: dict) -> float:
        """Per-SHARE cost of one fill: half the spread, so a round trip
        pays the whole of it. Scales with size, because crossing a book
        does."""
        key = item["tokenRaw"]
        if key not in charge_cache:
            spread, _measured = effective_spread(
                median_spread(item["csv"]), config.research.assumed_spread)
            charge_cache[key] = spread / 2.0
        return charge_cache[key]

    def _charge_for(item: dict) -> float:
        """Per-share cost for the TAPE replays (chains, sharp moves,
        longshots, wallet rules).

        Those modules model a per-share return and have no notion of a fill,
        so a flat fee has nowhere to go but into the per-share figure. Left
        exactly as it was — floored at the flat fee — because folding a
        $0.01 fee into a per-share cost overstates it, and overstating a
        cost is the safe direction for a family whose costs cannot be
        modelled properly. The bridge path does not need the fudge: it
        charges the fee per fill, via `_crossing_for` plus `fee_per_fill`.
        """
        return max(fee, _crossing_for(item))

    def _bridge_context_for(item: dict) -> dict:
        key = item["tokenRaw"]
        if key not in bridge_cache:
            csv_path = item["csv"]
            oos_price = median_price(csv_path)
            oos_spread, _measured = effective_spread(
                median_spread(csv_path), config.research.assumed_spread)
            bridge_cache[key] = _oos_context(
                bridge, csv_path.parent, root / "oos" / _safe_name(key),
                config, oos_price, oos_spread, median_bar_seconds(csv_path))
        return bridge_cache[key]

    # Rejected rules are not evaluated — EXCEPT the ones this very pass
    # re-discovered: still looking good in-sample earns them a fresh
    # challenge, and next_status lets them back in (at validating) only if
    # the whole cumulative record flips positive.
    #
    # RESEARCH ALLOCATION (operator's §7/§23): the holdout evaluation
    # budget is finite, so it goes to the candidates whose EXISTING
    # unseen-record most deserves more evidence — positive OOS expectancy,
    # breadth, temporal diversity, low concentration, low overfit risk —
    # with a per-idea-family cap so no family consumes every slot, and a
    # meta-learned family weight steering (never deciding). Priority uses
    # only already-collected evidence; it cannot promote or trade.
    from .library import (ATTEMPT_ERROR, ATTEMPT_EVIDENCE, ATTEMPT_NO_TRADES,
                          MAX_NO_TRADE_TRIES, meta_family_weights,
                          research_priority)
    meta_weights = meta_family_weights(library.family_metrics())
    # META-DISCOVERY (§10): which research STRUCTURES have historically
    # survived unseen data — engines, feature families, sequence lengths,
    # holding periods, regimes, complexity bands. Read-only over the library's
    # own record; it steers where compute goes next and can promote nothing.
    from . import meta as meta_mod

    meta_rows = [{"id": row["id"], "rule": row.get("rule") or {},
                  "source": row.get("source") or "",
                  "family": row.get("family") or "",
                  "status": row["status"],
                  **{k: library.cumulative(row["id"])[v] for k, v in
                     (("oos_trades", "trades"), ("oos_markets", "markets"),
                      ("oos_expectancy", "expectancy"),
                      ("oos_forward_markets", "forward_markets"))}}
                 for row in library.all_strategies()]
    meta_records = meta_mod.measure(meta_rows)
    structure_weights = meta_mod.weights(meta_records)
    meta_summary = meta_mod.summary(meta_records, structure_weights)
    funnel.update(meta_summary)
    if meta_summary["metaHasOpinion"]:
        say(f"  meta-discovery: {meta_summary['metaStructuresSteering']} of "
            f"{meta_summary['metaStructuresWithStanding']} structures now "
            f"steering (strongest: {meta_summary['metaStrongest']})")
    else:
        say("  meta-discovery: no structure has enough record to steer yet")
    with contextlib.suppress(OSError):
        (root / "meta-structures.json").write_text(
            json.dumps(meta_mod.report(meta_records, structure_weights),
                       indent=1), encoding="utf-8")

    # -- ADVERSARIAL SELF-CHALLENGE ------------------------------------------
    # The declared battery, finally executed. It attacks evidence the
    # validation engine already wrote — leave-one-market-out, subset and
    # temporal splits, concentration, cost and margin stress, and the sibling
    # variants the pass has been registering all along — so it commissions no
    # replays, consumes no markets and writes nothing to `validations`.
    #
    # Run BEFORE allocation on purpose: a robustness figure that arrived after
    # the slate was chosen could only decorate a decision it had no part in.
    # Everything it produces is a research signal; `next_status` is not called
    # from here and does not read any of it.
    from . import adversarial as adv_mod

    all_rows = library.all_strategies()
    cumulative_cache: dict[str, dict] = {}

    def _cum(candidate_id: str) -> dict:
        if candidate_id not in cumulative_cache:
            cumulative_cache[candidate_id] = library.cumulative(candidate_id)
        return cumulative_cache[candidate_id]

    # Relatives, indexed once. Scanning the whole library per attacked
    # candidate is quadratic, and a library that grows forever by design is
    # the wrong place to hide an O(n²).
    by_signature: dict[str, list[dict]] = {}
    by_parent: dict[str, list[dict]] = {}
    for row in all_rows:
        by_signature.setdefault(str(row.get("signature") or ""),
                                []).append(row)
        origin = str((row.get("rule") or {}).get("variant_of")
                     or row.get("parent_id") or "")
        if origin:
            by_parent.setdefault(origin, []).append(row)

    # THE PROBE (§4's "randomised/control comparisons" and "lower
    # liquidity"). Two questions a frozen ledger cannot answer, wired up here
    # because this is where the tape, the cost model and the pool CSVs live.
    # `ReplayProbe` itself is module-level and unit-tested: a test that has to
    # call a 2,000-line function to reach the logic is a test nobody writes.
    _probe_pool = {item["market"]: item for item in eval_entries}
    _volume_cache: dict[str, float] = {}

    def _probe_volumes(market_ids: list[str]) -> dict[str, float]:
        """Total traded value per market, as a depth proxy.

        Reading the tape to RANK markets the candidate has already been
        tested on is not contamination: no evidence is created, no market is
        consumed, and the ordering uses a property of the market rather than
        anything about the rule. The same query on the same markets returns
        the same split next pass.
        """
        missing = [m for m in market_ids if m not in _volume_cache]
        if missing:
            for chunk in (missing[i:i + 400]
                          for i in range(0, len(missing), 400)):
                marks = ",".join("?" * len(chunk))
                with contextlib.suppress(Exception):
                    for row in store.query(
                            "SELECT market_id, SUM(ABS(usdc)) AS v FROM "
                            f"wallet_trades WHERE market_id IN ({marks}) "
                            "GROUP BY market_id", tuple(chunk)):
                        _volume_cache[str(row["market_id"])] = \
                            float(row["v"] or 0.0)
                for market_id in chunk:
                    _volume_cache.setdefault(market_id, 0.0)
        return {m: _volume_cache.get(m, 0.0) for m in market_ids}

    probe = ReplayProbe(_probe_pool, _charge_for, _probe_volumes,
                        config.research)

    adversarial_reports: dict[str, adv_mod.AdversarialReport] = {}
    if config.research.adversarial_enabled:
        attackable = [r for r in all_rows
                      if adv_mod.worth_attacking(_cum(r["id"]), r["status"])]
        # Deepest records first. The cap is a wall-clock bound, and if it
        # binds it should bind on the candidates with least at stake.
        attackable.sort(key=lambda r: -int(_cum(r["id"])["trades"]))
        for row in attackable[:int(config.research.adversarial_max_per_pass)]:
            relatives = {str(r["id"]): r for r in
                         by_signature.get(str(row.get("signature") or ""), [])
                         + by_parent.get(str(row["id"]), [])}
            report = adv_mod.attack(
                row, _cum(row["id"]), library.market_ledger(row["id"]),
                config.research,
                adv_mod.siblings_of(row, relatives.values(), _cum),
                probe=probe)
            adversarial_reports[str(row["id"])] = report
        funnel.update(adv_mod.summary(adversarial_reports.values()))
        broken = funnel.get("adversarialByVerdict", {}).get(
            adv_mod.V_BROKEN, 0)
        say(f"  adversarial: {funnel['adversarialCandidatesAttacked']} "
            f"candidate(s) attacked, {funnel['adversarialTestsRun']} test(s) "
            f"run at {funnel['adversarialMeanCoverage']:.0%} mean coverage, "
            f"{broken} broken, {funnel['adversarialTestsFailed']} "
            "individual failure(s)")
        for name, count in list(
                funnel.get("adversarialFailuresByTest", {}).items())[:3]:
            say(f"    failed most often: {count} x {name}")
        with contextlib.suppress(OSError):
            (root / "adversarial.json").write_text(
                json.dumps([r.to_dict() for r in adversarial_reports.values()
                            if r.verdict != adv_mod.V_NOT_ATTACKED],
                           indent=1), encoding="utf-8")

    pool = (library.evaluable()
            + [s for s in library.all_strategies()
               if s["status"] == "rejected" and s["id"] in fresh_ids])
    # A rejection is a verdict on the evidence that existed when it was
    # made, not a proof about the rule, and the holdout pool grows every
    # pass. A bounded slice of the rejected pile — oldest verdict first —
    # comes back for another swing at unseen markets. The standard is
    # unchanged: `next_status` still requires the whole cumulative record
    # to turn positive before any of them re-enters validation, so this
    # widens the search and not the bar.
    attempt_pool = library.attempt_summaries()
    known_ids = {row["id"] for row in pool}
    for row in library.rejected_for_recheck(
            limit=int(config.research.rejected_recheck_per_pass)):
        if row["id"] not in known_ids:
            known_ids.add(row["id"])
            pool.append(row)
    # ADAPTIVE ALLOCATION (§13). Priority alone is a bandit at zero
    # exploration: priority is computed from evidence a candidate already
    # has, so a candidate with none scores low, is never allocated, and
    # therefore never gets any. That loop is how 153 of 231 candidates
    # reached this point never having been evaluated once. A reserved share
    # of every cycle now goes to the never-tested and to near misses, and
    # what is left is priority-ordered exactly as before.
    from .allocation import Allocatable, allocate
    from .library import maturity_of as _maturity

    # THE ELIGIBILITY SERVICE, built once over this pass's whole market set.
    # Candidates ask it; nothing else decides what a candidate may use. Built
    # before the slate rather than after it because the research reward needs
    # to know how DIVERSE a candidate's existing evidence is — a candidate
    # whose whole record sits in one category and one month is under-tested
    # however good its expectancy looks, and that is §7's entire point.
    from .eligibility import (MarketEligibilityService, classify_pool,
                              diversity_of, records_from_entries)

    market_records = records_from_entries(eval_entries)
    items_by_market = {item["market"]: item for item in eval_entries}
    eligibility = MarketEligibilityService(
        market_records, library, feature_domain=feature_domain,
        min_rows=int(config.research.oos_pool_min_rows))
    funnel.update(eligibility.census())
    # How the pool would classify for a candidate discovered NOW. Reported so
    # "no forward evidence exists yet" reads as a property of the data rather
    # than as a failing of every candidate in the library.
    funnel["poolTemporalMixNow"] = classify_pool(market_records, time.time())

    # THE RESEARCH REWARD. Multiplies into priority alongside the
    # meta-structure weight and decides one thing only: what is looked at
    # next. It reads adversarial robustness, evidence diversity, convergence
    # and the dead-end memory — and it is not an input to `next_status`,
    # which is what keeps "better at finding good strategies" from becoming
    # "better at convincing itself weak ones are good".
    from . import reward as reward_mod

    convergence_priorities = _convergence_priorities(root, config, say)
    rewards: dict[str, reward_mod.RewardBreakdown] = {}

    # FAMILY & MOTIF INTELLIGENCE. Strictly between discovery and hypothesis
    # generation, and strictly read-only over the library: it mines recurring
    # STRUCTURE across candidates, with the market ids behind every piece of
    # evidence so that two candidates replayed on the same markets can never
    # read as two independent confirmations. Its output is a bounded weight on
    # research priority and a handful of controlled mutations that queue for
    # the same OOS markets as everything else. A failure here loses the layer,
    # never the pass.
    motif_pass = None
    motif_scores: dict = {}
    motif_versions: dict[str, int] = {}
    motif_ledgers: dict[str, list[dict]] = {}
    motif_store = None
    if config.research.motif_enabled:
        try:
            motif_ledgers = library.evidence_ledgers()
            motif_versions = library.version_counts()
            motif_cumulative = {row["id"]: _cum(row["id"]) for row in pool}
            motif_store = motif_mod.MotifStore(root / "motifs.sqlite3")
            motif_pass = motif_mod.run_pass(
                library.all_strategies(), motif_ledgers,
                cumulative=motif_cumulative,
                versions=motif_versions,
                adversarial=adversarial_reports,
                market_categories={r.market_id: r.category
                                   for r in market_records},
                store=motif_store,
                mutation_budget=int(
                    config.research.motif_mutations_per_pass))
            motif_scores = motif_pass.scores
            funnel.update(motif_pass.summary())
            # §27: the denominator, cumulative across every pass ever run.
            # Reported next to the discovery rather than under it, because a
            # best-of-N result presented alone is not a finding.
            funnel["motifCumulativeSearch"] = motif_store.cumulative_scale()
            with contextlib.suppress(OSError):
                (root / "motifs.json").write_text(
                    json.dumps({"motifs": motif_pass.report(60),
                                "scale": motif_pass.summary()}, indent=1),
                    encoding="utf-8")
            standing = funnel.get("motifsWithStanding", 0)
            say(f"  motif layer: {funnel.get('motifsExamined', 0)} structural "
                f"motif(s) examined, {standing} with standing, "
                f"{funnel.get('motifFailures', 0)} recurring failure motif(s)")
            if funnel.get("motifStrongestWhy"):
                say(f"    {str(funnel['motifStrongestWhy'])[:150]}")
        except Exception as exc:                         # noqa: BLE001
            say(f"  motif layer unavailable: {exc}")
            motif_pass, motif_scores = None, {}

    slate: list[Allocatable] = []
    by_id: dict[str, dict] = {}
    for candidate_row in pool:
        row_cum = _cum(candidate_row["id"])
        priority = research_priority(
            row_cum, candidate_row["status"],
            float(candidate_row.get("in_win") or 0.0),
            library.periods_count(candidate_row["id"]), config.research,
            meta_weights.get(str(candidate_row.get("signature") or ""),
                             1.0))
        # ...then the STRUCTURE weight (§10). Applied to priority and to
        # nothing else, so meta-discovery can move a candidate up or down the
        # research queue and can never move it toward or away from a gate.
        # The exploration reserve sits above this and is untouched by it: a
        # never-tested candidate keeps its slot however unfashionable its
        # structure, or the layer would quietly close off the search.
        structure_weight = meta_mod.weight_for(
            {"rule": candidate_row.get("rule") or {},
             "source": candidate_row.get("source") or "",
             "family": candidate_row.get("family") or ""},
            structure_weights)
        priority *= structure_weight
        seen = attempt_pool.get(candidate_row["id"]) or {}
        # The motif weight. Bounded on the way in (0.6..1.6 from
        # `motif.weight_for`) and multiplied into the reward's steering term
        # only — it never touches `research_priority`'s own arithmetic above,
        # so switching the layer off restores the previous number exactly.
        motif_weight, motif_note = 1.0, ""
        if motif_scores:
            motif_weight = motif_mod.weight_for(
                candidate_row, motif_scores, row_cum,
                motif_versions.get(str(candidate_row.get("signature") or ""),
                                   1))
            _key, dominant = motif_mod.dominant_motif(
                candidate_row, motif_scores, row_cum,
                motif_versions.get(str(candidate_row.get("signature") or ""),
                                   1))
            motif_note = (dominant.why_elevated if motif_weight > 1.0
                          else dominant.why_deprioritised)
        if config.research.research_reward_enabled:
            family_name = str(candidate_row.get("family") or "")
            breakdown = reward_mod.score(
                candidate_row, row_cum, config.research,
                adversarial=adversarial_reports.get(candidate_row["id"]),
                diversity=diversity_of(library, candidate_row["id"], row_cum,
                                       eligibility),
                convergence=convergence_priorities.get(
                    _pattern_signature(candidate_row.get("rule") or {}), 0.0),
                structure_weight=structure_weight,
                family_weight=meta_weights.get(
                    str(candidate_row.get("signature") or ""), 1.0),
                motif_weight=motif_weight,
                motif_note=(motif_note[:160] if motif_note else ""),
                dead_end=", ".join(dead_ends.get(family_name, [])),
                attempts=seen)
            rewards[candidate_row["id"]] = breakdown
            # BLENDED, not replaced. `research_priority` is the operator's
            # own allocation rule and has a record of behaving sensibly; the
            # reward adds dimensions it does not have (robustness, diversity,
            # convergence, dead ends). Averaging them means a bug in the new
            # term can at worst halve a candidate's place in a queue, where
            # substitution would let it silently own the whole slate.
            priority = 0.5 * priority + 0.5 * breakdown.score
        by_id[candidate_row["id"]] = candidate_row
        slate.append(Allocatable(
            id=candidate_row["id"],
            # Pre-family rows classify from their rule, so legacy records
            # cannot all pile into one "other" bucket and starve the budget.
            family=(str(candidate_row.get("family") or "")
                    or family_of(candidate_row.get("rule") or {})),
            priority=priority,
            maturity=_maturity(candidate_row["status"], row_cum,
                               config.research),
            attempts=(int(seen.get("evidence", 0))
                      + int(seen.get("zeroTrades", 0))
                      + int(seen.get("errors", 0))),
            created_ts=float(candidate_row.get("created_ts") or 0.0)))

    allocation = allocate(
        slate, slots=int(config.research.oos_candidates_per_pass),
        per_family_cap=int(config.research.oos_family_slots),
        explore_fraction=float(config.research.oos_explore_fraction),
        near_miss_fraction=float(config.research.oos_near_miss_fraction))
    evaluable = [by_id[cid] for cid in allocation.ids]
    funnel.update(allocation.summary())
    if rewards:
        funnel.update(reward_mod.summary(rewards.values()))
        # §13's "WHY IS THIS CANDIDATE RECEIVING MORE RESEARCH?" — persisted
        # rather than printed, so the answer survives the pass that produced
        # it and the dashboard reads the same sentence the allocator acted on.
        with contextlib.suppress(OSError):
            (root / "research-priority.json").write_text(
                json.dumps([rewards[cid].to_dict() for cid in allocation.ids
                            if cid in rewards], indent=1), encoding="utf-8")
        for cid in allocation.ids[:3]:
            if cid in rewards and rewards[cid].why_more:
                say(f"    {cid[:36]}: {rewards[cid].why_more[:110]}")

    new_events = 0
    families_with_new_evidence: set[str] = set()
    eval_budget = int(config.research.oos_eval_budget)
    evals_done = 0
    variants_registered = 0
    # Attempt accounting (§3, §16). These are the numbers that distinguish
    # "the research budget was spent and produced evidence" from "the research
    # budget was spent on rules that never fire" — previously indistinguishable.
    zero_trade_attempts = 0
    replay_failures = 0
    parked_pairs = 0
    if eval_entries and evaluable:
        say(f"[4/4] Library validation: {len(evaluable)} strategies "
            f"(priority-ordered) x {len(eval_entries)} pool market(s), "
            f"replay budget {eval_budget}")
        for entry in evaluable:
            if evals_done >= eval_budget:
                say(f"    replay budget exhausted at {evals_done}; "
                    "remaining candidates wait for the next pass")
                break
            # ONE authoritative answer to "which markets may this candidate
            # legitimately use right now" (§4). Identity, contamination,
            # prior evidence, parking, data completeness, feature
            # availability and walk-forward position are all decided here,
            # and every refusal is counted rather than skipped in silence.
            verdict = eligibility.for_candidate(entry)
            eligible_items = [items_by_market[m.market_id]
                              for m in verdict.markets
                              if m.market_id in items_by_market]
            temporal = verdict.classes
            pass_trades = pass_wins = 0
            pass_pnl = 0.0
            for item in eligible_items:
                if evals_done >= eval_budget:
                    break
                market_id = item["market"]
                csv_path = item["csv"]
                token = item["token"]
                charge = _charge_for(item)
                rule_type = str(entry["rule"].get("type") or "threshold")
                started = time.time()
                try:
                    if rule_type == "sequence":
                        # Chains replay on the raw holdout series — same
                        # freeze discipline, their own mechanics.
                        from .analytics import sequences as seq_mod
                        stats = seq_mod.frozen_replay(
                            seq_mod.rows_from_csv(csv_path), entry["rule"],
                            charge * 2.0)      # full round trip per replay
                    elif rule_type == "sharp_move":
                        from .analytics import sequences as seq_mod
                        from .analytics import sharp_moves as sharp_mod
                        stats = sharp_mod.frozen_replay(
                            seq_mod.rows_from_csv(csv_path), entry["rule"],
                            charge * 2.0)
                    elif rule_type == "longshot":
                        # Longshots replay on the holdout market's raw
                        # TAPE with its known resolution — one observation
                        # per market, entry-time information only.
                        from .analytics import longshot as longshot_mod
                        payout_map = store.resolutions()
                        if token not in payout_map:
                            # Not a silent skip: this candidate cannot be
                            # replayed here because the market's settled
                            # payout is missing. That is a DATA_FAILURE and
                            # the operator should be able to see it.
                            raise ReplayDataUnavailable(
                                "no settled payout for this token")
                        tape = store.query(
                            "SELECT ts, price, usdc FROM wallet_trades "
                            "WHERE token_id = ? ORDER BY ts", (token,))
                        payout = (1.0 if float(payout_map[token]) >= 0.99
                                  else 0.0)
                        stats = longshot_mod.frozen_replay(
                            tape, entry["rule"], payout, charge * 2.0)
                    elif rule_type == "wallet_state":
                        # Wallet-state rules replay on the whole MARKET's
                        # tape (both outcome tokens — opposite-side buys
                        # live on the other token) with known payouts.
                        from .analytics import wallet_states as wstate_mod
                        payout_map = store.resolutions()
                        tape = store.query(
                            "SELECT wallet, market_id, token_id, ts, "
                            "price, usdc, side FROM wallet_trades "
                            "WHERE market_id = ? ORDER BY ts", (market_id,))
                        payouts = {t: (1.0 if float(p) >= 0.99 else 0.0)
                                   for t, p in payout_map.items()}
                        stats = wstate_mod.frozen_replay(
                            tape, entry["rule"], payouts, charge * 2.0)
                    elif rule_type == "wallet_behavior":
                        # Wallet-behavior rules replay WALLET-FREE on the
                        # market's exported series — the whole point: the
                        # extracted rule must stand where the source
                        # wallet never traded. Resolution holds need the
                        # settled payout; timed holds do not.
                        from .analytics import sequences as seq_mod
                        from .analytics import wallet_behavior as wbehav_mod
                        payout: Optional[float] = None
                        payout_map = store.resolutions()
                        if token in payout_map:
                            payout = (1.0 if float(payout_map[token]) >= 0.99
                                      else 0.0)
                        stats = wbehav_mod.frozen_replay(
                            seq_mod.rows_from_csv(csv_path), entry["rule"],
                            payout, charge * 2.0)
                    else:
                        stats = _frozen_run(_bridge_context_for(item),
                                            entry["rule"],
                                            _crossing_for(item))
                except Exception as exc:                  # noqa: BLE001
                    # §16: a replay failure is a fact about the candidate, not
                    # a line to skip. Swallowing it made a candidate that
                    # crashes on every market indistinguishable from one that
                    # was never allocated any — the same "no OOS events yet"
                    # for two completely different problems.
                    evals_done += 1
                    replay_failures += 1
                    library.record_attempt(
                        entry["id"], market_id, ATTEMPT_ERROR,
                        reason=str(exc)[:200], stage=f"replay:{rule_type}",
                        exc_type=type(exc).__name__,
                        cost_seconds=time.time() - started)
                    say(f"    ! replay failed {entry['id'][:32]} on "
                        f"{market_id[:18]}: {type(exc).__name__}: "
                        f"{str(exc)[:90]}")
                    continue
                evals_done += 1
                trades_made = int(stats.get("trades", 0) or 0)
                if trades_made <= 0:
                    # A NON-OBSERVATION. The rule's conditions never occurred
                    # in this market, which tells us nothing about the rule
                    # and must not spend the market. Recorded, bounded,
                    # released — never written as evidence.
                    tries = library.record_attempt(
                        entry["id"], market_id, ATTEMPT_NO_TRADES,
                        reason="rule never fired in this market",
                        stage=f"replay:{rule_type}",
                        cost_seconds=time.time() - started)
                    zero_trade_attempts += 1
                    if tries >= MAX_NO_TRADE_TRIES:
                        parked_pairs += 1
                    continue
                library.record_attempt(
                    entry["id"], market_id, ATTEMPT_EVIDENCE,
                    trades=trades_made, stage=f"replay:{rule_type}",
                    cost_seconds=time.time() - started)
                fresh = library.record_validation(
                    entry["id"], market_id, trades=trades_made,
                    wins=stats.get("wins", 0), pnl=stats.get("pnl", 0.0),
                    drawdown=stats.get("drawdown", 0.0), period=period,
                    temporal_class=temporal.get(market_id, ""))
                if fresh:
                    new_events += 1
                    families_with_new_evidence.add(
                        str(entry.get("signature") or ""))
                pass_trades += trades_made
                pass_wins += stats.get("wins", 0)
                pass_pnl += stats.get("pnl", 0.0)
            if pass_trades:
                library.record_pass(entry["id"], pass_trades, pass_wins,
                                    pass_pnl)
            cumulative = library.cumulative(entry["id"])
            recents = library.recent_passes(entry["id"], 1)
            status, reason = next_status(
                entry["status"], cumulative,
                recents[0] if recents else None, config.research)
            if status != entry["status"]:
                library.set_status(entry["id"], status, reason)
                say(f"    {entry['id'][:40]}: {entry['status']} -> {status}"
                    + (f" ({reason})" if reason else ""))
            # Bidirectional / hold discovery (operator's spec): direction
            # and holding period are variables, not assumptions. Evidence-
            # driven variants register as their OWN candidates — parent's
            # discovery exclusions inherited, evidence NOT.
            if config.research.variants_enabled \
                    and variants_registered < config.research.variants_per_pass:
                try:
                    parent_discovery = set(json.loads(
                        entry.get("discovery_markets") or "[]"))
                except (TypeError, ValueError):
                    parent_discovery = set()
                for variant_rule, variant_describe in variant_expansions(
                        library, entry, cumulative, config.research,
                        directive=directives.get(entry["id"], "")):
                    if variants_registered >= config.research.variants_per_pass:
                        break
                    variant_id = library.upsert_candidate(
                        signature_of(variant_rule), variant_rule,
                        variant_describe,
                        discovery_markets=parent_discovery,
                        family=family_of(variant_rule),
                        source=SOURCE_INVERSE_ADVERSARIAL,
                        parent_id=entry["id"])
                    variants_registered += 1
                    kind = str(variant_rule.get("variant") or "variant")
                    say(f"    variant [{kind}] registered: "
                        f"{variant_id[:44]}")
    elif evaluable:
        say("[4/4] No eligible pool markets this pass - library "
            "unchanged, nothing promoted")

    # MOTIF MUTATIONS. Registered from the SAME per-pass variant budget as
    # everything else (§9: no brute force, no second scheduler) and only from
    # motifs that reached standing and scored above the floor. Each one is an
    # ordinary candidate: the parent's discovery-market exclusions are
    # inherited so it cannot be validated on data that suggested it, and
    # NOTHING else is — no trades, no markets, no status, no significance.
    motif_mutations_registered = 0
    if motif_pass is not None and config.research.variants_enabled:
        for parent, rule, describe in motif_pass.mutations:
            if variants_registered >= config.research.variants_per_pass:
                break
            try:
                parent_discovery = set(json.loads(
                    parent.get("discovery_markets") or "[]"))
            except (TypeError, ValueError):
                parent_discovery = set()
            try:
                mutant_id = library.upsert_candidate(
                    signature_of(rule), rule, describe,
                    discovery_markets=parent_discovery,
                    family=family_of(rule),
                    source=SOURCE_INVERSE_ADVERSARIAL,
                    parent_id=str(parent.get("id") or ""))
            except Exception as exc:                     # noqa: BLE001
                say(f"    motif mutation refused: {exc}")
                continue
            variants_registered += 1
            motif_mutations_registered += 1
            if motif_store is not None:
                with contextlib.suppress(Exception):
                    motif_store.link(str(parent.get("id") or ""), "MUTATION",
                                     mutant_id,
                                     str(rule.get("motif_mutation") or ""))
            say(f"    motif mutation [{rule.get('motif_mutation')}] "
                f"registered: {mutant_id[:44]}")
        motif_pass.scale.mutations_registered = motif_mutations_registered
        funnel["motifMutationsRegistered"] = motif_mutations_registered
    funnel["oosAllocations"] = evals_done
    funnel["variantsRegistered"] = variants_registered
    # §3 and §16 made visible. `oosAllocations` counts compute spent;
    # `newIndependentEvents` counts evidence gained. The gap between them used
    # to be invisible, and it was where the market supply was going.
    funnel["zeroTradeAttempts"] = zero_trade_attempts
    funnel["replayFailures"] = replay_failures
    funnel["marketsParkedThisPass"] = parked_pairs
    funnel.update(library.attempt_health())

    # What the engine and UI load is a VIEW of the library — every version,
    # every status, retired included. Rebuilt each pass, never the source.
    from .library import blockers_of, blocking_of, evidence_score, maturity_of

    pool_market_ids = {item["market"] for item in eval_entries}
    attempt_rows = library.attempt_summaries()

    view: list[DiscoveredStrategy] = []
    tier = {"high_confidence": 6, "validated": 5, "watch": 4,
            "validating": 3, "new": 2, "degraded": 1, "retired": 0,
            "rejected": 0, "quarantined": 0}
    family_cache: dict[str, dict] = {}
    for entry in library.all_strategies():
        cumulative = library.cumulative(entry["id"])
        strategy = DiscoveredStrategy(
            rule=entry["rule"], signature=entry["id"],
            describe=entry.get("describe") or "",
            score=float(entry.get("in_score") or 0.0),
            win_rate=float(entry.get("in_win") or 0.0),
            sharpe=float(entry.get("in_sharpe") or 0.0))
        strategy.version = int(entry.get("version") or 1)
        strategy.status = entry["status"]
        strategy.oos_trades = cumulative["trades"]
        strategy.oos_markets = cumulative["markets"]
        strategy.oos_win = cumulative["win_rate"]
        strategy.oos_expectancy = cumulative["expectancy"]
        strategy.oos_drawdown = cumulative["drawdown"]
        strategy.oos_period = cumulative["period"]
        strategy.confidence = wilson_lower_bound(cumulative["wins"],
                                                 cumulative["trades"])
        # The composite evidence score (§23): sample x breadth x
        # sample-honest confidence x diversification. This — never win
        # rate, never one flattering metric — is what ranks the board.
        strategy.evidence = evidence_score(cumulative, config.research)
        strategy.last_validated_ts = float(
            entry.get("last_validated_ts") or 0.0)
        # The research layers (operator's family spec): maturity + the
        # named blocking condition + the hypothesis-family ledger. The
        # version record above stays authoritative; these never feed the
        # trading gate.
        strategy.maturity = maturity_of(entry["status"], cumulative,
                                        config.research)
        strategy.blocking = blocking_of(entry["status"], cumulative,
                                        config.research)
        # §9: EVERY active blocker with its numeric target, plus the
        # next-action state for market-starved candidates — a candidate
        # waiting on breadth must say whether unseen markets exist for it.
        strategy.blockers = blockers_of(entry["status"], cumulative,
                                        config.research,
                                        attempt_rows.get(entry["id"]))
        if any(b.startswith("OOS_MARKET_BREADTH") or b.startswith("RULE_NEVER")
               or b.startswith("DATA_FAILURE") for b in strategy.blockers):
            # §24: "what would allow it to progress", answered by the same
            # service that decides allocation — so the dashboard cannot
            # disagree with what the replay loop actually does.
            strategy.next_action = eligibility.for_candidate(entry).next_action()
        strategy.source = (str(entry.get("source") or "")
                           or source_of(entry.get("rule") or {}))
        strategy.parent_id = str(entry.get("parent_id") or "")
        strategy.complexity = _rule_complexity(entry.get("rule") or {})
        strategy.oos_forward_markets = int(
            cumulative.get("forward_markets") or 0)
        strategy.oos_periods = library.periods_count(entry["id"])
        from .library import overfit_risk as _overfit
        strategy.overfit_risk = _overfit(
            float(entry.get("in_win") or 0.0), cumulative)
        strategy.priority = research_priority(
            cumulative, entry["status"], float(entry.get("in_win") or 0.0),
            strategy.oos_periods, config.research,
            meta_weights.get(str(entry.get("signature") or ""), 1.0))
        family_signature = str(entry.get("signature") or "").split("#")[0]
        if family_signature not in family_cache:
            family_cache[family_signature] = \
                library.family_cumulative(family_signature)
        ledger = family_cache[family_signature]
        strategy.family_markets = int(ledger.get("markets") or 0)
        strategy.family_trades = int(ledger.get("trades") or 0)
        strategy.family_expectancy = float(ledger.get("expectancy") or 0.0)
        strategy.family_versions = int(ledger.get("versions") or 1)
        # THE MOTIF LAYER'S READ-OUT. Deliberately carried on the same row as
        # the OOS columns and deliberately named differently from them: a
        # reader must be able to see at a glance that `familyResearchScore` is
        # not evidence about THIS candidate. It is what the structural class
        # has done across other candidates, on other markets, and it buys this
        # row nothing but a place in the queue.
        if motif_scores:
            nversions = motif_versions.get(
                str(entry.get("signature") or ""), 1)
            strategy.motif_weight = motif_mod.weight_for(
                entry, motif_scores, cumulative, nversions)
            key, dominant = motif_mod.dominant_motif(
                entry, motif_scores, cumulative, nversions)
            strategy.motif = key
            strategy.family_research_score = dominant.score
            strategy.family_failure_motif = dominant.failure_motif
            strategy.why_family_elevated = dominant.why_elevated
            strategy.why_family_deprioritised = dominant.why_deprioritised
            record = (motif_pass.records.get(key)
                      if motif_pass is not None else None)
            if record is not None:
                strategy.family_replication = record.replication_rate
                strategy.family_independent_markets = len(record.markets)
                strategy.family_independent_candidates = len(
                    record.independent_candidates())
        # The research layer's own read-outs, carried on the view row so the
        # dashboard and the CLI report what the allocator actually used
        # rather than recomputing it and drifting. None of these is an input
        # to `tradable`, which still reads `status` alone.
        report = adversarial_reports.get(entry["id"])
        if report is not None:
            strategy.adversarial_verdict = report.verdict
            strategy.robustness = report.robustness
            strategy.adversarial_coverage = report.coverage
            strategy.adversarial_failed = report.failed_tests
        breakdown = rewards.get(entry["id"])
        if breakdown is not None:
            strategy.research_reward = breakdown.score
            strategy.why_more_research = breakdown.why_more
            strategy.why_stopped = breakdown.why_stopped
        view.append(strategy)

        # EXPERIMENT MEMORY. Written from the FINISHED record, after this
        # pass's replays, so what is remembered is what the pass actually
        # learned. `record` dedupes on the classification rather than the
        # timestamp: two thousand candidates re-examined hourly would
        # otherwise write two thousand identical rows an hour and bury the
        # moments something moved.
        if exp_store is not None:
            with contextlib.suppress(Exception):
                exp_store.record(from_candidate(
                    entry, cumulative, config.research,
                    adversarial=report,
                    attempts=attempt_rows.get(entry["id"]),
                    maturity=strategy.maturity,
                    hypothesis=_pattern_signature(entry.get("rule") or {})))
    view.sort(key=lambda s: (tier.get(s.status, 0), s.evidence,
                             s.oos_expectancy), reverse=True)
    # The BOARD cap, not a library cap — the library database is unbounded
    # and keeps every record forever. The operator caught the old hard 200
    # saturating his display; the view now carries far more, still bounded
    # so the engine's per-cycle reload stays cheap.
    result.strategies = view[:int(config.research.library_view_cap)]
    result.accepted = sum(1 for s in view if s.tradable)
    funnel["evaluated"] = len(evaluable) if eval_entries else 0
    funnel["libraryRecords"] = len(view)
    funnel["tradable"] = result.accepted
    # Family-layer counters (operator's §17): the fragmentation picture.
    # §23's DISCOVERY SOURCES panel: per research pathway, how many
    # candidates exist, how many have actually been tested, and how many
    # survived. Computed from the library view, so it cannot drift from what
    # the validation ladder did.
    # THE HYPOTHESIS LAYER (second directive). Strictly additive and strictly
    # last: it reads the finished library view and asks what the engines are
    # agreeing about. It cannot promote, validate or trade — the only thing
    # it can do is register ordinary candidates that queue for the same OOS
    # markets as everything else. Failures here never fail the pass.
    if config.research.hypothesis_layer_enabled:
        try:
            from .convergence import run_pass as _hypothesis_pass
            from .hypothesis import HypothesisStore

            hyp_store = HypothesisStore(root / "hypotheses.sqlite3")
            try:
                funnel.update(_hypothesis_pass(
                    library, hyp_store,
                    # `markets` was an empty list here, and because
                    # `convergence_priority` is multiplicative with a breadth
                    # term, an empty market set zeroed the priority of EVERY
                    # hypothesis — the whole convergence ranking evaluated to
                    # 0.0 and ordered nothing. The markets that testified for
                    # a candidate are what the relationship has actually been
                    # seen in, so they are what the layer needs.
                    [{"id": s.signature, "rule": s.rule, "source": s.source,
                      "family": family_of(s.rule),
                      "markets": sorted(
                          library.evidence_markets(s.signature)),
                      "periods": ([s.oos_period] if s.oos_period else []),
                      "regimes": [str((s.rule or {}).get("regime") or "")]
                      if (s.rule or {}).get("regime") else [],
                      "oos_trades": s.oos_trades,
                      "oos_expectancy": s.oos_expectancy} for s in view],
                    say, reports=adversarial_reports,
                    compose_enabled=bool(config.research.compose_enabled)))
            finally:
                hyp_store.close()
        except Exception as exc:                          # noqa: BLE001
            say(f"  hypothesis layer skipped: {type(exc).__name__}: {exc}")

    funnel["discoverySources"] = source_census(
        {"source": s.source, "rule": s.rule, "status": s.status,
         "oos_trades": s.oos_trades} for s in view)
    funnel["forwardEvidenceMarkets"] = sum(s.oos_forward_markets
                                           for s in view)
    funnel["uniqueFamilies"] = len(family_cache)
    funnel["newIndependentEvents"] = new_events
    funnel["familiesWithNewEvidence"] = len(families_with_new_evidence)
    funnel["insufficientEvidence"] = sum(
        1 for s in view if s.maturity == "INSUFFICIENT_EVIDENCE")
    funnel["nearMiss"] = sum(1 for s in view if s.maturity == "NEAR_MISS")
    # OOS-breadth starvation (operator's addendum): candidates waiting on
    # independent MARKETS, not on trades. When this dominates, the fix is
    # more unseen markets — the runner's re-collection trigger reads it.
    funnel["blockedOnBreadth"] = sum(
        1 for s in view if s.blocking.startswith("INSUFFICIENT_MARKETS"))
    # Wallet-research census (operator's §16): the wallet-derived slice
    # of the permanent library by status, so the dashboard can answer
    # "what happened to the hypotheses" from persisted data alone.
    wb_rows = [s for s in view
               if (s.rule or {}).get("type") == "wallet_behavior"]
    if "walletBehavior" in funnel or wb_rows:
        wb_census: dict[str, int] = {}
        for s in wb_rows:
            wb_census[s.status] = wb_census.get(s.status, 0) + 1
        wb_funnel = funnel.setdefault("walletBehavior", {})
        wb_funnel["library"] = wb_census
        wb_funnel["libraryTotal"] = len(wb_rows)
        wb_funnel["convergent"] = sum(
            1 for s in wb_rows if (s.rule or {}).get("convergent_with"))

    # -- §16 DISCOVERY HEALTH PANEL: where the pipeline actually stands ------
    active = [s for s in view if s.status not in ("rejected", "retired")]
    market_counts = sorted(s.oos_markets for s in active)
    blocked_by: dict[str, int] = {}
    for s in active:
        for b in s.blockers:
            key = b.split(" ")[0]
            blocked_by[key] = blocked_by.get(key, 0) + 1
    with_evidence = [s for s in active if s.oos_trades > 0]
    funnel["health"] = {
        "activeCandidates": len(active),
        "avgOosMarkets": (round(sum(market_counts) / len(market_counts), 2)
                          if market_counts else 0.0),
        "medianOosMarkets": (market_counts[len(market_counts) // 2]
                             if market_counts else 0),
        "maxOosMarkets": market_counts[-1] if market_counts else 0,
        "zeroEvidencePct": (round(
            100.0 * sum(1 for s in with_evidence if s.evidence == 0)
            / len(with_evidence), 1) if with_evidence else 0.0),
        "blockedBy": dict(sorted(blocked_by.items(),
                                 key=lambda kv: -kv[1])),
        "oosAllocationsThisPass": evals_done,
        "eligiblePoolMarkets": len(pool_market_ids),
    }
    health = funnel["health"]
    say(f"  health: pool {health['eligiblePoolMarkets']} markets | "
        f"{evals_done} replays allocated | OOS markets/candidate "
        f"avg {health['avgOosMarkets']} / median "
        f"{health['medianOosMarkets']} / max {health['maxOosMarkets']} | "
        f"blocked by: " + (", ".join(
            f"{v} {k}" for k, v in list(health['blockedBy'].items())[:4])
            or "nothing"))
    say(f"  families: {len(family_cache)} hypotheses over {len(view)} "
        f"version rows; {new_events} new independent event(s) this pass "
        f"across {len(families_with_new_evidence)} family(ies); "
        f"{funnel['insufficientEvidence']} version(s) at "
        "INSUFFICIENT_EVIDENCE")

    # §11: a failed strategy teaches the search something, and the lesson has
    # to be readable back. The taxonomy turns "rejected" into which of a dozen
    # different ways of being wrong it was, and each of those implies a
    # different next question.
    if exp_store is not None:
        try:
            experiment_summary = exp_store.summary()
            funnel.update(experiment_summary)
            reasons = experiment_summary.get("experimentFailureReasons") or {}
            if reasons:
                say("  failures classified: " + ", ".join(
                    f"{count} {reason}" for reason, count in
                    sorted(reasons.items(), key=lambda kv: -kv[1])[:4]))
            with contextlib.suppress(OSError):
                (root / "research-directives.json").write_text(
                    json.dumps(exp_store.directives(), indent=1),
                    encoding="utf-8")
        finally:
            exp_store.close()

    if motif_store is not None:
        with contextlib.suppress(Exception):
            motif_store.close()

    library.close()

    # Name the first stage that went to zero — the difference between "it is
    # broken" and "it is early". Ordered exactly as the pipeline flows.
    stage_order = [
        ("rawTrades", "no trades captured or backfilled yet"),
        ("seriesExported", "no series survived the row floor and the "
                           "uncertainty band - more uncertain-period history "
                           "needed"),
        ("discoverySeries", "every exported series was held out - universe "
                            "too small to split"),
        ("seriesResearched", "every discovery series failed in the bridge"),
        ("rankedCandidates", "the search ranked nothing on any series"),
        ("crossMarketCandidates", "no rule was independently found on "
                                  f"{min_tokens}+ series - patterns are not "
                                  "generalizing yet"),
        ("holdoutSeries", "no unseen markets available to validate against"),
        ("tradable", "candidates exist but none has yet cleared honest costs "
                     "on unseen markets - research continues, trading stays "
                     "parked"),
    ]
    funnel["zeroedAt"] = ""
    for key, why in stage_order:
        if int(funnel.get(key) or 0) == 0:
            funnel["zeroedAt"] = key
            funnel["zeroedWhy"] = why
            break
    say("  funnel: "
        + " -> ".join(f"{funnel.get(k, '?')} {k}" for k in (
            "rawTrades", "seriesExported", "seriesResearched",
            "rankedCandidates", "crossMarketCandidates",
            "registeredThisPass", "evaluated", "tradable")))
    if funnel["zeroedAt"]:
        say(f"  first zero at {funnel['zeroedAt']}: {funnel['zeroedWhy']}")

    path = config.data_dir / "strategies.json"
    save(path, result.strategies)
    say(f"  library: {len(view)} strategies on record, {result.accepted} "
        f"VALIDATED and tradable -> {path}")
    return result


def aggregate(per_token: list[tuple[str, list]],
              min_tokens: int = 2,
              market_of: Optional[dict[str, str]] = None
              ) -> list[DiscoveredStrategy]:
    """Keep rules independently RANKED on several tokens.

    With ``market_of``, the operator's no-leakage rule is enforced at MARKET
    level: every market is deterministically assigned to one of two halves,
    and a rule survives only if it was independently discovered and ranked
    on markets in BOTH halves. A pattern that only lives in one half of the
    market universe has not shown it generalizes — it has shown it fits.

    RESEARCH eligibility is not TRADE eligibility. The bridge's own
    acceptance gates were written for a futures instrument (30+ trades per
    series); a Polymarket series offers a handful of cost-clearing moves, so
    requiring bridge acceptance here zeroed the whole pipeline — hundreds of
    ranked candidates per token died invisibly before the library, the OOS
    replay, or the screen ever saw them. A ranked candidate now flows on as
    a RESEARCH candidate; the frozen out-of-sample replay on unseen markets
    (with honest costs) remains the only path to trading, unchanged.
    """
    grouped: dict[str, DiscoveredStrategy] = {}
    for token_id, reports in per_token:
        seen_here: set[str] = set()
        for report in reports:
            rule = report.strategy.to_dict()
            signature = signature_of(rule)
            if signature in seen_here:
                # One token may rank several threshold variants of the same
                # idea; they are one vote from that token, not several.
                continue
            seen_here.add(signature)
            entry = grouped.get(signature)
            metrics = getattr(report, "full", {}) or {}
            if entry is None:
                entry = DiscoveredStrategy(
                    rule=rule, signature=signature,
                    describe=report.strategy.describe())
                grouped[signature] = entry
            entry.tokens.append(token_id)
            entry.accepted_on += 1
            if market_of is not None:
                market = market_of.get(token_id, token_id)
                half = int(hashlib.sha1(market.encode()).hexdigest(), 16) % 2
                entry.halves.add(half)
            entry.score = max(entry.score, float(metrics.get("rank_score", 0.0)))
            entry.sharpe = max(entry.sharpe, float(metrics.get("sharpe", 0.0)))
            entry.oos_sharpe = max(entry.oos_sharpe,
                                   float(metrics.get("oos_sharpe", 0.0)))
            entry.win_rate = max(entry.win_rate,
                                 float(metrics.get("win_rate", 0.0)))
            entry.trades += int(metrics.get("trades", 0))

    kept = [s for s in grouped.values() if s.accepted_on >= min_tokens
            and (market_of is None or len(s.halves) == 2)]
    kept.sort(key=lambda s: (s.accepted_on, s.score), reverse=True)
    return kept


# --------------------------------------------------------------------------
# persistence — what the live engine loads
# --------------------------------------------------------------------------

def save(path: Path, strategies: list[DiscoveredStrategy]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "generatedTs": time.time(),
        "featureColumns": list(FEATURE_NAMES),
        "strategies": [s.to_dict() for s in strategies],
    }, indent=2), encoding="utf-8")


def load_strategies(path: Path) -> list[DiscoveredStrategy]:
    """Read what research produced. Absent or unreadable yields an empty list.

    Deliberately not fatal: a bridge starting with no discovered strategies is
    a bridge that has not researched yet, which is a normal state on day one and
    is reported by the engine rather than crashing the process.
    """
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [DiscoveredStrategy.from_dict(s)
            for s in (data.get("strategies") or []) if isinstance(s, dict)]
