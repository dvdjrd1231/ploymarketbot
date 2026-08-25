# Handover — Polymarket Quant Engine V2

For David · built 2026-08-24 · verified against the live data set.

---

## The headline, in one paragraph

The existing engine's zero-trade problem is **not** a filter problem. All
40,820 decisions it has ever made are `DO_NOTHING`, and all 40,820 carry one
reason: *learning mode — no validated strategy exists*. That gate sits above
every other entry gate, so the market-state, depth, spread and EV filters
**were never reached in production**; loosening them would have changed
nothing. Learning mode never opens because the research pipeline validates
against 3.8 days and 123 markets while **90 days and 1,285 markets sit in the
same database file**. Meanwhile, a second program in the same repository
(`wallet-strategy-lab`) has already validated two strategies — and nothing in
the bot reads them.

Everything else in this delivery follows from those two facts.

---

## What you have

A new project, `polymarket_quant_v2/`, sitting **beside** the existing one.

**The original installation is untouched.** V2 opens its databases read-only
and writes only under `polymarket_quant_v2/var/`. This is enforced by a test
that inspects every `sqlite3.connect` call in the package, not by convention.

- ~7,500 lines of Python, **standard library only** — no dependencies
- **158 tests**, offline, ~5 seconds, no database required
- A real, buildable Rust crate (not currently needed — see below)
- Nine documents under `docs/`, plus a one-click `INSTALL.bat`

---

## Run it

**Double-click `INSTALL.bat`.** It checks Python, runs the tests, verifies data
access, then runs the whole cycle and prints the dashboard and the 22-question
diagnostic. About five minutes.

Or by hand:

```bash
cd polymarket_quant_v2
python -m pytest tests/ -q       # 158 passed, 3 skipped
python -m pqv2 selftest          # confirms the real database is reachable
```

Then, in order — this is the tour that tells the story:

```bash
python -m pqv2 audit         # where the existing engine's opportunities go
python -m pqv2 reconcile --demo  # reconciliation exit safety, before/after
python -m pqv2 inventory     # how much evidence actually exists
python -m pqv2 features      # which features are inert, and what that costs
python -m pqv2 discover -v   # discovery + validation  (~90 s, 40 wallets)
python -m pqv2 gui           # visual dashboard (or double-click DASHBOARD.vbs)
python -m pqv2 dashboard     # the same, as terminal text
python -m pqv2 diagnose      # the 22 mandatory questions, answered from data
```

---

## The five things worth acting on

**1. Two VALIDATED strategies exist that nothing reads — but do not connect
them yet.**
`walletlab` validated two strategies; the engine reads a different database and
never opens theirs. However, V2 measures the same two wallets as strongly
*negative* out of sample (−0.33 and −0.93 on naive copy, against their reported
+0.21 and +0.20). The two engines split the tape differently — V2 splits
strictly by time. **Both cannot be right.** Resolving this is a few hours of
work and it gates everything else. See [`docs/PRIOR-WORK.md`](docs/PRIOR-WORK.md).

**2. `resolutions.settled_ts` is 0 in all 8,116 rows — fix this first.**
Eight wallet-state features are consequently *single-valued across every
observation*. Four of the eight search axes are inert, so the sweep tests 5,184
transformations per wallet of which 432 are distinct, and pays the
multiple-testing cost of all 5,184 — making the significance threshold ~12×
stricter than the evidence requires. One column at ingest fixes it, and it is a
bigger effective win than any amount of Rust.

**3. Nothing has been shown to transfer between wallets yet.**
36 strategies validated, every one on exactly one wallet. Rules are *positive*
on 16 of 19 wallets but *validated* on one. The next lever is **more wallets**
(211 are eligible; 40 were swept), not more rules.

**4. Wallet lifetimes are short, and it cost 40% of the sample.**
16 of 40 wallets had zero observations on one side of the time split — they
exist entirely within one window. A per-wallet rolling-origin split would
recover them.

**5. Rust is not the constraint.**
Profiling found 1.3M `str.split` calls — 18% of runtime building rejection text
the search never reads. One function later, throughput went **103 → 287
evaluations/sec (2.8×)** and the full pass runs in 88 seconds. The crate ships
complete and buildable; `accel.should_build()` states the trigger as a rule,
and none of the three thresholds has fired. Making a data-limited search faster
raises the false-discovery rate; it does not find edge.

---

## The architecture, briefly

Two genuinely independent routes that meet only at the portfolio layer:

```
   STRATEGY A                    STRATEGY B
existing engine              RN1 / wallet engine
own filters, own ladder      own filters, own ladder
        └──────────┬──────────────────┘
                   ▼
        PORTFOLIO / RISK LAYER  →  EXECUTION
```

Three guarantees, each enforced by a test rather than a promise:

1. **Strategy B is never blocked by a Strategy A gate.** Every rule that can
   stop a trade is registered with an owner; rejecting on the wrong route
   raises. Only `GLOBAL_SAFETY` gates bind both, and each must carry written
   evidence — a global gate without evidence is a Strategy A gate in disguise,
   and the suite fails the build if one appears.

2. **No signal disappears without a reason.** Every opportunity ends in exactly
   one terminal state carrying the gate that stopped it, and
   `Funnel.assert_balanced()` raises if the arithmetic does not close. It
   raised twice during development and caught two real bugs.

3. **Only the ladder promotes.** Nothing validates because it made money once,
   resembles RN1, survived an attack, or an AI liked it. Asserted by AST
   inspection.

Plus the control that makes any of it interpretable: **wallet alpha**. This
data set has a large favourite–longshot bias (+8.8 points at 0.60–0.70, +8.9 at
0.70–0.80), so "buy favourites" earns ~+20% while copying nobody. Every
candidate is scored against the same price band and week across all *other*
wallets; zero alpha means `NO_WALLET_ALPHA` and it cannot promote, regardless
of profit. **This control exists nowhere in the existing engine** — without it,
a price-band search across 40 wallets reports 40 "independent validated
strategies" that are all one market-wide effect, and it would look like the
best result the project has ever produced.

---

## What this does NOT claim

- Nothing here has traded real money.
- `VALIDATED` authorises **paper trading only**. Going live is a human
  decision this code never makes.
- Strategy A has never executed a trade, so V2 neither credits nor blames it.
  It is marked `PRESERVED_UNTRADED`, unchanged and unmodified.
- Several things the brief asks for **cannot be answered from this data at
  all** — early exits, depth, partial fills, and point-in-time wallet track
  record. [`docs/LIMITS.md`](docs/LIMITS.md) states which, why, and what would
  fix each.

**No claim of guaranteed profit is made anywhere in this system, and none
should be inferred from any number it produces.**

---

## Where to read next

| document | what it answers |
|---|---|
| [`docs/FINDINGS.md`](docs/FINDINGS.md) | what the first full pass found, with reproducing commands |
| [`docs/MAPPING.md`](docs/MAPPING.md) | the existing system mapped; where its opportunities go, with SQL |
| [`docs/PRIOR-WORK.md`](docs/PRIOR-WORK.md) | the earlier V2, and the orphaned-strategies finding |
| [`docs/LIMITS.md`](docs/LIMITS.md) | what cannot be answered from this data, and why |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | how it is built and what each guarantee costs |
| [`docs/RECONCILIATION-PATCH.md`](docs/RECONCILIATION-PATCH.md) | the exit-safety patch: the defect, the fix, before/after |
| [`docs/PERFORMANCE.md`](docs/PERFORMANCE.md) | the profile, the 2.8× fix, and when Rust becomes worth building |

---

## Recommended order of work

0. Resolve the walletlab time-split disagreement (#1 above).
1. Populate `resolutions.settled_ts` at ingest (#2).
2. Supply RN1's real address if known externally — `pqv2 rn1 --wallet 0x…`.
   An externally-named wallet costs no statistical power; a data-selected one
   is a hypothesis test and is charged to the budget.
3. Sweep all 211 eligible wallets, not 40 (#3).
4. Add a per-wallet rolling-origin split (#4).
5. Capture live order-book snapshots — the only way depth, spread and early
   exits become answerable.
6. Retune `risk.max_wallet_share` once wallet breadth increases.
7. Build the Rust extension only when a trigger in `should_build()` fires.
