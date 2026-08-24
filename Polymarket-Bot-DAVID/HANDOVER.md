# Handover — Autonomous Research Layer

Build 69 · verified end-to-end against a copy of your live data on 2026-08-21.

---

## 1. Read this before you run anything

**Run this once, before the first research pass:**

```
pqb resize-library --yes
```

Your library holds **2,376 validation rows recorded at the wrong account size**
(a sizing bug fixed in an earlier build). Those rows get summed with correctly
sized evidence, so nothing can promote and the adversarial layer stays dormant
reporting *0 candidates attacked*. That looks exactly like a broken feature and
it is not — it is the system refusing to act on evidence it knows is mis-sized.
The command clears those rows. Evidence re-accumulates over the following
passes; the strategies themselves are kept.

**The system currently reports 0 tradable strategies. That is the correct
answer, not a failure.** Your two positive records sit at 1 and 4 trades. A
library that reported a winner on four trades would be broken; this one says so
plainly and keeps researching.

---

## 2. What this layer does

It sits **above** the existing discovery and validation engines and changes
what gets researched next. It cannot promote, reject, or trade anything — the
existing validation ladder remains the only authority on status.

```
discovery → hypotheses → convergence → ADVERSARIAL ATTACK → research priority
                                                                    ↓
                                         (unchanged) OOS validation → ladder → trading gate
```

Three commands show it:

| Command | Shows |
|---|---|
| `pqb lab` | Why each candidate is getting research, what attack found, what each failure asks the search to try next |
| `pqb funnel` | Pipeline health stage by stage, and the first stage that went to zero |
| `pqb hypotheses` | Convergence groups and adversarial results |

In the dashboard, the **Discovery** tab now carries two new columns:
**"Attacked"** and **"Why researching"**.

---

## 3. Proof it works — from your own data

The battery caught this candidate on your markets:

```
seq|price_down_impulse|spread_widening|up#v1
   +0.0103 expectancy · 12 trades · 4 markets      ← looks promising

   VERDICT: BROKEN (robustness 0.46, 60% coverage)
   ✗ concentration        top market carries 100% of positive P&L
   ✗ leave_one_market_out +0.0103 → -0.0134 without one market
   ✗ edge_vs_dispersion   +0.0545 ± 0.0718 across 4 markets (t=0.76)
   ✗ drawdown_stress      worst-market drawdown is 2.07× total P&L
   ✓ placebo              beat random entries (p=0.015) — the signal IS real
   ✓ cost_stress          survives +0.0050/trade
```

That is the whole value in one row. Ordinary backtesting advances this
candidate. The attack shows its entire profit is one market, while separately
confirming the entry signal itself is genuine — so the idea is worth keeping
and the *evidence* is not yet worth anything.

**Note what did NOT happen: its status stayed `validating`.** A BROKEN verdict
lowers research priority. It never rejects.

---

## 4. What changed in this build

**Two dormant tests now run.** `placebo` and `liquidity_stress` were declared
but never executed. Battery coverage went **47% → 60%**.

- **placebo** — replays random entries of the same count and duration on the
  same markets, 200 draws, seeded from the candidate id so the verdict is
  reproducible. Catches the failure nothing else can see: a rule that captured
  market drift rather than a signal. Such a candidate passes leave-one-out,
  temporal split *and* dispersion, because drift is broad, stable and
  replicated.
- **liquidity_stress** — splits the candidate's markets by book depth and asks
  where the P&L came from. Fails an edge that exists only where the book is too
  thin to get filled. Uses measured spread where available, traded value
  otherwise (only 13% of series can quote a spread — historical series have no
  order book).

**The feature-availability gate was wrong, and it was expensive.** It
quarantined a rule if the raw CSV lacked a literal column named e.g.
`price_band_z`. But bridge-path rules are never replayed against the raw CSV —
`_oos_context` runs the feature engineer first, producing **988** engineered
columns from the ~121 on disk. The gate was answering a question the replay
never asks, and answering it "absent" for **159 of 235 candidates**.

Fixed by resolving an engineered name back to its base column (the exact
inverse of `live_features.required_columns`). Verified against the real
engineered frame: **all 95 requested features present, zero over-admission.**

Measured effect on your data:

| | before | after |
|---|---|---|
| OOS replays allocated per pass | 491 | **1,500** (budget saturated) |
| New independent evidence events | 84 | **1,279** |
| Candidates evaluated | 11 | 35 |
| Replay failures | 0 | **0** |

The research engine was running at roughly **7%** of its evidence-gathering
capacity. 39 candidates remain quarantined — those are genuinely unavailable
and correctly held.

Quarantine is also now **releasable**. Previously the loop skipped anything
already quarantined, which made any gate bug permanent. Released candidates
return at `new` — never at a remembered `validated` — so a data-availability
accident can never hand back a trading status no evidence re-earned.

**Dashboard wired.** The seven fields the layer writes now reach the screen.

---

## 5. Honest limitations

1. **`delayed_entry` is not run as an attack.** Entry timing is a different
   experiment, not a perturbation of an existing record, so the pass registers
   it as its own candidate with its own independent evidence. Counted against
   coverage rather than hidden.
2. **Composition has not fired on your data yet, and that is correct.** §10
   combines *independently discovered* signals. Your two SUPPORTED hypotheses
   both come from a single source (`SEQUENCE_STATE`), so there is no
   independent partner to combine with. The path is proven end-to-end in
   `test_composition_actually_fires_through_a_whole_pass`.
3. **`RULE_NEVER_FIRED` rose from 70 to 111** after the release fix. This is
   new true information, not a regression: released rules are now actually
   being tested, and some never fire. Their directive is `LOOSEN_ENTRY`.
4. **Cost model on tape replays is approximate.** Per-share costs are floored
   at the flat fee, which overstates cost — the safe direction.

---

## 6. What to work on next

1. **`RULE_NEVER_FIRED` (97 blocked).** The largest bottleneck now. These rules'
   entry conditions never occur in the pool. Says everything about the entry
   threshold, nothing about the idea.
2. **Pool breadth (81 markets).** Independent OOS markets per candidate now
   averages well below 1. More settled markets is the single biggest lever on
   how fast anything can validate.
3. **47 constant feature columns.** Order-book features are pinned in
   historical series because no historical book exists. Live capture is the
   only way to make them testable.

---

## 7. Test suite

```
944 passed
```

Run with `python -m pytest tests/ -q` from `polymarket-quant-bridge/`.
Requires `PyQt6` (`pip install PyQt6`) — 5 dashboard tests skip without it.

New coverage in this build:

| File | Covers |
|---|---|
| `test_replay_probe.py` | placebo and liquidity_stress against real series, including the drift case that must fail |
| `test_adversarial.py` | the probe contract — a probe that raises is missing coverage, never a pass |
| `test_feature_domain.py` | engineered-name resolution and quarantine release |
| `test_gui_research_columns.py` | the layer reaching the screen; a BROKEN row keeps its status |
| `test_research_layer.py` | composition firing through a whole pass |

---

## 8. The one rule this layer must never break

The research layer may optimise **what to investigate next**. It may never
optimise **what counts as success**. Every module in it is read-only with
respect to status: `test_adversarial.py` asserts by AST inspection that the
battery never calls `next_status`, `set_status`, or `record_validation`, and
`test_gui_research_columns.py` asserts a BROKEN candidate still renders with
the status the ladder gave it.

If a future change makes a strategy validate *because* it survived attack,
that rule has been broken.
