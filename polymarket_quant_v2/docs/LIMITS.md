# What this system cannot answer, and why

Stated plainly rather than buried, because the fastest way to lose money with a
quantitative system is to act on a number whose uncertainty nobody wrote down.

Everything below is a property of the **available data**, not of the code. Each
entry says what would fix it.

---

## 1. `resolutions.settled_ts` is 0 in all 8,116 rows

The moment an outcome became public is recorded nowhere in the database.

**What breaks.** Point-in-time wallet track record. The engine falls back to
`resolutions.ts` (when the system *observed* the resolution), which is later
than the trade in 100% of joined rows — so it is **safe**, it can only delay
information, never advance it. But its range spans ~7.4 days, so trades older
than that all appear to settle at once.

**Measured consequence.** `pit_evidence_share` is **0.00** for every wallet
tested: not one trade had any settled track record behind it at the moment it
was placed. So these search axes are structurally inert:

- `min_settled_n` · `min_roll_win_rate` · `max_consec_losses` · `min_edge_t`

The grid tests 5,184 transformations per wallet of which only ~432 are
distinct, and pays the multiple-testing cost of all 5,184 — making the BH
threshold roughly **12× stricter than the evidence requires**, for no benefit.

`python -m pqv2 features` reports this; `research/features.py` computes it.

**Fix.** Write the resolution timestamp at ingest. One column. The
highest-value change available to this project, and it is in the V1 ingester,
not here.

**Until then:** any hypothesis of the form *"follow this wallet while it is
running hot"* is untestable. Not false — untestable. Do not let a backtest tell
you otherwise.

---

## 2. There is no historical order book

**What breaks.** Depth, spread, partial fills, queue position, market impact,
and roughly half the behavioural sequences (add / exit / reversal ladders).

This is why 47 of V1's engineered feature columns are constant.

**How V2 handles it.** `DepthState.UNKNOWN` — never `OK`. Signals carry
`depth=None` and `spread=None`, not `0`; a zero would read as *measured and
empty* and silently justify a depth rejection. `ExecutionResult.uncertainty`
lists what could not be modelled on every fill.

**Interim.** `PriceTape` reconstructs a coarse price path from the aggregate
tape: if anyone printed at time T, that is a price you could plausibly have
paid. Real and causal, but **not continuous** — good enough for delay-decay and
early-exit *comparison*, not for depth.

**Fix.** Capture live order-book snapshots. Nothing else recovers it; the
history cannot be backfilled.

---

## 3. Early-exit results are MODELLED; settlement results are EXACT

Holding to resolution has an exactly known payoff: `resolution − price`, with
`resolution ∈ {0,1}`. Every other exit is priced off tape prints.

Consequences, all in the pessimistic direction and deliberately so:

- a profit target between two prints fills at the first print **past** it,
  never at the target
- a stop can be jumped straight through
- a token with sparse prints cannot support an early exit at all

So the two are **not directly comparable**. `Result.exit_confidence` says which
is which on every row, and `research/exits.py` refuses to crown a modelled
result over an exact one on a margin below 15% — inside the tape's own
uncertainty.

---

## 4. SELL and REDEEM events are outside the copyable universe

The tape holds 149,080 SELLs, 132,082 REDEEMs, 22,729 MERGEs, 6,714
CONVERSIONs, 3,555 SPLITs and 165,350 rows with no `side`.

Only `event_type='TRADE' AND side='BUY'` is treated as a decision — the rest are
mechanical position operations, and counting them would inflate every trade
count in the system.

**What breaks.** `HoldModel.settlement_share` is an **upper bound**, not a
measurement, and `settlement_share_confidence` says so. Full position-state
reconstruction across MERGE/SPLIT/CONVERSION is designed for but not built.

---

## 5. Wallet selection costs statistical power

Choosing the most profitable wallet out of 28,034 and then reporting how
profitable it is, is the oldest error in quantitative finance.

`Reference.provenance` records which happened:

- `operator_nominated` — external information, costs nothing
- `data_selected` — a hypothesis test; selection runs on the **in-sample window
  only**, every reported number is out-of-sample, and `selection_penalty()`
  charges the full eligible pool to the BH budget

Supply RN1 explicitly (`--wallet 0x…` or `PQV2_RN1`) whenever you have it from
outside the data. It is strictly stronger evidence.

---

## 6. Trade frequency is bounded by the wallets, not by the engine

The brief mentions 100–200 trades/day. The engine does **not** force this
(rule 11). What is available is measurable: `python -m pqv2 shadow` reports
opportunities/day, and the funnel shows exactly where they are lost.

If Strategy B produces fewer signals than expected, the ledger distinguishes
the causes — incomplete wallet reconstruction, restrictive behaviour matching,
portfolio saturation, or unpriceable fills — rather than leaving it to
guesswork. **Never solve a low count by lowering thresholds blindly**; find the
stage in the funnel first.

---

## 7. Statistical caveats that are easy to forget

- **Uncorrected p-values** are reported in `research/features.py` and
  `winners.separating_features`. With ~21 features tested at α=0.05, one false
  positive is expected. They prioritise research; they never promote anything.
- The **normal approximation** stands in for the t-distribution. At n ≥ 30 (the
  minimum for any promotion) it is accurate well beyond the precision of the
  effect sizes being judged.
- **Risk of ruin** is Monte Carlo under a Gaussian return assumption, which
  these returns violate — a 0.05 entry resolving YES returns +1900%. Use it to
  **compare sizing choices**, not as a forecast.
- **Correlation between strategies** is computed on aligned return sequences,
  which is a rough proxy: two strategies trading different markets at different
  times can show low correlation and still be one bet on one event.

---

## 8. What has NOT been demonstrated

- No strategy in this system has traded real money.
- `VALIDATED` authorises **paper trading only**. Going live is a human decision.
- Strategy A has never executed a trade, so it is neither credited nor blamed;
  it is preserved unchanged and marked `PRESERVED_UNTRADED`.
- Cross-wallet transfer is the strongest available evidence, and it is still
  *historical* evidence. A rule validated on several independent wallets should
  next be tested on wallets held out of the pass entirely — that experiment is
  the top-priority hypothesis the research module emits.

**No claim of guaranteed profit is made anywhere in this system, and none
should be inferred from any number it produces.**
