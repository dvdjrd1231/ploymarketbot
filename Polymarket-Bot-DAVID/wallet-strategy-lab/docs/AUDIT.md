# Architecture audit — Polymarket wallet strategy discovery

Directive §26 / §49. Written after inspecting the whole repository and querying
the live 2.4 GB `intel.sqlite3`. Every number below is measured, and every
measurement is reproducible with `python -m walletlab inventory`.

---

## 0. Two premises in the directive that the code does not support

Stated first because they change what the right build is.

**"the existing Rust architecture" does not exist.** There is no `Cargo.toml`,
no `.rs` file, and no PyO3/maturin dependency anywhere in the repository. Every
apparent hit for "rust" is a substring of *robust* or *trust*. The system is
45,386 lines of Python in `pqb/`, plus ~8,900 in `qc_lean_bridge/core/`.

**The system is not compute-bound, so porting it to Rust would not help.**
This is the load-bearing finding of the audit. The strategy search does not run
against the 878,650-row wallet tape — it runs against the exported feature
series, and that is:

```
  123 markets,  78,219 rows total,  median 280 rows per market
```

78k rows. A full sweep over that finishes in seconds in Python. The directive's
§21–§24 (Rust core, parallel sweeps, batch FFI, 16 GB discipline) are all
solutions to a bottleneck this system does not currently have. Rust becomes
worth doing when the substrate is 100× larger; today it would be a large
rewrite that makes the *actual* problem worse, because §14 and §34 are already
the binding constraint. Running more experiments per second against a fixed,
thin dataset raises the false-discovery rate — it does not find edge.

That is why this delivery is a Python engine on a much larger substrate, not a
Rust port of the existing one. §50 explicitly permits this reading, and §49
mandates audit-then-benchmark-then-minimum-viable, in that order.

---

## 1. The finding that actually unblocks the research

The `HANDOVER.md` for build 70 names the top bottleneck itself:

> **Pool breadth (81 markets).** Independent OOS markets per candidate now
> averages well below 1. More settled markets is the single biggest lever on
> how fast anything can validate.

That is correct, and the fix is already sitting in the same database.

| | rows | markets | span |
|---|---|---|---|
| what the search validates against today (`research_rows` / exports) | 78,219 | 123 | **3.8 days** |
| `wallet_trades` ⋈ `resolutions`, settled 0/1, BUY, in price band | **116,923** | **2,418 tokens** | **112.3 days** |

Scoring a trade against the market's *resolution* needs no order book and no
captured price series: buy one share of a token at `p`, hold to settlement, and
the payoff is exactly `resolution − p` where `resolution ∈ {0, 1}`. That is not
a model, it is arithmetic.

Switching the evaluation substrate from captured feature series to settled
outcomes multiplies the evaluable market count by ~20× and the time span by
~30×, using data already on disk. It also explains the three symptoms in the
handover — `RULE_NEVER_FIRED`, sub-1 OOS markets per candidate, and 47 constant
feature columns — as one symptom of one cause.

**Wallet evidence available on the new substrate:**

| wallets with ≥ N settled copyable trades | N=50 | N=100 | N=200 | N=500 |
|---|---|---|---|---|
| | 268 | 122 | 54 | 12 |

---

## 2. The trap this substrate contains, and the control for it

Calibrating all 116,923 settled trades against their outcomes:

| price band | n | mean price | actual win rate | gap |
|---|---|---|---|---|
| 0.10–0.20 | 17,477 | 0.148 | 0.080 | **−0.069** |
| 0.30–0.40 | 9,994 | 0.345 | 0.257 | **−0.088** |
| 0.60–0.70 | 11,200 | 0.650 | 0.742 | **+0.092** |
| 0.70–0.80 | 13,389 | 0.748 | 0.840 | **+0.092** |
| 0.80–0.90 | 20,532 | 0.847 | 0.900 | **+0.053** |

A large, systematic favourite–longshot bias. Consequence: **any** rule of the
form "buy between 0.6 and 0.9" earns strongly positive expectancy on this
dataset regardless of whose trade it copies.

A naive search over 1,728 price-band transformations × 122 wallets will find
this bias once per wallet, report ~122 "independent validated strategies", and
every one will be the same market-wide effect wearing a different wallet's
name. It would look like the best result the project has ever produced.

The engine therefore gates on **wallet alpha**, not P&L:

```
wallet_alpha = expectancy(strategy on this wallet)
             − expectancy(same price band, same time window, ALL other wallets)
```

A strategy with `alpha ≤ 0` is recorded as `NO_WALLET_ALPHA` and can never be
promoted, however profitable and however robust. If the effect is real but has
no alpha, it is a market-structure trade — trade it directly, do not follow
anyone to get it.

This control is the single most important thing in the delivery. It is
implemented in [`walletlab/baseline.py`](../walletlab/baseline.py).

---

## 3. Verdicts

### KEEP — reliable infrastructure, reused as-is

| Component | Why |
|---|---|
| `ploymarketbot/backend/services/price_service.py` | CLOB WebSocket, delta application, crossed-book repair. Hardest correct thing in the repo. |
| `services/gamma.py`, `services/data_api.py` | Resolution detection and the same-second de-duplication watermark encode real venue behaviour that is not in the docs. |
| `services/trading.py` | Signing off the event loop; the threading discipline is what keeps the loop responsive. |
| `pqb/analytics/store.py` + the ingestion path | It produced the 878k-trade, 8,116-resolution dataset this delivery runs on. It is the most valuable asset in the project. |
| `pqb/journal.py`, SQLite WAL patterns | Correct and cheap. |
| `qc_lean_bridge` boundary | §41: keep it as the execution/simulation consumer of validated specs. Do not make the research engine become LEAN. |
| The status-ladder discipline in `pqb/library.py` | The rule that research may optimise *what to investigate* but never *what counts as success* is the best idea in the codebase. Reproduced verbatim in the new registry. |

### REWRITE — replaced by `wallet-strategy-lab/`

| Component | Why |
|---|---|
| The export → discover → validate path in `pqb/research.py` (3,377 lines) | Validates against 123 markets / 3.8 days when 2,418 markets / 112 days are available. The bottleneck is structural, not tunable. |
| `pqb/wallet_state_research/` (~6,000 lines, 21 modules) | Closest existing work to the directive and genuinely rigorous, but built around one frozen hypothesis (RN1) on one wallet-condition pair rather than a search over wallets. Its *discipline* is preserved; its scope is replaced. |
| `pqb/features.py` + `analytics/features.py` + `feature_domain.py` | Three overlapping feature paths, 988 engineered columns from ~121 on disk, 47 of them constant. Replaced by 17 causal features that are all populated. |

### DELETE from the primary pipeline — complexity with no demonstrated value

Not deleted from disk; removed from the research path until each can show it
contributes information (§27).

| Component | Evidence |
|---|---|
| `pqb/motif.py` (1,230), `analytics/sequences.py`, `analytics/cascade.py` | 2,392 attempts and 2,376 validation rows across 234 strategies have produced **0 tradable strategies**. The handover states this is the correct answer, and it is — but that also means these detectors have not earned their place in the loop. |
| `pqb/adversarial.py` battery at 60% coverage | Good idea, wrong altitude: it attacks candidates that a population control would have killed for free, and it cannot see the favourite–longshot artefact at all. |
| `analytics/anomalies.py` (six detectors), `longshot.py`, `sharp_moves.py` | §28 — anomaly count is not the objective. None is currently gating a decision. |

### ACCELERATE — but not yet

Nothing needs Rust today. The order to revisit it, with the trigger:

1. `stream_features` — single causal pass over the tape. Trigger: tape > 10M rows.
2. `backtest.run` over the transformation grid. Trigger: grid > 100k per wallet.
3. Bootstrap/placebo resampling. Trigger: draws > 10⁵ per candidate.

Current full-sweep cost, measured: **20,748 hypotheses across 12 wallets in 40
seconds**, ~180 MB peak. Extrapolated to all 122 eligible wallets: ~211,000
hypotheses in ~7 minutes, comfortably inside 16 GB. Port when that becomes the
constraint, not before.

### RESEARCH — worth expanding, in this order

1. **Delay decay (§30).** The tape is its own price series: if anyone printed a
   price at T, that is a price you could have paid. `PriceTape` already does
   this. Sweeping delay 0/60/300/1800s measures how fast each wallet's edge
   decays, which is what distinguishes an informed wallet from a reactive one.
2. **Wallet archetypes (§31)** conditioned on the alpha measure, not on P&L.
3. **Cross-wallet transformation transfer (§9)** via `params_only_hash`.
4. **Backfill more resolutions.** 8,116 resolutions exist but only 2,418 join
   to a copyable trade. Closing that gap is now the biggest single lever, the
   same conclusion the handover reached, on a substrate 20× larger.

---

## 4. What was built, and what it proves

`wallet-strategy-lab/` — 10 modules, ~1,400 lines, 14 tests, no dependencies
beyond the standard library.

Measured end-to-end on the real database:

```
12 wallets · 15,495 causal observations · 20,748 hypotheses · 40 s
  BH threshold at FDR=0.10 ...... p <= 0.000274
  FAILED ......................... 16
  NOT_SIGNIFICANT ................ 16
  OVERFIT ......................... 3
  VALIDATED ....................... 2
```

The two survivors carry positive wallet alpha against a population control of
~24,000 and ~9,500 matched trades:

| wallet | conditions | OOS n | expectancy | population | **alpha** |
|---|---|---|---|---|---|
| `0x629da2…56c` | price 0.50–0.98 | 84 | +0.2082 | +0.0123 | **+0.1959** |
| `0x84cfff…f63` | price 0.70–0.98, delay 300 s | 78 | +0.2003 | +0.0578 | **+0.1425** |

**These are candidates, not conclusions.** Both rest on ~80 out-of-sample
trades over a ~4-week window, and both sit in the price region where the
market-wide bias is strongest — the control is what separates them from it, and
a control is an estimate. Neither should trade real capital until it has
survived a further independent window. The registry status they hold is
`VALIDATED`, which under §42 permits paper, not live.

What the run does establish is that the harness is sound: it tests hypotheses
it counts, applies a false-discovery threshold derived from that count, kills
market-wide effects with a population control, and reports "no edge" without
embarrassment when that is the answer.

---

## 5. What this engine cannot answer yet

Stated plainly rather than buried (§38).

1. **Exits.** Every position is held to resolution. Early exits need a
   continuous price path this dataset does not reliably have. Roughly half the
   directive's §3 sequence structures (add/exit/reversal ladders) are therefore
   out of reach until order-book history is captured live.
2. **Depth and partial fills.** The tape gives a *price*, not a size you could
   have taken. Unpriced copies are recorded `UNFILLED` and earn nothing, which
   is conservative but coarse.
3. **SELL-side and non-TRADE events.** 149,080 SELLs and 165,350 rows with no
   side are excluded from the copyable universe. Position-state reconstruction
   across REDEEM/MERGE/SPLIT is designed for but not built.
4. **Point-in-time wallet *discovery*.** Wallet *statistics* are point-in-time
   and enforced by test (§43); the *selection* of which 122 wallets to study
   uses the full window, so the leaderboard is not yet a historical simulation
   of what you would have chosen.

5. **There is no true settlement time in the database, and it matters.**
   `resolutions.settled_ts` is `0.0` in all 8,116 rows — the ingester declares
   the column but never writes it. The moment an outcome became public is
   therefore not recorded anywhere.

   The engine falls back to `resolutions.ts` (when the system *observed* the
   resolution), which is later than the trade in 100% of joined rows, so it can
   only delay an outcome entering wallet state, never advance it — safe, but
   weak: its range spans 7.4 days, so trades older than that all appear to
   settle at once.

   The practical consequence is that **the wallet track-record features
   (`min_settled_n`, `min_roll_win_rate`, `max_consec_losses`, `min_edge_t`)
   cannot currently discriminate**, which collapses the effective search space
   from 1,728 transformations per wallet to roughly 144 — price band × relative
   size × delay × first-entry-only. The two candidates in §4 use only price
   band and delay, so they are unaffected; but every hypothesis of the form
   "follow this wallet only while it is running hot" is untestable until
   settlement time is captured.

   **This is the highest-value backfill in the project**, ahead of more
   resolutions. It is one column.
5. **The AI researcher (§11–§13).** The registry, hashing, status ladder and
   compact summaries it needs are built. The hypothesis-proposing loop is not.
   Deliberate ordering: an AI proposing hypotheses against an uncontrolled
   substrate would have industrialised the favourite–longshot artefact.

---

## 6. Recommended order from here

1. **Populate `resolutions.settled_ts`.** One column, and it unlocks two thirds
   of the transformation grid plus all point-in-time wallet ranking. Cheapest
   high-value fix in the project.
2. Run `discover-strategies` across all 122 eligible wallets (~7 min) and read
   the status histogram before anything else.
3. Backfill resolutions to close the 8,116 → 2,418 join gap.
3. Add point-in-time wallet selection so the leaderboard becomes a historical
   simulation (§43).
4. Capture live order-book snapshots so exits and depth become answerable.
5. Only then: the AI hypothesis loop (§12), against a controlled substrate.
6. Rust, when and only when a trigger in ACCELERATE actually fires.
