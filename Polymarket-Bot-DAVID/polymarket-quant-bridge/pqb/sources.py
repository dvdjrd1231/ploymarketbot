"""ONE candidate object, whatever discovered it (§11).

Discovery is plural; validation is singular. That principle is already how this
system is built — threshold rules, sequence chains, sharp-move cells, longshot
bands, wallet-state models and wallet-behaviour patterns all register into the
same `strategies` table, take evidence from the same `validations` table, and
are promoted by the same `next_status` ladder. There is no second validation
database to remove, and this module does not add one.

What was missing is the label. A candidate knew its rule TYPE, which is a
mechanical fact about how it replays, but not its SOURCE — which research
pathway proposed it. Those are different questions, and only the second can
answer "which discovery engines produce durable candidates?" (§10) or fill
§23's DISCOVERY SOURCES panel. A sequence chain and an inverse of a sequence
chain replay identically and are the same `type`; they are emphatically not
the same source, and the whole point of Engine D is to find out whether the
inverse survives where the original did not.

So: one taxonomy, derived from the rule where it can be and stored on the row
where it cannot, plus `CandidateSpec` — the common shape every engine hands to
the registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

SOURCE_QUANT = "QUANT"
SOURCE_SEQUENCE_STATE = "SEQUENCE_STATE"
SOURCE_WALLET_BEHAVIOR = "WALLET_BEHAVIOR"
SOURCE_INVERSE_ADVERSARIAL = "INVERSE_ADVERSARIAL"
SOURCE_CONVERGENT = "CONVERGENT"

ALL_SOURCES = (SOURCE_QUANT, SOURCE_SEQUENCE_STATE, SOURCE_WALLET_BEHAVIOR,
               SOURCE_INVERSE_ADVERSARIAL, SOURCE_CONVERGENT)

# Rule type -> the engine that proposes that shape of rule.
_TYPE_SOURCE = {
    "sequence": SOURCE_SEQUENCE_STATE,
    "sharp_move": SOURCE_SEQUENCE_STATE,
    "longshot": SOURCE_QUANT,
    "wallet_state": SOURCE_WALLET_BEHAVIOR,
    "wallet_behavior": SOURCE_WALLET_BEHAVIOR,
}


def source_of(rule: dict) -> str:
    """Which research pathway proposed this rule.

    Order matters. A rule carrying a `variant` tag was produced by the
    inverse/adversarial engine FROM something else, and that provenance is
    what makes it interesting — reporting it under its parent's source would
    hide exactly the comparison Engine D exists to make.

    `convergent_with` is checked last and deliberately does not overwrite an
    adversarial label: convergence is a prioritisation signal (§4 of the
    second directive), and a candidate can be both.
    """
    rule = rule or {}
    if rule.get("variant"):
        return SOURCE_INVERSE_ADVERSARIAL
    if rule.get("convergent_with"):
        return SOURCE_CONVERGENT
    return _TYPE_SOURCE.get(str(rule.get("type") or ""), SOURCE_QUANT)


@dataclass
class CandidateSpec:
    """What every discovery engine hands to the registry — §11's list.

    Nothing here is a validation result and nothing here can become one.
    `research_priority` orders what is looked at next and is explicitly not an
    input to promotion (§17); `status` starts at `new` for everything and is
    thereafter owned by the validation ladder alone.
    """

    signature: str
    rule: dict
    describe: str = ""
    family: str = ""
    source: str = SOURCE_QUANT
    # Evidence about where the idea CAME from — permanently disqualified as
    # evidence FOR it. Both lists join the candidate's exclusion set.
    discovery_markets: set[str] = field(default_factory=set)
    source_markets: set[str] = field(default_factory=set)
    # In-sample figures, kept for reference and for overfit-risk measurement.
    # They are never allowed to promote anything.
    in_score: float = 0.0
    in_win: float = 0.0
    in_sharpe: float = 0.0
    # Set when this candidate was derived from another (Engine D).
    parent_id: str = ""
    variant: str = ""
    convergent_with: Iterable[str] = field(default_factory=tuple)

    @property
    def feature_dependencies(self) -> set[str]:
        from .feature_domain import features_of
        return features_of(self.rule)

    @property
    def complexity(self) -> int:
        """§9 of the second directive: conditions, parameters, interactions.

        Used as a research-quality metric — a more complicated rule must earn
        its complexity with better INDEPENDENT results, never with a better
        in-sample number.
        """
        return rule_complexity(self.rule)

    def exclusions(self) -> set[str]:
        return set(self.discovery_markets) | set(self.source_markets)

    def register(self, library) -> str:
        """Into the one registry. There is no other way in, by design."""
        return library.upsert_candidate(
            self.signature, self.rule, self.describe,
            in_score=self.in_score, in_win=self.in_win,
            in_sharpe=self.in_sharpe,
            discovery_markets=self.exclusions(),
            family=self.family, source=self.source)


def rule_complexity(rule: dict) -> int:
    """A count of the moving parts, comparable across rule types.

    Deliberately crude. Its job is to break ties toward the simpler
    explanation and to make "this rule got more complicated" visible, not to
    be a precise measure of anything.
    """
    rule = rule or {}
    count = 0
    for key in ("entry_feature", "filter_feature", "exit_feature",
                "entry_op", "filter_op", "direction", "side"):
        if rule.get(key):
            count += 1
    chain = rule.get("chain") or []
    count += len(chain)
    for key in ("hold_bars", "hold_seconds", "gap_bars", "prob_lo", "prob_hi",
                "price_lo", "price_hi", "quiet_minutes", "persist_minutes",
                "delay_bars", "max_premium"):
        if rule.get(key) is not None:
            count += 1
    return count


def source_census(rows: Iterable[dict]) -> dict[str, dict]:
    """§23's DISCOVERY SOURCES panel: per source, how many exist, how many
    have been tested, and how many survived. Computed from library rows so it
    cannot drift from what the ladder actually did."""
    out: dict[str, dict] = {
        name: {"candidates": 0, "tested": 0, "surviving": 0, "rejected": 0}
        for name in ALL_SOURCES}
    for row in rows:
        source = (str(row.get("source") or "")
                  or source_of(row.get("rule") or {}))
        bucket = out.setdefault(source, {"candidates": 0, "tested": 0,
                                         "surviving": 0, "rejected": 0})
        bucket["candidates"] += 1
        if int(row.get("oos_trades") or 0) > 0:
            bucket["tested"] += 1
        status = str(row.get("status") or "")
        if status in ("validated", "high_confidence", "watch", "validating"):
            bucket["surviving"] += 1
        elif status in ("rejected", "retired", "quarantined"):
            bucket["rejected"] += 1
    return out
