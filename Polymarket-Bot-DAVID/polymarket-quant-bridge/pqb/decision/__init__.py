"""
The decision layer: wallet quality -> trade quality -> expected value.

Deliberately separate from ``pqb.bridge``: these modules decide *whether a
trade is worth taking*, and know nothing about how an order is placed or how a
position is tracked. That separation is what makes the model testable on its
own, which is the point of the redesign — the previous engine's judgement was
tangled through its execution and could only be checked by running it.
"""

from .expected_value import EVConfig, Opportunity, evaluate, position_ev
from .portfolio import HeldPosition, Plan, build_plan
from .probability import ProbabilityEstimate, estimate

__all__ = [
    "EVConfig", "HeldPosition", "Opportunity", "Plan", "ProbabilityEstimate",
    "build_plan", "estimate", "evaluate", "position_ev",
]
