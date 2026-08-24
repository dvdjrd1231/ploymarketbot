"""
The seam between this project and the Quant Bridge.

**Read this before adding code anywhere else.** The existing Prop Firm Quant
Bridge (QuantConnect LEAN) is the brain; it is not present in this checkout, so
the boundary it will plug into is defined here explicitly and kept as narrow as
possible. Everything on the Polymarket side — adapters, journal, doubling rule,
reconciler, runner — is written against these two protocols and nothing else.

To drop the real bridge in, implement :class:`DecisionEngine` (one method) in a
module of your choosing and point ``engine.implementation`` at it in the config:

    engine:
      implementation: "propfirm_bridge.polymarket:LeanDecisionEngine"

Nothing else changes. ``BaselineDecisionEngine`` exists so the pipeline is
demonstrable end-to-end today; it is a reference implementation of the
behaviour section 4 specifies, not a competitor to the real analysis layer.

The inverse direction — results flowing back for analysis — is
:class:`JournalSink`, which the bundled SQLite ``Journal`` already satisfies. A
host bridge with its own persistence can implement it instead and the runner
will not notice.
"""

from __future__ import annotations

import importlib
from typing import Any, Optional, Protocol, runtime_checkable

from ..models import (
    BridgeContext, Decision, ExecutionReport, MarketFeatures, PositionView,
)


@runtime_checkable
class DecisionEngine(Protocol):
    """The brain. One call per evaluation cycle.

    Contract:
      * Return one :class:`Decision` per open position (exit management is
        mandatory, so every position must be adjudicated every cycle), plus zero
        or more entry decisions.
      * Emit ``DO_NOTHING``/``HOLD`` explicitly rather than omitting. A silent
        omission is indistinguishable from a bug, and the journal needs the
        record to later ask whether passing was correct.
      * Do not perform I/O. The context is complete; sizing legality and
        execution belong to the adapters.
    """

    def evaluate(self, context: BridgeContext) -> list[Decision]:
        ...


@runtime_checkable
class JournalSink(Protocol):
    """Where lifecycle records go. ``pqb.journal.Journal`` implements this."""

    def record_decision(self, decision: Decision, cycle_id: str,
                        market: Optional[MarketFeatures] = None,
                        features: Optional[dict] = None) -> int: ...

    def record_execution(self, report: ExecutionReport, side: str = "") -> int: ...

    def open_lifecycle(self, decision: Decision, report: ExecutionReport,
                       market: Optional[MarketFeatures] = None) -> int: ...

    def close_lifecycle(self, lifecycle_id: int, exit_price: float,
                        exit_size: float, realized_pnl: float, reason: str,
                        exit_style: str,
                        exit_decision_id: Optional[int] = None) -> None: ...

    def track_evolution(self, position: PositionView) -> None: ...

    def get_state(self, key: str, default: Any = None) -> Any: ...

    def set_state(self, key: str, value: Any) -> None: ...


def load_engine(spec: str, **kwargs) -> DecisionEngine:
    """Instantiate a decision engine from a ``"module.path:ClassName"`` string.

    Constructor keyword arguments are filtered to what the target accepts, so a
    host bridge's engine is free to take a different set from the bundled one.
    """
    if ":" not in spec:
        raise ValueError(
            f"engine.implementation must look like 'module.path:ClassName', "
            f"got '{spec}'.")
    module_name, _, class_name = spec.partition(":")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ImportError(
            f"Could not import '{module_name}' for engine.implementation "
            f"'{spec}': {exc}") from exc
    try:
        factory = getattr(module, class_name)
    except AttributeError as exc:
        raise ImportError(
            f"'{module_name}' has no attribute '{class_name}'.") from exc

    import inspect
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):       # pragma: no cover - exotic callables
        params = {}
    accepts_kwargs = any(p.kind is inspect.Parameter.VAR_KEYWORD
                         for p in params.values())
    filtered = kwargs if accepts_kwargs else {
        k: v for k, v in kwargs.items() if k in params
    }
    engine = factory(**filtered)
    if not hasattr(engine, "evaluate"):
        raise TypeError(
            f"'{spec}' does not implement DecisionEngine.evaluate(context).")
    return engine
