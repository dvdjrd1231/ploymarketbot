"""
The portfolio-doubling rule (prompt section 7).

    When live portfolio value reaches 2x the current active baseline:
      1. close ALL open positions
      2. advance the minimum trade size to the next value in the progression
      3. set the new live portfolio value as the new baseline
      4. resume trading

Written as an explicit two-state machine rather than an inline check, because
every one of the required edge cases is really a question about *when* the
transition completes:

  * **Partial fill during the mass close.** Advancing on trigger would raise the
    minimum trade size while positions are still open. So the trigger only moves
    us to ``FLATTENING``; the advance happens on the transition *out* of it,
    which requires zero open positions and zero in-flight orders.
  * **Trigger during an in-flight order.** ``FLATTENING`` blocks new entries, and
    the exit condition waits on ``pending_orders == 0``, so an order placed a
    moment before the trigger is allowed to settle first.
  * **End of the progression.** The index clamps at the last element; the rule
    keeps working, the size simply stops growing.
  * **Baseline across restarts.** Baseline and index live in the journal's
    ``engine_state`` table and are re-read on construction.

The class holds no exchange access at all — the runner drives it. That keeps it
directly unit-testable, which matters for a component that changes position
sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol

NORMAL = "NORMAL"
FLATTENING = "FLATTENING"

_KEY_BASELINE = "doubling.baseline"
_KEY_INDEX = "doubling.index"
_KEY_STATE = "doubling.state"
_KEY_CYCLES = "doubling.completed"


class StateStore(Protocol):
    """The persistence surface this rule needs (``Journal`` satisfies it)."""

    def get_state(self, key: str, default=None): ...
    def set_state(self, key: str, value) -> None: ...


@dataclass
class DoublingStatus:
    state: str
    baseline: Optional[float]
    index: int
    min_trade_size: float
    target: Optional[float]
    completed: int

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "baseline": self.baseline,
            "index": self.index,
            "minTradeSize": self.min_trade_size,
            "target": self.target,
            "completed": self.completed,
        }


class DoublingRule:
    def __init__(self, store: StateStore, progression: list[float],
                 multiple: float = 2.0, enabled: bool = True):
        if not progression:
            raise ValueError("The progression must contain at least one value.")
        self.store = store
        self.progression = [float(v) for v in progression]
        self.multiple = float(multiple)
        self.enabled = enabled

        self._baseline: Optional[float] = store.get_state(_KEY_BASELINE, None)
        self._index: int = int(store.get_state(_KEY_INDEX, 0) or 0)
        self._state: str = str(store.get_state(_KEY_STATE, NORMAL) or NORMAL)
        self._completed: int = int(store.get_state(_KEY_CYCLES, 0) or 0)
        self._clamp_index()

    # -- state ---------------------------------------------------------------

    def _clamp_index(self) -> None:
        self._index = max(0, min(self._index, len(self.progression) - 1))

    @property
    def state(self) -> str:
        return self._state

    @property
    def baseline(self) -> Optional[float]:
        return self._baseline

    @property
    def index(self) -> int:
        return self._index

    @property
    def min_trade_size(self) -> float:
        return self.progression[self._index]

    @property
    def at_end_of_progression(self) -> bool:
        return self._index >= len(self.progression) - 1

    @property
    def flattening(self) -> bool:
        return self._state == FLATTENING

    @property
    def target(self) -> Optional[float]:
        if self._baseline is None:
            return None
        return self._baseline * self.multiple

    def status(self) -> DoublingStatus:
        return DoublingStatus(
            state=self._state, baseline=self._baseline, index=self._index,
            min_trade_size=self.min_trade_size, target=self.target,
            completed=self._completed,
        )

    def _persist(self) -> None:
        self.store.set_state(_KEY_BASELINE, self._baseline)
        self.store.set_state(_KEY_INDEX, self._index)
        self.store.set_state(_KEY_STATE, self._state)
        self.store.set_state(_KEY_CYCLES, self._completed)

    # -- lifecycle -----------------------------------------------------------

    def initialize(self, portfolio_value: float) -> bool:
        """Adopt the first observed portfolio value as the baseline.

        Only ever sets a baseline that is missing, so a restart resumes against
        the stored one instead of silently re-basing to whatever the account
        happens to be worth at boot — which would forfeit progress toward the
        current target.
        """
        if self._baseline is not None:
            return False
        if portfolio_value <= 0:
            return False
        self._baseline = float(portfolio_value)
        self._persist()
        return True

    def should_trigger(self, portfolio_value: float) -> bool:
        if not self.enabled or self._state != NORMAL:
            return False
        if self._baseline is None or self._baseline <= 0:
            return False
        return portfolio_value >= self._baseline * self.multiple

    def begin_flatten(self, portfolio_value: float) -> bool:
        """Enter ``FLATTENING``: the runner must now cancel and close out.

        Returns False when the trigger no longer holds, so a caller that checked
        and acted on stale data cannot force the transition.
        """
        if not self.should_trigger(portfolio_value):
            return False
        self._state = FLATTENING
        self._persist()
        return True

    def can_complete(self, open_positions: int, pending_orders: int) -> bool:
        return (self._state == FLATTENING and open_positions == 0
                and pending_orders == 0)

    def complete_flatten(self, portfolio_value: float, open_positions: int = 0,
                         pending_orders: int = 0) -> bool:
        """Advance the progression and re-base. The transition out of FLATTENING.

        Refuses while anything is still open or in flight — that refusal is what
        makes a partially filled mass-close safe: the runner simply retries next
        cycle and the size only advances once the account really is flat.
        """
        if not self.can_complete(open_positions, pending_orders):
            return False
        if not self.at_end_of_progression:
            self._index += 1
        self._baseline = max(0.0, float(portfolio_value))
        self._completed += 1
        self._state = NORMAL
        self._persist()
        return True

    def rebase(self, portfolio_value: float) -> None:
        """Force the baseline (operator action; not part of the normal flow)."""
        self._baseline = max(0.0, float(portfolio_value))
        self._persist()

    def reset(self) -> None:
        self._baseline = None
        self._index = 0
        self._state = NORMAL
        self._completed = 0
        self._persist()
