# The earlier V2, and the finding it contributed

## What happened

A previous session had already begun a `polymarket_quant_v2/` package —
`gatemap.py`, `funnel.py`, `router.py`, `audit.py`, a CLI and two test files,
about 2,150 lines. When this build started, those files were absent from the
working tree while still present in git at `HEAD` (`4b3dd40`), and the current
package was written over the same directory before that was noticed.

**Nothing was lost.** Every file has been restored from git to
[`prior_v2/`](../prior_v2/) and is preserved verbatim. It is not imported by
the current package and does not run; it is kept because it is good work, it
documents its own reasoning well, and one of its findings is the most
actionable fact in this whole report.

If you would rather have the earlier design back, it is intact and one
`git show` away.

## What the two designs agreed on, independently

The earlier package reached the same architectural conclusions this one did,
arriving at them separately:

| concept | earlier V2 | this build |
|---|---|---|
| gate ownership registry | `gatemap.py` — owner, source, pattern, evidence | `gates.py` — owner + evidence, enforced by `assert_may_block` |
| opportunity ledger | `funnel.py` — `Opportunity`, terminal states | `ledger.py` — `SignalRecord`, terminal states, `assert_balanced` |
| independent route B | `router.py` — shadow by default | `strategy_b/engine.py` — `Mode.SHADOW` by default |
| read-only audit of V1 | `audit.py` | `strategy_a/adapter.py` |

Two independent analyses reaching the same structure is worth something. It is
the strongest evidence available that the diagnosis is right.

`gatemap.py` also carries something this build does not: a `file:line` source
map for each V1 gate, so a classification claim can be checked against the
emitting line. That is worth porting.

## The finding this build had missed

The earlier `audit.py` recorded:

> Strategy B (wallet-strategy-lab) — 20,748 hypotheses tested across 12
> wallets, 2 VALIDATED with out-of-sample p-values of 5.7e-174 and 2.7e-4.
> **Connected to the bot: NO. Zero references in the whole package.**

Independently confirmed here:

```
Polymarket-Bot-DATA/state/walletlab/experiments.sqlite3
  INSUFFICIENT_EVIDENCE 20 · FAILED 15 · NOT_SIGNIFICANT 14
  OVERFIT 3 · VALIDATED 2

grep -rl walletlab pqb/ ploymarketbot/   ->   no matches
```

So the account sat parked in learning mode *"until discovery produces a
validated strategy"* while discovery had already produced two — in a different
file that nothing opened.

This is now surfaced automatically by `python -m pqv2 audit`
(`strategy_a.adapter.orphaned_evidence`), so it cannot go unnoticed again.

## Do not act on those two strategies yet

The earlier report framed them as ready to connect. Measurement here says wait:

1. **Both sit in the favourite band.** `[0.70, 0.98]` and `[0.50, 0.98]` —
   precisely where this dataset's favourite–longshot bias is **+8.8 to +8.9
   points**. A price-band rule there earns ~+20% while copying nobody.

2. **The wallet-alpha control appears not to have fired.** No experiment in
   that database carries status `NO_WALLET_ALPHA`, though `walletlab`'s own
   README documents it. Either it was not applied on that pass or nothing
   tripped it. V2 applies it unconditionally.

3. **Concentration.** One reports a **1.00 win rate over 152 fills in 8
   markets**. A perfect record across so few markets is the signature of
   concentration, not skill.

4. **The two engines disagree about both wallets, sharply.**

   | wallet | walletlab test expectancy | V2 out-of-sample naive copy |
   |---|---:|---:|
   | `0x629da223adfc…` | **+0.2082** | **−0.3282** |
   | `0x84cfffc3f16d…` | **+0.2003** | **−0.9340** |

   Reproduce the right-hand column with `python -m pqv2 dashboard`
   (naive-copy baselines table).

   These are not small disagreements. On `0x84cfffc3f16d…`, walletlab reports a
   validated +20% edge with a 1.00 win rate; V2 measures a naive copy of the
   same wallet losing **93% of capital** over 38 out-of-sample fills.

   V2 splits the tape strictly by **time** — everything after the split
   timestamp is untouchable. If walletlab's split is by row, the same market
   can appear on both sides and the result stops being out-of-sample.

Point 4 matters most, and it is decisive rather than cautionary. **Both engines
cannot be right.** The resolution is a few hours of work, not a judgement call,
and until it is done neither of those two strategies should be connected to
anything.

## Recommended reconciliation

1. Read `prior_v2/pqv2/gatemap.py` and port its `file:line` source map into
   `pqv2/gates.py` — checkable claims beat assertable ones.
2. Determine how `walletlab` splits train/valid/test. If by row rather than by
   time, its two VALIDATED results need re-running.
3. Re-run those two specs through the V2 ladder, which applies wallet alpha,
   a pass-wide BH threshold, and a strict time split.
4. Only then decide whether to connect them — and to **paper**, never straight
   to live.
