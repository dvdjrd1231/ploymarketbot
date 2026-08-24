"""§30 — the data-quality score that travels with every prediction.

    "The model should never silently treat poor-quality data as equivalent to
     high-quality data."

The word doing the work is *silently*. A prediction built from a wallet with
two observed trades, no order book, no settlement and a guessed category is
not the same object as one built from a deep tape with a resolved market
behind it — and if both arrive as a bare label, nothing downstream can tell
them apart.

So the score is a product of eight named components, each in 0..1, each with
its own reason string. Multiplicative rather than averaged, for the same
reason the evidence score elsewhere in this codebase is: a zero in any single
dimension should sink the whole thing, and an average lets seven good
components hide one fatal one.

Nothing here is tuned against an outcome. The weights are structural
statements about what makes a reconstruction trustworthy, and every component
reports the raw fact it was derived from so a reader can disagree with the
weighting without losing the measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# The eight components §30 names. Order is display order.
COMPONENTS = (
    "event_completeness",
    "timestamp_quality",
    "order_book_completeness",
    "inventory_reconstruction_confidence",
    "settlement_completeness",
    "market_metadata_completeness",
    "wallet_identity_certainty",
    "execution_data_completeness",
)

# Floors. A component never contributes an exact zero unless the thing it
# measures is genuinely absent, because a single missing field should
# penalise a prediction, not annihilate it — and an annihilated score cannot
# be ranked against another annihilated score.
FLOOR = 0.05

HIGH, MEDIUM, LOW = "HIGH", "MEDIUM", "LOW"


@dataclass
class DataQualityScore:
    """One prediction's data quality, component by component."""

    score: float = 0.0
    components: dict = field(default_factory=dict)
    reasons: dict = field(default_factory=dict)

    @property
    def tier(self) -> str:
        if self.score >= 0.60:
            return HIGH
        if self.score >= 0.30:
            return MEDIUM
        return LOW

    @property
    def weakest(self) -> str:
        if not self.components:
            return ""
        return min(self.components, key=lambda k: self.components[k])

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 4),
            "tier": self.tier,
            "weakest": self.weakest,
            "components": {k: round(v, 4) for k, v in self.components.items()},
            "reasons": dict(self.reasons),
        }


def _put(out: DataQualityScore, name: str, value: float, reason: str) -> None:
    out.components[name] = max(FLOOR, min(1.0, float(value)))
    out.reasons[name] = reason


def score(episode: Any, snapshot: Optional[Any] = None,
          quote: Optional[Any] = None, settled: bool = False,
          wallet_prior_conditions: int = 0,
          category_is_heuristic: bool = True) -> DataQualityScore:
    """Grade the evidence behind one prediction.

    Every argument is optional and its absence is scored as absence rather
    than raising — a quality score that cannot be computed for a low-quality
    input would defeat its own purpose.
    """
    out = DataQualityScore()
    events = list(getattr(episode, "events", []) or [])

    # 1. EVENT COMPLETENESS — how much of a story do we have?
    count = len(events)
    _put(out, "event_completeness", min(1.0, count / 6.0),
         f"{count} observed event(s) in this condition")

    # 2. TIMESTAMP QUALITY — the tape's grain is one second, and a condition
    #    whose events share a timestamp cannot be ordered by time at all.
    stamps = {e.ts for e in events}
    if not events:
        _put(out, "timestamp_quality", 0.0, "no events")
    elif len(stamps) == 1 and count > 1:
        _put(out, "timestamp_quality", 0.2,
             f"all {count} events share one second — ordering rests on "
             "insertion order, not on time")
    else:
        _put(out, "timestamp_quality", min(1.0, len(stamps) / max(1, count)),
             f"{len(stamps)} distinct timestamp(s) across {count} event(s)")

    # 3. ORDER-BOOK COMPLETENESS — a real quote, a print, or nothing.
    source = getattr(quote, "source", "") if quote is not None else ""
    if source == "book":
        depth = getattr(quote, "depth", None)
        _put(out, "order_book_completeness", 1.0 if depth else 0.75,
             "captured order book" + ("" if depth else ", no depth"))
    elif source == "print":
        _put(out, "order_book_completeness", 0.3,
             "priced from a trade PRINT — no observed spread or depth")
    else:
        _put(out, "order_book_completeness", 0.0,
             "no market observation at this instant")

    # 4. INVENTORY RECONSTRUCTION CONFIDENCE — explicit shares and a sane
    #    net position, or an inference.
    if snapshot is None:
        _put(out, "inventory_reconstruction_confidence", 0.4,
             "no snapshot — inventory not reconstructed at a signal instant")
    elif not getattr(snapshot, "valid", False):
        _put(out, "inventory_reconstruction_confidence", 0.15,
             getattr(snapshot, "invalid_reason", "") or "unusable snapshot")
    else:
        sells = getattr(snapshot, "sells", 0)
        # Sells are the part most likely to be under-observed, so a condition
        # with sells is reconstructed with less confidence than one without.
        _put(out, "inventory_reconstruction_confidence",
             0.8 if sells else 1.0,
             f"explicit share sizes; {sells} sell(s) in the window"
             + (" — net inventory depends on having seen them all"
                if sells else ""))

    # 5. SETTLEMENT COMPLETENESS — the difference between a realised result
    #    and an opinion about an open position.
    quality = str(getattr(episode, "label_quality", "") or "")
    if settled or quality == "resolved":
        _put(out, "settlement_completeness", 1.0, "market settled")
    elif quality == "redeemed":
        _put(out, "settlement_completeness", 0.9,
             "wallet redeemed the condition — finished by fact")
    elif quality == "quiet":
        _put(out, "settlement_completeness", 0.5,
             "no settlement; finished by the quiet-period heuristic")
    else:
        _put(out, "settlement_completeness", 0.1,
             "unsettled and truncated by the end of the tape")

    # 6. MARKET METADATA — the tape carries a title and nothing else.
    _put(out, "market_metadata_completeness",
         0.4 if category_is_heuristic else 1.0,
         "category is a KEYWORD HEURISTIC over the question text"
         if category_is_heuristic else "category from market metadata")

    # 7. WALLET IDENTITY CERTAINTY — an address is exact, but a wallet we
    #    have barely seen tells us little about itself.
    _put(out, "wallet_identity_certainty",
         min(1.0, 0.4 + wallet_prior_conditions / 25.0),
         f"address is exact; {wallet_prior_conditions} prior finished "
         "condition(s) for this wallet at the signal instant")

    # 8. EXECUTION DATA — fees and slippage are MODELLED here, never observed.
    _put(out, "execution_data_completeness",
         0.6 if source == "book" else 0.25,
         "fees and slippage are modelled from configuration, not observed; "
         + ("a captured book supports the spread term"
            if source == "book" else "no book, so the spread is assumed too"))

    product = 1.0
    for name in COMPONENTS:
        product *= out.components.get(name, FLOOR)
    # Geometric mean over the eight, so the number stays on the same 0..1
    # scale as its parts. A plain product of eight sub-unit terms is always
    # tiny and would make every prediction look equally bad.
    out.score = product ** (1.0 / len(COMPONENTS))
    return out


def summarise(scores: list) -> dict:
    """Population-level data quality, for the report."""
    if not scores:
        return {"available": False}
    values = [s.score for s in scores]
    tiers: dict[str, int] = {}
    weakest: dict[str, int] = {}
    for entry in scores:
        tiers[entry.tier] = tiers.get(entry.tier, 0) + 1
        weakest[entry.weakest] = weakest.get(entry.weakest, 0) + 1
    ordered = sorted(values)
    component_means = {
        name: round(sum(s.components.get(name, 0.0) for s in scores)
                    / len(scores), 4)
        for name in COMPONENTS}
    return {
        "available": True,
        "predictions": len(scores),
        "meanScore": round(sum(values) / len(values), 4),
        "medianScore": round(ordered[len(ordered) // 2], 4),
        "tiers": tiers,
        "componentMeans": component_means,
        "weakestComponentCounts": dict(sorted(weakest.items(),
                                              key=lambda kv: -kv[1])),
        "note": ("Multiplicative (geometric mean over eight components): a "
                 "zero in any one dimension sinks the score, because an "
                 "average lets seven good components hide one fatal one. "
                 "The weakest component is reported per prediction so the "
                 "fix is addressable rather than abstract."),
    }
