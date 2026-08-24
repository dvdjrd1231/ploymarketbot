# wallet-strategy-lab

Wallet-specific Polymarket strategy discovery, with the multiple-testing and
population controls that make its output interpretable.

Read [`docs/AUDIT.md`](docs/AUDIT.md) first. It explains why this sits beside
`polymarket-quant-bridge` instead of replacing it, and it contains the two
measurements that determined the design.

## Install

Standard library only. Python 3.11+.

```
cd wallet-strategy-lab
python -m pytest tests/ -q          # 14 tests, offline, ~2 s
```

Point it at the data (defaults to the repo's `Polymarket-Bot-DATA` location):

```
set WALLETLAB_DATA_DB=D:\...\Polymarket-Bot-DATA\state\intel.sqlite3
set WALLETLAB_WORK_DIR=D:\...\Polymarket-Bot-DATA\state\walletlab
```

## Commands

```
python -m walletlab inventory                     measure the substrate
python -m walletlab baselines                     naive-copy edge per wallet
python -m walletlab analyze-wallet <address>      deep dive on one wallet
python -m walletlab discover-strategies           the full pass
python -m walletlab leaderboard [--status ...]    what has survived
python -m walletlab live-signals                  VALIDATED strategies only
```

`discover-strategies --max-wallets 12` takes ~40 s and tests ~21,000
hypotheses. All 122 eligible wallets is ~7 minutes.

## The three rules the engine enforces mechanically

**1. No look-ahead.** A trade's outcome enters its wallet's statistics at
`settled_ts`, never at `ts`. Prediction markets pay at resolution, so a win
rate computed over unresolved trades is information nobody had. `state.py`
holds unsettled trades in a heap and folds them in only as the clock passes
them. Asserted by `test_wallet_state_only_counts_settled_outcomes`.

**2. The denominator is always reported.** A sweep tests 1,728 transformations
per wallet. At p<0.05, ~10,500 of 211,000 hypotheses are expected to "win" by
chance. Promotion is gated on a Benjamini-Hochberg threshold computed over the
whole pass, so a p-value can never be quoted without the search that produced
it. Asserted by `test_bh_threshold_tightens_as_hypotheses_grow`.

**3. Wallet alpha, not P&L.** This dataset has a large favourite–longshot bias
(+9 points at 0.6–0.8), so "buy favourites" earns ~+20% expectancy while
copying nobody in particular. Every candidate is scored against the same price
band and time window across all *other* wallets. Zero alpha means the wallet
contributed nothing, and the strategy is recorded `NO_WALLET_ALPHA` regardless
of profit. See `baseline.py`.

A fourth, in the backtest: a delayed copy with no printed price in the window
is `UNFILLED` and earns nothing — never the wallet's own fill price. That one
line is the difference between a real copy backtest and a fictional one.

## Status ladder

```
INSUFFICIENT_EVIDENCE   < 30 out-of-sample fills
FAILED                  negative out-of-sample expectancy
NOT_SIGNIFICANT         did not clear the pass's BH threshold
NO_WALLET_ALPHA         real, but it is market structure, not the wallet
OVERFIT                 fails parameter perturbation, bootstrap or placebo
CONCENTRATED            > 60% of profit from one market
UNSTABLE                positive in < half of walk-forward folds
VALIDATED               survived all of the above  -> paper, not live (S42)
```

Nothing in this engine may promote a strategy for any reason other than
evidence. `VALIDATED` authorises paper trading; going live is a human decision.

## Layout

```
walletlab/
  config.py      paths, cost model, engine version (hashed into every result)
  data.py        settled-trade substrate + PriceTape for delayed execution
  state.py       point-in-time wallet state and the causal feature vector
  strategy.py    CopyStrategy spec, spec_hash, the transformation grid
  backtest.py    execution model, costs, UNFILLED accounting, metrics
  baseline.py    the population control  <- the most important file
  stats.py       Benjamini-Hochberg, bootstrap, placebo
  validate.py    time splits, walk-forward, robustness, score, status ladder
  registry.py    experiment database; never rediscover a failed strategy
  discover.py    the pass that ties it together
  report.py      human-readable output
  cli.py         commands
```
