# Surgical patch v2 — the consistency / loss-minimisation layer

## What this is

A second exit layer that sits *behind* the strategy, not inside it.

The record that motivated it: take-profit exits were 100% winners at +30.8%
average, stop exits were 0% winners at -46.3% average. The engine producing the
first number is the asset. The loss tail is the liability. So this patch does
not touch entries, sizing, take-profit, the stop, trailing, the edge exit, the
wallet exit, or discovery — it adds one layer that can only ever look at a
position the strategy has already decided to **hold**, and ask whether the
reason for being in the trade still exists.

    Layer 1  the existing strategy         — unchanged, always decides first
    Layer 2  thesis / account safety exit  — only ever converts a HOLD

That ordering is enforced by the shape of `BaselineDecisionEngine._evaluate_position`,
not by discipline inside Layer 2: Layer 2 is only called when Layer 1 returned
HOLD, so there is no code path by which it can displace a take-profit.

## The distinction it exists to make

A position that is **red** is not a position that is **wrong**.

`THESIS_HEALTH` is computed from the conditions the entry actually recorded —
its score, the wallet evidence that drove it, the liquidity and spread it was
scored against, the price band it qualified in, the market state it was taken
under — checked against those same conditions now. **The P&L is not an input.**
A losing position with intact conditions reads HEALTHY; a winning position with
failed conditions reads INVALIDATED. That is the only mechanical way to tell
normal adverse movement inside a winner's path from genuine failure.

States are `HEALTHY` / `WEAKENING` / `INVALIDATED` / `UNKNOWN`. `UNKNOWN` is a
real answer — a position whose entry predates the patch has no stored thesis —
and it never triggers anything.

## Nothing fires by default

| setting | default | effect |
|---|---|---|
| `mode` | `shadow` | records the verdict, changes no decision |
| `min_adverse_room_pct` | `0.0` | hard-blocks the thesis exit even in `enforce` |
| `loss_tail_enabled` | `false` | account-protection guard off |
| `profit_floor_arm_pct` | `0.0` | profit locking off |

The two numbers that would need a distribution to set honestly — how much room
a real winner needs, and the excursion at which profit is worth protecting —
default to *disabled*, not to a guess. They are outputs of the research
command. Nothing in this patch was fitted to the 16-trade sample, and nothing
can be promoted on it.

## Running the study

    pqb consistency                 # the report
    pqb consistency --json --write  # and state/consistency.json

It reports, in this order:

1. **Baseline** — what actually happened, whole distribution.
2. **Protected growth** (§23) — peak, current, retained-from-peak, top five
   winners and losers, and what share of all profit the top five winners are.
3. **Winner room** (§6) — the MAE distribution of trades that *won*, and for
   each candidate stop distance, how many historical winners it would have
   killed and how much profit that destroys. This is the most important table
   in the report.
4. **Separation** (§2) — at which horizon (+30s … +2h) eventual winners begin
   to look different from eventual losers, measured, not assumed. If the
   distributions overlap at every horizon it says so.
5. **Candidates** (§5) — seven loss-control concepts (existing / time /
   thesis / volatility / market-state / wallet / hybrid), each replayed
   against the captured price and feature history, walk-forward validated,
   scored, and marked SHADOW or ENABLED.
6. **Shadow review** (§20) — every exit the live layer proposed, against what
   the trade actually went on to do: loss avoided *minus* profit sacrificed.
7. **Three-system comparison** (§24–25) — A / B / C, and a KEEP B / ADOPT C
   decision.

## How a rule gets promoted

`promotion_verdict` checks all seven of §21's requirements independently and
refuses unless every one passes:

1. out-of-sample expectancy improvement **or** meaningful drawdown reduction
2. no material destruction of profitable trades (≥90% of baseline avg winner)
3. catastrophic-loss exposure no worse
4. stable in ≥3 folds and ≥60% of them
5. ≥30 replayable trades
6. no reachable future data
7. ≤3 parameters

Promotion is then **a human action**: set `engine.consistency.mode: "enforce"`
and `min_adverse_room_pct` to the measured 90th-percentile winner MAE. The
research module cannot change a running bot, by construction.

### What the scorer optimises

Expectancy, profit factor, drawdown, loss-tail and catastrophic-loss
improvement, winner preservation, and out-of-sample stability — minus
complexity, small-sample, instability and winner-damage penalties.

**Win rate is not in the score at all.** It is reported everywhere and ranks
nothing. §17's example (60% / +$100 → 80% / −$20) is rejected by the win-rate
guard and by the promotion gate independently.

## What it writes

- `lifecycles.thesis_state` / `thesis_streak` / `thesis_ts` — the health
  reading and its confirmation count, so the streak survives a restart.
  Added by an automatic migration on existing journals.
- `consistency_shadow` — one row the **first** time the layer would have
  exited a position, write-once per (lifecycle, style), which is what makes
  "what happened after it wanted out" answerable.

## Known limits, stated rather than papered over

- **No Rust accelerator exists in this checkout.** §29's instruction is to use
  one rather than write Python loops; where none exists the intent is not to
  build a second architecture, so the study reuses the existing analytics
  shape — each token's series read once and cached, each trade's path
  materialised once and shared by every candidate and fold.
- **Candidates are additions to the existing exit set, not replacements.** A
  trade a candidate does not fire on keeps the outcome the existing exits gave
  it. That is what Layer 2 does in production, so it is what is measured.
- **System B cannot be replayed.** The surgical risk patch is an entry-side
  control; on a record of trades that were already opened it changes nothing,
  so B is reported as identical to A and the reason is printed. Its real
  effect — which trades exist at all — is measurable only forward.
- **The score condition is not replayable.** Layer 1's live hold conviction is
  not captured per row, so the backtest of the thesis rule runs with that one
  condition inert. The other five replay against real captured values.
- **Trades with no captured series cannot be replayed.** They are counted and
  named in `coverage`, never dropped silently.
- **Drawdown is realised, in trade order.** A mark-to-market drawdown
  including open positions would need marks this data does not have.

## What it does not claim

It cannot produce constant wins and does not try to. The objective is higher
expectancy with a smaller loss tail and the large winners left intact — not a
higher win rate, which is a different and much easier thing to achieve badly.
