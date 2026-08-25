# Polymarket Quant Bridge V3

**A rebuild of the V2 installation**, not a second product beside it. One
directory, one `var/`, one install file, one dashboard.

A research-first, continuously learning quantitative intelligence and execution
system for Polymarket. Python computational core with Rust hot kernels, a local
URL dashboard in the original Quant Bridge visual language, a **configurable**
starting bankroll, and live trading disabled until a human authorises it
against measured evidence.

**Nothing here has traded real money. No number in this system is a claim of
profitability.**

---

## Install

Python 3.11+. **Standard library only** — no dependencies, nothing to
download, and nothing dials out unless you run `COLLECT.bat`.

```
double-click  INSTALL.bat        install check, full research pass, dashboard
double-click  DASHBOARD.vbs      live dashboard at http://127.0.0.1:8787/
double-click  COLLECT.bat        start live capture (the ONLY networked file)
double-click  STOP-DASHBOARD.bat stop the background server
```

### Your bankroll is configured, never assumed

$100 is the default because it was specified, not because the system believes
anything about it. Change it in any of three ways:

```bat
set PQV3_STARTING_CAPITAL=250     & REM environment, persists for the session
python -m pqv3 --capital 250 scan & REM per run
```
…or edit the one line at the top of `INSTALL.bat`.

Every risk fraction is a fraction of *equity*, so the model behaves identically
at $100 and at $100,000. What does **not** scale is the venue's absolute
minimum order, and that collision is the whole reason `CAPITAL_INFEASIBLE`
exists as a first-class result. Run `python -m pqv3 capital` to see the sizing
decision across the whole price curve at your bankroll, including the refusals.
At $25, for example, it correctly refuses contracts above ~$0.80 rather than
pretending it can size them.

---

## Layout

```
pqv2/          the PRESERVED V2 engine — causal substrate, validation ladder,
               gate ownership, 158 tests. V3 imports it rather than
               reimplementing it. Unmodified.
pqv3/          the V3 engine — collectors, point-in-time layer, 25 agents,
               probability ensemble, capital model, 12 gates, the discovery ->
               validation pass, learning loop, dashboard server.
  research/    matrix · hypothesis · sweep · baseline · backtest · walkforward
               robustness · validate · discover · stats
  intelligence/ wallet DNA · cross-wallet graph · sequence analysis
  news/        news -> market causality
  learning/    loss forensics · counterfactuals · online learning
  report/      the research report
tests/         V2 suite; tests/v3/ is the V3 suite. 301 pass together.
rust/          V2's PyO3 accel crate (unbuilt, not currently needed)
rust_v3/       V3's PyO3 accel crate  (unbuilt — see ENGINE-PERFORMANCE.md)
docs/          ENGINE-ARCHITECTURE · ENGINE-LIMITS · ENGINE-PERFORMANCE
               plus the original V2 docs (MAPPING, FINDINGS, PRIOR-WORK …)
var/           everything this system writes. Nothing outside it is touched.
```

Both engines share one config surface, one database directory and one
dashboard. `python -m pqv2 <cmd>` still runs every V2 tool exactly as before.

---

## Read this first

Three facts about the available data shape everything. They are properties of
the evidence, not of the code, and the system reports each rather than hiding
it.

**1. There is no order-book history, and it cannot be recovered.** Depth,
spread, partial fills, queue position and market impact for past markets are
gone. V3 captures them going forward; until enough accumulates, every
depth-dependent feature is gated and the dashboard says so. This is why
`UNAVAILABLE` is a first-class state — a zero spread reads as *measured, and
tight*, and would silently justify an execution gate passing on evidence that
was never collected.

**2. `resolutions.settled_ts` is 0 in all 8,116 rows.** The moment an outcome
became public is recorded nowhere. The fallback can only delay information,
never advance it, so it is safe — but it makes point-in-time wallet track
record structurally untestable and inflates the multiple-testing penalty
roughly 12×. `COLLECT.bat` repairs it from the venue; `python -m pqv3
inventory` reports coverage.

**3. This dataset has a large favourite–longshot bias.** Buying anything in the
0.60–0.80 band earns roughly +9 points of expectancy while copying nobody at
all. **Ranking wallets by win rate produces a leaderboard of people who like
favourites.** Every wallet is scored by `alpha_vs_band` — its return against
every *other* wallet in the same band and week.

---

## The five controls that make results interpretable

**1. Point-in-time or nothing.** `get_information_state(timestamp, market_id)`
reconstructs the whole information environment at a timestamp. Every layer is
bounded by `<= as_of`; news is filtered on *capture* time, not publication
time. A gate fires if any layer is dated after `as_of` — and it did fire during
development, catching a real leak in market metadata.

**2. Absent is not zero.** Layers report `OK`, `STALE`, `INSUFFICIENT_HISTORY`,
`UNAVAILABLE` or `NOT_CONFIGURED`. **A gate that cannot judge fails.** On a
fresh install ~10 of 25 agents abstain, and the dashboard shows who and why.

**3. The denominator is always reported.** A p-value without the search that
produced it is uninterpretable, so `STATISTICAL_VALIDITY` refuses any strategy
whose hypothesis count was not recorded.

**4. The red team is a veto, not a vote.** If an adversarial agent objects with
conviction, the candidate dies — otherwise a sufficiently enthusiastic majority
could always outvote it.

**5. Alpha is measured against a price-band-matched baseline.** Returns are
`(resolution − p)/p`, so a winning longshot pays +19 and a winning favourite
+0.11. Against a raw baseline, any rule that avoids longshots looks
spectacular — the first run of this system scored `price >= 0.53` at +0.50
"alpha", which is a price preference anyone can type. Every observation is now
compared only against others in the same price band and the same week,
leave-one-out. A rule must ALSO make money in absolute terms: on the real tape
2,928 rules beat their band while still losing money.

---

## Live trading

`LIVE` cannot be entered by any code path. `pqv3 mode LIVE` is refused. The only
route is `python -m pqv3 authorize-live --yes`, which prints the measured
requirements, records which were unmet, and stores the full system snapshot
alongside your consent. Requesting LIVE without it forces the engine back to
PAPER and raises an alert.

Credentials live in the OS credential store or an environment variable, never
in source, logs, the database, an agent prompt or a dashboard payload. The
secrets module answers presence questions and signs; there is no
`get_private_key()` to call, and the test suite plants a key then greps every
API payload and the rendered dashboard for it.

---

## Honest scope

- Nothing here has traded real money.
- The discovery engine runs. A pass over the real tape searched 7,840
  transformations, screened them against a price-band-matched baseline, and
  put 15 candidates through walk-forward, a robustness battery and the $100
  capital test. **Those 15 collapse to 8 distinct findings** — a grid search
  returns the same effect at several thresholds, and the pass says so.
- **The $100 capital returns are MODELLED, not measured.** This database's
  settlement timestamps are degenerate (see limit 2 below), so the simulation
  uses an assumed 3-day hold. Out-of-sample expectancy is unaffected: it needs
  only entry price and outcome. Every validated strategy carries this caveat
  on its own record.
- `VALIDATED` authorises paper trading. Nothing has been promoted past it.
- **Neither Rust crate has been compiled** — no toolchain on the build machine.
  The Python reference kernels are authoritative and fully tested; the Rust
  equivalence tests skip with a stated reason rather than passing silently.
  Profiling says don't build them yet: 83% of scan time was SQLite connection
  setup, and pooling gave a 55× speedup. See `docs/ENGINE-PERFORMANCE.md`.
- News→market **direction is hardcoded 0.0** by design. Headline sentiment does
  not tell you which side of a binary market benefits, and a wrong sign is
  worse than no sign.
- **Chain events are stored unparsed** — decoding needs the CTF/USDC ABIs.
  Agent 3 sees counts, not semantics.
- News, blockchain and order-book layers start empty. They accumulate; they
  cannot be backfilled.

`docs/ENGINE-LIMITS.md` lists all seven limits, why each exists, and what would
fix it.

No claim of guaranteed profit is made anywhere in this system, and none should
be inferred from any number it produces.
