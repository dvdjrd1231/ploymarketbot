"""Agent debate. Not a vote.

The brief is explicit that agents must not simply vote, and the reason is that
voting throws away the two most useful things a disagreement contains: *which*
agent dissented, and *why*. A 20–5 vote and a 20–5 vote where the five
dissenters are the red team, the overfitting detector, the data-quality auditor
and two forensics agents are not the same situation, and averaging them into
0.80 destroys that distinction.

So the procedure is:

    1  run every agent over the same immutable evidence state
    2  collect theses, evidence and probability estimates
    3  run the adversarial subset, which is scored separately
    4  the red team's verdict is a VETO, not a weight
    5  compute consensus over non-abstaining agents only
    6  compute disagreement from the probability estimates offered
    7  cut confidence for disagreement and for missing channels
    8  record everything, including who abstained and why

Step 4 is the one that matters. If the red team's objections merely reduced a
weighted average, a sufficiently enthusiastic majority could always outvote it,
which is precisely how a system talks itself into a bad trade.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..config import Settings
from ..core.canon import EvidenceState
from .base import Stance, Verdict, agree, clamp
from .registry import AGENTS, ADVERSARIAL


@dataclass
class DebateResult:
    run_id: str
    subject: str
    verdicts: list = field(default_factory=list)
    consensus: float = 0.0
    disagreement: float = 1.0
    confidence: float = 0.0
    killed: bool = False
    objections: list = field(default_factory=list)
    theses_for: list = field(default_factory=list)
    theses_against: list = field(default_factory=list)
    abstentions: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    elapsed_ms: int = 0

    @property
    def direction(self) -> float:
        """+1 toward YES, -1 toward NO, scaled by consensus strength."""
        return (self.consensus - 0.5) * 2.0

    def red_team_dict(self) -> dict:
        """The shape ADVERSARIAL_VALIDITY expects."""
        return {"killed": self.killed, "objections": self.objections,
                "consensus": self.consensus,
                "model_disagreement": self.disagreement,
                "n_agents": self.stats.get("n_active", 0)}

    def to_dict(self) -> dict:
        return {"run_id": self.run_id, "subject": self.subject,
                "consensus": round(self.consensus, 4),
                "disagreement": round(self.disagreement, 4),
                "confidence": round(self.confidence, 4),
                "direction": round(self.direction, 4),
                "killed": self.killed, "objections": self.objections,
                "theses_for": self.theses_for,
                "theses_against": self.theses_against,
                "abstentions": self.abstentions,
                "proposals": self.proposals,
                "stats": self.stats, "elapsed_ms": self.elapsed_ms,
                "verdicts": [v.to_dict() for v in self.verdicts]}


class Debate:
    def __init__(self, st: Settings) -> None:
        self.st = st

    def run(self, ev: EvidenceState, ctx: dict, *,
            subject: str = "") -> DebateResult:
        t0 = time.perf_counter()
        run_id = uuid.uuid4().hex[:16]
        res = DebateResult(run_id=run_id,
                           subject=subject or f"{ev.market_id}@{ev.as_of}")

        # The evidence state is shared and must not be mutated by an agent, or
        # agent N's conclusion would depend on the order agents happened to run
        # in. Agents receive it read-only by convention and `ctx` by copy.
        with ThreadPoolExecutor(max_workers=self.st.agents.max_parallel) as pool:
            res.verdicts = list(pool.map(lambda a: a.run(ev, dict(ctx)), AGENTS))

        res.stats = agree(res.verdicts)
        res.consensus = res.stats["consensus"]
        res.disagreement = res.stats["disagreement"]

        for v in res.verdicts:
            if v.stance is Stance.ABSTAIN:
                res.abstentions.append(
                    {"agent": v.agent, "reason": v.abstain_reason})
                if v.evidence and v.agent == "STRATEGY_DISCOVERY":
                    res.proposals.extend(v.evidence)
            elif v.stance is Stance.FOR:
                res.theses_for.append({"agent": v.agent, "thesis": v.thesis,
                                       "confidence": round(v.confidence, 3)})
            else:
                res.theses_against.append({"agent": v.agent, "thesis": v.thesis,
                                           "confidence": round(v.confidence, 3)})
            res.objections.extend(f"[{v.agent}] {o}" for o in v.objections)

        # -- the veto. Any adversarial agent voting AGAINST with real
        # conviction kills the candidate outright.
        adv_names = {a.name for a in ADVERSARIAL}
        killers = [v for v in res.verdicts
                   if v.agent in adv_names and v.stance is Stance.AGAINST
                   and v.confidence >= 0.5]
        if self.st.agents.red_team_must_pass and killers:
            res.killed = True
            res.confidence = 0.0
            res.objections.insert(0, "VETOED by " + ", ".join(
                v.agent for v in killers))
        else:
            res.confidence = self._confidence(res, ev)

        res.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return res

    def _confidence(self, res: DebateResult, ev: EvidenceState) -> float:
        """Consensus, discounted for everything that should discount it.

        Three multiplicative penalties rather than an additive score, because
        each one independently invalidates the result: high disagreement, thin
        participation, and an incomplete information environment are not
        failures you can offset with strength elsewhere.
        """
        base = abs(res.consensus - 0.5) * 2.0        # 0 at a tie, 1 at unanimity
        disagreement_penalty = 1.0 - clamp(res.disagreement)
        n_active = res.stats.get("n_active", 0)
        # Six active agents is where participation stops being the constraint.
        participation = clamp(n_active / 6.0)
        completeness = clamp(ev.completeness / 0.7)
        return round(clamp(base * disagreement_penalty * participation
                           * completeness), 4)


def persist(store, res: DebateResult, *, source: str = "debate") -> None:
    store.insert("agent_outputs", [
        {"run_id": res.run_id, "agent": v.agent, "subject": res.subject,
         "stance": v.stance.value, "confidence": v.confidence,
         "probability": v.probability, "thesis": v.thesis,
         "evidence": v.evidence, "objections": v.objections,
         "inputs_used": v.inputs_used}
        for v in res.verdicts], source=source)


def agent_accuracy(store, agent: str, lookback_days: int = 30) -> dict:
    """How often has this agent's stance matched the eventual outcome?

    Joins the agent's recorded stance to the realised PnL of the decision that
    followed it. An agent with no closed positions behind it returns
    `n=0` rather than a flattering default — the AGENTS dashboard shows the
    difference between "accurate" and "never tested".
    """
    since = int(time.time()) - lookback_days * 86_400
    rows = store.query(
        "SELECT a.stance, p.realized_pnl FROM agent_outputs a "
        "  JOIN decisions d ON d.run_id = a.run_id "
        "  JOIN positions p ON p.market_id = d.market_id "
        "                  AND p.opened_ts >= d.ts AND p.status != 'OPEN' "
        " WHERE a.agent = ? AND a.ts >= ? AND a.stance != 'ABSTAIN'",
        (agent, since))
    if not rows:
        return {"agent": agent, "n": 0, "accuracy": None,
                "note": "no closed positions follow this agent's opinions yet"}
    hits = sum(1 for r in rows
               if (r["stance"] == "FOR") == (float(r["realized_pnl"] or 0) > 0))
    return {"agent": agent, "n": len(rows),
            "accuracy": round(hits / len(rows), 4)}
