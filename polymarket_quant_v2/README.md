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
download, and nothing dials out unless you run `2-COLLECT.bat`.

Run the numbered files in order — no commands to type.

```
START-HERE.txt          read this first
1-INSTALL.bat  ~15 min  once. checks, tests, first research pass,
                        results, then opens the dashboard
2-COLLECT.bat  ~20 min  capture live data, repair, re-research ONLY if
                        the data improved, then results.
                        (the ONLY file that uses the network)
3-RESULTS.bat   ~4 min  optional. steps 1 and 2 call it for you.
                        re-read the findings any time
4-DASHBOARD.vbs         reopen http://127.0.0.1:8787/
5-STOP-DASHBOARD.bat    stop the background server
```

Steps 1 and 2 both end by calling step 3, so `strategies`, `signals`,
`invert` and `report` run automatically. Nothing needs to be typed at a
prompt.

### Your bankroll is configured, never assumed

$100 is the default because it was specified, not because the system believes
anything about it. Change it in any of three ways:

```bat
set PQV3_STARTING_CAPITAL=250     & REM environment, persists for the session
python -m pqv3 --capital 250 scan & REM per run
```
…or edit the one line at the top of `1-INSTALL.bat`.

Every risk fraction is a fraction of *equity*, so the model behaves identically
at $100 and at $100,000. What does **not** scale is the venue's absolute
minimum order, and that collision is the whole reason `CAPITAL_INFEASIBLE`
exists as a first-class result. Run `python -m pqv3 capital` to see the sizing
decision across the whole price curve at your bankroll, including the refusals.
At $25, for example, it correctly refuses contracts above ~$0.80 rather than
pretending it can size them.

---

## The console

The dashboard's first page is **CHAT**, and it is a control interface rather
than a chat widget. Plain English goes in; a computed, sourced answer comes
back. Also available without a browser:

```
pqv3 chat                        interactive session
pqv3 chat "audit the whole system"
pqv3 chat "why is the news panel empty?"
pqv3 chat "what should I do next?"
pqv3 ingest research-note.docx   read a document, convert it to candidates
pqv3 watch                       what the system noticed on its own
pqv3 doctrine                    the charter in force, and its real boundary
```

Every reply is a record, not prose: the mode it was read as, the operating
state it was answered in, the finding, the store rows behind it, the commands
that would advance it, and — the field that keeps the rest honest — what this
installation **cannot** do about it.

Three properties are worth knowing before you rely on it:

**The language model is optional and is consulted last.** The store is read,
the answer is computed, and only then is a local model asked to narrate what
was computed. Unplug it and every figure survives, because no figure was ever
the model's to produce. With no model configured — the default — the console
works fully and says so.

**A diagnosis names a layer, not a symptom.** Ask why a panel is empty and the
reply walks INPUT → PROCESSING → STORAGE → MODEL → DECISION → UI, reports the
first link that breaks, and states explicitly that everything downstream of it
is a consequence rather than a second fault.

**It cannot edit your source, and it says so.** Ask it to fix a module and it
locates the files, sizes them, plans the change and names the test — then
tells you the modify step is not available here. That limit is published on
the DOCTRINE page alongside the charter that authorises it.

### Documents in, candidates out

Name a file in the chat — or run `pqv3 ingest` — and it is read, not
acknowledged. TXT, MD, CSV, TSV, JSON, DOCX and XLSX; PDF is refused by name
with the reason, because guessing at a PDF's text layer without a parser
produces plausible-looking garbage.

Statements are classified as claim, assumption, formula or limitation, then
translated into the engine's own observation columns: *"wallets with a high
rolling win rate outperform"* becomes candidates on `w_win_rate` and
`w_roll_win_rate`. Three rules make that translation honest rather than
flattering:

- **No threshold survives the trip.** The document's own `0.6` is discarded and
  the candidate is swept over the standard quantile grid. A number lifted from
  prose has no denominator behind it.
- **An unstated direction costs two tests, and says so.** Both are tried and
  both count against the multiple-comparison denominator.
- **A partly-translatable claim is marked partly translatable.** *"Order-book
  imbalance predicts price movement"* reaches the price columns through the
  word "price" while its actual condition — imbalance — has no column at all.
  The claim carries that caveat, and the concept is reported as a data
  requirement instead of being silently proxied.

Nothing is adopted. A candidate from a document clears the same in-sample
screen, the same BH threshold, the same walk-forward and the same robustness
battery as one the sweep generated by itself.

### It tells you what it noticed

The research loop ranks what changed by importance × expected economic impact ×
urgency, and what clears the floor rides out on your next reply — a failing
collector, a drawdown halfway to the hard stop, a gate that has forgone more
than it avoided, settlement timestamps crossing the threshold that unlocks four
search axes. A product rather than a sum, so an alarming reading that cannot
cost anything stays quiet; below the floor it is still recorded, so *"you never
told me"* has an answer. The same condition, unchanged, surfaces once.

Those three factors are **estimates**, labelled as such wherever they appear.
They order a queue. Nothing downstream reads them, and no gate or sizing
decision ever sees them.

The charter itself lives in [`docs/MASTER-SYSTEM-PROMPT.md`](docs/MASTER-SYSTEM-PROMPT.md)
and is read at runtime. Editing that file changes how the embedded model is
instructed — no code change, no rebuild.

---

## Research methods

Six commands that look for structure correlation cannot see. Every one of them
shares a discipline, because every estimator here is **biased away from zero
under the null** — an unremarkable series will produce a confident-looking
number from all of them. So none reports a raw statistic. Each is measured
against surrogate data built to destroy the specific structure under test and
nothing else, and the comparison is what gets reported.

```
pqv3 depend  <token_a> <token_b>   mutual information, transfer entropy, lead-lag
pqv3 cycles  <token>               periodicity on irregular sampling
pqv3 states  <token>               hidden-state (HMM) regime model
pqv3 montecarlo <strategy_id>      sequencing risk: ruin across orderings
pqv3 checkpoint                    change control: a rollback point
pqv3 watch                         what the system noticed on its own
```

**`depend`** — mutual information sees a U-shape that Pearson reads as zero.
Transfer entropy adds a direction, and is implemented as the conditional MI
`I(Y_{t+1}; X_t | Y_t)` so the conditioning that makes direction meaningful is
provably there. The null is a cyclic shift, which preserves each series' own
autocorrelation and destroys only the alignment — without it, two independent
random walks read as a strong discovery. On real tape the raw MI between two
tokens was 0.29 nats and the null's was 0.25: almost all of it was bias.

**`cycles`** — Lomb-Scargle fits sinusoids directly to the timestamps as they
occurred, so nothing is resampled and no observation is invented. The statistic
is the tallest peak *anywhere in the scan*, so the multiplicity of searching
200 frequencies is priced into the null by construction. It also computes the
spectrum of the arrival rhythm itself, because a surrogate null cannot remove
aliasing: with a true 12-hour cycle sampled 16 hours a day, the tallest peak
lands at 8.01 h — `1/8 = 1/12 + 1/24` exactly. That case returns
`PERIODICITY_ALIASED` and names the fold rather than reporting 8 hours.

**`states`** — Baum-Welch with BIC model selection, seeded restarts, and scaled
forward-backward so a long series cannot underflow. A fit must beat reordered
copies of itself *and* beat a plain first-order Markov chain on the observed
symbols before the word regime is used. It reports what it cannot do: it does
not reliably separate a switching regime from a smooth latent process.

**`montecarlo`** — nineteen trades at +6% and one at −60% have the same
terminal wealth in every ordering, and the same expectancy, win rate and profit
factor. A backtest cannot tell those orderings apart. **30% of them hit the
hard stop and stop trading.** That gap is what this measures. Both an iid and a
block resampler are run, and where they disagree the block figure — losses
allowed to cluster — is the one to plan against.

**`checkpoint`** — git already versions the code better than a bespoke table
would; what it cannot record is which strategies were live and how many rows
each table held. A checkpoint joins the two. Rollback is planned and printed,
never taken automatically, and refuses against a dirty tree.

### What these found when pointed at real tape

Nothing tradable, and they say so. Two tokens showed significant mutual
information but no directional flow and a best lead-lag at lag **zero** — a
contemporaneous relationship, not a signal. The hidden-state model returned
`SHORT_MEMORY_NOT_REGIMES`: its states lost to a first-order Markov chain by 81
BIC. One token carries 10,690 prints at a single price, which returns
`DEGENERATE_SERIES` rather than "no periodicity found", because those are
different statements.

---

## Layout

```
pqv2/          the PRESERVED V2 engine — causal substrate, validation ladder,
               gate ownership, 158 tests. V3 imports it rather than
               reimplementing it. Unmodified.
pqv3/          the V3 engine — collectors, point-in-time layer, 25 agents,
               probability ensemble, capital model, 12 gates, the discovery ->
               validation pass, learning loop, dashboard server.
  agents/      25 specialists · debate · optional local LLM · the charter
               (doctrine.py) · the control console (console.py)
  research/    matrix · hypothesis · sweep · baseline · backtest · walkforward
               robustness · validate · discover · stats
  intelligence/ wallet DNA · cross-wallet graph · sequence analysis
  news/        news -> market causality
  learning/    loss forensics · counterfactuals · online learning
  report/      the research report
tests/         V2 suite; tests/v3/ is the V3 suite. 301 pass together.
rust/          V2's PyO3 accel crate (unbuilt, not currently needed)
rust_v3/       V3's PyO3 accel crate  (unbuilt — see ENGINE-PERFORMANCE.md)
docs/          MASTER-SYSTEM-PROMPT (the charter, loaded at runtime)
               ENGINE-ARCHITECTURE · ENGINE-LIMITS · ENGINE-PERFORMANCE
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
roughly 12×. `2-COLLECT.bat` attempts a repair from the venue. On the supplied
database it finds **no matches** — those markets are not in the venue's public
catalogue — which the step states on screen. See `docs/ENGINE-LIMITS.md` §2.

**3. This dataset has a large favourite–longshot bias.** Buying anything in the
0.60–0.80 band earns roughly +9 points of expectancy while copying nobody at
all. **Ranking wallets by win rate produces a leaderboard of people who like
favourites.** Every wallet is scored by `alpha_vs_band` — its return against
every *other* wallet in the same band and week.

---

## The six controls that make results interpretable

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

**5. The gates are audited, not trusted.** `pqv3 invert` scores every blocking
condition four ways on the same rows — as signalled, inverted (buying the
COMPLEMENT contract, a real instrument with an exact payoff), stood aside, and
a coin flip — then reports whether the block was correct, too strict, or
carries information pointing the other way. Outcomes are never edited, costs
are charged on both sides, and each side is compared against its own price band
so an inversion cannot score well merely by turning longshots into favourites.

**6. Alpha is measured against a price-band-matched baseline.** Returns are
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
- **The gates were previously refusing everything for a definitional reason.**
  16 validated strategies sat in the store while `decide()` was called with
  `strategy={}`, so the two research gates failed on every candidate by
  construction. Fixed: `scanner/signals.py` fires validated strategies over the
  wallet observations they were discovered on. Candidates are now refused for
  substantive reasons — edge inside the cost floor, negative EV after costs —
  which is the real answer to "why zero trades".
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
