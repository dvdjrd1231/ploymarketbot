"""Gate ownership. The module that makes "silently blocked by Strategy A"
impossible rather than merely discouraged.

The V1 audit found the real failure, and it was a single gate:

    lean_engine._entry_block_reason  ->  "Learning mode: no validated
    strategies yet - capital is parked until discovery produces one"

40,820 of 40,820 journalled decisions. That gate sits ABOVE every other entry
gate, so the market-state, depth, spread and EV filters the brief suspected
were never even reached in production. Diagnosing by reading log lines would
have blamed the wrong rules and loosened them for nothing.

So in V2 every rule that can stop a trade must be registered here with an
owner, and the owner determines who it may stop:

    STRATEGY_A     may only stop Strategy A
    STRATEGY_B     may only stop Strategy B
    GLOBAL_SAFETY  may stop both -- and must carry written evidence why
    PORTFOLIO_RISK may stop both, but never erases the signal (it is recorded
                   as a portfolio rejection, not a strategy rejection, so
                   strategy quality stays measurable separately)
    EXECUTION      may stop a fill, never a signal

`assert_no_cross_ownership` is called by the Strategy B route and raises if a
Strategy A gate is ever evaluated inside it. `tests/test_isolation.py` asserts
this fires.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Owner(str, Enum):
    STRATEGY_A = "STRATEGY_A"
    STRATEGY_B = "STRATEGY_B"
    GLOBAL_SAFETY = "GLOBAL_SAFETY"
    PORTFOLIO_RISK = "PORTFOLIO_RISK"
    EXECUTION = "EXECUTION"

    def may_block(self, route: str) -> bool:
        if self is Owner.STRATEGY_A:
            return route == "A"
        if self is Owner.STRATEGY_B:
            return route == "B"
        return True          # global / portfolio / execution apply to both


@dataclass(frozen=True)
class Gate:
    """One rule that can stop something, and the evidence that justifies it."""

    key: str
    owner: Owner
    description: str
    evidence: str = ""
    # A GLOBAL_SAFETY gate with no evidence is a Strategy A gate wearing a
    # disguise. `audit()` reports those rather than trusting the label.

    def justified(self) -> bool:
        return self.owner is not Owner.GLOBAL_SAFETY or bool(self.evidence)


# ---------------------------------------------------------------------------
# The registry. Every gate V2 can apply, and every V1 gate we inherited a
# verdict on. The V1 entries are documentation of the audit, not live code --
# V2 never calls into them.
# ---------------------------------------------------------------------------

REGISTRY: dict[str, Gate] = {}


def register(gate: Gate) -> Gate:
    if gate.key in REGISTRY:
        raise ValueError(f"duplicate gate {gate.key}")
    REGISTRY[gate.key] = gate
    return gate


def _g(key, owner, description, evidence=""):
    return register(Gate(key, owner, description, evidence))


# --- inherited from V1, classified by the audit -----------------------------
# These are recorded so the brief's question 21 ("which suppressions are merely
# inherited from Strategy A?") has a mechanical answer.

_g("v1.learning_mode", Owner.STRATEGY_A,
   "No validated strategy in the library => no entries at all.",
   "MEASURED: 40820/40820 journalled decisions. library.sqlite3 holds 234 "
   "strategies: 170 rejected, 49 validating, 13 new, 2 quarantined, 0 "
   "validated. This is Strategy A's own discovery ladder gating Strategy A's "
   "own engine. It must NOT gate Strategy B, which has an independent ladder.")

_g("v1.market_state_not_entry", Owner.STRATEGY_A,
   "high_confidence.py: ms_state not in allowed_states.",
   "Never reached in production (learning mode short-circuits above it). A "
   "market-microstructure state machine is Strategy A's thesis about *when a "
   "move is born*. Strategy B's thesis is *who is trading*, which is a "
   "different question. Inheriting this would be exactly the silent block the "
   "brief forbids.")

_g("v1.depth_under_multiple", Owner.EXECUTION,
   "high_confidence.py: visible depth < min_depth_x_stake * stake.",
   "Reclassified EXECUTION, not STRATEGY_A: it is a statement about whether a "
   "fill is achievable, which is true regardless of which strategy asked. V2 "
   "applies the equivalent in risk/execution.py to both routes. NOTE: 47 "
   "order-book feature columns are constant in the historical substrate, so "
   "this cannot be backtested -- only applied live.")

_g("v1.no_exit_condition_met", Owner.STRATEGY_A,
   "baseline_engine.py: HOLD because no exit rule fired.",
   "Strategy A's exit model. Strategy B derives its own exit model per "
   "strategy family (research/exits.py) and may hold to settlement.")

_g("v1.empirical_no_setup_history", Owner.STRATEGY_A,
   "high_confidence.py: fewer than min_setup_sample CLOSED lifecycles for "
   "this (category, state, score-band).",
   "DEADLOCK: the gate reads `lifecycles WHERE status='CLOSED'`, and "
   "journal.sqlite3 holds 0 lifecycles and 0 executions. History can only "
   "accumulate by trading, and trading requires history. V2 breaks this by "
   "separating the paper ladder from the live ladder -- see ledger.Mode.")

# --- V2 gates ---------------------------------------------------------------

_g("b.behavior_match", Owner.STRATEGY_B,
   "Signal does not resemble the reference behaviour closely enough.")
_g("b.wallet_evidence", Owner.STRATEGY_B,
   "Followed wallet has too little point-in-time settled evidence.")
_g("b.price_band", Owner.STRATEGY_B,
   "Entry price outside the strategy's own band.")
_g("b.notional_floor", Owner.STRATEGY_B,
   "Wallet's own stake below the conviction floor for this strategy.")
_g("b.strategy_not_validated", Owner.STRATEGY_B,
   "Strategy has not reached a status that authorises this mode.")
_g("b.conditions", Owner.STRATEGY_B,
   "Strategy-specific entry conditions not met.")

_g("g.price_bounds", Owner.GLOBAL_SAFETY,
   "Price outside [min_price, max_price].",
   "Tail prices cannot be sized or exited sanely and the resolution-payoff "
   "model degenerates as price -> 0. Applies to any strategy.")
_g("g.drawdown_halt", Owner.GLOBAL_SAFETY,
   "Account drawdown past the hard stop.",
   "Capital preservation. A drawdown limit that one strategy can opt out of "
   "is not a drawdown limit.")
_g("g.per_trade_cap", Owner.GLOBAL_SAFETY,
   "Stake exceeds the per-trade fraction of equity.",
   "Risk of ruin is a property of the account, not of a strategy.")
_g("g.per_market_cap", Owner.GLOBAL_SAFETY,
   "Total exposure to one market exceeds the cap.",
   "Two strategies independently loading the same market is one bet, not two.")

_g("p.max_open", Owner.PORTFOLIO_RISK, "Open position count at the cap.")
_g("p.strategy_share", Owner.PORTFOLIO_RISK, "One strategy dominates exposure.")
_g("p.wallet_share", Owner.PORTFOLIO_RISK, "One followed wallet dominates exposure.")
_g("p.correlated", Owner.PORTFOLIO_RISK, "Correlated exposure at the cap.")
_g("p.duplicate", Owner.PORTFOLIO_RISK, "Already holding this token.")
_g("p.insufficient_capital", Owner.PORTFOLIO_RISK, "Not enough deployable capital.")

_g("x.unpriced", Owner.EXECUTION, "No printed price inside the fill window.")
_g("x.depth", Owner.EXECUTION, "Visible depth cannot absorb the stake.")
_g("x.spread", Owner.EXECUTION, "Spread wider than the strategy's edge.")
_g("x.price_moved", Owner.EXECUTION, "Executable price moved out of the band.")


def get(key: str) -> Gate:
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"unregistered gate {key!r} - every rule that can stop "
                       "a trade must declare an owner in gates.py") from None


def assert_may_block(key: str, route: str) -> None:
    """Raise if `key` is being evaluated on a route it does not own.

    This is the mechanical form of non-negotiable rule 4.
    """
    gate = get(key)
    if not gate.owner.may_block(route):
        raise AssertionError(
            f"gate {key!r} is owned by {gate.owner.value} and must not be "
            f"evaluated on route {route!r}. Rule 4: Strategy B is never "
            "silently blocked by Strategy A.")


def audit() -> dict:
    """Which suppressions are justified, and which are inherited (Q20/Q21)."""
    out = {"by_owner": {}, "unjustified_global": [], "inherited_from_a": []}
    for gate in REGISTRY.values():
        out["by_owner"].setdefault(gate.owner.value, []).append(gate.key)
        if not gate.justified():
            out["unjustified_global"].append(gate.key)
        if gate.owner is Owner.STRATEGY_A:
            out["inherited_from_a"].append(gate.key)
    return out
