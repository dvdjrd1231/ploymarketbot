# Map of the existing system, and where the opportunities go

Steps 1 and 4–15 of the brief. Every number here was measured on the client's
own files on 2026-08-24 and the command that reproduces it is given.

---

## 1. What is actually installed

`Polymarket-Bot-DAVID/` holds four separate programs, not one:

| project | lines | tests | role |
|---|---:|---:|---|
| `polymarket-quant-bridge` (`pqb`) | 60,000+ | 1,142 | the quant engine, research layer, LEAN bridge, dashboard |
| `wallet-strategy-lab` (`walletlab`) | 2,157 | 15 | a later, correct wallet-copy discovery engine |
| `ploymarketbot` | ~4,000 | **0** | FastAPI backend; **holds the private key and signs orders** |
| `qc_lean_bridge` | ~4,000 | **0** | QuantConnect/LEAN experiment |

Data lives outside all four, in `Polymarket-Bot-DATA/state/`:

| file | size | contents |
|---|---:|---|
| `intel.sqlite3` | 2.6 GB | 878,650 wallet trades, 70,571 wallets, 8,116 resolutions |
| `journal.sqlite3` | 20 MB | 40,820 decisions, **0 executions**, **0 lifecycles** |
| `library.sqlite3` | — | 234 strategies, 2,376 validation rows |

---

## 2. Strategy A: the decision path, in order

```
runner.py
  └─ pqb/bridge/lean_engine.py  LeanDecisionEngine.evaluate
       ├─ _entry_block_reason(context, open_count)      <-- LEARNING MODE
       │     if cfg.entry.require_strategies and not self.trading_strategies:
       │         return "Learning mode: no validated strategies yet ..."
       ├─ (only if unblocked) baseline_engine scoring
       ├─ _entry_filter -> pqb/decision/high_confidence.py
       │     1 STATE          ms_state in allowed_states
       │     2 EXHAUSTION     ms_exhaustion < max_exhaustion
       │     3 LIQUIDITY      spread <= max_spread, depth >= 3x stake
       │     4 EV             ev_per_dollar >= min_expected_value
       │     5 CONTRADICTION  count structure arguing against
       │     6 PORTFOLIO      category concentration
       │     7 EMPIRICAL      >= min_setup_sample CLOSED lifecycles
       └─ exits: baseline_engine.py -> "No exit condition met." (HOLD)
```

**The learning-mode gate sits above every other entry gate.**

---

## 3. Where the opportunities actually go

```sql
SELECT action, COUNT(*) FROM decisions GROUP BY 1;
-- DO_NOTHING | 40820

SELECT substr(reason,1,80), COUNT(*) FROM decisions GROUP BY 1;
-- Learning mode: no validated strategies yet - capital is parked ... | 40820

SELECT COUNT(*) FROM executions;   -- 0
SELECT COUNT(*) FROM lifecycles;   -- 0
```

**100% of decisions, one reason, one gate.**

This matters more than it first looks. The messages the brief asks about —

- `"market state 0 is not an entry state"`
- `"depth is under ..."`
- `"High-confidence filter: ..."`
- `"No exit condition met"`

— come from `high_confidence.py` and `baseline_engine.py`, which sit **below**
the learning-mode gate. In production they were **never reached**. Loosening
any of them would have changed nothing and degraded the engine.

This is exactly the outcome the brief's rule 27 anticipates: *first identify
the actual bottleneck.* Diagnosing from log lines would have blamed the wrong
rules.

---

## 4. Why learning mode never opens

It waits for a strategy with status `validated`. There are none:

```sql
SELECT status, COUNT(*) FROM strategies GROUP BY 1 ORDER BY 2 DESC;
-- rejected    | 170
-- validating  |  49
-- new         |  13
-- quarantined |   2
-- validated   |   0
```

So the question becomes: why does the discovery pipeline never validate
anything?

### The measured answer: substrate starvation

| substrate | rows | markets | span |
|---|---:|---:|---:|
| what `pqb/research.py` validates against | 78,219 | 123 | **3.8 days** |
| `wallet_trades ⋈ settled resolutions` | **116,923** | **1,285** | **90.0 days** |

Reproduce: `python -m pqv2 inventory`

The engine is validating against **~1.6%** of the evidence sitting in the same
database file. Its own validation bar requires independent out-of-sample
markets per candidate; over 123 markets that number averages below 1, so
nothing can clear it.

Scoring a hold-to-resolution trade needs no order book — buy at `p`, hold,
payoff is exactly `resolution − p`. That is why the larger substrate is
available and the captured-feature substrate is not.

**One cause, four symptoms:**

| symptom | previously attributed to |
|---|---|
| 0 validated strategies | "the rules have no edge" |
| `RULE_NEVER_FIRED` × 97 | "entry thresholds too tight" |
| OOS markets per candidate < 1 | "need more data collection" |
| 40,820 × `DO_NOTHING` | "filters too strict" |

All four are the same starvation.

---

## 5. The second-order deadlock

Even with learning mode open, `high_confidence.py` gate 7 would bind:

```python
rows = self.journal.query(
    "... FROM lifecycles l JOIN decisions d ... WHERE l.status='CLOSED' ...")
```

`lifecycles` holds **0 rows**. A setup needs closed trades to be trusted, and
cannot obtain closed trades without being trusted.

V2 breaks this **without loosening anything** — see `ledger.Mode`. SHADOW mode
runs the full pipeline with simulated fills and no capital, so operating
evidence accumulates; PAPER requires `VALIDATED`; LIVE additionally requires an
explicit human promotion. Every original live requirement is preserved.

---

## 6. Gate ownership: what V2 inherited and what it declined

Full table: `python -m pqv2 gates`

| V1 gate | classified | why |
|---|---|---|
| `v1.learning_mode` | STRATEGY_A | Strategy A's own discovery ladder gating Strategy A's own engine. Strategy B has an independent ladder and must not inherit it. |
| `v1.market_state_not_entry` | STRATEGY_A | A microstructure state machine is a thesis about *when a move is born*. Strategy B's thesis is *who is trading*. Different question. |
| `v1.depth_under_multiple` | **EXECUTION** | Reclassified. Whether a fill is achievable is true regardless of which strategy asked. V2 applies the equivalent to **both** routes. |
| `v1.no_exit_condition_met` | STRATEGY_A | Strategy A's exit model. Strategy B derives its own per family. |
| `v1.empirical_no_setup_history` | STRATEGY_A | The deadlock above. |

Only `GLOBAL_SAFETY` gates block both routes, and each carries written
evidence. `tests/test_isolation.py` fails the build if one does not.

---

## 7. Components with no demonstrated contribution

2,392 attempts across 234 strategies produced 0 tradable strategies. These are
not gating any decision today:

`pqb/motif.py` (1,230 lines), `analytics/sequences.py`, `analytics/cascade.py`,
`analytics/anomalies.py`, `longshot.py`

V2 does not delete or modify them — rule 3. It simply does not depend on them.
Whether they earn a place back is a measurement, and `pqv2 features` is the
tool for it.

Also measured: `pqb/features.py`, `analytics/features.py` and
`feature_domain.py` produce **988 engineered columns** from ~121 on disk, **47
of them constant** because no historical order book exists.

---

## 8. There is no Rust in the existing project

No `Cargo.toml`, no `.rs`, no PyO3, no maturin. Every apparent hit in the source
is a substring of *robust* or *trust*. Project documentation refers to an
"existing Rust architecture" that does not exist.

See `docs/PERFORMANCE.md` for what V2 did instead, and when Rust becomes worth
building.

---

## 9. Preservation

V2 opens `intel.sqlite3`, `journal.sqlite3` and `library.sqlite3` **read-only**
(`mode=ro`, `PRAGMA query_only=ON`) and writes only under
`polymarket_quant_v2/var/`.

`tests/test_isolation.py::test_v2_never_writes_to_the_v1_installation` asserts
this by AST inspection of every `sqlite3.connect` call in the package.

The original installation is unchanged, recoverable and executable.
