"""FAMILY & MOTIF INTELLIGENCE — what STRUCTURE keeps working, and what keeps
failing.

`meta.py` already asks a question one level above the candidate: of everything
we have tried, which research *structures* survived? This module asks the
question one level above that, and adds the two things `meta` deliberately does
not do:

* **Provenance.** `meta` sums `oos_markets` across a structure's candidates. Two
  candidates tested on the same three markets therefore read as six independent
  markets. That is the single most dangerous number a research layer can
  produce, because it makes a motif look replicated when it has been measured
  once. Here every motif carries the SET of market ids behind it, and
  independent confirmation is counted by disjointness, not by cardinality.
* **Mutation.** A motif that looks promising generates controlled nearby
  hypotheses — one interpretable change at a time — and the remove-one-dimension
  variants that ask which component is actually load-bearing.

Everything in this file steers ALLOCATION. That is not an aspiration, it is an
import graph:

    motif.py  ->  reads library rows, evidence ledgers, adversarial reports
    motif.py  ->  writes only its own knowledge store (motifs.sqlite3)
    reward.py ->  reads a bounded motif weight
    next_status, eligibility, live_gate -> have never heard of this module

A family that has produced eight validated candidates cannot lend one trade of
its evidence to a ninth. A new candidate from a strong family starts at zero
and walks the same ladder as everything else. The only thing a strong family
buys is a place nearer the front of the research queue.

**On the shape motif.** The Discovery board shows recurrences like `2t/1m x 1v`
and `3t/3m x 5v`. Those are not rules and are not hard-coded here as winners —
they are the *evidence shape* of a candidate, and the fact that a shape recurs
among interesting rows is exactly the kind of thing this layer exists to
notice, investigate, and usually refute. `1m` in particular is a warning: a
motif whose evidence lives in one market per candidate is a concentration
motif, and it is scored as one.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from . import meta as meta_mod

# ---------------------------------------------------------------------------
# Standing. A motif with a thin record has no opinion, and says so.
# ---------------------------------------------------------------------------

# Distinct CANDIDATES a motif needs before its weight may move off 1.0.
MIN_CANDIDATES = 4

# ...and INDEPENDENT evidence markets across those candidates — deduplicated,
# so four candidates sharing one market still count as one market.
MIN_INDEPENDENT_MARKETS = 6

# ...and independent confirmations: candidates whose evidence markets do not
# overlap any earlier kept candidate's. Two strategies are not two pieces of
# evidence.
MIN_INDEPENDENT_CANDIDATES = 2

# Shrinkage strength, in candidates, at which a motif's own replication rate
# carries equal weight with the library's base rate.
SHRINKAGE = 8.0

# The bound on the research-priority multiplier. Same discipline as
# `meta.WEIGHT_MIN/MAX`: steering may roughly halve or double a motif's share
# of attention and can do nothing else at all.
WEIGHT_MIN = 0.6
WEIGHT_MAX = 1.6

SURVIVING = ("validated", "high_confidence", "watch", "validating")
TERMINAL = ("rejected", "retired", "quarantined")

# Concentration ceilings used by the failure classifier.
CONCENTRATION_CEILING = 0.7
SINGLE_MARKET_SHARE = 0.6


def _hold_band(rule: dict) -> str:
    return meta_mod._hold_band(rule)


def _direction_of(rule: dict) -> str:
    """LONG/SHORT orientation in one vocabulary.

    Four engines spell direction four ways (`direction: long|short`,
    `direction: up|down`, `side: high|low`). A motif that cannot see that
    `up` and `long` are the same claim cannot ask whether the family is
    directional, which is §11's whole question.
    """
    direction = str(rule.get("direction") or "").lower()
    if direction in ("long", "up", "buy"):
        return "long"
    if direction in ("short", "down", "sell"):
        return "short"
    side = str(rule.get("side") or "").lower()
    if side in ("high", "yes"):
        return "long"
    if side in ("low", "no"):
        return "short"
    return ""


def shape_of(cumulative: dict, versions: int) -> str:
    """The evidence SHAPE — the `2t/1m x 1v` recurrence, bucketed.

    Bucketed rather than exact, because `47t/6m` and `48t/6m` are the same
    shape and treating them as two motifs would guarantee that no shape ever
    reaches standing. The `1m` bucket is kept separate from everything else on
    purpose: single-market evidence is a category of its own.
    """
    trades = int(cumulative.get("trades") or 0)
    markets = int(cumulative.get("markets") or 0)
    if trades <= 0:
        return "untested"
    t_band = ("1-2t" if trades <= 2 else "3-9t" if trades < 10 else
              "10-29t" if trades < 30 else "30t+")
    m_band = ("1m" if markets <= 1 else "2m" if markets == 2 else
              "3-5m" if markets <= 5 else "6m+")
    v_band = ("1v" if versions <= 1 else "2-4v" if versions <= 4 else "5v+")
    return f"{t_band}/{m_band}x{v_band}"


def motifs_of(row: dict, cumulative: Optional[dict] = None,
              versions: int = 1) -> list[tuple[str, str]]:
    """Every structural motif one candidate belongs to.

    Built ON TOP of `meta.structures_of` rather than beside it: the engine,
    feature family, complexity band, sequence length, holding period and any
    regime/category conditions are already normalised there, and re-deriving
    them here is how two layers end up quietly disagreeing about what a
    candidate is. This function adds the dimensions `meta` has no reason to
    carry — direction, evidence shape, variant lineage, and the composite
    'this exact structure' key that lets two differently-NAMED strategies be
    recognised as one motif.
    """
    rule = row.get("rule") or {}
    out: list[tuple[str, str]] = list(meta_mod.structures_of(row))

    direction = _direction_of(rule)
    if direction:
        out.append(("direction", direction))
        hold = _hold_band(rule)
        if hold:
            # The interaction, not just the two marginals. "SHORT works, and
            # short holds work" is a different claim from "SHORT works AT
            # short holds", and only the second is testable as a condition.
            out.append(("direction_hold", f"{direction}:{hold}"))

    variant = str(rule.get("variant") or "")
    if variant:
        out.append(("variant", variant))

    if cumulative is not None:
        out.append(("shape", shape_of(cumulative, versions)))

    if rule.get("wallet") or rule.get("wallets"):
        out.append(("source_type", "wallet-derived"))
    elif rule.get("type") in ("sequence", "sharp_move"):
        out.append(("source_type", "tape-derived"))

    out.append(("structure", structural_signature(row)))
    return [(d, v) for d, v in out if v]


def structural_signature(row: dict) -> str:
    """The NORMALISED structure of a rule — its identity as a shape.

    Deliberately excludes thresholds, feature column names and market ids:
    what is left is what two strategies would have in common if they were
    'the same idea spelled differently'. That is the property §3 asks for —
    normalise structurally equivalent representations so the system can
    recognise one motif under two names.
    """
    rule = row.get("rule") or {}
    structures = dict(meta_mod.structures_of(row))
    parts = [
        str(rule.get("type") or "feature"),
        str(row.get("family") or "") or structures.get("family", "unclassified"),
        _direction_of(rule) or "undirected",
        _hold_band(rule) or "unspecified-hold",
        meta_mod._complexity_band(_complexity(rule)),
    ]
    chain = rule.get("chain") or []
    if chain:
        parts.append(f"chain{len(chain)}")
    feature = str(rule.get("entry_feature") or "")
    if feature:
        parts.append(meta_mod._feature_family(feature))
    return "|".join(parts)


def _complexity(rule: dict) -> int:
    from .sources import rule_complexity
    return rule_complexity(rule)


# ---------------------------------------------------------------------------
# The record
# ---------------------------------------------------------------------------


@dataclass
class MotifRecord:
    """One motif's whole history, WITH provenance.

    `markets`, `categories`, `eras` and `wallets` are sets rather than counts
    on purpose. The moment they become counts, two candidates measured on the
    same market become two markets, and every downstream number — replication,
    breadth, concentration — is inflated by exactly the amount that would make
    a coincidence look like a discovery.
    """

    dimension: str
    value: str
    candidates: list[str] = field(default_factory=list)
    signatures: set = field(default_factory=set)
    tested: int = 0
    surviving: int = 0
    terminal: int = 0
    # provenance
    markets: set = field(default_factory=set)
    categories: set = field(default_factory=set)
    eras: set = field(default_factory=set)
    wallets: set = field(default_factory=set)
    market_pnl: dict = field(default_factory=dict)   # market_id -> first pnl
    market_trades: dict = field(default_factory=dict)
    # per-candidate evidence markets, for the independence walk
    evidence_by_candidate: dict = field(default_factory=dict)
    positive_candidates: set = field(default_factory=set)
    forward_markets: set = field(default_factory=set)
    attacked: int = 0
    survived_attack: int = 0
    broke_under_attack: int = 0
    failure_reasons: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.dimension}={self.value}"

    @property
    def trades(self) -> int:
        return sum(self.market_trades.values())

    @property
    def pnl(self) -> float:
        return sum(self.market_pnl.values())

    @property
    def expectancy(self) -> float:
        trades = self.trades
        return (self.pnl / trades) if trades else 0.0

    @property
    def top_market_share(self) -> float:
        """How much of the motif's POSITIVE P&L sits in its best market."""
        positives = [v for v in self.market_pnl.values() if v > 0]
        return (max(positives) / sum(positives)) if positives else 0.0

    def independent_candidates(self) -> list[str]:
        """Candidates that are genuinely independent CONFIRMATIONS.

        Greedy over the candidates in the order their evidence was first
        earned: a candidate is kept only if its evidence markets are disjoint
        from every kept candidate's. Two strategies replayed on the same
        market are one observation of that market, however different their
        rules look — §19 in six lines.

        Chronological order, not best-first, so the answer cannot be improved
        by picking whichever candidate happened to score well.
        """
        kept: list[str] = []
        used: set = set()
        for cid in self.candidates:
            markets = self.evidence_by_candidate.get(cid) or set()
            if not markets:
                continue
            if markets & used:
                continue
            kept.append(cid)
            used |= markets
        return kept

    @property
    def replication_rate(self) -> float:
        """Of the INDEPENDENT confirmations, how many were positive."""
        independent = self.independent_candidates()
        if not independent:
            return 0.0
        good = sum(1 for cid in independent if cid in self.positive_candidates)
        return good / len(independent)

    @property
    def adversarial_survival(self) -> float:
        return (self.survived_attack / self.attacked) if self.attacked else 0.0

    @property
    def failure_rate(self) -> float:
        return (self.terminal / self.tested) if self.tested else 0.0

    @property
    def has_standing(self) -> bool:
        return (len(self.candidates) >= MIN_CANDIDATES
                and len(self.markets) >= MIN_INDEPENDENT_MARKETS
                and len(self.independent_candidates())
                >= MIN_INDEPENDENT_CANDIDATES
                and self.tested > 0)

    def to_dict(self) -> dict:
        independent = self.independent_candidates()
        return {
            "motif": self.key,
            "dimension": self.dimension,
            "value": self.value,
            "candidates": len(self.candidates),
            "signatures": len(self.signatures),
            "tested": self.tested,
            "surviving": self.surviving,
            "terminal": self.terminal,
            "independentMarkets": len(self.markets),
            "independentCandidates": len(independent),
            "trades": self.trades,
            "expectancy": round(self.expectancy, 6),
            "pnl": round(self.pnl, 4),
            "replicationRate": round(self.replication_rate, 4),
            "topMarketShare": round(self.top_market_share, 4),
            "categories": len(self.categories),
            "eras": len(self.eras),
            "wallets": len(self.wallets),
            "forwardMarkets": len(self.forward_markets),
            "attacked": self.attacked,
            "adversarialSurvival": round(self.adversarial_survival, 4),
            "failureRate": round(self.failure_rate, 4),
            "failureReasons": dict(self.failure_reasons),
            "standing": self.has_standing,
        }


def _era_of(ts: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(float(ts or 0.0)))


def mine(rows: Iterable[dict],
         ledgers: dict[str, list[dict]],
         cumulative: Optional[dict] = None,
         versions: Optional[dict] = None,
         adversarial: Optional[dict] = None,
         market_categories: Optional[dict] = None
         ) -> dict[str, MotifRecord]:
    """Build every motif's record from library rows plus their EVIDENCE
    LEDGERS. Read-only; nothing here writes anywhere.

    `ledgers` maps candidate id -> the rows of `library.market_ledger`, which
    is what carries the market ids. Passing counts instead would be cheaper
    and would silently reintroduce the double-counting this module exists to
    prevent, so the ledger is required rather than optional.
    """
    cumulative = cumulative or {}
    versions = versions or {}
    adversarial = adversarial or {}
    market_categories = market_categories or {}
    records: dict[str, MotifRecord] = {}

    ordered = sorted(rows, key=lambda r: float(r.get("created_ts") or 0.0))
    for row in ordered:
        cid = str(row.get("id") or "")
        if not cid:
            continue
        ledger = ledgers.get(cid) or []
        cum = cumulative.get(cid) or _cumulative_from_ledger(ledger)
        signature = str(row.get("signature") or "")
        nversions = int(versions.get(signature, 1) or 1)
        status = str(row.get("status") or "")
        rule = row.get("rule") or {}
        report = adversarial.get(cid)

        markets = {str(e.get("market_id") or "") for e in ledger
                   if int(e.get("trades") or 0) > 0}
        markets.discard("")
        positive = float(cum.get("expectancy") or 0.0) > 0 \
            and int(cum.get("trades") or 0) > 0

        for dimension, value in motifs_of(row, cum, nversions):
            record = records.setdefault(f"{dimension}={value}",
                                        MotifRecord(dimension, value))
            record.candidates.append(cid)
            record.signatures.add(signature)
            record.evidence_by_candidate[cid] = set(markets)
            if positive:
                record.positive_candidates.add(cid)
            if markets:
                record.tested += 1
                if status in SURVIVING and positive:
                    record.surviving += 1
                elif status in TERMINAL:
                    record.terminal += 1
            for entry in ledger:
                market_id = str(entry.get("market_id") or "")
                trades = int(entry.get("trades") or 0)
                if not market_id or trades <= 0:
                    continue
                # FIRST testimony only. A market that two candidates were both
                # replayed on contributes its result once, to whichever
                # candidate reached it first — the same rule
                # `library.family_cumulative` uses one level down, applied
                # here across candidates instead of across versions.
                if market_id not in record.market_pnl:
                    record.market_pnl[market_id] = float(entry.get("pnl") or 0.0)
                    record.market_trades[market_id] = trades
                    record.eras.add(_era_of(entry.get("ts") or 0.0))
                    category = market_categories.get(market_id)
                    if category:
                        record.categories.add(str(category))
                record.markets.add(market_id)
                if str(entry.get("period") or "") == "forward":
                    record.forward_markets.add(market_id)
            wallet = rule.get("wallet") or rule.get("wallets")
            if wallet:
                for address in ([wallet] if isinstance(wallet, str) else wallet):
                    record.wallets.add(str(address).lower())
            if report is not None:
                verdict = str(getattr(report, "verdict", "") or "")
                if verdict and verdict != "NOT_ATTACKED":
                    record.attacked += 1
                    if verdict == "BROKEN":
                        record.broke_under_attack += 1
                        for name in list(
                                getattr(report, "failed_tests", []) or [])[:3]:
                            record.failure_reasons[str(name)] = \
                                record.failure_reasons.get(str(name), 0) + 1
                    else:
                        record.survived_attack += 1
    return records


def _cumulative_from_ledger(ledger: list[dict]) -> dict:
    """The candidate's own cumulative record, from its ledger alone.

    Used only when the caller did not already have one. Mirrors
    `library.cumulative` for the fields this module reads, so a test can build
    a record without a library.
    """
    rows = [e for e in ledger if int(e.get("trades") or 0) > 0]
    trades = sum(int(e.get("trades") or 0) for e in rows)
    wins = sum(int(e.get("wins") or 0) for e in rows)
    pnl = sum(float(e.get("pnl") or 0.0) for e in rows)
    return {"trades": trades, "wins": wins, "pnl": pnl,
            "markets": len({str(e.get("market_id")) for e in rows}),
            "expectancy": (pnl / trades) if trades else 0.0,
            "win_rate": (wins / trades) if trades else 0.0}


# ---------------------------------------------------------------------------
# The Family Research Score, and what it is allowed to do
# ---------------------------------------------------------------------------


@dataclass
class MotifScore:
    """One motif's research score plus the sentences behind it (§23, §24).

    The explanations are produced HERE, by the same function that produced the
    number, from the same counts. Deriving them at display time is how a
    dashboard ends up confidently explaining a number it did not compute.
    """

    key: str = ""
    score: float = 0.0
    weight: float = 1.0
    components: dict = field(default_factory=dict)
    rewards: list = field(default_factory=list)
    penalties: list = field(default_factory=list)
    why_elevated: str = ""
    why_deprioritised: str = ""
    failure_motif: str = ""

    def to_dict(self) -> dict:
        return {
            "motif": self.key,
            "score": round(self.score, 4),
            "weight": round(self.weight, 4),
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "rewards": list(self.rewards),
            "penalties": list(self.penalties),
            "whyElevated": self.why_elevated,
            "whyDeprioritised": self.why_deprioritised,
            "failureMotif": self.failure_motif,
        }


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def score_motif(record: MotifRecord, base_replication: float = 0.0
                ) -> MotifScore:
    """The FAMILY RESEARCH SCORE (§16). Research priority only.

    Multiplicative, like `library.evidence_score` and for the same reason: a
    motif that replicates beautifully in one market, one category and one
    month has not shown anything, and no amount of expectancy should be able
    to compensate for a zero in breadth. The score is then shrunk toward the
    library's own replication base rate, so a motif is compared with what this
    library typically achieves rather than with perfection.
    """
    out = MotifScore(key=record.key)
    independent = record.independent_candidates()

    if not record.has_standing:
        out.score = 0.0
        out.weight = 1.0
        out.why_deprioritised = (
            f"no standing yet: {len(record.candidates)} candidate(s), "
            f"{len(record.markets)} independent market(s), "
            f"{len(independent)} independent confirmation(s) — needs "
            f"{MIN_CANDIDATES}/{MIN_INDEPENDENT_MARKETS}/"
            f"{MIN_INDEPENDENT_CANDIDATES}")
        out.failure_motif = classify_failure(record)
        return out

    replication = record.replication_rate
    breadth = _clamp(len(record.markets) / 12.0)
    confirmations = _clamp(len(independent) / 4.0)
    categories = _clamp(len(record.categories) / 3.0)
    eras = _clamp(len(record.eras) / 3.0)
    diversification = _clamp(1.0 - record.top_market_share)
    expectancy_term = 1.0 if record.expectancy > 0 else 0.15
    robustness = (record.adversarial_survival if record.attacked
                  else 0.7)   # un-attacked ranks below attacked-and-held

    out.components.update({
        "replication": replication, "breadth": breadth,
        "confirmations": confirmations, "categories": categories,
        "eras": eras, "diversification": diversification,
        "expectancy": expectancy_term, "robustness": robustness,
    })

    if record.expectancy > 0:
        out.rewards.append(
            f"positive pooled unseen expectancy ({record.expectancy:+.4f}) "
            f"over {record.trades} trades")
    else:
        out.penalties.append(
            f"negative pooled unseen expectancy ({record.expectancy:+.4f})")
    if len(independent) >= MIN_INDEPENDENT_CANDIDATES:
        out.rewards.append(
            f"{len(independent)} independent candidate(s) across "
            f"{len(record.markets)} non-overlapping market(s)")
    if len(record.categories) >= 2:
        out.rewards.append(f"holds across {len(record.categories)} categories")
    else:
        out.penalties.append("evidence sits in one category")
    if len(record.eras) < 2:
        out.penalties.append("evidence sits in one time period")
    if record.top_market_share > CONCENTRATION_CEILING:
        out.penalties.append(
            f"P&L concentrated in one market ({record.top_market_share:.0%})")
    if record.attacked and record.adversarial_survival < 0.5:
        out.penalties.append(
            f"{record.broke_under_attack} of {record.attacked} attacked "
            "candidates broke")
    elif record.attacked:
        out.rewards.append(
            f"{record.survived_attack} of {record.attacked} attacked "
            "candidates held")
    if record.failure_rate > 0.5:
        out.penalties.append(
            f"{record.terminal} of {record.tested} tested candidates ended "
            "terminal")

    raw = (expectancy_term * max(0.1, replication) * max(0.1, breadth)
           * max(0.1, confirmations) * max(0.2, categories)
           * max(0.2, eras) * max(0.2, diversification) * robustness)
    out.score = round(_clamp(raw), 4)

    # The bounded steering multiplier, shrunk toward the library base rate.
    n = len(independent)
    base = max(1e-6, base_replication)
    shrunk = (replication * n + base * SHRINKAGE) / (n + SHRINKAGE)
    ratio = shrunk / base
    out.weight = round(min(WEIGHT_MAX, max(WEIGHT_MIN, ratio)), 4)

    out.failure_motif = classify_failure(record)
    if out.failure_motif:
        # A recognised failure motif throttles rather than bans (§13): the
        # weight floor is the throttle, and any future success in the motif
        # lifts it back on its own, because the score is recomputed from
        # evidence every pass rather than latched.
        out.weight = min(out.weight, 0.75)
        out.penalties.append(f"failure motif: {out.failure_motif}")

    out.why_elevated = _explain_elevated(record, out)
    out.why_deprioritised = _explain_deprioritised(record, out)
    return out


def classify_failure(record: MotifRecord) -> str:
    """Recurring FAILURE structure, named (§13). Empty when none applies.

    Only speaks once a motif has been tested enough times for a repetition to
    mean anything; a motif with two failures has had bad luck, not a
    structural problem.
    """
    if record.tested < MIN_CANDIDATES:
        return ""
    if record.terminal >= max(3, int(0.75 * record.tested)) \
            and record.surviving == 0:
        return "ALL_TESTED_CANDIDATES_TERMINAL"
    if record.trades and record.expectancy <= 0 and record.tested >= 4:
        return "NEGATIVE_POOLED_EXPECTANCY"
    if record.markets and record.top_market_share > SINGLE_MARKET_SHARE \
            and len(record.markets) >= 3:
        return "DEPENDS_ON_ONE_MARKET"
    if len(record.independent_candidates()) <= 1 and len(record.candidates) >= 6:
        return "NO_INDEPENDENT_CONFIRMATION"
    if len(record.categories) == 1 and record.tested >= 6:
        return "SINGLE_CATEGORY_DEPENDENCE"
    if record.attacked >= 3 and record.adversarial_survival < 0.34:
        return "BREAKS_UNDER_ATTACK"
    return ""


def _explain_elevated(record: MotifRecord, scored: MotifScore) -> str:
    """§23: 'WHY IS THIS FAMILY IMPORTANT?', from stored counts only."""
    if scored.score <= 0.05 or not scored.rewards:
        return ""
    independent = len(record.independent_candidates())
    return (
        f"elevated because {independent} independent candidate(s) across "
        f"{len(record.markets)} non-overlapping OOS market(s) share this "
        f"structure; pooled cost-adjusted expectancy {record.expectancy:+.4f} "
        f"persists across {len(record.categories)} categor(ies) and "
        f"{len(record.eras)} time period(s); {record.attacked} adversarial "
        f"test(s) run, {record.broke_under_attack} broke. Research priority "
        "only — no candidate is validated by this.")


def _explain_deprioritised(record: MotifRecord, scored: MotifScore) -> str:
    """§24: 'WHY DID THIS FAMILY LOSE PRIORITY?', from stored counts only."""
    if not scored.penalties:
        return ""
    return ("deprioritised because " + "; ".join(scored.penalties[:3])
            + f". {record.terminal} of {record.tested} tested candidate(s) "
              "ended terminal. Still queued — a throttle, not a ban.")


def base_replication_rate(records: dict[str, MotifRecord]) -> float:
    """The library's own replication base rate, over motifs with standing.

    Judging a motif against zero would mark every motif a winner in a library
    that mostly works and every motif a loser in one that mostly does not —
    which is a statement about the library, not about the motif.
    """
    standing = [r for r in records.values() if r.has_standing]
    if not standing:
        return 0.0
    confirmations = sum(len(r.independent_candidates()) for r in standing)
    positives = sum(sum(1 for cid in r.independent_candidates()
                        if cid in r.positive_candidates) for r in standing)
    return (positives / confirmations) if confirmations else 0.0


def score_all(records: dict[str, MotifRecord]) -> dict[str, MotifScore]:
    base = base_replication_rate(records)
    return {key: score_motif(record, base) for key, record in records.items()}


def weight_for(row: dict, scores: dict[str, MotifScore],
               cumulative: Optional[dict] = None, versions: int = 1) -> float:
    """One candidate's combined motif weight — the GEOMETRIC mean.

    A candidate belongs to a dozen motifs at once; multiplying them would
    compound a mild preference into a landslide, and a candidate that is
    merely fashionable in six dimensions would own the whole slate. The mean
    keeps the whole term inside the bounds of any single motif, exactly as
    `meta.weight_for` does for structures.
    """
    values = [scores[f"{d}={v}"].weight
              for d, v in motifs_of(row, cumulative, versions)
              if f"{d}={v}" in scores]
    if not values:
        return 1.0
    product = 1.0
    for value in values:
        product *= max(1e-6, value)
    return round(min(WEIGHT_MAX, max(WEIGHT_MIN,
                                     product ** (1.0 / len(values)))), 4)


def dominant_motif(row: dict, scores: dict[str, MotifScore],
                   cumulative: Optional[dict] = None,
                   versions: int = 1) -> tuple[str, MotifScore]:
    """The motif with the most to say about this candidate, for display.

    'Most to say' is distance from 1.0, not highest weight: a motif that has
    learned this structure keeps failing is exactly as informative as one that
    has learned it keeps working, and showing only the flattering one would
    make the column a cheerleader.
    """
    best_key, best = "", MotifScore()
    for dimension, value in motifs_of(row, cumulative, versions):
        key = f"{dimension}={value}"
        found = scores.get(key)
        if found is None or not found.score and found.weight == 1.0:
            continue
        if abs(found.weight - 1.0) > abs(best.weight - 1.0):
            best_key, best = key, found
    return best_key, best


# ---------------------------------------------------------------------------
# Controlled mutation (§8, §15)
# ---------------------------------------------------------------------------

# One structural change at a time, and never more than this many per parent
# per pass. The point of a mutation is to be INTERPRETABLE — if the variant
# differs in three ways, its result cannot attribute the difference to any of
# them, and the search has bought nothing but compute.
MAX_MUTATIONS_PER_PARENT = 4


def mutations(row: dict, budget: int = MAX_MUTATIONS_PER_PARENT
              ) -> list[tuple[dict, str, str]]:
    """Controlled nearby hypotheses for one candidate.

    Returns ``[(rule, describe, tag), ...]``. Each rule is a fresh hypothesis
    with ZERO inherited evidence — it carries `variant_of` so lineage survives,
    and nothing else. The generator is generic over the structural dimensions
    the rule actually has, so it works for every engine's rule shape and gains
    new dimensions automatically when a rule shape gains a field.

    Deliberately NOT a grid search (§9). Each dimension contributes at most one
    step in each direction, the list is truncated to `budget`, and the caller
    is the existing per-pass variant budget rather than a new one.
    """
    rule = dict(row.get("rule") or {})
    parent = str(row.get("id") or "")
    label = str(row.get("describe") or parent)[:60]
    out: list[tuple[dict, str, str]] = []

    def _emit(changes: dict, tag: str, why: str) -> None:
        variant = dict(rule)
        variant.update(changes)
        variant["variant_of"] = parent
        variant["variant"] = tag
        variant["motif_mutation"] = tag
        out.append((variant, f"{tag.upper()} of {label} — {why}"[:120], tag))

    # -- 1. HOLD: the dimension every engine has in some form ---------------
    if rule.get("hold_bars") is not None:
        bars = int(rule.get("hold_bars") or 15)
        _emit({"hold_bars": max(2, bars // 2)}, "motif-shorter-hold",
              "is the payoff horizon shorter than assumed?")
        _emit({"hold_bars": max(3, bars * 2)}, "motif-longer-hold",
              "is the payoff horizon longer than assumed?")
    elif rule.get("hold_seconds") is not None:
        seconds = float(rule.get("hold_seconds") or 3600.0)
        _emit({"hold_seconds": max(600.0, seconds / 2.0)},
              "motif-shorter-hold", "is the payoff horizon shorter?")
        _emit({"hold_seconds": seconds * 2.0}, "motif-longer-hold",
              "is the payoff horizon longer?")

    # -- 2. DIRECTION: §11 asks it of every family, not just losers --------
    direction = _direction_of(rule)
    if direction:
        if rule.get("side") is not None:
            _emit({"side": "low" if str(rule.get("side")) == "high" else "high"},
                  "motif-inverse", "if this is directional, the other side "
                                   "should NOT pay")
        else:
            flipped = {"long": "short", "short": "long",
                       "up": "down", "down": "up"}
            current = str(rule.get("direction") or "")
            if current in flipped:
                _emit({"direction": flipped[current]}, "motif-inverse",
                      "if this is directional, the other side should NOT pay")

    # -- 3. ENTRY TIMING: is it the signal, or the instant? ----------------
    if rule.get("type") in ("sequence", "sharp_move"):
        _emit({"delay_bars": int(rule.get("delay_bars") or 0) + 2},
              "motif-delayed-entry",
              "does the edge survive being entered late?")

    # -- 4. REMOVE-ONE-DIMENSION (§15): which component is load-bearing? ---
    chain = [str(c) for c in (rule.get("chain") or [])]
    if len(chain) > 2:
        _emit({"chain": chain[1:]}, "motif-drop-first-link",
              "is the first condition doing any work?")
        _emit({"chain": chain[:-1]}, "motif-drop-last-link",
              "is the last condition doing any work?")
    for key in ("regime", "category", "band", "liquidity"):
        if rule.get(key):
            stripped = dict(rule)
            stripped.pop(key, None)
            variant = dict(stripped)
            variant["variant_of"] = parent
            variant["variant"] = f"motif-drop-{key}"
            variant["motif_mutation"] = f"motif-drop-{key}"
            out.append((variant,
                        f"MOTIF-DROP-{key.upper()} of {label} — is the "
                        f"{key} condition load-bearing?"[:120],
                        f"motif-drop-{key}"))

    # -- 5. WINDOW: the effect that lives at exactly one threshold ---------
    if rule.get("gap_bars") is not None:
        _emit({"gap_bars": int(rule.get("gap_bars") or 15) * 2},
              "motif-wider-window",
              "does the effect live at exactly one threshold?")

    return out[:max(0, int(budget))]


def counterfactual_questions(record: MotifRecord) -> list[str]:
    """§14: what SHOULD fail if this motif is real, stated as questions.

    Generated from the record's own composition, so a motif with no wallet in
    its evidence is never asked whether the wallet is causal. Every question
    here is answered by an ordinary candidate walking the ordinary ladder —
    this function raises them, it does not answer them.
    """
    out = [
        "if this structure is meaningful, does removing one component "
        "destroy the effect? (remove-one-dimension variants)",
        "if the directional reading is correct, does the inverse fail? "
        "(inverse variant)",
    ]
    if len(record.markets) >= 3:
        out.append(
            f"does the effect survive removing its strongest market "
            f"({record.top_market_share:.0%} of positive P&L)?")
    if record.wallets:
        out.append(
            f"if the wallet is causal, does the motif survive without "
            f"{len(record.wallets)} wallet-derived candidate(s)?")
    if len(record.categories) >= 2:
        out.append("does the effect hold in a category it has not been "
                   "tested in?")
    else:
        out.append("does the effect exist outside its single category, or is "
                   "the category the effect?")
    if record.dimension == "shape":
        out.append("is the evidence shape a property of the strategies, or "
                   "of how much data the allocator gave them?")
    return out


# ---------------------------------------------------------------------------
# Search scale (§27): how many things did we look at before finding this?
# ---------------------------------------------------------------------------


@dataclass
class SearchScale:
    """The multiple-testing ledger.

    A motif layer that examines two thousand structures and reports its best
    one as though it were discovered in isolation has not found a pattern, it
    has found the maximum of two thousand draws. This is the denominator, and
    it is carried into the funnel so it is visible next to the discovery.
    """

    families_examined: int = 0
    motifs_examined: int = 0
    motifs_with_standing: int = 0
    mutations_generated: int = 0
    mutations_registered: int = 0
    promising: int = 0
    replicated: int = 0
    failure_motifs: int = 0

    def to_dict(self) -> dict:
        return {
            "motifFamiliesExamined": self.families_examined,
            "motifsExamined": self.motifs_examined,
            "motifsWithStanding": self.motifs_with_standing,
            "motifMutationsGenerated": self.mutations_generated,
            "motifMutationsRegistered": self.mutations_registered,
            "motifsPromising": self.promising,
            "motifsReplicated": self.replicated,
            "motifFailures": self.failure_motifs,
            # The honest headline: of everything examined, how much survived.
            "motifSurvivalShare": (
                round(self.replicated / self.motifs_examined, 5)
                if self.motifs_examined else 0.0),
        }


def search_scale(records: dict[str, MotifRecord],
                 scores: dict[str, MotifScore]) -> SearchScale:
    scale = SearchScale()
    scale.motifs_examined = len(records)
    scale.families_examined = len({r.value for r in records.values()
                                   if r.dimension == "structure"})
    for key, record in records.items():
        if record.has_standing:
            scale.motifs_with_standing += 1
        scored = scores.get(key)
        if scored is None:
            continue
        if scored.failure_motif:
            scale.failure_motifs += 1
        if scored.score >= 0.25:
            scale.promising += 1
        if (record.has_standing and record.replication_rate >= 0.5
                and len(record.independent_candidates())
                >= MIN_INDEPENDENT_CANDIDATES
                and record.expectancy > 0):
            scale.replicated += 1
    return scale


# ---------------------------------------------------------------------------
# The knowledge store: lineage, versions, and history that is never rewritten
# ---------------------------------------------------------------------------

_SCHEMA = """
-- A motif DEFINITION, versioned. §21: when the structural definition of a
-- family changes, its evidence lineage starts again rather than being
-- silently re-attributed to the new definition. The old row stays.
CREATE TABLE IF NOT EXISTS motif_versions (
    motif_key    TEXT NOT NULL,
    version      INTEGER NOT NULL,
    definition   TEXT NOT NULL,          -- json: the dims that define it
    def_hash     TEXT NOT NULL,
    first_seen   REAL NOT NULL,
    last_seen    REAL NOT NULL,
    PRIMARY KEY (motif_key, version)
);

-- Append-only. One row per motif per pass that had something to say. Never
-- updated: a research conclusion that can be edited later is not a record.
CREATE TABLE IF NOT EXISTS motif_history (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL NOT NULL,
    motif_key    TEXT NOT NULL,
    version      INTEGER NOT NULL,
    score        REAL DEFAULT 0,
    weight       REAL DEFAULT 1,
    candidates   INTEGER DEFAULT 0,
    independent_candidates INTEGER DEFAULT 0,
    independent_markets    INTEGER DEFAULT 0,
    trades       INTEGER DEFAULT 0,
    expectancy   REAL DEFAULT 0,
    replication  REAL DEFAULT 0,
    failure      TEXT DEFAULT '',
    why_elevated TEXT DEFAULT '',
    why_stopped  TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mh_key ON motif_history(motif_key, ts);

-- The relationship model (§18). Deliberately a plain edge list rather than a
-- graph engine: the queries this layer asks are one hop deep.
CREATE TABLE IF NOT EXISTS motif_links (
    src   TEXT NOT NULL,
    kind  TEXT NOT NULL,          -- CANDIDATE|MUTATION|INVERSE|RELATED|REGIME
    dst   TEXT NOT NULL,
    ts    REAL NOT NULL,
    note  TEXT DEFAULT '',
    PRIMARY KEY (src, kind, dst)
);
CREATE INDEX IF NOT EXISTS idx_ml_dst ON motif_links(dst, kind);

-- §27's denominator, per pass.
CREATE TABLE IF NOT EXISTS motif_search (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    scale  TEXT NOT NULL
);
"""


class MotifStore:
    """Persistent motif knowledge. Reads library evidence; writes only here.

    Storage discipline copied from `journal.py` and `experiments.py` — one
    connection under an RLock, WAL, state-change-only writes — so the research
    pass can call it without thinking about concurrency.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- versioning ---------------------------------------------------------

    def version_for(self, motif_key: str, definition: dict) -> int:
        """The current version of this motif's definition, creating one if the
        definition has CHANGED.

        The whole of §21 lives here. A changed definition does not overwrite
        the old row and does not re-stamp the old history: it gets the next
        version number, and every history row written from then on carries it.
        Old conclusions therefore stay attached to the definition that
        produced them, which is what makes them still true.
        """
        payload = json.dumps(definition, sort_keys=True, default=str)
        digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT version, def_hash FROM motif_versions "
                "WHERE motif_key=? ORDER BY version DESC LIMIT 1",
                (motif_key,)).fetchone()
            if row is not None and str(row["def_hash"]) == digest:
                self._conn.execute(
                    "UPDATE motif_versions SET last_seen=? "
                    "WHERE motif_key=? AND version=?",
                    (now, motif_key, int(row["version"])))
                self._conn.commit()
                return int(row["version"])
            version = (int(row["version"]) + 1) if row is not None else 1
            self._conn.execute(
                "INSERT INTO motif_versions(motif_key, version, definition, "
                "def_hash, first_seen, last_seen) VALUES(?,?,?,?,?,?)",
                (motif_key, version, payload, digest, now, now))
            self._conn.commit()
            return version

    # -- history ------------------------------------------------------------

    def record(self, record: MotifRecord, scored: MotifScore,
               version: int) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO motif_history(ts, motif_key, version, score, "
                "weight, candidates, independent_candidates, "
                "independent_markets, trades, expectancy, replication, "
                "failure, why_elevated, why_stopped) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), record.key, int(version),
                 float(scored.score), float(scored.weight),
                 len(record.candidates),
                 len(record.independent_candidates()), len(record.markets),
                 record.trades, float(record.expectancy),
                 float(record.replication_rate), scored.failure_motif,
                 scored.why_elevated, scored.why_deprioritised))
            self._conn.commit()

    def history(self, motif_key: str, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM motif_history WHERE motif_key=? "
                "ORDER BY ts DESC LIMIT ?", (motif_key, int(limit))).fetchall()
        return [dict(r) for r in rows]

    def latest(self, limit: int = 200) -> list[dict]:
        """The most recent record for each motif, best first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT h.* FROM motif_history h JOIN ("
                "  SELECT motif_key, MAX(ts) mts FROM motif_history "
                "  GROUP BY motif_key) latest "
                "ON h.motif_key = latest.motif_key AND h.ts = latest.mts "
                "ORDER BY h.score DESC LIMIT ?", (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # -- lineage ------------------------------------------------------------

    def link(self, src: str, kind: str, dst: str, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO motif_links(src, kind, dst, ts, note) "
                "VALUES(?,?,?,?,?) ON CONFLICT(src, kind, dst) DO UPDATE "
                "SET ts=excluded.ts", (src, kind, dst, time.time(), note))
            self._conn.commit()

    def links_from(self, src: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM motif_links WHERE src=? ORDER BY ts DESC",
                (src,)).fetchall()
        return [dict(r) for r in rows]

    def lineage_of(self, candidate_id: str) -> list[dict]:
        """Every motif that has ever claimed this candidate, and how."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM motif_links WHERE dst=? ORDER BY ts",
                (candidate_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- search scale -------------------------------------------------------

    def record_scale(self, scale: SearchScale) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO motif_search(ts, scale) VALUES(?,?)",
                (time.time(), json.dumps(scale.to_dict())))
            self._conn.commit()

    def cumulative_scale(self) -> dict:
        """Every pass's search scale, summed. §27's real denominator: what
        matters is not how many motifs this pass examined but how many have
        ever been examined before the current best one was reported."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT scale FROM motif_search").fetchall()
        totals: dict[str, Any] = {"passes": len(rows)}
        for row in rows:
            try:
                payload = json.loads(row["scale"])
            except (TypeError, ValueError):
                continue
            for key, value in payload.items():
                if isinstance(value, (int, float)) and key != \
                        "motifSurvivalShare":
                    totals[key] = totals.get(key, 0) + value
        return totals


# ---------------------------------------------------------------------------
# The pass: the one entry point research.py calls
# ---------------------------------------------------------------------------


@dataclass
class MotifPass:
    """What one motif pass produced. Purely descriptive."""

    records: dict = field(default_factory=dict)
    scores: dict = field(default_factory=dict)
    scale: SearchScale = field(default_factory=SearchScale)
    mutations: list = field(default_factory=list)   # (parent_row, rule, desc)

    def report(self, limit: int = 40) -> list[dict]:
        rows = []
        for key, record in self.records.items():
            scored = self.scores.get(key)
            if scored is None or not record.has_standing:
                continue
            entry = record.to_dict()
            entry.update(scored.to_dict())
            rows.append(entry)
        rows.sort(key=lambda e: -float(e.get("score") or 0.0))
        return rows[:limit]

    def summary(self) -> dict:
        out = self.scale.to_dict()
        top = self.report(1)
        out["motifStrongest"] = top[0]["motif"] if top else ""
        out["motifStrongestWhy"] = top[0].get("whyElevated", "") if top else ""
        failures = [s.failure_motif for s in self.scores.values()
                    if s.failure_motif]
        out["motifFailureKinds"] = len(set(failures))
        return out


def run_pass(rows: list[dict], ledgers: dict[str, list[dict]],
             cumulative: Optional[dict] = None,
             versions: Optional[dict] = None,
             adversarial: Optional[dict] = None,
             market_categories: Optional[dict] = None,
             store: Optional[MotifStore] = None,
             mutation_budget: int = 6) -> MotifPass:
    """Mine, score, remember, and propose. The whole layer in one call.

    Order matters and is not arbitrary: mining reads evidence, scoring reads
    the mined records, persistence records what was scored, and mutation
    proposes from what scored well. Nothing later in that chain can reach back
    and change anything earlier in it, which is why a promising motif cannot
    end up having improved the evidence that made it promising.
    """
    records = mine(rows, ledgers, cumulative, versions, adversarial,
                   market_categories)
    scores = score_all(records)
    scale = search_scale(records, scores)

    if store is not None:
        for key, record in records.items():
            scored = scores[key]
            if not record.has_standing and not scored.failure_motif:
                continue          # nothing to say; do not write noise
            version = store.version_for(key, {
                "dimension": record.dimension, "value": record.value,
                "definition": "motifs_of/v1"})
            store.record(record, scored, version)
            for cid in record.candidates[:64]:
                store.link(key, "CANDIDATE", cid)
        store.record_scale(scale)

    # -- mutation: only from motifs that have EARNED a nearby look ----------
    by_id = {str(r.get("id") or ""): r for r in rows}
    proposals: list[tuple[dict, dict, str]] = []
    promising = sorted(
        ((key, records[key], scores[key]) for key in records
         if records[key].has_standing and scores[key].score >= 0.25
         and not scores[key].failure_motif),
        key=lambda item: -item[2].score)
    seen_parents: set = set()
    for _key, record, _scored in promising:
        for cid in record.independent_candidates():
            if len(proposals) >= mutation_budget:
                break
            if cid in seen_parents:
                continue
            parent = by_id.get(cid)
            if parent is None:
                continue
            seen_parents.add(cid)
            for rule, describe, _tag in mutations(parent, 2):
                if len(proposals) >= mutation_budget:
                    break
                proposals.append((parent, rule, describe))
        if len(proposals) >= mutation_budget:
            break
    scale.mutations_generated = len(proposals)

    return MotifPass(records=records, scores=scores, scale=scale,
                     mutations=proposals)
