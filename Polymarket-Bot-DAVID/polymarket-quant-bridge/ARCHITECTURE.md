# Architecture note — reused vs. newly added

The guiding constraint was to achieve the outcome with the least new code and
architectural change possible. This note states exactly where that was honoured,
where it could not be, and why.

---

## 1. The Quant Bridge is the brain, and it is imported

Earlier sessions recorded the Prop Firm Quant Bridge as absent and assumed a C#
LEAN project. It is present at `../Advanced Strategy Development for
QuantConnect LEAN/qc_lean_bridge`, and it is **Python** — a research platform
(`core/`, ~8,900 lines) built around QuantConnect LEAN concepts: tick engines,
feature engineering, automated strategy discovery, walk-forward and Monte-Carlo
validation, ranking, a live strategy manager and a prop-firm risk gate.

Being Python, this is integration **shape A** from the integration plan — the
cheapest of the three. The bridge is imported and executed through a single path
shim, [`pqb/quant.py`](pqb/quant.py), exactly as `ploymarketbot` is reached
through `pqb/upstream.py`. Nothing is reimplemented and nothing crosses a
process boundary.

The seam is unchanged and still one method:

```python
# pqb/bridge/ports.py
class DecisionEngine(Protocol):
    def evaluate(self, context: BridgeContext) -> list[Decision]: ...
```

`LeanDecisionEngine` implements it. What it decides comes from the bridge's
discovered rules, the analytical layer, and the journal's own record — never
from a rule written by hand here.

**The one structural mismatch, and how it is resolved.** The bridge's
`ResearchEngine` is *offline*: it runs over a CSV history and emits `Strategy`
rule objects. It is not a per-cycle `evaluate(context)`. So the integration is
split across the two timescales it actually has:

| Timescale | What runs | Where |
|---|---|---|
| Continuous | capture the feature series live | `runner._capture_research` |
| Occasional (a command) | export → discover → validate → rank → cross-check | `pqb/research.py` |
| Every cycle | evaluate the surviving rules against live features | `bridge/lean_engine.py` |

`BaselineDecisionEngine` is no longer a placeholder for the brain — it is the
**fallback scoring and the structural exit ladder**. `LeanDecisionEngine`
subclasses it, keeping the structure (exits adjudicated first, one outcome per
market, rank-then-fund, wallets as evidence) and overriding everything that
constitutes a view. Those structural properties are properties of the problem,
not strategy, and re-deriving them in the new engine would have duplicated the
part that was already right.

---

## 2. Reused from `ploymarketbot` — imported, not copied

Referenced through a single path shim, [`pqb/upstream.py`](pqb/upstream.py), so
there is exactly one file to change if the upstream layout moves. No upstream
file was modified.

| Reused | What it provides | Why not rewritten |
|---|---|---|
| `services/price_service.py` | CLOB order-book WebSocket, REST batch fallback, crossed-book repair from the authoritative touch, per-quote provenance | This is the hardest part of the integration and it is already correct. Reimplementing delta application and reconnect repair would be pure duplicated risk. |
| `services/gamma.py` | Market metadata, resolution detection, 60s cache dropping to 5s near expiry, in-flight request sharing, closed-market retry | The closed-market retry and the decisive-resolution check (`resolved_price`) encode real Polymarket behaviour that is not obvious from the docs. |
| `services/data_api.py` | Wallet activity feed with a same-second de-duplication watermark, on-chain positions, account value | The watermark handles a genuine edge case (the feed's one-second resolution) that a naive `>` comparison drops trades on. |
| `services/trading.py` | `py-clob-client` facade, all calls off the event loop, key normalisation, order-response parsing, `autodetect_account` | Signing is CPU-bound; the threading discipline here is what keeps the loop responsive. |
| `models.Side`, `TargetTrade` | Shared vocabulary with the upstream clients | Keeps the adapter boundary thin. |
| SQLite patterns (`db.py`) | WAL + `synchronous=NORMAL`, one connection under an `RLock`, state-change-only writes | Pattern reused in `journal.py`; the tables themselves are new, since a decision journal is not a trade log. |
| Logging shape (`applog.py`) | Rotating file + stream handler, configured once | Pattern reused in `logs.py`, writing to this project's own data directory rather than the other project's. |

Also reused conceptually, not by import: the five-independent-loops discipline
(no slow call can stall another concern) and the "never block the event loop"
rule.

---

## 3. Newly added

Only what the brief asked for, plus the seam.

| New | Section | Notes |
|---|---|---|
| `adapters/data_adapter.py` | 3 | Universe resolution (explicit ids + filters + wallet-discovered markets), the full inbound feature set, one `DataApiClient` **per wallet** so watermarks don't collide. |
| `adapters/execution_adapter.py` | 6 | Decision → legal order → fill report. In-flight guard, classified retry, and the simulated `PaperBook`. |
| `adapters/sizing.py` | 6 | Tick/min-size/cash validation as pure functions, so the code path that decides how much money moves is testable without a network. |
| `bridge/ports.py` | 4 | The seam. Read first. |
| `bridge/baseline_engine.py` | 4, 4.1 | Reference engine. Replace. |
| `journal.py` | 5 | Decision journal + persisted engine state. |
| `doubling.py` | 7 | Two-state machine; all four specified edge cases fall out of the design. |
| `reconcile.py` | 6 | Record → adopt exchange truth → alert, in that order. |
| `runner.py` | — | The only glue. Decides nothing. |
| `cli.py`, `config.py`, `logs.py` | 8, 9 | Config with `${env:}` resolution and redaction; kill switch; commands. |
| `scripts/analyze_journal.py` | 5 | The feedback loop's read side, for a person. |
| `quant.py` | — | The path shim to the Quant Bridge. One file to change if it moves. |
| `features.py` | 3 | **The** feature vector. Research export and live evaluation call the same function — see design decisions below. |
| `research.py` | 4 | Export per token → the bridge's `ResearchEngine` → cross-token confirmation. |
| `bridge/lean_engine.py` | 4 | Discovered rules evaluated live. |
| `bridge/live_features.py` | 4 | The bridge's own `FeatureEngineer`, run over a rolling window. |
| `analytics/store.py` | 3 | Broad observations, hourly market rollups, settlements, detections, the research series. |
| `analytics/features.py` | 3 | Per-wallet and per-market feature generation. |
| `analytics/ranking.py` | 3 | Dynamic ranking: shrinkage, staleness decay, cohort labelling. |
| `analytics/anomalies.py` | 3 | The six detectors. |
| `analytics/feedback.py` | 5 | Journal outcomes → bounded score tilts. Closes the loop. |
| `analytics/pipeline.py` | 3 | The per-cycle analytical pass, on two cadences. |
| `tests/` | 9 | 252 tests, offline. |

### Minimal extensions, and why they were unavoidable

- **Richer Gamma parsing.** Upstream's `MarketInfo` carries no category, volume,
  liquidity, tick size or min order size — all of which section 3 requires and
  section 6 must enforce. The adapter parses the raw Gamma record for those and
  still delegates resolution/settlement to the upstream client's cached path.
- **`recent_trades`.** Upstream has no trades endpoint; a best-effort Data API
  call was added that degrades to an empty list rather than failing a cycle.
- **A separate journal database.** Upstream's `copied_trades` is a copy-trade
  log. A decision journal needs decisions that led to nothing, position paths,
  reconciliation events and persisted engine state. New tables in a new file,
  same storage discipline.

### Deliberately NOT reused

`services/engine.py` (the copy engine), `api.py`, `hub.py`, `state.py`, `main.py`
and the React dashboard. The copy engine hard-codes copy-trading rules — Fixed
50%, "copy the soonest-settling trade", mirror-the-target's-exit — which are the
exact opposite of this brief. The web layer serves a UI this project does not
have.

---

## 3b. The autonomous research layer

Added above discovery and beside — never inside — validation. The principle it
implements is one sentence: *the system should get better at deciding what to
research next, and must not get better at convincing itself that weak
strategies are good.* Everything below steers ALLOCATION. Nothing below can
move a candidate toward a gate, and switching all four off leaves
`library.next_status` doing exactly what it did before.

| New | What it does | Why it is a module and not an extension |
|---|---|---|
| `adversarial.py` | Runs the declared battery against a candidate's own persisted per-market ledger: leave-one-market-out, subset and temporal splits, concentration, cost/margin/drawdown stress, and the sibling variants the pass already registers. | Attacking evidence is a different concern from producing it. It takes plain dicts, never a library handle, so it structurally cannot write. |
| `experiments.py` | Classifies every outcome against the failure taxonomy, remembers it, and turns each failure into the next question. Throttles families that repeatedly die the same way. | The search's memory. The library remembers what happened; this remembers what it meant. |
| `reward.py` | The research-quality score: what would testing this again TEACH. Quality is multiplicative, information is additive, steering is bounded. | The one opinionated number in the system, kept in a file that imports nothing that can promote. |

Extended rather than duplicated: `convergence.run_pass` now folds the
candidate-level attacks into hypothesis records and composes SUPPORTED
relationships into ordinary candidates; `eligibility` orders eligible markets
by information gain within each walk-forward class; `allocation` receives a
blended priority; `meta` is unchanged and composes with the reward.

**Three bugs the audit found, all of them silent:**

- **The battery was declared and never executed.** `ADVERSARIAL_TESTS` named
  eight tests, `record_adversarial` had no caller, and every hypothesis
  carried an empty `adversarial` dict — which `convergence_priority` scores at
  0.5, the same as one that had been attacked and drawn. A field that exists
  and is never written reads as evidence of a test.
- **Convergence priority was structurally zero.** The research pass passed
  `"markets": []` into the hypothesis layer, and `convergence_priority` is
  multiplicative with a breadth term, so *every* hypothesis scored exactly
  0.0 and the ranking ordered nothing. Now the candidate's evidence markets
  are passed.
- **`delay_bars` was written and read by nothing.** Every DELAYED-ENTRY
  variant replayed identically to its parent, so its "independent" evidence
  was a copy of evidence that already existed — the adversarial machinery
  fabricating its own confirmation. Both `sequences` and `sharp_moves`
  now honour it, and a test pins that a zero delay leaves existing results
  bit-identical.

**What it deliberately does not do.** A placebo control and a liquidity stress
need fresh replays, so they are reported NOT RUN with the reason and counted
against `coverage` rather than quietly shrinking the denominator. Cost stress
declines to run on bridge rules, because their expectancy is dollars per
position and the spread is dollars per share; `margin_haircut` asks the
unit-free version of the question instead. Scoring an unrun test as a pass is
the exact reward-hacking failure the layer exists to prevent.

Read it through `pqb lab` (why each candidate is being researched, what attack
found, what each failure asks next) and `pqb funnel` (the aggregate blocks).

---

## 4. Notable design decisions

**Two flags to go live.** `dry_run: false` alone does nothing; `allow_live: true`
is also required. One careless edit cannot arm real trading.

**Never price an order from a price nobody is quoting.** The executable price
comes from the book, or from a REST refetch when the book is cold, or the order
is refused. There is deliberately no fallback to the entry price or the last
mark. Both are prices we *wish* existed: an exit sized from the entry price
produces a sell limit above the market that a Fill-And-Kill order kills, so
nothing exits — while the journal records a tidy zero-P&L close that never
happened. This mattered most in the case it was found: a cold-start flatten,
where the process had just restarted and the kill switch was closing everything
out. See `_touch()` in the execution adapter.

**A position pins its own market.** The discovered universe is a ranked slice,
so a market can fall out of it while we still hold a position in it. A filter
decides what is worth *entering* and has no business deciding what we can still
see once we are in. Without pinning, such a position goes blind: no book, mark
falls back to cost, P&L freezes, and stop/trailing exits can never fire because
the mark never moves.

**Rank first, then fund.** Entry candidates are scored, sorted, and only then
allocated from a single running bankroll. Sizing during the scan gave the money
to whichever market the iteration reached first and — because each candidate was
sized against the same opening balance — let one cycle commit $130 of a $100
account. Caught in the first live dry-run; fixed on both sides (engine budgets,
runner tracks cash as fills land).

**One outcome per market.** The outcomes of a binary market are complements that
sum to ~1.00, so holding both is a locked-in loss. Scoring them independently
made both look attractive; the constraint is applied during allocation, where
the whole ranked set is visible. Also caught in the first live dry-run.

**The wallet term is undefined, not zero.** With no wallets configured, blending
a structural zero into every score caps the maximum achievable score at
`1 - wallet_signal_weight` and silently disables all entries — a config that
looks armed but can never fire. The blend weight drops to zero when no wallets
are tracked.

**Exits before entries, in the same cycle.** Capital released by an exit is
spendable by an entry in the same pass, so the ordering is load-bearing, not
cosmetic.

**Decisions are journalled before execution.** A crash mid-order still leaves
evidence of what was intended.

**Reconciliation records before it adopts.** Adopting first would leave no record
of what the divergence actually was.

**One feature function, called by both paths.** The research export and the live
engine build the row with the same `features.token_features()`. Building them
separately would drift, and the failure is silent and total: discovery finds a
rule on a column the live engine computes slightly differently, or not at all,
and the rule quietly never fires.

**The live path runs the bridge's own `FeatureEngineer`.** Discovery does not
search the raw captured columns — it searches what the bridge *derives* from
them, so a discovered rule names `flow_z_z`, not `flow_z`. Caught by inspecting
the first real discovery output. Without live engineering, every rule would have
been looked up in a row that never contains it, counted as stale, and the whole
researched view would have been switched off with the bridge still running,
deciding and journalling as if nothing were wrong. Reimplementing those
definitions was rejected for the same reason: a subtly different rolling window
is not an error, it is a validated rule quietly meaning something else.

**A settled price of 0.00 is a result, not a missing value.** The first version
of `score_trade` rejected any value `<= 0`, which discarded **every losing
trade** — so every wallet's record consisted only of its wins and the ranking
was meaningless. Caught by a test asserting that selling above a zero
settlement is a win. A mark of zero *is* still rejected, because an absent book
is not a valuation.

**Settlements are swept for markets outside the tracked universe.** Wallet skill
is scored against settled outcomes, and the wallets worth ranking trade far
beyond the 25 markets being watched. The first live run observed 1,466 wallets
across 91 markets and knew the outcome of none of them, so nothing was
scoreable and the ranking was permanently empty. A periodic batched Gamma lookup
fixes it.

**Disabling a prop-firm cap means putting it out of reach, not zeroing it.** The
bridge's backtester caps the trading day when `daily_pnl >= daily_profit_cap`,
so a cap of `0` is satisfied on the first bar and blocks every entry. Setting it
to zero to "disable" it produced 399 strategies with zero trades each. Three
override keys were also misspelled, which does not error — it silently leaves
the futures default in force, including a 1.5% account-drawdown halt that stops
a prediction-market backtest before it opens a position. A test now pins the
override keys against what `PropConstraints.from_config` actually reads.

**An active anomaly is recorded once but stays visible.** A detection remains
true for as long as its evidence is inside the recent window, so the same
cluster is re-found every cycle. The engine must keep seeing it — it is still
live evidence — but re-recording it would write one event 180 times an hour and
make "how often does this fire?" unanswerable from the table that exists to
answer it.

**Each token is researched as its own instrument.** The bridge's backtester
reads consecutive rows as consecutive bars. Stacking every token into one file
would put a false price jump at each token boundary, and positions carried
across one would be stopped out against an unrelated outcome — fabricated trades
scored as real. One directory per token, then a cross-token confirmation step,
which is also the cheapest real defence against curve-fitting available.

**A stale rule dilutes rather than votes.** A rule naming a column this build no
longer produces is skipped from the numerator but kept in the denominator.
Dropping it from both would let a stale strategy file silently concentrate
conviction into whichever rules still parse.

**`HOLD` and `DO_NOTHING` are recorded.** A journal of only the trades taken
cannot answer whether passing was correct — half of what a feedback loop is for.
A cycle where nothing qualified emits an explicit record with the counts behind
it, so "every quote was stale" and "everything scored just under the bar" are
distinguishable.

---

## 5. Phase status

| Phase | Status |
|---|---|
| **1 — Data adapter (read-only)** | **Done and demonstrated.** Live Polymarket data flowing: 25 markets / 50 tokens resolved from filters, streamed books verified against REST for the same tokens (no crossed and no zero-spread books), wallet feeds primed and polled, all visible in logs and the journal. |
| **2 — Execution + reconciliation** | **Done in dry-run**, including depth-aware simulated fills, sizing validation, the in-flight guard, classified retry, and the reconciliation loop (clean, missing, unknown, drift, resting orders, balance drop). Restart-with-open-positions and cold-start flatten both verified. **Not yet exercised against the live exchange** — that is the next gate and needs tiny sizes and supervision. |
| **3 — Brain features** | **Done, with the real bridge.** Exit management with an override threshold that adapts to what has paid; the doubling rule (trigger → flatten → advance → re-base observed end to end); journal + report; the learning loop closed. |
| **4 — Broad ingestion, ranking, anomalies** | **Done and demonstrated live.** 1,936 wallets observed with no configured list, 129 ranked from measured results, all six detectors firing, every detection persisted with its evidence. |
| **5 — Strategy discovery through the Quant Bridge** | **Pipeline proven end to end** — export → 412 engineered features → 399 candidates → validation → ranking → cross-token confirmation (18 rules kept). Run so far on a **seeded synthetic series**; a real one needs hours of live capture. |

### Not yet done

- **The first real research run.** Discovery needs ~200 captured rows per token
  at one row per minute, so roughly 3–4 hours of continuous running. The series
  is captured live and cannot be backfilled — which is why capture is on from
  run one and why "run it for a day" is the top of the next-steps list.
- **Settlement-based wallet scoring.** Ranking currently scores most wallets
  against live marks, because the settlement sweep has only just begun and few
  observed markets have resolved yet. It becomes settlement-based on its own as
  markets settle; nothing needs changing for that.
- Live-mode execution has never touched the real exchange. Everything on that
  path is written and unit-tested, but "tested" and "traded" are different words.
- The multi-hour continuous dry-run session (section 9.4) has been run only in
  short bounded sessions so far — the longest to date is 70 cycles.
- Fees are recorded as 0.0: Polymarket publishes no per-fill fee on the order
  response today. The field exists and is journalled, so it will populate if that
  changes.
- Order-book depth beyond the touch is only available on streamed books; the
  REST fallback knows the touch but not the size behind it, and says so via
  `source`.
- The bridge's generated LEAN algorithms (`strategies/<ID>/main.py`) target a
  futures contract. They are produced by the research run and are useful for
  inspecting a rule in QuantConnect, but they are **not** the live path here —
  execution goes through the Polymarket adapter.
