"""The agent contract.

An agent is a function from `EvidenceState` to `Verdict`. That is the entire
interface, and the narrowness is the point:

  * An agent receives no database handle, so it cannot read the outcome.
  * An agent receives no wall clock, so it cannot read past `ev.as_of`.
  * An agent receives no credential — `secrets.py` has nothing to hand out.
  * An agent returns an opinion, never an order.

Agents ABSTAIN when the layers they need are unavailable, and abstention is
weighted at zero rather than counted as neutral agreement. This matters more
than it sounds: with news, chain and book layers empty on a fresh install,
counting their abstentions as "no objection" would let a four-agent quorum
masquerade as a twenty-five-agent consensus.

Each agent declares `requires` — the layers it needs. The debate records which
agents ran, which abstained and why, and the dashboard shows the difference.
Hiding an abstention is how a system reports high confidence from thin evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Callable, Sequence

from ..core.canon import EvidenceState


class Stance(str, Enum):
    FOR = "FOR"
    AGAINST = "AGAINST"
    ABSTAIN = "ABSTAIN"

    @property
    def sign(self) -> float:
        return {"FOR": 1.0, "AGAINST": -1.0, "ABSTAIN": 0.0}[self.value]


@dataclass
class Verdict:
    agent: str
    stance: Stance = Stance.ABSTAIN
    confidence: float = 0.0            # 0..1; the agent's own certainty
    probability: float | None = None   # its estimate of P(outcome=YES), if any
    thesis: str = ""
    evidence: list = field(default_factory=list)
    objections: list = field(default_factory=list)
    inputs_used: list = field(default_factory=list)
    abstain_reason: str = ""

    @property
    def weight(self) -> float:
        """Abstentions carry zero weight. Never 0.5, never 'neutral'."""
        return 0.0 if self.stance is Stance.ABSTAIN else self.confidence

    def to_dict(self) -> dict:
        d = asdict(self)
        d["stance"] = self.stance.value
        d["weight"] = round(self.weight, 4)
        return d


@dataclass
class AgentSpec:
    number: int
    name: str
    role: str
    requires: tuple                    # layer names it cannot work without
    fn: Callable[[EvidenceState, dict], Verdict]
    adversarial: bool = False          # counts toward the red team

    def run(self, ev: EvidenceState, ctx: dict) -> Verdict:
        missing = [r for r in self.requires if not ev.layer(r).ok]
        if missing:
            return Verdict(agent=self.name, stance=Stance.ABSTAIN,
                           abstain_reason=f"requires {', '.join(missing)}",
                           inputs_used=list(self.requires))
        try:
            v = self.fn(ev, ctx)
        except Exception as exc:                              # noqa: BLE001
            # An agent that raises must not take down the debate, and must not
            # silently become a pass either.
            return Verdict(agent=self.name, stance=Stance.ABSTAIN,
                           abstain_reason=f"error: {type(exc).__name__}: {exc}")
        v.agent = self.name
        if not v.inputs_used:
            v.inputs_used = list(self.requires)
        return v


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def linear_confidence(value: float, weak: float, strong: float) -> float:
    """Map a measurement onto 0..1 with named endpoints.

    Used everywhere instead of ad-hoc arithmetic so that every confidence in
    the system is traceable to two numbers a human can argue with, rather than
    to a magic constant inside a formula.
    """
    if strong == weak:
        return 0.0
    return clamp((value - weak) / (strong - weak))


def agree(verdicts: Sequence[Verdict]) -> dict:
    """Weighted consensus, disagreement, and the abstention count.

    `consensus` is the weighted share favouring FOR among agents that took a
    side. `disagreement` is the dispersion of the probability estimates that
    were actually offered — not of the stances, because two agents can agree on
    direction while disagreeing wildly on magnitude, and that is precisely the
    situation where confidence should be cut.
    """
    active = [v for v in verdicts if v.stance is not Stance.ABSTAIN]
    abstained = [v for v in verdicts if v.stance is Stance.ABSTAIN]
    total_w = sum(v.weight for v in active)
    if total_w <= 0:
        return {"consensus": 0.0, "disagreement": 1.0, "n_agents": len(verdicts),
                "n_active": 0, "n_abstained": len(abstained),
                "n_for": 0, "n_against": 0,
                "note": "no agent took a side"}
    for_w = sum(v.weight for v in active if v.stance is Stance.FOR)
    probs = [v.probability for v in active if v.probability is not None]
    if len(probs) > 1:
        mean = sum(probs) / len(probs)
        var = sum((p - mean) ** 2 for p in probs) / (len(probs) - 1)
        disagreement = clamp(var ** 0.5 * 2.0)     # 0.5 sd -> full disagreement
    else:
        # One estimate is not agreement, it is an absence of corroboration.
        disagreement = 0.5 if len(probs) == 1 else 1.0
    return {
        "consensus": round(for_w / total_w, 4),
        "disagreement": round(disagreement, 4),
        "n_agents": len(verdicts), "n_active": len(active),
        "n_abstained": len(abstained),
        "n_for": sum(1 for v in active if v.stance is Stance.FOR),
        "n_against": sum(1 for v in active if v.stance is Stance.AGAINST),
        "mean_probability": round(sum(probs) / len(probs), 5) if probs else None,
        "n_estimates": len(probs),
    }
