"""
Reconciliation (prompt section 6, mandatory part).

The bridge's belief about the account and the exchange's record of it must never
silently diverge. They diverge for ordinary reasons — a partial fill counted
optimistically, a redemption, a position closed by hand in the Polymarket UI, an
order that landed after we stopped waiting — so the question is not whether it
happens but how quickly it is caught.

On startup and on a timer this compares the journal's open lifecycles against
what the exchange actually reports, and on any mismatch it:

  1. records a reconciliation event with expected vs actual,
  2. **adopts the exchange as canonical**, and
  3. raises an ERROR-level alert.

Order matters: adopting first and logging second would leave no record of what
the divergence was.

In dry-run there is no exchange position to compare against, so the same routine
checks the simulated book against the journal instead. That is a weaker check,
but it exercises the identical code path and catches the class of bug — a
lifecycle that was never opened or never closed — that would otherwise only
surface with real money on the line.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .logs import Log
from .models import PositionView


@dataclass
class Mismatch:
    kind: str
    subject: str
    expected: Any = None
    actual: Any = None
    action: str = ""
    # True when the bridge's belief about what it *holds* is wrong. Those halt
    # trading. Informational findings (a resting order where we only place
    # Fill-And-Kill, say) are logged and journalled but do not, on their own,
    # mean we would be trading on a false position.
    critical: bool = True

    def to_dict(self) -> dict:
        return {"kind": self.kind, "subject": self.subject,
                "expected": self.expected, "actual": self.actual,
                "action": self.action, "critical": self.critical}


@dataclass
class ReconcileResult:
    ts: float = field(default_factory=time.time)
    checked: int = 0
    mismatches: list[Mismatch] = field(default_factory=list)
    adopted: int = 0
    closed: int = 0
    resized: int = 0
    orphan_orders: int = 0

    skipped_young: int = 0

    @property
    def clean(self) -> bool:
        return not self.mismatches

    @property
    def critical(self) -> list[Mismatch]:
        """Divergences that mean the bridge does not know what it holds."""
        return [m for m in self.mismatches if m.critical]

    @property
    def should_halt(self) -> bool:
        return bool(self.critical)

    def summary(self) -> str:
        if not self.mismatches:
            return "clean"
        return ", ".join(f"{m.kind}({m.subject[:10]})" for m in self.critical) \
            or "informational only"

    def to_dict(self) -> dict:
        return {
            "ts": self.ts, "checked": self.checked, "adopted": self.adopted,
            "closed": self.closed, "resized": self.resized,
            "orphanOrders": self.orphan_orders,
            "skippedYoung": self.skipped_young,
            "criticalCount": len(self.critical),
            "mismatches": [m.to_dict() for m in self.mismatches],
        }


class Reconciler:
    def __init__(self, config, log: Log, journal, data_adapter,
                 paper=None):
        self.config = config
        self.log = log
        self.journal = journal
        self.data = data_adapter
        self.paper = paper
        self.last_run: float = 0.0
        self.last_result: Optional[ReconcileResult] = None

    async def run(self, mark_for) -> ReconcileResult:
        result = ReconcileResult()
        live = self.config.mode.live
        actual = await self._actual_positions(live)
        lifecycles = {row["token_id"]: row
                      for row in self.journal.open_lifecycles()}
        result.checked = len(set(actual) | set(lifecycles))
        tolerance = max(1e-6, self.config.reconciliation.size_tolerance)

        # 1. Lifecycles the exchange does not back any more.
        grace = max(0, self.config.reconciliation.settle_grace_seconds)
        now = time.time()
        for token_id, row in lifecycles.items():
            if token_id in actual:
                continue
            # A fill reaches the CLOB before the Data API reports the position.
            # Flagging a just-opened position as missing would halt on every
            # single entry, which would make halt-on-mismatch unusable rather
            # than protective.
            if grace and now - (row["entry_ts"] or 0) < grace:
                result.skipped_young += 1
                continue
            entry_price = row["entry_price"] or 0.0
            mark = mark_for(token_id) or entry_price
            size = row["entry_size"] or 0.0
            pnl = (mark - entry_price) * size
            mismatch = Mismatch(
                kind="position.missing", subject=token_id,
                expected={"size": size, "entryPrice": entry_price},
                actual=None,
                action="closed lifecycle at last known mark",
            )
            self.journal.record_reconciliation(
                mismatch.kind, token_id, mismatch.expected, mismatch.actual,
                mismatch.action,
                {"mark": mark, "impliedPnl": pnl, "live": live})
            self.journal.close_lifecycle(
                lifecycle_id=row["id"], exit_price=mark, exit_size=size,
                realized_pnl=pnl,
                reason="Reconciliation: no longer held on the exchange.",
                exit_style="reconciled")
            result.mismatches.append(mismatch)
            result.closed += 1
            self.log.error("Reconciliation: position gone from the exchange",
                           token=token_id[:12], size=size, mark=mark)

        # 2. Exchange positions the journal has never seen, and size drift.
        for token_id, position in actual.items():
            row = lifecycles.get(token_id)
            if row is None:
                mismatch = Mismatch(
                    kind="position.unknown", subject=token_id,
                    expected=None,
                    actual={"size": position.size,
                            "avgPrice": position.avg_price},
                    action="adopted into the journal")
                self.journal.record_reconciliation(
                    mismatch.kind, token_id, None, mismatch.actual,
                    mismatch.action, {"live": live})
                self._adopt(position)
                result.mismatches.append(mismatch)
                result.adopted += 1
                self.log.error("Reconciliation: adopting an unknown position",
                               token=token_id[:12], size=position.size,
                               avg=position.avg_price)
                continue

            recorded = (row["entry_size"] or 0.0) - (row["exit_size"] or 0.0)
            drift = abs(position.size - recorded)
            if drift > max(tolerance, recorded * 0.005):
                mismatch = Mismatch(
                    kind="position.size", subject=token_id,
                    expected={"size": recorded},
                    actual={"size": position.size},
                    action="resized to the exchange's figure")
                self.journal.record_reconciliation(
                    mismatch.kind, token_id, mismatch.expected,
                    mismatch.actual, mismatch.action,
                    {"drift": drift, "live": live})
                self._resize(row, position)
                result.mismatches.append(mismatch)
                result.resized += 1
                self.log.error("Reconciliation: position size drift",
                               token=token_id[:12], recorded=recorded,
                               exchange=position.size)

        # 3. Resting orders the bridge is not tracking.
        if live:
            orders = await self.data.open_orders()
            result.orphan_orders = len(orders)
            if orders:
                self.journal.record_reconciliation(
                    "orders.resting", "", None,
                    [str(o.get("id") or o.get("orderID") or "") for o in orders],
                    "reported; the engine places only Fill-And-Kill orders",
                    {"count": len(orders)})
                result.mismatches.append(Mismatch(
                    kind="orders.resting", subject="",
                    actual={"count": len(orders)},
                    action="reported", critical=False))
                self.log.warning("Reconciliation: resting orders found",
                                 count=len(orders))

        # 4. Balance sanity check.
        await self._check_balance(result, live)

        # In DRY-RUN there is no exchange to disagree with — the comparison is
        # against the bot's own simulated book, and every mismatch above was
        # already self-healed (adopted / closed / resized) and journalled. A
        # halt exists to stop real orders leaving on state we cannot trust;
        # halting a simulation over its own bookkeeping protects nothing and
        # reads as "records disagree with the exchange" to an operator who is
        # not trading on an exchange at all. Logged loudly, never halted.
        if not live:
            for mismatch in result.mismatches:
                mismatch.critical = False

        self.last_run = time.time()
        self.last_result = result
        self.journal.set_state("reconcile.last", result.to_dict())
        if result.clean:
            self.log.event("reconcile.clean", checked=result.checked,
                           skippedYoung=result.skipped_young, live=live)
        else:
            self.log.event("reconcile.mismatch", checked=result.checked,
                           mismatches=len(result.mismatches),
                           critical=len(result.critical),
                           adopted=result.adopted, closed=result.closed,
                           resized=result.resized, live=live)
        return result

    def due(self, now: Optional[float] = None) -> bool:
        """True when reconciliation should run this cycle.

        Defaults to every loop, per the safety requirement that the bridge
        never trades on state it has not just verified.
        """
        if self.config.reconciliation.every_loop:
            return True
        now = now or time.time()
        interval = max(30, self.config.reconciliation.interval_seconds)
        return (now - self.last_run) >= interval

    # -- helpers -------------------------------------------------------------

    async def _actual_positions(self, live: bool) -> dict[str, PositionView]:
        if live:
            positions = await self.data.exchange_positions()
            return {p.token_id: p for p in positions if p.size > 0}
        if self.paper is None:
            return {}
        return {p.token_id: p
                for p in self.paper.views(lambda _t: None) if p.size > 0}

    def _adopt(self, position: PositionView) -> None:
        """Write an exchange-truth position into the journal as a lifecycle.

        Tagged ``adopted`` so the analysis script can separate positions the
        engine chose from positions it merely inherited — mixing the two would
        corrupt any read of how well the engine's own decisions performed.
        """
        from .models import Action, Decision, ExecutionReport
        decision = Decision(
            action=Action.BUY, token_id=position.token_id,
            market_id=position.market_id, outcome=position.outcome,
            question=position.question, size_shares=position.size,
            size_usdc=position.cost, limit_price=position.avg_price,
            reason="Adopted from the exchange during reconciliation.",
            exit_style="", rationale={"adopted": True},
        )
        report = ExecutionReport(
            decision=decision, submitted=True, status="ADOPTED",
            requested_size=position.size, filled_size=position.size,
            avg_price=position.avg_price,
            simulated=not self.config.mode.live,
        )
        lifecycle_id = self.journal.open_lifecycle(decision, report)
        position.lifecycle_id = lifecycle_id
        if self.paper is not None:
            self.paper.link_lifecycle(position.token_id, lifecycle_id)

    def _resize(self, row: dict, position: PositionView) -> None:
        """Adopt the exchange's size for an existing lifecycle."""
        self.journal.execute(
            "UPDATE lifecycles SET entry_size = ?, entry_cost = ? WHERE id = ?",
            (position.size + (row["exit_size"] or 0.0),
             position.size * (row["entry_price"] or position.avg_price),
             row["id"]),
        )

    async def _check_balance(self, result: ReconcileResult,
                             live: bool) -> None:
        if not live:
            return
        account = await self.data.account()
        recorded = self.journal.get_state("account.balance", None)
        self.journal.set_state("account.balance", account.balance)
        if recorded is None:
            return
        tolerance = max(0.01, self.config.reconciliation.balance_tolerance)
        # Only a *decrease* the bridge did not cause is interesting; deposits and
        # settlement credits arrive from outside and are expected.
        if float(recorded) - account.balance > tolerance:
            mismatch = Mismatch(
                kind="balance.drop", subject=account.address,
                expected={"balance": float(recorded)},
                actual={"balance": account.balance},
                action="adopted the exchange balance")
            self.journal.record_reconciliation(
                mismatch.kind, account.address, mismatch.expected,
                mismatch.actual, mismatch.action, None)
            result.mismatches.append(mismatch)
            self.log.warning("Reconciliation: balance fell unexpectedly",
                             recorded=float(recorded), actual=account.balance)
