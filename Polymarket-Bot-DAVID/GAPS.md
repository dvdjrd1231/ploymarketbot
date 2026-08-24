# Gap analysis — Polymarket Bot handover

Sorted by what unblocks the most work per unit of effort, not by severity in
the abstract. Every claim is measured; the command that reproduces it is given.

Measured 2026-08-24 against `Polymarket-Bot-DATA/state/intel.sqlite3` (2.4 GB)
and `journal.sqlite3`.

---

## The one-paragraph summary

The data pipeline is excellent and has collected a genuinely valuable dataset:
878,650 wallet trades across 70,571 wallets and 112 days. The research pipeline
is rigorous but is validating against 1.6% of it. The trading path has never
fired — all 40,820 decisions ever journalled are `DO_NOTHING`. Nothing here is
broken; the system is correctly refusing to act on evidence it knows is too
thin. The gaps below are, in order, what makes the evidence thick enough.

---

## TIER 1 — Blocking. Cheap to fix, unlocks everything downstream.

### 1. `resolutions.settled_ts` is never populated

```sql
SELECT COUNT(*) FROM resolutions WHERE settled_ts = 0;   -- 8116 of 8116
```

The column exists and is written as `0.0` in every row. The moment an outcome
became public is recorded nowhere in the database.

**Why it blocks:** a wallet's track record can only be computed point-in-time if
you know when each of its trades resolved. Without it, every "follow this wallet
while it is running hot" hypothesis is untestable — in `walletlab` this silently
collapses the search grid from 1,728 transformations per wallet to ~144.

**Fix:** write the resolution timestamp at ingest. One column. Highest
value-per-hour item in the project by a wide margin.

**Interim:** `walletlab` falls back to `resolutions.ts` (observation time),
which is later than the trade in 100% of joined rows — safe, but its range spans
only 7.4 days so it is weak.

---

### 2. The research pipeline validates against 1.6% of the available evidence

| substrate | rows | markets | span |
|---|---|---|---|
| what `pqb/research.py` uses (exports / `research_rows`) | 78,219 | 123 | **3.8 days** |
| `wallet_trades ⋈ resolutions`, settled 0/1 | **116,923** | **2,418** | **112.3 days** |

Scoring a trade against its resolution needs no order book: buy at `p`, hold,
payoff is exactly `resolution − p` where `resolution ∈ {0,1}`.

**Why it blocks:** this is the direct cause of the three symptoms in
`HANDOVER.md` §6 — `RULE_NEVER_FIRED` (97 rules), "independent OOS markets per
candidate averages well below 1", and 47 constant feature columns. One cause,
three symptoms.

**Fix:** implemented in `wallet-strategy-lab/`. Reproduce with
`python -m walletlab inventory`.

---

### 3. Resolution join coverage: 8,116 recorded, 2,418 usable

Only 30% of recorded resolutions join to a copyable trade. Closing that gap is
the largest remaining lever on statistical power once #1 and #2 are done.

```
python -m walletlab inventory        # settled_tokens vs resolutions
```

---

## TIER 2 — Correctness. Silent failure modes that produce confident wrong answers.

### 4. The favourite–longshot bias is uncontrolled

Calibrating all 116,923 settled trades against their outcomes:

| price band | n | mean price | actual win rate | gap |
|---|---|---|---|---|
| 0.30–0.40 | 9,994 | 0.345 | 0.257 | **−0.088** |
| 0.60–0.70 | 11,200 | 0.650 | 0.742 | **+0.092** |
| 0.70–0.80 | 13,389 | 0.748 | 0.840 | **+0.092** |

**Why it is dangerous:** any rule of the form "buy between 0.6 and 0.9" earns
~+20% expectancy while copying nobody in particular. A search over price-band
transformations × 122 wallets will find this once per wallet and report ~122
"independent validated strategies" — all the same market-wide effect. It would
look like the best result the project has ever produced.

**Fix:** `wallet-strategy-lab/walletlab/baseline.py` gates on **wallet alpha**
(strategy expectancy minus the same price band and time window across all
*other* wallets). Zero alpha ⇒ status `NO_WALLET_ALPHA`, cannot promote,
regardless of profit. The equivalent control does not exist in `pqb`.

---

### 5. Point-in-time wallet ranking is not enforced anywhere but `walletlab`

Directive §43. Ranking wallets by performance that includes trades unresolved at
the signal instant is look-ahead, and on this venue it is a large effect because
payoff happens at resolution, not at trade.

`walletlab/state.py` enforces it mechanically (outcomes enter a heap and are
folded in only as the clock passes `settled_ts`) and asserts it by test. The
`pqb` ranking path in `analytics/ranking.py` has no equivalent guarantee.

Blocked behind gap #1 for a full fix.

---

### 6. The bundled `.venv` points at a directory that no longer exists

```
polymarket-quant-bridge/.venv  ->  D:\tasks\olaf_David\Polymarket-Bot-DAVID\...
```

Running the test suite through it fails at collection. The same suite passes
cleanly on a fresh interpreter:

```
python -m pytest tests/ -q        # 1142 passed in 113s
```

**Fix:** never ship a venv (467 MB, and not relocatable). Excluded from the
handover zip; `INSTALL.md` rebuilds it. Note the suite is now **1142 tests**,
up from the 944 quoted in `HANDOVER.md`.

---

### 7. Two of four projects have zero test coverage

| project | tests |
|---|---|
| `polymarket-quant-bridge` | 1142 ✅ |
| `wallet-strategy-lab` | 15 ✅ |
| `ploymarketbot` | **0** |
| `qc_lean_bridge` | **0** |

`ploymarketbot` is the component that holds the private key and signs orders.
It is the least tested and the highest consequence.

---

## TIER 3 — Capability. Real limits, needs new data collection, not new code.

### 8. No order-book history ⇒ exits, depth and partial fills are unanswerable

Every backtest in `walletlab` holds to resolution. Early exits need a continuous
price path this dataset does not have; ~half the behavioural sequences the brief
asks for (add / exit / reversal ladders) are out of reach until live order-book
snapshots are captured. This is also why 47 feature columns are constant.

**Interim:** `PriceTape` reconstructs a coarse price series from the aggregate
tape — if anyone printed at time T, that is a price you could have paid. Enough
for delay-decay analysis, not for depth.

### 9. SELL-side and non-TRADE events are excluded

149,080 SELLs and 165,350 rows with no `side` are outside the copyable universe.
Position-state reconstruction across `REDEEM` / `MERGE` / `SPLIT` is designed
for in `walletlab/state.py` but not built.

### 10. The AI researcher (directive §11–§13) is not built

The registry, spec hashing, status ladder and compact summaries it needs are in
place. The hypothesis-proposing loop is not — deliberately sequenced last, because
an AI proposing hypotheses against an uncontrolled substrate (gap #4) would have
industrialised the favourite–longshot artefact at scale.

### 11. There is no Rust anywhere in the project

No `Cargo.toml`, no `.rs`, no PyO3/maturin. Every apparent hit is a substring of
*robust* / *trust*. Flagged because project documentation refers to an "existing
Rust architecture" that does not exist.

**It is also not currently needed.** Measured: 20,748 hypotheses across 12
wallets in 40 s, ~180 MB peak. All 122 eligible wallets extrapolates to ~7
minutes inside 16 GB. Revisit when the tape exceeds ~10M rows or the grid
exceeds ~100k per wallet — not before. Making a data-starved search faster
raises the false-discovery rate; it does not find edge.

---

## TIER 4 — Observations worth knowing, no action required yet.

### 12. The trading path has never executed

```sql
SELECT action, COUNT(*) FROM decisions GROUP BY action;
-- DO_NOTHING | 40820
SELECT COUNT(*) FROM executions;   -- 0
SELECT COUNT(*) FROM lifecycles;   -- 0
```

40,820 cycles over 3.8 days, every one a no-op. This is *correct* — no strategy
has validated, so the gate never opens — but it means the order-placement,
reconciliation and lifecycle paths are entirely unexercised in production. They
are covered by unit tests only. Treat the first live trade as a first run, not a
resumption.

### 13. Research components with no demonstrated contribution

2,392 attempts across 234 strategies and 2,376 validation rows have produced 0
tradable strategies. `pqb/motif.py` (1,230 lines), `analytics/sequences.py`,
`analytics/cascade.py`, `analytics/anomalies.py` and `longshot.py` are not
currently gating any decision. Directive §27 says remove them from the primary
pipeline until each can demonstrate it contributes information. Not deleted —
moved out of the loop.

### 14. Three overlapping feature paths

`pqb/features.py`, `analytics/features.py` and `feature_domain.py` produce 988
engineered columns from ~121 on disk, 47 of them constant. `walletlab` uses 17
causal features, all populated.

---

## Recommended order

1. Populate `resolutions.settled_ts` — one column (gap #1)
2. Run `walletlab discover-strategies` across all 122 wallets, read the status
   histogram (gap #2 verification)
3. Backfill resolutions to close the 8,116 → 2,418 gap (#3)
4. Port the wallet-alpha control into `pqb`, or retire the `pqb` discovery path
   in favour of `walletlab` (#4)
5. Add point-in-time wallet *selection* so the leaderboard is a true historical
   simulation (#5)
6. Test coverage for `ploymarketbot` before any live key is loaded (#7)
7. Capture live order-book snapshots (#8)
8. Only then: the AI hypothesis loop (#10)
9. Rust: only when a trigger in #11 actually fires
