# polymarket_quant_v2

Separate project. **The original installation is never modified** — every path
into `Polymarket-Bot-DAVID/` and `Polymarket-Bot-DATA/` is opened `mode=ro`.

```
python -m polymarket_quant_v2.pqv2 audit        # where did the trades go?
python -m polymarket_quant_v2.pqv2 gates        # who owns each filter?
python -m polymarket_quant_v2.pqv2 strategies   # what route B has validated
python -m pytest polymarket_quant_v2/tests -q   # 48 tests, offline
```

## Why this exists — the measured finding

The question was "why only 18 trades when the other strategy makes 200". The
answer, from the real databases, is not a threshold:

| | Strategy A (`polymarket-quant-bridge`) | Strategy B (`wallet-strategy-lab`) |
|---|---|---|
| decisions | 40,820 over 92.3 h | 20,748 hypotheses tested |
| all `DO_NOTHING` | **100%** | — |
| one reason for all of them | *"Learning mode: no validated strategies yet"* | — |
| its own library | 170 rejected, 49 validating, 13 new, 2 quarantined | 20 insufficient, 15 failed, 14 not-significant, 3 overfit |
| **VALIDATED** | **0** | **2** (oos p = 5.7e-174, 2.7e-4) |
| connected to execution | yes | **no — zero references in the engine** |
| trades | **0** | 0 |

So the account took no trades for two reasons at once, and neither is a filter
being too strict:

1. **Strategy A** is gated by `require_strategies` — a single boolean that
   blocks *every* entry until A's own discovery validates a rule. It never did.
2. **Strategy B** already has two out-of-sample-validated strategies, and
   nothing in the trading engine reads them. They were never rejected. They
   were never routed.

Lowering Strategy A's thresholds would not have produced one route-B trade.

## What this project adds

**`gatemap.py`** — every gate in the engine, classified by owner with the
evidence for the call, and `blocks_route()`, which is the executable form of
the rule *only global-safety / portfolio / execution gates may block both
routes*. 8 gates belong to Strategy A; 5 are genuine global safety; 4 are
portfolio; 3 are execution.

**`funnel.py`** — the opportunity ledger. Every signal terminates in a named
state with a named gate; `suppression_ranking()` flags any Strategy A gate that
stops a route-B signal as a **wiring bug**, not a tuning observation.

**`router.py`** — the two-route pipeline. Strategy B goes
`wallet signal → behaviour match → B's conditions → B's risk → portfolio →
execution`, never through A's filters. Defaults to **shadow**: it builds
signals, runs every gate, writes the ledger, and sends nothing.

**`audit.py`** — answers the 22 diagnostic questions from the live databases.

## What is NOT in here

Stated plainly, because the master prompt asked for far more than this:

- **RN1 reconstruction is not done.** I was never given RN1's wallet address.
  Steps 18–19 cannot start without it. `wallet-strategy-lab` already implements
  the methodology the prompt describes for RN1 (behavioural decomposition, not
  trade copying) — supply the address and it can be pointed at it.
- **Cross-wallet strategy families, win expansion, compounding, portfolio
  allocation, AI research module** — not built. The audit had to come first,
  and it changed what the next step should be.
- **Rust acceleration is not built, and the profile says do not.** There is no
  Rust in the project and never was (`GAPS.md` §11 documents the same finding
  independently). Measured on this build: the full consistency study is 0.30 s;
  walletlab tests 20,748 hypotheses in 40 s; the live safety layer costs 54 µs
  per position per cycle inside a 20-second budget. Nothing is CPU-bound. The
  prompt says *"PROFILE FIRST… move only genuine CPU-bound bottlenecks"* and
  *"do not claim Rust improved the system until benchmarks demonstrate it"* —
  the profile does not currently justify it.
- **No backtest of route B inside the bot**, because the bot has zero closed
  trades to compare against. walletlab's own out-of-sample numbers stand on
  their own and are reported unchanged.

## The honest caveat

Connecting route B makes it *testable*, not *true*. Its two strategies are
validated on walletlab's own historical tape with FDR control and no
look-ahead — which is real evidence, and is still not the same as having
traded. Run the router in shadow first and compare the ledger against what the
markets actually did.
