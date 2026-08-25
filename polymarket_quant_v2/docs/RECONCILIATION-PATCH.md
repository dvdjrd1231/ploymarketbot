# Reconciliation exit safety — patch report

Surgical patch, applied 2026-08-25. Scope: exit-state correctness only.

---

## 1. The defect, located exactly

`polymarket-quant-bridge/pqb/reconcile.py:121-153`

```python
for token_id, row in lifecycles.items():
    if token_id in actual:
        continue
    if grace and now - (row["entry_ts"] or 0) < grace:
        result.skipped_young += 1
        continue
    ...
    self.journal.close_lifecycle(
        lifecycle_id=row["id"], exit_price=mark, exit_size=size,
        realized_pnl=pnl,
        reason="Reconciliation: no longer held on the exchange.",
        exit_style="reconciled")
```

**One absence from one snapshot closes the position**, at last known mark, with
realized P&L written to the journal. There is no re-check, no second
observation, no market-resolution test, and no distinction between *temporarily
missing from a snapshot* and *confirmed no longer held*.

The only existing protection is a grace window keyed on `entry_ts`, which
protects a position for its first few minutes and nothing afterwards.

### Two consequences

**A. Mass closure on an API blip.** `_actual_positions` returns
`await self.data.exchange_positions()`. If that call returns an empty list —
rate limit, transient failure, expired auth — then *every* open position past
its grace window is closed in a single pass. Nothing distinguishes "the venue
reports no positions" from "the call returned nothing".

**B. Contaminated training data.** `pqb/decision/high_confidence.py::_load_setups`
reads `lifecycles WHERE status='CLOSED'` and scores `realized_pnl > 0` as a
win. A reconciliation closure is therefore indistinguishable from a real
strategy exit and teaches the empirical entry gate that a setup won or lost
when nothing was decided.

---

## 2. Was it actually happening? — the honest answer

**No, not yet.** Measured on the live journal:

```
reconciliations   0 rows
lifecycles        0 rows
executions        0 rows
```

The existing engine has never opened a position, so this code path has never
executed in production. **The defect is latent: real in the code, unobserved in
the data.**

That matters for how the patch is described. It is **preventive**. No
historical trade was harmed, no P&L was lost, and no before/after profit
comparison is possible. The patch does not claim to have recovered anything.

The brief asked to establish this before changing behaviour. This is that
answer.

---

## 3. The corrected flow

`pqv2/reconciliation.py`

```
RECONCILIATION EVENT
        │
        ▼
   VERIFY POSITION STATE     (strongest evidence first)
        │
   ┌────┴─────────────────────────────────────────┐
   │ market resolved?          → MARKET_SETTLED    │ close, as settlement
   │ confirmed exit fill?      → CONFIRMED_CLOSED  │ close, as reconciliation
   │ token balance == 0?       → CONFIRMED_CLOSED  │ close, as reconciliation
   │ snapshot says present?    → STILL_OPEN        │ HOLD
   │ snapshot silent?          → UNCERTAIN         │ HOLD + re-check
   │ nothing observed?         → UNCERTAIN         │ HOLD
   └───────────────────────────────────────────────┘
```

`verify()` has **no fall-through that closes a position**. Every path that is
not an explicit confirmation returns `may_close=False`, which makes fail-safe
structural rather than a policy. Asserted exhaustively by
`test_verify_has_no_path_that_closes_without_explicit_evidence`, which walks all
81 combinations of the four evidence inputs.

### Evidence is three-valued, not two

`None` means **not observed** and is never read as zero or as absent. "The
snapshot did not mention it" and "the venue reports a zero balance" are
different facts; the unpatched code conflates them, and that conflation *is* the
bug.

### Suspect snapshots

A snapshot is refused as evidence of disappearance when:

- the source reported unhealthy, **or**
- it lists **zero positions in total** while we hold some (the mass-closure
  case), **or**
- it **predates our last confirmed state** (it cannot disprove a newer fill).

### Bounded retry

A position must be observed missing `required_confirmations` times (default 3),
in separate cycles, from a healthy source, with a minimum interval between
attempts — so a fast loop cannot "confirm" a closure by polling one snapshot
three times.

After `max_attempts` the event is `ABANDONED_TO_MONITORING`: it stops consuming
reconciliation attention, **the position stays open**, and it is flagged. It is
never closed by timeout, because a timeout is not evidence.

---

## 4. Protections that were required, and how each is enforced

| requirement | mechanism | test |
|---|---|---|
| §1 reconciled ≠ exit | `verify()` defaults to `may_close=False` | `test_temporary_snapshot_gap_does_not_close_a_position` |
| §2 verify position state | `PositionEvidence`, 3-valued | `test_stale_snapshot_cannot_disprove_a_newer_fill` |
| §3 require confirmation | 4 authoritative sources only | `test_verify_has_no_path_that_closes_without_explicit_evidence` |
| §4 bounded retry, no stall | `max_attempts` → abandon, not close | `test_retries_are_bounded_and_abandon_to_monitoring_not_to_closure` |
| §5 protect real exits | `classify_exit()`, strategy always wins | 7 parametrised cases |
| §6 no entry changes | AST import check | `test_reconciliation_module_touches_nothing_else` |
| §7 protect training data | `training_filter()`, quarantine | `test_unverified_reconciliation_is_quarantined_from_training` |
| §8 preserve raw event | `raw` written once, `resolution` moves | `test_raw_event_is_preserved_alongside_its_resolution` |
| §9 diagnostics only | AST check that `verify()` never reads counters | `test_diagnostics_never_influence_decisions` |
| §10 exit-reason integrity | `ExitReason` enum, two reconciliation entries | `test_exit_reason_taxonomy_separates_decisions_from_bookkeeping` |
| §14 fail safe to HOLD | no closing fall-through | `test_no_observation_at_all_fails_safe_to_uncertain` |
| §15 Strategy B untouched | AST import check | `test_strategy_b_engine_is_untouched_by_the_patch` |

**32 tests, all passing.** Full suite: **143 passed, 3 skipped**.

---

## 5. Legitimate exits are untouched

`classify_exit()` gives the strategy priority unconditionally. TAKE_PROFIT stays
TAKE_PROFIT, STOP stays STOP, TRAILING stays TRAILING, SETTLEMENT stays
SETTLEMENT — even when a reconciliation event fires in the same cycle.

The taxonomy also separates *who decided*:

- `is_strategy_decision` — the strategy chose the timing
- `training_eligible` — the record may teach an exit model

`RECONCILIATION_UNVERIFIED` is neither. `RECONCILIATION_CONFIRMED` and
`SETTLEMENT` are training-eligible but are **not** strategy decisions — an exit
model must not learn "I chose to exit here" from a market that simply resolved.

---

## 6. Legacy records

`training_filter()` quarantines historical rows carrying
`exit_style='reconciled'`. Those were written by the unpatched path with no
verification, so they cannot be trusted as decided exits until each is
re-verified. There are currently **0** such rows, but the filter is in place for
when there are.

---

## 7. Before / after

```
python -m pqv2 reconcile --demo
```

**BEFORE** — 0 reconciliation events, 0 lifecycles, 0 reconciled exits, 0 P&L
attributed. The path has never run.

**AFTER** — demonstrated on the observed failure pattern: a single empty
snapshot arriving while 20 positions are held.

```
old path would close :  20
patched path closes  :   0
prevented            :  20
```

**No P&L delta is estimated, deliberately.** A position that is no longer closed
goes on to have a different future, and that future is not in this data. Any
P&L number quoted from such a replay would be invented. Measure it forward in
shadow mode instead.

---

## 8. Remaining anomalies

1. **The root cause is upstream and unaddressed by this patch.**
   `exchange_positions()` still cannot distinguish "no positions" from "the call
   failed". The guard now refuses to act on an empty snapshot, but the engine
   will still log a mass mismatch on every blip. A source-health check at the
   adapter is the complete fix.

2. **The patch is implemented in V2 and is NOT applied to the original
   installation.** The standing rule is never to modify it, and this patch
   itself forbids modifying Strategy A. The exact minimal change is supplied as
   a reviewable diff at `patches/v1_reconcile_guard.patch` — **apply it
   deliberately, not automatically.** It also names the one-line change needed
   in `high_confidence.py::_load_setups` to stop reconciliation closures
   entering the empirical gate.

3. **`grace` is keyed on `entry_ts` only.** A position open for hours has no
   grace at all. The guard's cross-cycle confirmation supersedes this in V2, but
   in V1 the window remains as it was — this patch does not change it.

---

## 9. What was NOT changed

Entry thresholds · market-state filters · wallet filters · confidence
thresholds · strategy gates · trade frequency · position sizing · stake
calculation · Win Expansion · compounding · wallet copying · RN1 logic ·
Strategy A · Strategy B · the Rust crate · the execution engine.

Verified by AST inspection in `tests/test_reconciliation.py`: no strategy, risk,
validation or research module imports the patch. Only the CLI and its own report
renderer do.

---

## 10. Success criteria

| criterion | met |
|---|---|
| a temporary inconsistency can no longer silently close a valid position | **yes** — 20/20 prevented in the replay |
| legitimate strategy exits continue functioning | **yes** — 7 parametrised cases |
| reconciliation no longer contaminates learning data before verification | **yes** — `training_filter()`, quarantine by default |
| Strategy B behaviour unmodified | **yes** — asserted by AST |

**No claim is made that this patch improves profitability.** It prevents a class
of incorrect closure that had not yet occurred.
