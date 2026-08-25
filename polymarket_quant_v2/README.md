# Polymarket Quant Engine V2

Wallet-behaviour strategy discovery with the controls that make its output
interpretable, running **beside** the existing engine rather than replacing it.

The original installation under `Polymarket-Bot-DAVID/` is **unchanged,
recoverable and executable**. V2 opens its databases read-only and writes only
under `polymarket_quant_v2/var/`. This is enforced by a test, not by
convention.

---

## Read this first

**The existing engine's zero-trade problem is not a filter problem.**

All 40,820 decisions it has ever journalled are `DO_NOTHING`, and all 40,820
carry the same reason — *learning mode: no validated strategy exists*. That
gate sits above every other entry gate, so the market-state, depth, spread and
EV filters the brief asks about **were never reached in production**. Loosening
them would have changed nothing and degraded the engine.

The measured cause is one step further back: the research pipeline validates
against **78,219 rows / 123 markets / 3.8 days** while **116,923 rows / 1,285
markets / 90 days** sit in the same database file. It is starved, not
mis-tuned.

Full derivation with reproducing SQL: [`docs/MAPPING.md`](docs/MAPPING.md).
What the first pass found: [`docs/FINDINGS.md`](docs/FINDINGS.md).

---

## Install and run

Python 3.11+. **Standard library only** — no dependencies, nothing to download.

```bash
cd polymarket_quant_v2
python -m pytest tests/ -q          # 158 tests, offline, ~5 s
python -m pqv2 selftest             # checks the real database is reachable
```

Point it at the data if it is not in the default location:

```bash
set PQV2_DATA_DB=D:\...\Polymarket-Bot-DATA\state\intel.sqlite3
set PQV2_WORK_DIR=D:\...\polymarket_quant_v2\var
```

### The five-minute tour

```bash
python -m pqv2 inventory      # what evidence actually exists
python -m pqv2 reconcile      # reconciliation exit safety, before/after
python -m pqv2 audit          # where Strategy A's opportunities go, and why
python -m pqv2 gates          # who owns which suppression
python -m pqv2 rn1            # reconstruct the reference wallet
python -m pqv2 features       # which features carry information; which are inert
```

### The full cycle

```bash
python -m pqv2 discover --max-wallets 40 -v   # discovery + validation
python -m pqv2 leaderboard                    # what survived
python -m pqv2 exits                          # settlement vs early exit
python -m pqv2 expansion                      # the Win Expansion ladder
python -m pqv2 shadow                         # full pipeline over history
python -m pqv2 dashboard                      # everything, one screen (text)
python -m pqv2 gui                            # visual dashboard (or DASHBOARD.vbs)
python -m pqv2 diagnose                       # the 22 mandatory questions
```

---

## The two routes

```
                    POLYMARKET DATA
                          |
          +---------------+---------------+
          |                               |
     STRATEGY A                      STRATEGY B
  existing engine                RN1 / wallet engine
  own filters, own ladder        own filters, own ladder
          |                               |
          +---------------+---------------+
                          |
                 PORTFOLIO / RISK LAYER
                          |
                      EXECUTION
```

**Strategy B never passes through Strategy A's gates.** Every rule that can
stop a trade is registered with an owner, and `SignalRecord.reject()` raises if
a Strategy A gate is evaluated on route B. `tests/test_isolation.py` asserts
the raise fires and that `strategy_b/` imports nothing from `strategy_a/`.

Only `GLOBAL_SAFETY` gates bind both routes, and each must carry written
evidence — a global gate without evidence is a Strategy A gate in disguise, and
the test suite fails the build if one appears.

---

## The four controls that make results interpretable

**1. Wallet alpha.** This dataset has a large favourite–longshot bias
(**+8.8 points at 0.60–0.70**, **+8.9 at 0.70–0.80**, measured over all 116,923
settled trades). So "buy between 0.6 and 0.9" earns ~+20% expectancy while
copying nobody. Every candidate is scored against the same price band and week
across all *other* wallets. Zero alpha ⇒ `NO_WALLET_ALPHA`, cannot promote,
regardless of profit. **This control exists nowhere in the V1 engine** — without
it, a price-band search across 40 wallets reports 40 "independent validated
strategies" that are all the same market-wide effect.

**2. The denominator is always reported.** A sweep tests 5,184 transformations
per wallet. Promotion is gated on a Benjamini–Hochberg threshold computed over
the *whole pass*, so a p-value can never be quoted without the search that
produced it — and choosing the reference wallet from data is charged to that
budget too.

**3. No look-ahead.** A trade's outcome enters its wallet's statistics at
`settled_ts`, never at `ts`. Prediction markets pay at resolution, so a win rate
computed over unresolved trades is information nobody had. Enforced with a heap
in `substrate/state.py` and asserted by `tests/test_causality.py` — including a
case a naive implementation would pass.

**4. An unpriceable copy earns nothing.** If no price printed inside the fill
window, the trade is `UNFILLED` — never filled at the wallet's own price. That
one line is the difference between a copy backtest and a fiction.

---

## Status ladder

```
INSUFFICIENT_EVIDENCE   too few out-of-sample fills or markets
UNPRICEABLE             fill rate too low to be a real strategy
FAILED                  negative out-of-sample expectancy
NOT_SIGNIFICANT         did not clear the pass's BH threshold
NO_WALLET_ALPHA         real, but it is market structure, not the wallet
CONCENTRATED            too much profit from one market
UNSTABLE                positive in under half of walk-forward folds
FRAGILE                 fails perturbation or block bootstrap
DRIFT                   random entries in the same pool do as well
VALIDATED               survived all of the above
```

`VALIDATED` authorises **paper trading**. Going live is a human decision this
code never makes. `validation/validate.py` is the only module permitted to
assign a status, asserted by AST inspection.

---

## Layout

```
pqv2/
  config.py        every threshold, each owned by a named layer
  gates.py         who may block what, and the evidence for it
  ledger.py        every signal, one terminal state, reconciled
  shadow.py        full pipeline over history, no capital
  substrate/       causal reconstruction: data, price tape, wallet state
  strategy_a/      the existing engine, wrapped and never modified
  strategy_b/      RN1, decomposition, behaviour matching, discovery, engine
  validation/      backtest, statistics, wallet alpha, ladder, research log
  risk/            sizing + Win Expansion, compounding, portfolio, execution
  research/        feature inertness, winner/loser, exits, AI assistant
  accel/           Rust bridge: enabled / disabled / shadow, Python fallback
  reconciliation.py exit-safety guard (surgical patch)
  report/          dashboard, 22-question diagnostic, reconciliation report
rust/              real, buildable PyO3 crate (not currently needed)
tests/             158 tests, offline, no database required
docs/              MAPPING · FINDINGS · LIMITS · PERFORMANCE · RECONCILIATION-PATCH
```

---

## Honest scope

- Nothing here has traded real money.
- `VALIDATED` means *survived historical out-of-sample validation*, not
  *profitable*.
- Strategy A has never executed a trade, so V2 neither credits nor blames it.
  It is preserved as `PRESERVED_UNTRADED` and left alone.
- Several questions the brief asks **cannot be answered from this data at all**
  — early exits, depth, partial fills, and point-in-time wallet track record.
  [`docs/LIMITS.md`](docs/LIMITS.md) says which, why, and what would fix each.

No claim of guaranteed profit is made anywhere in this system, and none should
be inferred from any number it produces.
