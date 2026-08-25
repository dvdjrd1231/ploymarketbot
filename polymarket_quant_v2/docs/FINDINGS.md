# Findings from the first full pass

Run on 2026-08-24 against the client's own `intel.sqlite3`.

```
python -m pqv2 discover --max-wallets 40 -v
40 wallets · 124,440 hypotheses · 87.9 s
```

Reproduce any number here with the command shown beside it.

---

## 1. The existing engine's zero-trade problem is not a filter problem

`python -m pqv2 audit`

All **40,820** journalled decisions are `DO_NOTHING`, and all 40,820 carry one
reason: *learning mode — no validated strategy exists*. That gate sits above
every other entry gate, so the market-state, depth, spread and EV filters were
**never reached in production**.

Learning mode never opens because the library holds 234 strategies — 170
rejected, 49 validating, 13 new, 2 quarantined, **0 validated** — and that in
turn is because the research pipeline validates against **78,219 rows / 123
markets / 3.8 days** while **116,923 rows / 1,285 markets / 90 days** sit in
the same file.

**Loosening the entry filters would have changed nothing.** Full derivation:
[`MAPPING.md`](MAPPING.md).

---

## 1b. Two VALIDATED strategies already exist, and nothing reads them

`python -m pqv2 audit`

Found by an earlier V2 effort (preserved under [`prior_v2/`](../prior_v2/), see
[`PRIOR-WORK.md`](PRIOR-WORK.md)) and independently confirmed here.

`wallet-strategy-lab` has run a full pass and validated two strategies:

```
Polymarket-Bot-DATA/state/walletlab/experiments.sqlite3
  INSUFFICIENT_EVIDENCE 20 · FAILED 15 · NOT_SIGNIFICANT 14
  OVERFIT 3 · VALIDATED 2

grep -rl walletlab pqb/ ploymarketbot/   ->   no matches
```

The trading engine reads `library.sqlite3`. There is **not one reference to
`walletlab` anywhere in `pqb/` or `ploymarketbot/`**. So the account was parked
in learning mode *"until discovery produces a validated strategy"* while
discovery had already produced two, in a file nothing opened.

**Do not connect them yet.** All the caveats are in
[`PRIOR-WORK.md`](PRIOR-WORK.md); the decisive one is that for wallet
`0x629da223adfc…`, walletlab reports test expectancy **+0.208** while V2's
out-of-sample naive copy of the same wallet is **−0.3282**. V2 splits the tape
strictly by time. That disagreement is answerable and should be answered before
either number is trusted.

---

## 2. Four of eight search axes are inert, costing ~12× multiple-testing power

`python -m pqv2 features`

| feature | distinct values |
|---|---:|
| `w_settled_n`, `w_win_rate`, `w_roi`, `w_roll_win_rate`, `w_roll_roi`, `w_edge_t`, `w_consec_losses`, `w_consec_wins` | **1** |

Every wallet-state feature is **single-valued across all 30,000 observations
tested**. Not weak — constant.

The cause is `resolutions.settled_ts = 0` in all 8,116 rows. Outcomes can only
arrive at *observation* time, so no trade has any settled track record behind
it when it is placed. `pit_evidence_share` is 0.00 for every wallet.

Consequence: the sweep tests **5,184 transformations per wallet of which 432
are distinct**, and pays the multiple-testing cost of all 5,184 — making the
Benjamini–Hochberg threshold roughly **12× stricter than the evidence
requires**, for zero coverage benefit.

**This is visible in the pass output.** The validated list contains exact
duplicate pairs:

```
0.796  0xe8e086f7ef  exp +0.3622  copy when price 0.70-0.98, enter +300s
0.796  0xe8e086f7ef  exp +0.3622  copy when price 0.70-0.98, <= 3 losses in a row, ...
```

Identical expectancy, identical fills, different spec hash — because
`max_consec_losses ≤ 3` is a no-op when `consec_losses` is always 0. The
duplication is the inert-axis finding confirming itself empirically.

> **Highest-value fix in the project: populate `settled_ts` at ingest.** One
> column. It would enable four search axes, make every "follow this wallet
> while it is hot" hypothesis testable for the first time, and loosen the
> significance threshold ~12× — a larger effective speedup than Rust, achieved
> by making the search *smaller*.

---

## 3. Nothing has been shown to transfer between wallets

`python -m pqv2 diagnose` (question 18)

36 strategies reached `VALIDATED`. Every one of them validated on **exactly one
wallet**:

```
cross-wallet agreement
  validated on 1, positive on 16/19  alpha +0.0451  price 0.70-0.98, enter +60s
  validated on 1, positive on 10/13  alpha +0.0633  price 0.70-0.98, enter +300s
  validated on 1, positive on 14/19  alpha +0.0285  price 0.70-0.98, enter +0s
```

Rules are *positive* on many wallets (16 of 19) but *validated* on one. Positive
on many is weak evidence — mean wallet alpha of +0.03 to +0.06 is a real but
small excess over the market-wide bias.

**This is the honest headline.** The strongest evidence this architecture can
produce is a rule that survives on several independent wallets, and that has
not happened yet. The 36 validated strategies cluster on two wallets and remain
vulnerable to the wallet having been chosen first.

**Next lever: widen the wallet universe, not the rule grid.** 211 wallets carry
≥60 settled trades; only 40 were swept.

---

## 4. Wallet lifetimes are short, and it cost 16 of 40 wallets

An unplanned finding, and a structural one.

```
0xdc41c39b95:   0 in-sample /  796 out-of-sample - skipped, cannot be split
0x6dfbb9fd37: 440 in-sample /    0 out-of-sample - skipped, cannot be split
0x47138dc1ee:   9 in-sample /  487 out-of-sample - skipped, cannot be split
... 16 of 40 wallets skipped
```

Most high-volume wallets exist entirely inside one side of the 70/30 time
split. They are short-lived bursts of activity, not persistent operators.

Implications, and none of them are code fixes:

- **Effective sample is ~24 wallets, not 40.** Raising `--max-wallets` recovers
  fewer wallets than it appears to.
- **"Follow a proven wallet" is weakly supported by this venue.** A wallet that
  trades for three weeks and stops cannot build the track record such a
  strategy needs — this compounds finding #2 rather than being separate from
  it.
- A **rolling-origin split** per wallet would recover some of these and is the
  obvious next engineering step. It was not done here because a per-wallet
  split makes the pass-wide BH threshold harder to define honestly, and that
  trade-off deserves a decision rather than a default.

---

## 5. The reference wallet's edge did not survive out of sample

`python -m pqv2 rn1` · `python -m pqv2 exits`

No RN1 address was supplied, so the engine selected one from data, on the
in-sample window only, and charged the selection (211 wallets) to the
multiple-testing budget.

Selected: `0x629da223adfc…`

| window | expectancy |
|---|---:|
| in-sample (how it was selected) | **+0.2118** |
| out-of-sample, naive copy | **−0.3282** |

A complete reversal. This is the selection effect the design predicted, caught
by the mechanism built to catch it. Under every exit model tested the wallet
stays negative out of sample:

```
target +30%    -0.0899   (modelled)
trail 30%      -0.1086   (modelled)
settlement     -0.3282   (exact)
```

Early exits reduce the loss; none rescues it. `target +30%` beats settlement by
+0.2382 — but it is a **modelled** result against an **exact** one, and the
system says so on every row rather than reporting the winner alone.

> **If you have RN1's real address from outside this dataset, supply it:**
> `python -m pqv2 rn1 --wallet 0x…`
> An externally-nominated wallet costs no statistical power and is strictly
> stronger evidence than anything selected here.

---

## 6. The favourite–longshot bias is large and is being controlled

Measured over all 116,923 settled trades:

| price band | n | mean price | actual win rate | gap |
|---|---:|---:|---:|---:|
| 0.10–0.20 | 21,014 | 0.148 | 0.083 | **−0.065** |
| 0.30–0.40 | 11,212 | 0.346 | 0.267 | **−0.079** |
| 0.60–0.70 | 12,215 | 0.650 | 0.739 | **+0.088** |
| 0.70–0.80 | 14,565 | 0.748 | 0.837 | **+0.089** |

"Buy favourites" earns roughly +20% expectancy while copying nobody. Without
the wallet-alpha control, a price-band search across 40 wallets would report
~40 "independent validated strategies" that are all this one effect — and it
would look like the best result the project has ever produced.

With the control on, the 36 validated strategies show wallet alpha of **+0.25
to +0.32**, measured against the same price band and week across all *other*
wallets, at 100% coverage. That is a genuine measured excess. It is also
measured over 9–26 markets in a 27-day window, which is why it is `VALIDATED`
(paper) and not `PRODUCTION`.

**This control does not exist anywhere in the V1 engine.**

---

## 7. The pipeline runs, and every lost opportunity is accounted for

`python -m pqv2 shadow` · `python -m pqv2 diagnose`

```
opportunities              3,147
signals                   19,616
  behaviour rejected       2,224   b.behavior_match      [STRATEGY_B]
  conditions rejected     13,208   b.conditions          [STRATEGY_B]
  risk rejected                0
  portfolio rejected       4,174   p.wallet_share (3,904)[PORTFOLIO_RISK]
  execution attempted         10
  execution failed             4   x.unpriced            [EXECUTION]
  execution successful         6
```

The funnel reconciles exactly — `Funnel.assert_balanced()` raises otherwise,
and it did raise twice during development, catching a real accounting bug and a
portfolio bootstrap stall.

**A tuning finding, correctly attributed:** `p.wallet_share` (cap 0.35) removed
3,904 of 4,184 risk-approved signals. Copying a handful of wallets means the
book fills with those wallets, so the cap binds almost immediately. That is
**capital management, not strategy quality** — the distinction the brief asks
for, and the reason the two are counted separately. Raising
`risk.max_wallet_share`, or spreading across more wallets, is the lever; it
changes nothing about whether the strategies are good.

Six completed trades is far too few to conclude anything about expectancy, and
the diagnostic says so rather than reporting −0.49 as a result.

---

## 8. Performance: the first real bottleneck was Python string formatting

`docs/PERFORMANCE.md`

Profiling the sweep found **1.3 million `str.split` calls** — 18% of runtime
building rejection-reason text that the search never reads. Splitting the
predicate into an explain path and a fast path took throughput from **103 to
287 candidate-evaluations/second (2.8×)**, verified equivalent across 150
strategies × 60 observations.

The full 40-wallet pass now takes **87.9 seconds**.

**No Rust build is warranted yet**, and `accel.should_build()` states the rule:
none of the three triggers (>10M tape rows, >100k hypotheses/wallet, >60 min
pass) has fired. The crate ships complete and buildable; the constraint on this
system is evidence, not compute.

---

## Recommended order of work

0. **Resolve the walletlab disagreement** (finding #1b). Two VALIDATED
   strategies exist and nothing reads them, but V2 measures one of the same
   wallets as strongly negative out-of-sample. Determining which split is
   correct is a few hours' work and gates everything downstream.
1. **Populate `resolutions.settled_ts` at ingest** (finding #2). One column;
   unlocks four search axes and ~12× of multiple-testing power. Nothing else
   comes close.
2. **Supply RN1's real address** if it is known externally (finding #5).
3. **Sweep all 211 eligible wallets**, not 40 — transfer evidence is the goal
   and it needs breadth (finding #3).
4. **Implement a per-wallet rolling-origin split** to recover the 40% of
   wallets currently unusable (finding #4).
5. **Capture live order-book snapshots.** The only way to make depth, spread
   and early exits answerable at all ([`LIMITS.md`](LIMITS.md) #2).
6. **Retune `max_wallet_share`** once wallet breadth increases (finding #7).
7. **Rust: only when a trigger in `should_build()` fires.**

---

## What has not been demonstrated

- Nothing here has traded real money.
- `VALIDATED` authorises **paper trading only**; going live is a human decision.
- No rule has transferred across independent wallets, which is the evidence
  standard this architecture was built to reach.
- Strategy A remains `PRESERVED_UNTRADED` — never modified, never deleted,
  neither credited nor blamed.

**No claim of guaranteed profit is made, and none should be inferred.**
