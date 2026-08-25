# Architecture

## Module map

```
pqv3/
  bootstrap.py        makes the validated V2 package importable, unmodified
  config.py           every threshold, each owned by a named layer
  secrets.py          the signing boundary; the ONLY module that reads a key
  runtime.py          15-step startup, background loops, live authorisation
  cli.py              the command line

  core/
    canon.py          EvidenceState, Layer, Opportunity, Signal, WalletDNA
    store.py          durable V3 SQLite; provenance stamped on every row
    source.py         read-only V1 access, pooled, every query bounded by as_of
    pit.py            get_information_state(timestamp, market_id)

  ingest/
    base.py           collector contract, health recording, stdlib HTTP
    collectors.py     markets, order book, news, chain
    settled_ts.py     THE FIX for V1's highest-value data gap

  intelligence/
    wallets.py        wallet DNA, alpha-vs-band control, cohorts, COPY_SCORE

  agents/
    base.py           agent contract; abstention weighs zero
    registry.py       the 25 agents
    debate.py         adversarial debate; the red team is a veto

  probability/
    ensemble.py       8 heterogeneous estimators, calibration, disagreement

  regime/detect.py    observable regimes, calibrated from the data
  crash/meter.py      10-input crash meter; max-with-support, not average

  portfolio/
    capital.py        the $100 model; CAPITAL_INFEASIBLE as a first-class result
    correlation.py    structural correlation; three positions, one bet

  decision/
    gates.py          the twelve validity gates
    decide.py         evidence -> probability -> debate -> size -> gates

  execution/
    simulator.py      no idealised fills; UNFILLED is a real outcome
                      plus segmented / staged execution planning

  learning/
    forensics.py      loss classification, missed opportunities, counterfactuals

  server/
    api.py            22 sections, all scrubbed
    ui.py             the dashboard shell, old Quant Bridge visual language
    app.py            loopback HTTP server

  accel/              Rust bridge: enabled / disabled / shadow, Python reference
rust/                 real PyO3 crate, four kernels, ships unbuilt by design
```

## The load-bearing decisions

### `EvidenceState` is the only thing a decision may read

Agents, estimators, gates and the scanner receive an `EvidenceState` and
nothing else. No database handle, no HTTP client, no wall clock. A component
that can only see an `EvidenceState` **cannot** reach past its `as_of`, because
there is no API on it that returns the future. Look-ahead becomes something you
would have to work to introduce rather than something you must remember to
avoid.

`resolution_for()` — the outcome — lives on `HistoricalSource`, is used only by
scoring code, and is deliberately absent from `EvidenceState`. An agent that
could reach it would be reading the answer sheet.

### Every gate runs, even after one fails

V1 journalled 40,820 consecutive `DO_NOTHING` decisions, all with the same
reason, from a single gate that sat above every other. Nobody could see that
the gates below it had never been reached, so a diagnosis by log-reading would
have loosened the wrong rules. V3 runs all twelve every time and records all
twelve verdicts on the decision row. The first *critical* failure sets the
action; it does not stop the evaluation.

### A gate that cannot judge fails

This inverts the usual default and is the most consequential single choice in
the system. On missing data V3 declines to trade rather than trading blind. It
is why a fresh install produces `DO_NOT_TRADE` on everything and explains why
for each candidate.

### Abstention is not agreement

With news, chain and book layers empty, roughly ten of the twenty-five agents
abstain. Counting those abstentions as neutral would let a four-agent quorum
masquerade as a twenty-five-agent consensus. Abstentions weigh **zero** and are
displayed, never hidden.

### The red team is a veto

If an adversarial agent objects with conviction at or above 0.5, the candidate
dies and confidence goes to zero. Were the objection merely a weight, a
sufficiently enthusiastic majority could always outvote it.

### Two independent uncertainty measures, both multiplicative

Debate confidence is cut by agent disagreement, participation and information
completeness. It is then cut *again* by the probability ensemble's estimator
dispersion. Each independently invalidates a result, so neither can be offset
by strength elsewhere.

### The data clock is not the wall clock

The tape's newest trade can be days old. Research is anchored to the newest
`TRADE` — filtered to exclude REDEEM/MERGE/SPLIT, which keep arriving for days
after the last real decision. Each candidate is then evaluated at the moment of
its own most recent print, which is the event-driven anchor an honest backtest
uses. Trading is never anchored this way: `DATA_VALIDITY` measures staleness
against the wall clock and refuses.

### Provenance on every row

`ts`, `capture_ts`, `source`, `data_version`, `schema_version` are stamped by
`Store.insert`, not by the caller, so no call site can forget them. Without
`source`, "wallet win rate 68%" is a claim; with it, it is a claim you can walk
back to a row.

## Two real bugs the design caught during development

Both were found by V3's own machinery rather than by inspection, which is the
argument for building the machinery.

**A look-ahead leak in market metadata.** `market_meta()` returned
`MAX(ts)` over a market's whole history, so a state built early in the tape
carried a timestamp from the end of it. `INFORMATION_VALIDITY` reported
`LEAK: layers dated after as_of: ['market']` and refused to trade.
Regression test: `tests/test_causality.py::test_market_metadata_is_bounded`.

**Silent column loss in the store.** `Store.insert` took its column list from
the first row of a batch, so a batch of `{realized_pnl}` followed by
`{unrealized_pnl}` wrote the second row with its P&L discarded and no error
anywhere. The union of keys then exposed a second failure — missing keys bind
NULL, and a NULL against a `NOT NULL DEFAULT` is a constraint violation that
`INSERT OR IGNORE` swallows, so the row vanished entirely. Rows are now grouped
by key set. Regression test:
`tests/test_capital.py::test_account_is_derived_from_the_ledger_not_cached`.

A third was a latent production hazard: `Store`'s thread-local connection cache
ignored the database path, so every store in a thread shared the first one's
file.
