# Requirements audit — what is built, what is partial, what is not

Checked against the master prompt, module by module, on 2026-08-24. Written to
be checkable: where something is partial or absent, it says so and says why.

Legend: **DONE** · **PARTIAL** · **NOT BUILT**

---

## Non-negotiable rules (1–30)

| # | rule | status | evidence |
|---|---|---|---|
| 1–3 | never overwrite / always separate V2 / never delete original | **DONE** | `git status Polymarket-Bot-DAVID/` empty; `test_v2_never_writes_to_the_v1_installation` inspects every `sqlite3.connect` by AST |
| 4 | B never silently blocked by A | **DONE** | `gates.assert_may_block` raises; `test_isolation.py` (4 tests) |
| 5–6 | never hide a rejected signal / always log exact reason | **DONE** | `SignalRecord.advance` refuses a terminal state with no gate key; `Funnel.assert_balanced` |
| 7 | never use future information | **DONE** | `substrate/state.py` settlement heap; `test_causality.py` (7 tests, incl. a case a naive impl passes). **One violation was found and fixed during this audit** — see "Bugs found" below |
| 8–11 | no profit claims / not win-rate or trade-count optimised | **DONE** | `_score()` is multi-factor; expectancy not win rate throughout |
| 12 | never blindly increase size | **DONE** | `expand()` can only reduce; 6 preconditions, each naming its blocker |
| 13 | never blindly loosen filters | **DONE** | no threshold was loosened; the diagnosis says loosening would have changed nothing |
| 14–15 | account for losses / fees / realistic execution | **DONE** | `risk/execution.py`, `cost_sensitivity`, UNFILLED accounting |
| 16 | always validate out-of-sample | **DONE** | strict time split; `oos_split_ts` |
| 17 | control drawdown and risk of ruin | **DONE** | `hard_stop_drawdown`, `drawdown_derisk_at`, `stats.risk_of_ruin` |
| 18–19 | capital safeguards / compounding | **DONE** | `risk/compounding.py`, `Account.check()` invariant |
| 20 | distinguish strategy quality from portfolio rejection | **DONE** | separate ledger stages and counters |
| 21 | keep A and B statistics separate | **PARTIAL** | separate counters exist and render; **route A is never populated live** — see gap G1 |
| 22 | always preserve Python fallback for Rust | **DONE** | `test_accel_equivalence.py` fallback tests run without a toolchain |
| 23 | measure before claiming optimisation | **DONE** | `docs/PERFORMANCE.md`; no Rust speedup is claimed because none was built |
| 24 | AI never overrides evidence | **DONE** | `status` always `PROPOSED`; `test_llm_output_cannot_arrive_pre_validated` |
| 25 | never promote an overfit backtest | **DONE** | ladder: OVERFIT/FRAGILE/DRIFT/CONCENTRATED/UNSTABLE gates |
| 26–27 | don't modify original for trade count / find the real bottleneck first | **DONE** | the bottleneck was found before anything was changed |
| 28 | RN1 is a reference, not truth | **DONE** | measured RN1's OOS edge as **negative** and reported it |
| 29 | search other wallets for recurring behaviour | **DONE** | `similarity.strategic_agreement` on `params_only_hash` |
| 30 | prefer robust expectancy over fragile statistics | **DONE** | wallet alpha + BH + block bootstrap |

## Implementation steps 1–43

| steps | area | status |
|---|---|---|
| 1–12 | inspect, map architecture / A / wallet / market / entry / exit / risk / execution / rejection paths | **DONE** — `docs/MAPPING.md` |
| 13 | profile the current system | **PARTIAL** — V2's own hot loop is profiled (`docs/PERFORMANCE.md`); the V1 engine was **not** profiled in-process, because it never trades and its cost is dominated by a gate that returns immediately |
| 14–15 | why so few trades / instrument the pipeline | **DONE** — the answer is one gate; `ledger.py` |
| 16 | preserve Strategy A | **DONE** — untouched, `PRESERVED_UNTRADED` |
| 17–19 | independent Strategy B / reconstruct + deconstruct RN1 | **DONE** |
| 20–22 | similarity, wallet search, cross-wallet families | **DONE** — and the honest result is *nothing transfers yet* |
| 23–26 | backtest, OOS, walk-forward, robustness/stress | **DONE** — `walk_forward`, `perturbation`, `block_bootstrap_ci`, `placebo_p`, `cost_sensitivity` |
| 27 | settlement vs early-exit research | **DONE** — `research/exits.py`, with modelled-vs-exact confidence |
| 28 | winner/loser asymmetry | **DONE** — `research/winners.py`, `pqv2 winners` |
| 29–31 | Win Expansion / dynamic sizing / compounding | **DONE** — `risk/sizing.py`, `risk/compounding.py` |
| 32 | portfolio capital allocation | **DONE** — `risk/portfolio.py` |
| 33 | realistic execution modelling | **PARTIAL** — fees, slippage, delay, unpriceable fills, price drift are modelled; **depth, partial fills, latency and market impact are not, because no historical order book exists**. Reported as `DepthState.UNKNOWN`, never as OK |
| 34–38 | profile, move CPU work to Rust, equivalence, shadow, benchmark | **PARTIAL by design** — crate is complete and buildable, equivalence + shadow + fallback are implemented and tested, but **the extension is not built and `should_build()` says not to**. No Rust benchmark is claimed |
| 39 | full historical testing | **DONE** — 124,440 hypotheses, 40 wallets |
| 40 | paper / live-shadow testing | **PARTIAL** — historical shadow replay works (`pqv2 shadow`); **no real-time paper loop** — see gap G2 |
| 41 | compare A vs B independently | **PARTIAL** — the comparison renders, but A has no trades to compare, and the diagnostic says exactly that rather than inventing a winner |
| 42 | compare RN1 vs other families | **DONE** |
| 43 | promote only on predefined criteria | **DONE** — ladder is the sole authority |

## The 22 diagnostic questions

**All 22 answered from measured data.** `python -m pqv2 diagnose`. Question 18
correctly answers "None — nothing has transferred", which is the honest state
of the evidence rather than a manufactured winner.

## Dashboard fields

All funnel, P&L, expectancy, win/loss, drawdown, equity, compounded return,
rejection, risk/portfolio/execution, Win Expansion, sizing and correlation
fields render. **TOP REGIMES is absent** — see gap G3.

---

## Known gaps, stated plainly

**G1 — Strategy A generates no live signals inside V2.**
The adapter is a read-only observer of V1's journals, so route A's live
counters are structurally zero and the A-vs-B comparison has nothing to compare.
This was deliberate — importing and driving 85,535 lines of V1 in-process is the
main way to breach rules 1–3 by accident — but it means "Strategy A signals/day"
is not measurable from V2 today. Closing it means an IPC bridge or running V1
with its own ledger writer.

**G2 — There is no real-time paper loop.**
`pqv2 shadow` replays history through the full live pipeline, which validates
the code paths but is not the same as running against a live feed. Steps 40 and
the LIVE mode path are implemented and gated, but LIVE deliberately refuses to
self-promote and no live adapter is wired.

**G3 — Regime analysis is not a module.**
Walk-forward folds catch temporal instability, and `UNSTABLE` fires on it. But
there is no explicit regime classifier, so "which regimes support this strategy"
is answered only implicitly. `research/ai.py` proposes the experiment; nothing
runs it.

**G4 — Strategy lifecycle is manual.**
`RESEARCH → CANDIDATE → VALIDATING → VALIDATED → PRODUCTION → MONITORING →
RETIRED` is defined and `Registry.promote()` records transitions with evidence,
but **nothing calls it automatically**. Controlled promotion cycles are an
operator action today, not a scheduled process. This is the safe direction, but
it is not what "continuous learning with controlled promotion cycles" fully
asks for.

**G5 — Rust is unbuilt, by rule.**
Complete, buildable, equivalence-tested, shadow-mode-ready — and off. See
`docs/PERFORMANCE.md` for the trigger that would change that.

**G6 — Several questions are unanswerable from this data at all.**
Depth, partial fills, early exits at known prices, and point-in-time wallet
track record. `docs/LIMITS.md` enumerates them with what would fix each.

---

## Bugs found while auditing, and fixed

1. **Rule-7 violation in `compare_sizing_modes`.** The `edge` staking mode read
   `f.ret` — the outcome of the trade it was sizing. In practice the expression
   collapsed to a constant so the reported numbers were not corrupted, but it
   was look-ahead in the code path and would have become live look-ahead the
   moment anyone made it do real work. Rewritten to size on expectancy over
   *already-closed* fills, with a warmup. Two regression tests added
   (`test_sizing_modes_are_causal`, `test_edge_sizing_uses_only_prior_outcomes`).

2. **Winner/loser decomposition was unreachable.** `research/winners.py` was
   implemented and tested but wired to no CLI command, so a required
   deliverable (step 28) could not actually be run. Added `pqv2 winners`.

3. *(during build)* Funnel under-counted `SIGNAL_RECEIVED`, and portfolio share
   caps could never be satisfied by the first trade — a bootstrap stall of the
   same class as V1's deadlock. Both caught by `assert_balanced()`.

**Test suite: 110 passed, 3 skipped** (the skips are Rust equivalence tests,
which require a toolchain).
