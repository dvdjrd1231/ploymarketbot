# Architecture

```
                        POLYMARKET DATA  (intel.sqlite3, read-only)
                                    |
                        MARKET + WALLET RECONSTRUCTION
                          substrate/data.py, substrate/state.py
                          point-in-time, causal, streamed
                                    |
                        STRATEGY / FAMILY DISCOVERY
                          strategy_b/decompose.py, discover.py,
                          similarity.py
                                    |
                +-------------------+-------------------+
                |                                       |
                v                                       v
          STRATEGY A                              STRATEGY B
      strategy_a/adapter.py                  strategy_b/engine.py
      (existing engine, wrapped,             RN1 reconstruction
       never modified)                       wallet decomposition
      own filters, own ladder                behaviour matching
                |                            own filters, own ladder
                |                                       |
                +-------------------+-------------------+
                                    |
                          SIGNAL VALIDATION
                          validation/validate.py   <- the ONLY authority
                                    |
                          RISK / SIZING ENGINE
                          risk/sizing.py  (+ Win Expansion)
                                    |
                          COMPOUNDING ENGINE
                          risk/compounding.py
                                    |
                          PORTFOLIO ALLOCATION
                          risk/portfolio.py   (may bind, never erases)
                                    |
                              EXECUTION
                          risk/execution.py
                                    |
                               RESULTS
                          ledger.py  (every signal, one terminal state)
                                    |
                          RESEARCH / LEARNING
                          research/*.py, validation/registry.py
                                    |
                          BETTER STRATEGIES
```

`PYTHON = control/orchestration · RUST = computation · AI = research assistant
· QUANTITATIVE VALIDATION = final authority`

---

## The three structural guarantees

Everything else in this design follows from these, and each is enforced by a
test rather than by convention.

### 1. Strategy B is never silently blocked by Strategy A

Every rule that can stop a trade is registered in `gates.py` with an **owner**:

| owner | may block |
|---|---|
| `STRATEGY_A` | route A only |
| `STRATEGY_B` | route B only |
| `GLOBAL_SAFETY` | both — **and must carry written evidence** |
| `PORTFOLIO_RISK` | both, but recorded separately so strategy quality stays measurable |
| `EXECUTION` | a fill, never a signal |

`SignalRecord.reject()` calls `gates.assert_may_block(key, route)` and raises on
a cross-owner block. `test_isolation.py` asserts the raise fires, asserts
`strategy_b/` imports nothing from `strategy_a/`, and asserts no
`GLOBAL_SAFETY` gate exists without evidence — that being the loophole through
which V1's filters could otherwise be re-imposed on both routes by relabelling.

### 2. No signal disappears without a reason

Every opportunity becomes a `SignalRecord` and leaves in exactly one terminal
state, carrying the registered gate that stopped it:

```
SIGNAL_RECEIVED → BEHAVIOR_MATCHED → STRATEGY_ACCEPTED → RISK_PASSED
                → PORTFOLIO_APPROVED → EXECUTION_ATTEMPTED
                → EXECUTION_SUCCESSFUL
   terminal: STRATEGY_REJECTED | RISK_REJECTED | PORTFOLIO_REJECTED
             | EXECUTION_FAILED | EXECUTION_SUCCESSFUL
```

`Funnel.assert_balanced()` **raises** if

```
received ≠ strategy_rejected + risk_rejected + portfolio_rejected
           + execution_failed + execution_successful + in_flight
```

A log line is evidence a human read something. A ledger is evidence the numbers
add up. V1 had excellent logging and still nobody could say where 40,820
opportunities went, because `DO_NOTHING` was one string covering every cause.

*(This guard earned its place during development: it caught a real accounting
bug on its first run, and a portfolio bootstrap stall on its second.)*

### 3. Only the ladder promotes

`validation/validate.py` is the sole authority on status. Nothing promotes
because it made money once, has a high win rate, resembles RN1, survived an
attack, or an AI liked it. `test_isolation.py` asserts by AST inspection that
no module outside `validate.py` calls `assign_status`.

---

## Layer by layer

### substrate/ — what is knowable, and when

`data.py` defines the evaluable universe: wallet BUYs joined to a settled
resolution. Hold-to-resolution payoff is then **exact**, not modelled, which is
why this substrate is 34× larger in markets and 24× longer in time than the
captured-feature series V1 uses.

`state.py` enforces causality mechanically: *a trade's outcome enters wallet
state at `settled_ts`, never at `ts`.* Unsettled trades sit in a heap and fold
in only as the clock passes them. Memory is bounded by simultaneously-unsettled
trades, not by tape size.

`PriceTape` answers "what could I actually have paid `delay` seconds later"
from real prints, and returns `None` rather than interpolating a price nobody
paid.

### strategy_b/ — the RN1 route

- `rn1.py` — reference selection, with **provenance**. An operator-named wallet
  costs no statistical power. A data-selected one is a hypothesis test, so the
  selection is counted in the multiple-testing budget (`selection_penalty`) and
  every reported number is out-of-sample.
- `decompose.py` — entry / sizing / hold / risk / outcome models as
  distributions over *measurable conditions*, never prices or tokens. That is
  what makes a profile matchable against a wallet nobody has studied.
- `behavior.py` — weighted agreement across independent dimensions.
  Transparent arithmetic on purpose: a score fitted on outcomes would be
  look-ahead with a friendly name.
- `similarity.py` — behavioural clustering **proposes**; `strategic_agreement`
  (grouping on `params_only_hash`, the rule *without* the wallet) **disposes**.
- `engine.py` — the live route. No Strategy A gate appears anywhere in it.

### validation/ — what counts as real

`baseline.py` is the control that makes any of it interpretable. Measured on
the client's data, the favourite–longshot bias is **+8.8 points at 0.60–0.70**
and **+8.9 at 0.70–0.80**. So "buy between 0.6 and 0.9" earns ~+20% expectancy
while copying nobody. A price-band search across 40 wallets finds this once per
wallet and reports 40 "independent validated strategies" — all the same
market-wide effect, and it would look like the best result the project has ever
produced. Candidates are scored against the same price band and week across all
*other* wallets; zero alpha ⇒ `NO_WALLET_ALPHA`, cannot promote, regardless of
profit. **This control exists nowhere in V1.**

`stats.py` enforces the denominator via Benjamini–Hochberg over the whole pass.
`block_bootstrap_ci` respects that trades in one market are correlated —
resampling single trades treats a run as many independent wins and narrows the
interval fraudulently.

The ladder, cheapest disqualifier first:

```
INSUFFICIENT_EVIDENCE → UNPRICEABLE → FAILED → NOT_SIGNIFICANT
→ NO_WALLET_ALPHA → CONCENTRATED → UNSTABLE → FRAGILE → DRIFT → VALIDATED
```

`VALIDATED` authorises **paper**. Live is a human decision this code never
makes.

### risk/ — the account, and what it will lend a trade

Sizing and Win Expansion are separate questions. Sizing is about the account
and is bounded by `GLOBAL_SAFETY`; expansion is about the evidence and is
bounded by the evidence. The final stake is the **minimum** of the two, never a
compromise — so `expand()` can only ever reduce, and every refusal names the
precondition that failed.

`fit_expansion` **discovers** the multiplier rather than assuming 1.5×, and
recommends on return **per unit of drawdown**: the step that makes the most
money is almost always the largest one; the step that makes the most money per
unit of pain usually is not.

`portfolio.py` may bind a trade but never erases the signal — a portfolio
rejection is recorded as such, so "is this strategy good?" and "can we afford
it?" stay separate questions.

`execution.py` reports `DepthState.UNKNOWN` rather than `OK` where no order
book exists. A depth gate that always passes in backtest and always fires in
production makes the backtest a different strategy from the one that trades.

### accel/ — Rust, honestly gated

`RUST ENABLED` (fall back to Python on any error) · `RUST DISABLED` ·
`RUST SHADOW` (run both, compare, **return Python**). Python is the reference
implementation. A failed import can never take the application down.

See `docs/PERFORMANCE.md`: profiling found a **2.8× speedup in one Python
function** before any Rust was warranted, and `should_build()` states the
trigger as a rule rather than leaving it to enthusiasm.

### research/ — and the AI's boundaries

The AI is a research assistant, never an authority. It returns `Hypothesis`
objects that carry a test, a prediction and a falsifier — and `status` is
always `PROPOSED`, including for LLM-produced ones. The default backend is
deterministic and offline: it runs on the numbers the pass measured, needs no
API key and no model, and cannot hallucinate a figure. `test_isolation.py`
asserts the AI module is not importable from the execution loop.
