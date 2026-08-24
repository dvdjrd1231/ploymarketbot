"""
Opportunity ranking and position replacement.

The previous engine could say *"Qualified, but the 8-position limit is full"* —
discovering something better and then declining to act because capital was
already committed to something worse. A position limit is a risk control, not a
reason to hold an inferior trade.

So held positions and new candidates are ranked on **one board, by the same
measure**: expected value per dollar. Holding is simply the option of buying
what we already own, at today's price. If a new opportunity is materially
better than the weakest thing we hold, the weakest is sold to fund it.

"Materially" is doing real work. Swapping needs to clear a margin, because a
swap is not free: it pays a spread and a fee on the way out and again on the
way in, and it throws away a position whose thesis may simply need more time.
Churn between two near-identical edges is a guaranteed loss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from ..models import PositionView
from .expected_value import EVConfig, Opportunity


@dataclass
class HeldPosition:
    """A position we own, priced on the same scale as a new candidate."""

    position: PositionView
    ev_per_dollar: float
    probability: float
    exit_price: float

    @property
    def value(self) -> float:
        return self.position.market_value


@dataclass
class Plan:
    """What the portfolio layer decided to do this cycle."""

    enter: list[Opportunity] = field(default_factory=list)
    replace: list[tuple[HeldPosition, Opportunity]] = field(default_factory=list)
    exit: list[tuple[HeldPosition, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "enter": [o.to_dict() for o in self.enter],
            "replace": [{"out": h.position.token_id, "outEv": round(h.ev_per_dollar, 5),
                         "in": o.token_id, "inEv": round(o.ev_per_dollar, 5)}
                        for h, o in self.replace],
            "exit": [{"token": h.position.token_id, "ev": round(h.ev_per_dollar, 5),
                      "why": why} for h, why in self.exit],
            "notes": self.notes,
        }


def build_plan(candidates: list[Opportunity], held: list[HeldPosition],
               cfg: EVConfig, max_positions: int,
               replace_margin: float = 0.05,
               spendable: float = 0.0, min_stake: float = 0.0) -> Plan:
    """Decide what to hold, what to drop, and what to take on.

    ``replace_margin`` is how much better a newcomer must be, in EV per dollar,
    before an existing position is sold to make room.
    """
    plan = Plan()
    acceptable = sorted((c for c in candidates if c.acceptable),
                        key=lambda c: c.ev_per_dollar, reverse=True)
    held_sorted = sorted(held, key=lambda h: h.ev_per_dollar)

    # 1. Positions whose edge has gone. Judged before anything is bought, so
    #    the capital they release is available in the same cycle.
    keeping: list[HeldPosition] = []
    for position in held_sorted:
        if position.ev_per_dollar <= cfg.exit_ev:
            plan.exit.append((position, (
                f"edge is gone: holding is now {position.ev_per_dollar:+.1%} "
                f"per dollar against our {position.probability:.2f} estimate")))
        else:
            keeping.append(position)

    # 2. Free slots first — no reason to churn while there is room.
    room = max(0, max_positions - len(keeping))
    taken: list[Opportunity] = []
    for candidate in acceptable:
        if len(taken) >= room:
            break
        if any(c.market_id == candidate.market_id for c in taken):
            continue                       # one outcome per market
        if any(h.position.market_id == candidate.market_id for h in keeping):
            continue
        taken.append(candidate)
    plan.enter = taken

    # 3. Still-unfunded candidates may displace the weakest thing we hold.
    remaining = [c for c in acceptable if c not in taken]
    if remaining and keeping:
        keeping.sort(key=lambda h: h.ev_per_dollar)
        for candidate in remaining:
            if not keeping:
                break
            weakest = keeping[0]
            if any(h.position.market_id == candidate.market_id for h in keeping):
                continue
            gain = candidate.ev_per_dollar - weakest.ev_per_dollar
            if gain < replace_margin:
                plan.notes.append(
                    f"kept {weakest.position.outcome[:16]} "
                    f"({weakest.ev_per_dollar:+.1%}) over "
                    f"{candidate.outcome[:16]} ({candidate.ev_per_dollar:+.1%}) — "
                    f"only {gain:+.1%} better, under the {replace_margin:.0%} "
                    "swap margin")
                break                      # nothing weaker will clear it either
            plan.replace.append((weakest, candidate))
            keeping.pop(0)

    if not acceptable and candidates:
        best = max(candidates, key=lambda c: c.ev_per_dollar)
        plan.notes.append(
            f"scanned {len(candidates)}; none had an edge. Closest: "
            f"{best.outcome[:20]} at {best.ev_per_dollar:+.1%} — {best.reject}")
    return plan
