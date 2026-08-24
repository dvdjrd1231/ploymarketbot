# Integration plan

Required by section 1.3, **for review before further implementation**. It states
what has been built, the decisions taken and why, and the four open questions
whose answers change the work.

---

## 1. The shape of the integration

```
Polymarket API
   │  CLOB WS (books) · CLOB REST (orders, balance) · Gamma (metadata) · Data API (trades, positions)
   ▼
1. BROAD INGESTION ─────────────────► every wallet observed  data_adapter.observe_trades()
   │                                   identity, ts, market, side, price, size
   ▼
2. NORMALISATION ───────────────────► WalletTrade            models.py
   ▼
3. FEATURE GENERATION ──────────────► wallet + market + token features
   │                                   analytics/features.py · features.py
   ▼
5. RANKING / ANOMALY DETECTION ─────► WalletIntel, AnomalySignal
   │                                   analytics/ranking.py · anomalies.py
   ▼
   BridgeContext (markets, positions, account, wallet intel, anomalies, perf)
   ▼
4. QUANT BRIDGE ANALYSIS ◄──────────── THE SEAM              pqb/bridge/ports.py
   │  discovered rules, evaluated live   bridge/lean_engine.py
   ▼
6. [BUY · SELL · HOLD · REDUCE · EXIT · DO NOTHING]  + rationale
   ▼
7. EXECUTION ───────────────────────► orders                 adapters/execution_adapter.py
   ▼
   Reconciler ──────────────────────► halt on divergence     pqb/reconcile.py
   ▼
8. OUTCOME FEEDBACK ────────────────► journal → analytics/feedback.py → back to 4
```

The numbering is the ordering the brief's 7:54 PM message specifies, and each
step names the module that performs it.

**Offline, on its own timescale:** captured features → `pqb/research.py` → the
bridge's `ResearchEngine` (discovery, walk-forward, Monte Carlo, ranking) →
cross-token confirmation → `state/strategies.json`, which step 4 loads.

## 2. How the Quant Bridge plugs in

The contract is one method:

```python
class DecisionEngine(Protocol):
    def evaluate(self, context: BridgeContext) -> list[Decision]: ...
```

with three rules: return one decision per open position (exit management is
mandatory), emit `HOLD`/`DO_NOTHING` explicitly rather than omitting, and do no
I/O — the context is complete.

Switching engines is one config line:

```yaml
engine:
  implementation: "propfirm_bridge.polymarket:LeanDecisionEngine"
```

**Shape A was taken, and it is the cheapest of the three.** The bridge turned
out to be Python, so it is imported and executed rather than messaged:

| Shape | What changes | Status |
|---|---|---|
| **A. Bridge as a library** — imported; `evaluate()` called each cycle | Nothing outside `ports.py` | ✅ **Taken.** `pqb/quant.py` + `bridge/lean_engine.py`. |
| **B. Bridge drives the clock** — Polymarket becomes a LEAN custom data source | `runner.py`'s loop replaced by LEAN's scheduler | Not needed. The bridge's research layer is offline by design, so it does not want the clock. |
| **C. Out-of-process** | A transport plus serialisation of `BridgeContext`/`Decision` (both already `to_dict()`-able) | Not needed — same host, same language. |

The one adaptation shape A required: the bridge's `ResearchEngine` is offline
(history in, rule objects out), not a per-cycle call. So the work is split
across the two timescales it actually has — capture continuously, discover
occasionally, evaluate every cycle. See `ARCHITECTURE.md` §1.

## 3. Phase status against the brief's acceptance checklists

### Phase 1 — read-only data adapter

| Acceptance item | Status |
|---|---|
| Pulls all listed fields for live markets | ✅ prices, bid/ask, spread, depth, liquidity, volume (24h + cumulative), recent trades, expiration, time remaining, status |
| Data lands correctly in the bridge's data model | ✅ into `BridgeContext`, then into the flat feature vector the bridge's `DataPipeline` consumes |
| **Broad ingestion, not a configured shortlist** | ✅ verified live: **1,936 wallets observed with an empty `wallets:` list**, identity/timestamp/market/side/price/size preserved on every observation |
| **Dynamic ranking, wallet identity maintained** | ✅ verified live: **129 wallets ranked** from measured results, addresses preserved, shrinkage and staleness applied. Currently scored against live marks; settlement-based scoring fills in as markets resolve. |
| **Anomaly detection with demonstrable evidence** | ✅ all six kinds firing on live data; each persisted with its numbers, readable via `pqb.cli anomalies` |
| Runs stably across an extended session | 🟡 clean over ~70-cycle sessions, 0 errors. The multi-hour run has not been done. |
| Nothing writes to the exchange | ✅ the adapter has no write path; the client is passed read-only |

### Phase 2 — execution & reconciliation

| Acceptance item | Status |
|---|---|
| Dry-run produces correct simulated orders/fills | ✅ depth-capped fills; buy at ask, sell at bid, spread actually costs money |
| Reconciliation catches **injected** mismatches and halts | ✅ tested by injecting a phantom position — halted within one loop |
| Kill switch halts within one loop | ✅ tested; also cancels resting orders |
| One operator-initiated minimal-size live order places, fills, reconciles | ❌ **NOT DONE — the gate to automated live trading** |

### Phase 3 — decision & exit brain

| Acceptance item | Status |
|---|---|
| Decisions explainable from logged inputs | ✅ every decision carries an inputs snapshot, component scores, which rules fired, the feedback evidence, and a rationale |
| Exit logic correct across scenarios in dry-run | ✅ stop, trailing (armed), take-profit, reduce-once, time-decay, wallet follow/override, resolution, flatten |
| Doubling rule triggers and advances correctly | ✅ observed end to end: trigger → flatten → 0.19 → 0.29 → re-base |
| Journal captures the full lifecycle | ✅ integrity check passes |
| **The Quant Bridge is the decision layer** | ✅ `LeanDecisionEngine`; discovery run end to end through the bridge (160 ranked candidates → 18 rules surviving cross-token confirmation) |
| **No hard-coded strategy** | ✅ rules come from the bridge's search; the hand-written baseline is now only the fallback when nothing has been discovered yet, and says so in every rationale |
| **Learning loop closed** | ✅ `analytics/feedback.py` — journal outcomes tilt future scores and move the wallet-override threshold |

### The 7:54 PM revision — broad ingestion and dynamic ranking

| Requirement | Status |
|---|---|
| Don't hard-code "top wallets only" | ✅ no shortlist anywhere; `wallets:` is empty by default |
| Broadest practical wallet/market dataset | ✅ two sweeps: deep per tracked market, shallow exchange-wide |
| Preserve identity, timestamps, trades, positions, sizes, market context | ✅ `WalletTrade`, all fields, de-duplicated on a natural key |
| Dynamically rank wallets itself, keeping identity | ✅ derived every pass from settled/marked results |
| Top-N cohort important but **not an absolute restriction** | ✅ a label on a wallet; every observed wallet still reaches the engine |
| Discover a signal from *outside* the cohort | ✅ `lead_lag` exists precisely for this, and cohort membership is a feature the discovery layer can condition on |
| Keep raw data available, don't force it all into the model | ✅ ~48 raw columns → ~412 engineered by the bridge → its feature selection keeps the top 40 |
| Let the analytical layer decide what has predictive value | ✅ one feature column per anomaly kind, so "which kinds predict anything" is measured, not asserted |

**Phase 3 is now complete for the real brain**, with one honest caveat: the
discovery run demonstrated so far used a **seeded synthetic series**, because
the live capture needed for a real one is measured in hours. The pipeline is
proven; the rules it has produced are not yet meaningful.

## 4. Decisions taken, and why

**Halt beats adopt.** The previous brief said reconciliation should adopt
exchange truth and continue; this one says halt and alert. This build now does
both, in that order: record the divergence, adopt the exchange's figures so the
stored state is not stale, then **stop trading** until an operator clears it.
Halt does not flatten — closing out is still trading, and a halt means we do not
currently know what we hold.

**A settle grace period, or halt-on-mismatch is unusable.** A fill reaches the
CLOB before the Data API reports the position. Without a grace window
(`settle_grace_seconds`, default 120) every single entry would trip the halt
seconds after it filled. This is the difference between a safety feature and an
outage.

**Two files, one gate.** `KILL` (operator) and `HALT` (automatic) both stop
trading, need no redeploy, work from any shell, survive an SSH drop, and leave
an audit trail. `HALT` persists across restarts on purpose: a divergence nobody
has looked at must not be cleared by bouncing the process, and `resume` refuses
to clear it without `--force` after printing what diverged.

**Decisions are journalled while halted.** The engine keeps evaluating and
recording; nothing reaches the exchange. A halted bridge that also goes blind
tells the operator nothing about what it wanted to do.

**Reconcile before deciding, not after.** A divergence found after execution has
already been traded on.

## 5. Open questions — these change the work

1. ~~Where is the Prop Firm Quant Bridge, and which shape?~~ **Answered.** It is
   Python, it is present, and shape A is implemented.
2. ~~Target wallets — which addresses?~~ **No longer a question.** Ingestion is
   broad and ranking is derived, so the system needs no addresses to start.
   Pinning one in `wallets:` is optional and only seeds a label and an influence
   floor.
3. **Admin UI: CLI only, reuse `ploymarketbot`'s React dashboard, or a Filament
   page in `paymentor`?** Recommendation and costs in `REUSE-MAP.md`. A scope
   decision, not a technical one. `pqb.cli wallets` / `anomalies` / `report`
   currently cover the operator surface.
4. **`py-clob-client` v1 or v2?** A v2 client now exists. This project reaches
   the client through `ploymarketbot`, so migrating changes *that* repo. Flagged
   rather than actioned.
5. **How long to capture before the first real research run?** Discovery needs
   `min_rows` (default 200) captured rows per token, at one row per
   `capture_seconds` (default 60) — so roughly 3–4 hours of continuous running
   before the first meaningful `pqb.cli research`. The series is captured live
   and **cannot be backfilled**, which is why capture is on by default from run
   one.

## 6. Proposed next steps, in order

1. **Run continuously for a day.** This is now the gating item for everything
   else: it is what produces the captured feature series discovery needs, and
   what lets settlements accumulate so wallet ranking is scored against settled
   outcomes rather than live marks.
2. `python -m pqb.cli research`, then review the surviving rules and
   `pqb.cli wallets` / `pqb.cli anomalies`.
3. Multi-hour dry-run with the discovered rules loaded; review `pqb.cli report`.
4. **Then, and only then**: one operator-initiated, minimum-size live order,
   supervised, with `pqb.cli kill` ready in a second terminal. Confirm it
   places, fills and reconciles before any automated live trading is enabled.
