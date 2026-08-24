"""META-DISCOVERY: which research STRUCTURES survive, not which rules do.

Every other part of this system asks whether a particular candidate works.
This one asks a level up — of everything we have ever tried, which *kinds* of
research produced candidates that survived contact with unseen markets? Feature
families, sequence lengths, holding periods, market regimes, categories,
discovery engines, complexity. The answer steers where future compute goes.

Three constraints make it safe, and all three are the difference between
meta-learning and a slow-motion overfit of the whole library:

* **It allocates, it never validates.** The output is a multiplier on research
  priority, bounded, and priority decides only what is looked at next (§17).
  No structure weight can promote a candidate, and none is an input to any
  gate.
* **It never rewrites history.** §10 is explicit: do not use future validation
  results to rewrite historical validation results. Nothing here writes to
  `validations`; it only reads.
* **It requires evidence before it has an opinion.** A structure with a thin
  record gets a weight of exactly 1.0 — no bonus, no penalty. Structures are
  many and evidence is scarce, so without this the layer would mostly be
  amplifying noise, and amplified noise is indistinguishable from insight.

The measure is not raw expectancy. It is **survival**: the share of a
structure's candidates that reached a non-terminal status carrying positive
unseen-market evidence, shrunk toward the library's own base rate in
proportion to how little of a record stands behind it. A structure with two
lucky candidates does not get to redirect the research budget.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from .sources import rule_complexity, source_of

# Candidates a structure needs before its weight may move off 1.0 at all.
MIN_CANDIDATES = 6

# ...and independent evidence markets across those candidates. A structure
# whose entire record is four candidates tested on one market knows nothing.
MIN_EVIDENCE_MARKETS = 8

# Shrinkage strength: the number of candidates at which a structure's own
# survival rate carries equal weight with the library base rate.
SHRINKAGE = 12.0

# The bound. Meta-learning steers effort; it never decides anything, so the
# most it can do is roughly halve or double a structure's share of attention.
WEIGHT_MIN = 0.6
WEIGHT_MAX = 1.6

SURVIVING = ("validated", "high_confidence", "watch", "validating")
TERMINAL = ("rejected", "retired", "quarantined")


@dataclass
class StructureRecord:
    """One research structure's whole history."""

    dimension: str
    value: str
    candidates: int = 0
    tested: int = 0
    surviving: int = 0
    rejected: int = 0
    evidence_markets: int = 0
    evidence_trades: int = 0
    pnl: float = 0.0
    forward_markets: int = 0
    members: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.dimension}={self.value}"

    @property
    def survival_rate(self) -> float:
        """Of the candidates that were actually TESTED, how many survived.

        Untested candidates are excluded from both sides. Counting them as
        failures would penalise whichever structure the allocator has not got
        to yet — and since the allocator reads this, that is a loop where
        being under-researched makes you more under-researched.
        """
        return (self.surviving / self.tested) if self.tested else 0.0

    @property
    def expectancy(self) -> float:
        return (self.pnl / self.evidence_trades) if self.evidence_trades \
            else 0.0

    @property
    def has_standing(self) -> bool:
        return (self.candidates >= MIN_CANDIDATES
                and self.evidence_markets >= MIN_EVIDENCE_MARKETS
                and self.tested > 0)


def structures_of(row: dict) -> list[tuple[str, str]]:
    """The research structures one candidate belongs to.

    A candidate belongs to several at once — it has an engine, a feature
    family, a holding period, a complexity band — and each is measured
    separately. That is the point: it lets the layer say "sequence discovery
    is working but only at length two", which no single label could.
    """
    rule = row.get("rule") or {}
    out: list[tuple[str, str]] = [
        ("engine", str(row.get("source") or "") or source_of(rule)),
        ("family", str(row.get("family") or "") or "unclassified"),
        ("complexity", _complexity_band(rule_complexity(rule))),
    ]

    chain = rule.get("chain") or []
    if chain:
        out.append(("sequence_length", str(len(chain))))

    hold = _hold_band(rule)
    if hold:
        out.append(("holding_period", hold))

    feature = str(rule.get("entry_feature") or "")
    if feature:
        out.append(("feature_family", _feature_family(feature)))

    for key, dimension in (("category", "category"), ("regime", "regime"),
                           ("liquidity", "liquidity_regime"),
                           ("band", "probability_band")):
        value = str(rule.get(key) or "")
        if value:
            out.append((dimension, value))
    return out


def _complexity_band(count: int) -> str:
    if count <= 3:
        return "simple"
    if count <= 6:
        return "moderate"
    return "complex"


def _hold_band(rule: dict) -> str:
    if str(rule.get("hold") or "") == "resolution":
        return "resolution"
    if rule.get("hold_seconds") is not None:
        seconds = float(rule.get("hold_seconds") or 0)
        return ("immediate" if seconds <= 900 else
                "short" if seconds <= 7200 else
                "medium" if seconds <= 86_400 else "long")
    if rule.get("hold_bars") is not None:
        bars = int(rule.get("hold_bars") or 0)
        return ("immediate" if bars <= 5 else
                "short" if bars <= 20 else "medium")
    return ""


def _feature_family(feature: str) -> str:
    for prefix, name in (("np_", "book-structure"), ("ms_", "market-state"),
                         ("liq_", "liquidation"), ("wallet_", "wallet-flow"),
                         ("regime_", "regime"), ("tape_", "tape"),
                         ("flow_", "flow"), ("price", "price"),
                         ("mid", "price"), ("volume", "volume")):
        if feature.startswith(prefix):
            return name
    if "spread" in feature or "depth" in feature:
        return "order-book"
    return "other"


def measure(rows: Iterable[dict]) -> dict[str, StructureRecord]:
    """Build every structure's record from library rows. Read-only."""
    records: dict[str, StructureRecord] = {}
    for row in rows:
        status = str(row.get("status") or "")
        trades = int(row.get("oos_trades") or 0)
        markets = int(row.get("oos_markets") or 0)
        expectancy = float(row.get("oos_expectancy") or 0.0)
        for dimension, value in structures_of(row):
            if not value:
                continue
            record = records.setdefault(f"{dimension}={value}",
                                        StructureRecord(dimension, value))
            record.candidates += 1
            record.members.append(str(row.get("id") or ""))
            record.evidence_markets += markets
            record.evidence_trades += trades
            record.pnl += expectancy * trades
            record.forward_markets += int(row.get("oos_forward_markets") or 0)
            if trades > 0:
                record.tested += 1
                if status in SURVIVING and expectancy > 0:
                    record.surviving += 1
                elif status in TERMINAL:
                    record.rejected += 1
    return records


def weights(records: dict[str, StructureRecord]) -> dict[str, float]:
    """Structure -> bounded research-priority multiplier.

    Shrunk toward the library's own base rate, so a structure is compared
    with what this library typically achieves rather than with zero. Judging
    against zero would mark every structure a winner in a library that mostly
    works and every structure a loser in one that mostly does not — which is
    a statement about the library, not about the structure.
    """
    tested = sum(r.tested for r in records.values())
    survived = sum(r.surviving for r in records.values())
    base = (survived / tested) if tested else 0.0

    out: dict[str, float] = {}
    for key, record in records.items():
        if not record.has_standing or base <= 0:
            out[key] = 1.0
            continue
        n = record.tested
        shrunk = ((record.survival_rate * n + base * SHRINKAGE)
                  / (n + SHRINKAGE))
        ratio = shrunk / base
        out[key] = round(min(WEIGHT_MAX, max(WEIGHT_MIN, ratio)), 4)
    return out


def weight_for(row: dict, table: dict[str, float]) -> float:
    """One candidate's combined structure weight.

    The GEOMETRIC mean of its structures, not the product. A candidate
    belongs to six or seven structures at once, and multiplying them would
    compound a mild preference into a landslide — a rule that is simple AND
    short-hold AND from a good engine would end up with a 4x multiplier off
    three weak signals. The mean keeps the whole term inside the same bounds
    as any single structure.
    """
    values = [table.get(f"{d}={v}", 1.0) for d, v in structures_of(row) if v]
    if not values:
        return 1.0
    product = 1.0
    for value in values:
        product *= max(1e-6, value)
    return round(min(WEIGHT_MAX, max(WEIGHT_MIN,
                                     product ** (1.0 / len(values)))), 4)


def report(records: dict[str, StructureRecord],
           table: dict[str, float], min_candidates: int = MIN_CANDIDATES
           ) -> list[dict]:
    """What survives, ordered. For the operator, and for §10's question."""
    out = []
    for key, record in records.items():
        if record.candidates < min_candidates:
            continue
        out.append({
            "structure": key,
            "dimension": record.dimension,
            "value": record.value,
            "candidates": record.candidates,
            "tested": record.tested,
            "surviving": record.surviving,
            "rejected": record.rejected,
            "survivalRate": round(record.survival_rate, 4),
            "evidenceMarkets": record.evidence_markets,
            "evidenceTrades": record.evidence_trades,
            "expectancy": round(record.expectancy, 6),
            "forwardMarkets": record.forward_markets,
            "weight": table.get(key, 1.0),
            "standing": record.has_standing,
        })
    out.sort(key=lambda entry: (-entry["weight"], -entry["survivalRate"]))
    return out


def summary(records: dict[str, StructureRecord],
            table: dict[str, float]) -> dict:
    """§23/§10 headline: is meta-discovery saying anything yet?"""
    with_standing = [k for k, r in records.items() if r.has_standing]
    moved = [k for k in with_standing if abs(table.get(k, 1.0) - 1.0) > 0.01]
    ranked = sorted(with_standing, key=lambda k: -table.get(k, 1.0))
    return {
        "metaStructures": len(records),
        "metaStructuresWithStanding": len(with_standing),
        "metaStructuresSteering": len(moved),
        "metaStrongest": ranked[0] if ranked else "",
        "metaWeakest": ranked[-1] if ranked else "",
        # The honest headline. Until structures have records, this layer's
        # correct output is "nothing yet", and it should say so rather than
        # producing confident-looking weights of 1.0.
        "metaHasOpinion": bool(moved),
    }
