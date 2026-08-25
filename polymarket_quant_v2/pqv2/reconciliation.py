"""Reconciliation exit safety: a data disagreement is not a trading decision.

THE DEFECT THIS CORRECTS, located in the existing engine at
`polymarket-quant-bridge/pqb/reconcile.py:121-153`:

    for token_id, row in lifecycles.items():
        if token_id in actual:
            continue
        if grace and now - (row["entry_ts"] or 0) < grace:
            result.skipped_young += 1
            continue
        ...
        self.journal.close_lifecycle(..., exit_style="reconciled")

One absence from ONE snapshot closes the position, at last known mark, with
realized P&L written. There is no re-check, no second observation, no
market-resolution test, and no distinction between "temporarily missing from a
snapshot" and "confirmed no longer held". The only protection is a grace window
keyed on `entry_ts`, which protects a position for its first few minutes and
nothing after that.

Two consequences, and the second is the one the brief cares about:

  1. MASS CLOSURE ON A BLIP. `_actual_positions` returns
     `await self.data.exchange_positions()`. If that call returns an empty list
     -- API hiccup, rate limit, transient failure, auth expiry -- then EVERY
     open position past its grace window is closed in a single pass.

  2. CONTAMINATED TRAINING DATA. `decision/high_confidence.py::_load_setups`
     reads `lifecycles WHERE status='CLOSED'` and scores `realized_pnl > 0` as
     a win. A reconciliation-closed lifecycle is therefore indistinguishable
     from a real strategy exit, and teaches the empirical gate that a setup
     won or lost when in fact nothing was decided.

This module is the corrected flow:

    RECONCILIATION EVENT -> VERIFY POSITION STATE -> {VALID EXIT | HOLD/RETRY}

It never sells. It answers one question -- may this position be closed on
reconciliation evidence? -- and defaults to NO.

SCOPE. This module touches nothing else. It does not read or alter entry
thresholds, sizing, compounding, wallet logic, Strategy A's filters or Strategy
B's route. `tests/test_reconciliation.py` asserts the isolation by AST.
"""

from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional


class Resolution(str, Enum):
    """The FINAL classification of a reconciliation event.

    `PENDING` is a first-class outcome, not a failure: an event that has not
    yet been resolved must be visible as unresolved rather than defaulted into
    a closure.
    """

    PENDING = "PENDING"
    POSITION_STILL_OPEN = "POSITION_STILL_OPEN"
    CONFIRMED_POSITION_CLOSED = "CONFIRMED_POSITION_CLOSED"
    MARKET_SETTLED = "MARKET_SETTLED"
    UNCERTAIN = "RECONCILIATION_UNCERTAIN"
    ABANDONED = "ABANDONED_TO_MONITORING"


class ExitReason(str, Enum):
    """Exit-reason taxonomy. Reconciliation gets TWO entries, deliberately.

    `RECONCILIATION_CONFIRMED` is a real closure that reconciliation happened
    to discover. `RECONCILIATION_UNVERIFIED` is a closure forced without
    independent confirmation -- it must never occur through this module, and it
    exists so that historical records written by the unpatched path can be
    relabelled rather than silently trusted.
    """

    STRATEGY_EXIT = "strategy_exit"
    RISK_EXIT = "risk_exit"
    TAKE_PROFIT = "take_profit"
    STOP = "stop"
    TRAILING = "trailing"
    EDGE_GONE = "edge_gone"
    SETTLEMENT = "settlement"
    WALLET_EXIT = "wallet_exit"
    RECONCILIATION_CONFIRMED = "reconciliation_confirmed"
    RECONCILIATION_UNVERIFIED = "reconciliation_unverified"

    @property
    def is_strategy_decision(self) -> bool:
        """Did the strategy decide this, or did bookkeeping?

        The distinction the whole patch exists to preserve.
        """
        return self in _STRATEGY_DECISIONS

    @property
    def training_eligible(self) -> bool:
        """May a trade closed for this reason teach an exit model?

        An unverified reconciliation closure teaches nothing, because nothing
        was decided. Section 7 of the patch, as a property.
        """
        return self is not ExitReason.RECONCILIATION_UNVERIFIED


_STRATEGY_DECISIONS = frozenset({
    ExitReason.STRATEGY_EXIT, ExitReason.TAKE_PROFIT, ExitReason.STOP,
    ExitReason.TRAILING, ExitReason.EDGE_GONE, ExitReason.WALLET_EXIT,
})

# Settlement and risk exits are real and training-eligible, but they are not
# the strategy's own timing decision -- an exit model should not learn "I chose
# to exit here" from a market that simply resolved.
_NOT_TIMING_DECISIONS = frozenset({
    ExitReason.SETTLEMENT, ExitReason.RISK_EXIT,
    ExitReason.RECONCILIATION_CONFIRMED,
})


@dataclass
class PositionEvidence:
    """Everything independently knowable about whether a position still exists.

    `None` means NOT OBSERVED and is never read as zero or as absent. That
    distinction is the whole verification: "the snapshot did not mention it" and
    "the venue reports a zero balance" are different facts, and the unpatched
    code conflates them.
    """

    token_id: str
    # local belief
    local_size: float = 0.0
    local_entry_ts: float = 0.0
    # independent observations -- None = not observed this cycle
    snapshot_present: Optional[bool] = None
    snapshot_size: Optional[float] = None
    token_balance: Optional[float] = None
    market_resolved: Optional[bool] = None
    resolution_price: Optional[float] = None
    confirmed_exit_fill: Optional[bool] = None
    last_confirmed_ts: float = 0.0
    snapshot_ts: float = 0.0
    # was the whole snapshot empty / did the source fail?
    snapshot_total_positions: Optional[int] = None
    source_healthy: bool = True

    @property
    def stale(self) -> bool:
        """Is the snapshot older than the last thing we know for certain?

        A snapshot that predates our last confirmed fill cannot disprove that
        fill.
        """
        return bool(self.snapshot_ts and self.last_confirmed_ts
                    and self.snapshot_ts < self.last_confirmed_ts)

    @property
    def snapshot_is_suspect(self) -> bool:
        """Reasons to distrust this snapshot as evidence of disappearance.

        The empty-snapshot case is the mass-closure defect: if the source
        reports zero positions in total while we believe we hold some, the
        snapshot is far more likely to be broken than the book to be empty.
        """
        if not self.source_healthy:
            return True
        if self.snapshot_total_positions == 0 and self.local_size > 0:
            return True
        return self.stale


@dataclass
class ReconciliationEvent:
    """One raw event, preserved verbatim, plus its eventual resolution.

    Section 8: never delete the raw event. `raw` is written once and never
    mutated; `resolution` is the only field that moves.
    """

    token_id: str
    ts: float
    raw: str = "RECONCILIATION_EVENT"
    kind: str = "position.missing"
    expected: dict = field(default_factory=dict)
    observed: dict = field(default_factory=dict)
    resolution: str = Resolution.PENDING.value
    exit_reason: str = ""
    attempts: int = 0
    first_seen_ts: float = 0.0
    resolved_ts: float = 0.0
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def closed_position(self) -> bool:
        return self.resolution in (Resolution.CONFIRMED_POSITION_CLOSED.value,
                                   Resolution.MARKET_SETTLED.value)

    @property
    def training_eligible(self) -> bool:
        if not self.exit_reason:
            return False
        return ExitReason(self.exit_reason).training_eligible


@dataclass
class VerificationVerdict:
    may_close: bool
    resolution: Resolution
    exit_reason: Optional[ExitReason]
    reason: str
    evidence_used: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"may_close": self.may_close,
                "resolution": self.resolution.value,
                "exit_reason": self.exit_reason.value if self.exit_reason else "",
                "reason": self.reason, "evidence_used": self.evidence_used}


def verify(ev: PositionEvidence) -> VerificationVerdict:
    """May this position be closed on reconciliation evidence alone?

    Evaluated strongest-evidence-first. Every path that is not an explicit
    confirmation returns `may_close=False` -- there is no fall-through that
    closes a position, which is what makes section 14 (fail safe to HOLD)
    structural rather than a policy.
    """
    used: list = []

    # 1. SETTLEMENT. The strongest evidence there is: the market resolved, so
    #    the position ended for a reason that has nothing to do with
    #    reconciliation. Classify it as settlement, not as a reconciled exit.
    if ev.market_resolved is True:
        used.append("market_resolved")
        return VerificationVerdict(
            True, Resolution.MARKET_SETTLED, ExitReason.SETTLEMENT,
            f"market resolved at {ev.resolution_price}; this is a settlement, "
            "not a reconciliation exit", used)

    # 2. A CONFIRMED EXIT FILL. We sold, and the fill is confirmed. The
    #    position is genuinely gone and reconciliation merely noticed.
    if ev.confirmed_exit_fill is True:
        used.append("confirmed_exit_fill")
        return VerificationVerdict(
            True, Resolution.CONFIRMED_POSITION_CLOSED,
            ExitReason.RECONCILIATION_CONFIRMED,
            "an exit fill is confirmed for this token", used)

    # 3. AN OBSERVED ZERO BALANCE. Note the asymmetry with absence: a reported
    #    balance of zero is a measurement; not being mentioned in a snapshot is
    #    not.
    if ev.token_balance is not None:
        used.append("token_balance")
        if ev.token_balance <= 0:
            if ev.snapshot_is_suspect:
                return VerificationVerdict(
                    False, Resolution.UNCERTAIN, None,
                    "balance reads zero but the source is unhealthy or stale; "
                    "refusing to close on a suspect reading", used)
            return VerificationVerdict(
                True, Resolution.CONFIRMED_POSITION_CLOSED,
                ExitReason.RECONCILIATION_CONFIRMED,
                "token balance independently confirmed at zero", used)
        return VerificationVerdict(
            False, Resolution.POSITION_STILL_OPEN, None,
            f"token balance is {ev.token_balance:g}: the position is still "
            "held and the snapshot was wrong", used)

    # 4. THE SNAPSHOT SAYS IT IS THERE. Nothing to reconcile.
    if ev.snapshot_present is True:
        used.append("snapshot_present")
        return VerificationVerdict(
            False, Resolution.POSITION_STILL_OPEN, None,
            "the position is present in the latest snapshot", used)

    # 5. THE SNAPSHOT IS SILENT. This is the case the unpatched code treats as
    #    proof of closure. It is not proof of anything.
    if ev.snapshot_present is False:
        used.append("snapshot_absent")
        if ev.snapshot_is_suspect:
            why = ("source reported unhealthy" if not ev.source_healthy else
                   "snapshot lists zero positions in total"
                   if ev.snapshot_total_positions == 0 else
                   "snapshot predates the last confirmed state")
            return VerificationVerdict(
                False, Resolution.UNCERTAIN, None,
                f"absent from the snapshot, but {why} - absence from a suspect "
                "snapshot is not evidence of closure", used)
        return VerificationVerdict(
            False, Resolution.UNCERTAIN, None,
            "absent from one snapshot with no corroborating evidence; a single "
            "absence is a data disagreement, not a closure", used)

    # 6. NOTHING WAS OBSERVED AT ALL.
    used.append("no_observation")
    return VerificationVerdict(
        False, Resolution.UNCERTAIN, None,
        "no independent observation available this cycle", used)


@dataclass
class Diagnostics:
    """Section 9. Counters only -- nothing here feeds a trading decision.

    `tests/test_reconciliation.py::test_diagnostics_never_influence_decisions`
    asserts by AST that `verify()` and `ReconciliationGuard.observe()` never
    read this object.
    """

    counters: Counter = field(default_factory=Counter)

    KEYS = ("reconciliation_events", "reconciliation_uncertain",
            "reconciliation_exits_prevented", "reconciliation_exits_confirmed",
            "reconciliation_retries", "reconciliation_position_still_open",
            "reconciliation_position_confirmed_closed",
            "reconciliation_data_conflicts", "reconciliation_settled",
            "reconciliation_abandoned_to_monitoring")

    def bump(self, key: str, n: int = 1) -> None:
        self.counters[key] += n

    def to_dict(self) -> dict:
        return {k: int(self.counters.get(k, 0)) for k in self.KEYS}

    def report(self) -> str:
        d = self.to_dict()
        width = max(len(k) for k in d)
        return "\n".join(f"  {k:<{width}}  {v:>8,}" for k, v in d.items())


class ReconciliationGuard:
    """Bounded, stateful verification across cycles.

    A position must be observed missing `required_confirmations` times, in
    separate cycles, with non-suspect evidence, before reconciliation may close
    it. Until then the position is held and monitored.

    Bounded on purpose (section 4): after `max_attempts` the event is
    ABANDONED_TO_MONITORING -- it stops consuming reconciliation attention, the
    position stays open, and it is flagged for an operator. It is never closed
    by timeout, because a timeout is not evidence.
    """

    def __init__(self, *, required_confirmations: int = 3,
                 max_attempts: int = 12,
                 min_seconds_between_attempts: float = 30.0,
                 diagnostics: Optional[Diagnostics] = None) -> None:
        if required_confirmations < 2:
            raise ValueError(
                "at least two independent observations are required; one is "
                "what the unpatched engine already does")
        self.required_confirmations = required_confirmations
        self.max_attempts = max_attempts
        self.min_seconds = min_seconds_between_attempts
        self.diag = diagnostics or Diagnostics()
        self.open_events: dict = {}
        self.history: list = []
        # Tokens whose reconciliation was abandoned to normal monitoring. They
        # are NOT re-opened as fresh events -- doing so restarts the attempt
        # budget every cycle and produces the unbounded loop section 4 forbids.
        # Authoritative evidence can still resolve them; repeated silence
        # cannot.
        self.abandoned: dict = {}

    # -- the entry point -----------------------------------------------------
    def observe(self, ev: PositionEvidence,
                now: Optional[float] = None) -> ReconciliationEvent:
        """Record one reconciliation observation and return the current event.

        Call this INSTEAD OF closing a lifecycle. It returns an event whose
        `resolution` tells the caller what to do; only
        `CONFIRMED_POSITION_CLOSED` and `MARKET_SETTLED` authorise a close.
        """
        now = now if now is not None else time.time()

        # Already abandoned to monitoring. Only AUTHORITATIVE evidence may
        # revive it -- another silent snapshot is the same non-information that
        # abandoned it in the first place, and re-opening on that would restart
        # the attempt budget forever.
        parked = self.abandoned.get(ev.token_id)
        if parked is not None:
            verdict = verify(ev)
            authoritative = (verdict.may_close or
                             verdict.resolution is Resolution.POSITION_STILL_OPEN)
            if not authoritative:
                return parked
            self.abandoned.pop(ev.token_id, None)
            parked.notes.append(
                f"revived at {now:.0f} by authoritative evidence: "
                f"{verdict.reason}")
            self.open_events[ev.token_id] = parked
            parked.ts = now - self.min_seconds - 1     # allow immediate re-check

        event = self.open_events.get(ev.token_id)
        if event is None:
            event = ReconciliationEvent(
                token_id=ev.token_id, ts=now, first_seen_ts=now,
                expected={"size": ev.local_size,
                          "entry_ts": ev.local_entry_ts})
            self.open_events[ev.token_id] = event
            self.diag.bump("reconciliation_events")
        else:
            # Rate-limit re-checks so a fast loop cannot burn the attempt
            # budget in one second and "confirm" a closure from a single
            # snapshot repeated three times.
            if now - event.ts < self.min_seconds:
                event.notes.append(
                    f"re-check at {now:.0f} ignored: inside the "
                    f"{self.min_seconds:g}s minimum between attempts")
                return event
            self.diag.bump("reconciliation_retries")

        event.ts = now
        event.attempts += 1
        event.observed = {
            "snapshot_present": ev.snapshot_present,
            "token_balance": ev.token_balance,
            "market_resolved": ev.market_resolved,
            "confirmed_exit_fill": ev.confirmed_exit_fill,
            "source_healthy": ev.source_healthy,
            "snapshot_total_positions": ev.snapshot_total_positions,
        }

        verdict = verify(ev)
        event.notes.append(f"attempt {event.attempts}: {verdict.reason}")

        if ev.snapshot_is_suspect:
            self.diag.bump("reconciliation_data_conflicts")

        # Position is back / never left -> the disagreement resolved itself.
        if verdict.resolution is Resolution.POSITION_STILL_OPEN:
            self.diag.bump("reconciliation_position_still_open")
            self.diag.bump("reconciliation_exits_prevented")
            return self._resolve(event, Resolution.POSITION_STILL_OPEN, None,
                                 now)

        # Immediate, authoritative closure evidence.
        if verdict.may_close:
            if verdict.resolution is Resolution.MARKET_SETTLED:
                self.diag.bump("reconciliation_settled")
            else:
                self.diag.bump("reconciliation_position_confirmed_closed")
            self.diag.bump("reconciliation_exits_confirmed")
            return self._resolve(event, verdict.resolution,
                                 verdict.exit_reason, now)

        # Uncertain. Count corroborating absences, but only clean ones.
        self.diag.bump("reconciliation_uncertain")
        self.diag.bump("reconciliation_exits_prevented")
        if verdict.resolution is Resolution.UNCERTAIN \
                and ev.snapshot_present is False and not ev.snapshot_is_suspect:
            clean = sum(1 for n in event.notes if "absence" in n or
                        "absent from one snapshot" in n)
            event.notes.append(f"clean absences: {clean}")
            if clean >= self.required_confirmations:
                # Repeated, clean, independent absence across separate cycles.
                # This is the one path where accumulated absence becomes
                # evidence -- and it is still labelled CONFIRMED only because
                # the observations were corroborating and non-suspect.
                self.diag.bump("reconciliation_position_confirmed_closed")
                self.diag.bump("reconciliation_exits_confirmed")
                return self._resolve(
                    event, Resolution.CONFIRMED_POSITION_CLOSED,
                    ExitReason.RECONCILIATION_CONFIRMED, now)

        if event.attempts >= self.max_attempts:
            event.notes.append(
                f"{event.attempts} attempts without resolution; abandoned to "
                "normal monitoring - the position REMAINS OPEN and is flagged "
                "for an operator. A timeout is not evidence of closure.")
            self.diag.bump("reconciliation_abandoned_to_monitoring")
            resolved = self._resolve(event, Resolution.ABANDONED, None, now)
            self.abandoned[event.token_id] = resolved
            return resolved

        event.resolution = Resolution.UNCERTAIN.value
        return event

    def _resolve(self, event: ReconciliationEvent, resolution: Resolution,
                 exit_reason: Optional[ExitReason],
                 now: float) -> ReconciliationEvent:
        event.resolution = resolution.value
        event.exit_reason = exit_reason.value if exit_reason else ""
        event.resolved_ts = now
        self.open_events.pop(event.token_id, None)
        self.history.append(event)
        return event

    # -- queries -------------------------------------------------------------
    def may_close(self, token_id: str) -> bool:
        ev = next((e for e in self.history if e.token_id == token_id), None)
        return bool(ev and ev.closed_position)

    def pending(self) -> list:
        return list(self.open_events.values())

    def summary(self) -> dict:
        return {"diagnostics": self.diag.to_dict(),
                "pending": len(self.open_events),
                "resolved": len(self.history),
                "resolutions": dict(Counter(e.resolution
                                            for e in self.history))}


def classify_exit(*, strategy_reason: Optional[str],
                  reconciliation: Optional[ReconciliationEvent]) -> ExitReason:
    """Section 10: the final exit-reason of a closed trade.

    A strategy exit ALWAYS wins. If the strategy independently decided to take
    profit, stop out or trail, that is the reason the trade ended, and a
    reconciliation event happening nearby does not relabel it. Section 5 of the
    patch, in one function.
    """
    if strategy_reason:
        try:
            reason = ExitReason(strategy_reason)
        except ValueError:
            return ExitReason.STRATEGY_EXIT
        if reason not in (ExitReason.RECONCILIATION_CONFIRMED,
                          ExitReason.RECONCILIATION_UNVERIFIED):
            return reason
    if reconciliation is None:
        return ExitReason.STRATEGY_EXIT
    if reconciliation.resolution == Resolution.MARKET_SETTLED.value:
        return ExitReason.SETTLEMENT
    if reconciliation.resolution == Resolution.CONFIRMED_POSITION_CLOSED.value:
        return ExitReason.RECONCILIATION_CONFIRMED
    return ExitReason.RECONCILIATION_UNVERIFIED


def training_filter(records) -> tuple:
    """Split closed trades into what may teach a model and what may not.

    Section 7. Returns `(eligible, quarantined)`. A record is quarantined if it
    was closed by an unverified reconciliation -- nothing was decided, so there
    is nothing to learn, and letting it through would teach an exit model that
    a setup won or lost when the position may never have closed at all.
    """
    eligible, quarantined = [], []
    for r in records:
        reason = r.get("exit_reason") if isinstance(r, dict) else \
            getattr(r, "exit_reason", "")
        try:
            ok = ExitReason(reason).training_eligible
        except (ValueError, TypeError):
            # An unrecognised reason is not automatically trusted. The
            # historical `exit_style="reconciled"` written by the unpatched
            # engine lands here, which is the correct place for it.
            ok = str(reason) not in ("reconciled", "", None)
        (eligible if ok else quarantined).append(r)
    return eligible, quarantined
