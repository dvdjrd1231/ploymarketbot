# Polymarket Quant Bridge

The Prop Firm Quant Bridge, given a Polymarket brain. The Quant Bridge does the
analysis and decides; this project gives it eyes and hands on Polymarket.

```
POLYMARKET API
    ↓
BROAD INGESTION                  every wallet observed, identity preserved
    ↓
NORMALISATION                    models.WalletTrade
    ↓
FEATURE GENERATION               pqb/features.py · pqb/analytics/features.py
    ↓
QUANT BRIDGE ANALYSIS            pqb/bridge/lean_engine.py  ← discovered rules
  + DYNAMIC RANKING / ANOMALIES  pqb/analytics/ranking.py · anomalies.py
    ↓
BUY / SELL / HOLD / REDUCE / EXIT / DO NOTHING
    ↓
POLYMARKET EXECUTION ADAPTER     pqb/adapters/execution_adapter.py
    ↓
POLYMARKET API
    ↓
OUTCOME FEEDBACK                 journal → pqb/analytics/feedback.py → decisions
```

It is **not** a copy trader, and it does not follow a configured wallet list.
Every wallet that trades is observed and kept; the system ranks them itself from
measured results, and a wallet's activity is weighted evidence the engine may
follow, partly follow, or override — never an instruction.

---

## Read this first

**The brain is the Prop Firm Quant Bridge** (`qc_lean_bridge`), reached through
one path shim, [`pqb/quant.py`](pqb/quant.py). Rules are not written by hand:
Polymarket features are captured live, exported into the CSV shape the bridge's
`DataPipeline` already reads, and put through its existing discovery →
walk-forward → Monte-Carlo → ranking pipeline. What survives is what the live
engine evaluates.

```bash
python -m pqb.cli research      # discover rules from captured features
python -m pqb.cli wallets       # the ranking, as the system derived it
python -m pqb.cli anomalies     # what the detectors found, with evidence
```

The bridge is located automatically at `../Advanced Strategy Development for
QuantConnect LEAN/qc_lean_bridge`, or wherever `PQB_QUANT_BRIDGE_PATH` points.
If it is absent the system still runs: `pqb.cli research` reports why, and the
engine falls back to baseline market-quality scoring and says so in every
decision's rationale.

The seam is still one method — [`pqb/bridge/ports.py`](pqb/bridge/ports.py) —
so a different analysis layer can replace this one by changing a single config
line.

| Document | What it covers |
|---|---|
| **[docs/VPS-GUIDE.md](docs/VPS-GUIDE.md)** | **Fresh server → supervised live trade.** Sizing, the three-repo layout, install, operating, troubleshooting |
| [docs/REUSE-MAP.md](docs/REUSE-MAP.md) | Every capability → the component reused and the gap |
| [docs/INTEGRATION-PLAN.md](docs/INTEGRATION-PLAN.md) | How the bridge plugs in, phase status, open questions |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Reused vs newly added, design decisions, what is not done |

## Funding: USDC on Polygon

Polymarket settles in **USDC on Polygon** (chain 137). USDC held on Ethereum
mainnet must be **bridged to Polygon first** — an unbridged wallet reads as a
zero balance and every order fails with nothing in the error naming the cause.
`pqb.cli check` prints this whenever the config is armed for live trading.

## Key custody

The private key is read from the environment **on the machine where the signer
runs** and nowhere else. It is never transmitted off that machine, never written
to a log, commit, or config file, and no API path returns it. Whoever controls
the host and its credentials effectively controls the wallet — so the wallet
owner should own the host.

---

## Setup

Python 3.10+. The Polymarket clients are reused from the sibling
[`ploymarketbot`](../ploymarketbot) project, so the simplest route is to reuse
its virtualenv:

```bash
# from this directory
../ploymarketbot/.venv/Scripts/python -m pip install pyyaml     # Windows
../ploymarketbot/.venv/bin/python     -m pip install pyyaml     # Linux/macOS
```

Or install standalone:

```bash
python -m venv .venv && .venv/Scripts/activate
pip install -r requirements.txt
pip install -r ../ploymarketbot/requirements.txt
```

`ploymarketbot` must sit next to this project, or `PQB_POLYMARKETBOT_PATH` must
point at it.

```bash
cp config/config.example.yaml config/config.yaml
cp config/.env.example .env          # only needed for live trading
python -m pqb.cli check              # validates everything, no network calls
```

---

## Run

```bash
python -m pqb.cli run                # dry-run (the default)
python -m pqb.cli run --cycles 20    # bounded session, then exit
python -m pqb.cli run --dry-run      # force dry-run whatever the config says
```

Then, in another shell:

```bash
python -m pqb.cli status             # doubling state, open positions, last reconcile
python -m pqb.cli report             # what the journal says has actually worked
```

Logs go to `state/pqb.log` and stdout, one `event=` record per line:

```
event=decision action=EXIT token=1178052066 score=0.31 style=stop reason="Stop hit: -26.4% vs -25% limit."
event=position.closed token=1178052066 price=0.37 shares=48.65 pnl=-5.35 style=stop
event=reconcile.clean checked=6 live=False
```

### Dry-run vs live

Dry-run runs the entire pipeline — data in, decisions made, sizing validated,
journal written — and simulates the fill instead of sending it. Fills respect
visible book depth, so a thin market fills partially on paper exactly as it
would in reality.

Live trading needs **both** flags, so one careless edit cannot arm it:

```yaml
mode:
  dry_run: false
  allow_live: true
```

...plus `PQB_PRIVATE_KEY` in `.env`. Start with `engine.portfolio.min_order_usdc`
at the exchange minimum and a small `max_position_fraction`.

### Stopping trading

Two files can close the gate. Both take effect within one cycle, need no
redeploy, work from any shell, survive your SSH session dropping, and leave an
audit trail.

```bash
python -m pqb.cli kill               # halt new orders; positions untouched
python -m pqb.cli kill --flatten     # ...and close every open position
python -m pqb.cli resume             # clear the kill switch
python -m pqb.cli resume --force     # also clear a reconciliation halt
```

- **`state/KILL`** — yours. Arming it also cancels all resting orders.
- **`state/HALT`** — engaged automatically when reconciliation finds the
  exchange and the bridge disagree. It **never flattens**: closing out is still
  trading, and a halt means the bridge does not currently know what it holds.

While either is in force the engine keeps evaluating and journalling — you can
see what it *wanted* to do — but nothing reaches the exchange.

A halt persists across restarts on purpose: a divergence nobody has looked at
must not be cleared by bouncing the process. `resume` prints what diverged and
refuses to clear it until you pass `--force`.

---

## What the engine is given

Every cycle, per tracked market: best bid/ask, spread, depth, mid, last trade,
liquidity, 24h and cumulative volume, recent prints, tick size, min order size,
resolution time and time remaining, and status (active/closed/resolved). Per
open position: size, average entry, live mark, cost, value, unrealised P&L,
return, peak and drawdown-from-peak, and time to resolution.

Plus the analytical layer's output: every observed wallet's derived profile
(rank, score, confidence, win rate, average edge, sizing habits, cohort
membership), per-market capital flow measured against that market's own
baseline, and this cycle's anomalies.

All of it arrives as one flat, fixed feature vector
([`pqb/features.py`](pqb/features.py)) — **the same function that writes the
research export builds the live row**, so a rule discovered on
`wallet_cohort_entries` or `anomaly_convergence` evaluates against exactly the
column it was validated on.

Quote provenance travels with every price (`stream`, `rest`, or `none`) so the
engine can tell a live book from a polled one and never trades on a quote it
knows is stale.

## Broad ingestion, then the system ranks wallets itself

Nothing is restricted to a configured shortlist. Two sweeps run every cycle —
deep per tracked market, shallow across the whole exchange — and **every wallet
seen trading is recorded with its identity, timestamp, market, side, price and
size**. A first run typically observes well over a thousand wallets with an
empty `wallets:` list.

Ranking is then derived, not asserted. Each wallet's trades are scored against
what the outcome turned out to be worth — the settlement price where the market
has resolved, the live mark where it has not — and the resulting record is
shrunk toward the population mean in proportion to how little evidence stands
behind it. A wallet with two lucky trades does not top the board, and it is not
ranked at all until it has enough settled history to order.

The top-N cohort is a **label on a wallet, not a filter on the feed**. Every
observed wallet reaches the engine with its full profile, which is precisely
what makes a signal from outside the cohort discoverable.

Pinning a wallet in `wallets:` seeds a label and an influence floor while its
record is thin. It does not affect the ranking: an operator's interest in a
wallet is not evidence about it.

## Anomaly detection

Six detectors, one per pattern the brief names, each comparing a subject
against **its own** baseline rather than a global constant — because a $5,000
trade is enormous for one wallet and routine for another.

| Kind | What it finds |
|---|---|
| `size_spike` | a wallet betting far outside its own usual size |
| `lead_lag` | a wallet repeatedly entering before better-ranked ones |
| `convergence` | several distinct wallets converging on one outcome, quickly |
| `behaviour_drift` | a wallet unlike its own history in size, cadence or focus |
| `market_flow` | a market taking unusual capital against its own hourly baseline |
| `combo_edge` | a wallet × category × timing combination with a settled record |

Every detection is persisted with the numbers behind it, so
`python -m pqb.cli anomalies` can show what was found and when. Each kind gets
its own feature column, so which of them actually predict anything is something
strategy discovery measures rather than something this README asserts.

## Exit management

The highest-priority capability: every open position is adjudicated every cycle,
before any entry is considered. In precedence order — flatten in progress,
market resolved, stop loss, trailing drawdown (armed only once sufficiently in
profit), max hold time, take profit, wallet exit, partial reduce (once per
position), time-to-resolution policy — otherwise hold. Every threshold is
config, none is a constant in the code.

When a tracked wallet exits, the engine compares its own conviction against
`wallet_exit_override_score`: below it, the exit is followed; above it, the exit
is **overridden** and the position held. Both branches are journalled with the
reasoning.

That bar then **moves with the evidence**. Once both arms have enough closed
positions, the journal can say which one actually paid, and the threshold shifts
toward it — capped at ±0.15, and only when both arms have a sample, because one
arm alone says nothing about the other.

Reading that comparison correctly is subtler than it looks: following a wallet
*closes* the position, so it lands in the journal as `exit_style='wallet'`, but
**overriding one is a HOLD** — the position stays open and eventually closes for
some entirely different reason, leaving no trace on the lifecycle. Comparing
exit styles alone would score every followed exit against an arm that is
permanently empty and conclude that following is the only thing ever tried. The
override arm is reached through the decision that made it.

## Learning from results

The journal records the whole lifecycle, and that record feeds back into
decisions rather than only into a report a person reads. Closed positions are
grouped by exit style, category, liquidity regime and time-to-resolution, and
each group's realised return produces a small, bounded adjustment to future
scores for candidates with the same tags.

Three properties keep it honest. Groups are **shrunk** toward the book's own
mean, so three trades in a category is not evidence about that category. The
total tilt is **bounded** (±0.10 of score), because a feedback loop that can
move a score arbitrarily far will eventually find a spurious pattern and trade
it with total conviction. And only **realised** results vote — an unrealised
gain is a price, not a result.

Groups are also judged against the book's mean rather than against zero: a
category returning +2% in a book averaging +5% is an underperformer, and scoring
it positively would reward the worst of what we do for merely being profitable.

## Portfolio-doubling rule

At 2× the active baseline: close everything, advance the minimum trade size to
the next step of the 50-value progression, re-base, resume. Implemented as an
explicit `NORMAL → FLATTENING → NORMAL` machine
([`pqb/doubling.py`](pqb/doubling.py)) so the awkward cases are handled by
construction: a partially filled mass close leaves the rule in `FLATTENING` and
the size does not advance; an in-flight order is allowed to settle first; the
index clamps at the end of the progression; and the baseline and index persist,
so a restart resumes mid-progression instead of starting over.

## Reconciliation

**The live Polymarket account is the source of truth.** On startup and then
every loop, the journal's open lifecycles are compared against what the exchange
reports — *before* any decision is executed, because a divergence found
afterwards has already been traded on.

On a critical mismatch the divergence is recorded, the exchange's figures are
adopted so stored state is not stale, an ERROR-level alert is raised, and
**trading halts** until an operator clears it. Critical means the bridge's
belief about what it *holds* is wrong: a position missing on the exchange, one
it holds that the journal never saw, or a size that has drifted. Informational
findings (a resting order, when this engine only places Fill-And-Kill) are
alerted and journalled but do not halt on their own.

A fill reaches the CLOB before the Data API reports the position, so a
just-opened position is exempt for `settle_grace_seconds` (default 120).
Without that window every single entry would trip the halt seconds after it
filled — the difference between a safety feature and an outage.

## Backups

The journal is not just history: it holds the doubling-rule baseline and
progression index and every open-position lifecycle. Losing it rewinds the trade
size to 0.19 and turns every open position into an "unknown position" at the
next reconciliation.

```bash
sudo bash scripts/backup.sh                  # consistent hot copy via sqlite3 .backup
sudo bash scripts/restore.sh <archive.tar.gz>
```

Run it nightly from cron and copy the archive off the server. Test a restore
before you need one — `APP_DIR=/tmp/pqb-restore-test bash scripts/restore.sh …`.

## Deploying to a VPS

Full walkthrough in **[docs/VPS-GUIDE.md](docs/VPS-GUIDE.md)**. Debian
12+/Ubuntu 22.04+, 2 GB RAM minimum (4 GB recommended). Two options, same code
and same state directory:

**Docker** — the three-repo layout is baked in, so it cannot be got wrong:

```bash
bash deploy/docker/build.sh      # stages all three repos, builds, verifies
docker compose up -d
docker compose run --rm cli status
```

**Native systemd** — journald, timers, systemd hardening:

```bash
sudo bash deploy/install.sh      # idempotent; leaves the service stopped, dry-run
sudo systemctl start pqb
bash scripts/healthcheck.sh
```

The installer builds **one** virtualenv for all three projects — the trading
loop needs the Polymarket clients and the bridge's feature engineering in the
same process, so two environments cannot work. It deliberately does not install
PyQt6: that is for the bridge's desktop GUI and nothing here imports it.

| Unit | What it does |
|---|---|
| `pqb.service` | The trading loop. Dedicated user, `Restart=always`, SIGINT for graceful shutdown, `ProtectSystem=strict` with a single `ReadWritePaths`. |
| `pqb-research.timer` | Nightly strategy discovery (04:10 UTC), niced and memory-capped so it can never starve the trading loop. |
| `pqb-backup.timer` | Nightly backup (03:30 UTC) of the journal, the intel store and the discovered rules. |

`scripts/healthcheck.sh` answers the question `systemctl status` cannot — not
"is it running" but "is it working": is the loop turning, is a halt engaged, is
ingestion producing wallets, are the strategies stale, is the clock synced, is
there disk left. Exits 0/1/2 for HEALTHY/DEGRADED/UNHEALTHY, so it drops into
cron or an uptime monitor unchanged.

The key comes from an `EnvironmentFile` owned by the service user at chmod 600;
it is never in the unit, the repo, or journald.

## The decision journal

`state/journal.sqlite3` records the whole lifecycle — decision → entry → path
(peak, trough, best and worst unrealised) → exit → result — plus every
`HOLD`/`DO_NOTHING`, because a journal of only the trades taken cannot answer
whether passing was right. Each record is tagged with market category, liquidity
regime, time-to-resolution bucket, which wallet influenced it, the exit style,
and the full rationale.

`python -m pqb.cli report` groups on those tags: P&L and win rate by exit style,
by time-to-resolution, by liquidity regime, by category, by wallet — plus a
followed-vs-overridden comparison and a journal integrity check.

---

## Tests

```bash
python -m pytest tests -q
```

252 tests, no network, no plugins beyond pytest: adapter data mapping and trade
normalisation, order size/tick validation, the doubling-rule state machine
including every specified edge case, reconciliation mismatch handling, engine
behaviour (exit precedence, wallet override, one-outcome-per-market, cash
budgeting), wallet scoring and shrinkage, all six anomaly detectors, the
feedback loop's guards, the research export contract, and discovered-rule
evaluation. The Quant-Bridge-dependent tests skip cleanly when it is absent.

## Layout

```
pqb/
├─ upstream.py            the single reference into ploymarketbot
├─ quant.py               the single reference into qc_lean_bridge (the brain)
├─ config.py              config + ${env:} resolution + redaction
├─ models.py              what crosses the adapter ↔ bridge boundary
├─ features.py            THE feature vector — research and live share it
├─ research.py            export → Quant Bridge discovery → cross-token check
├─ journal.py             decision journal + persisted engine state
├─ doubling.py            portfolio-doubling state machine
├─ reconcile.py           exchange-truth reconciliation
├─ runner.py              the cycle loop (the only glue)
├─ cli.py                 check/run/status/kill/resume/report + wallets/
│                         anomalies/research
├─ adapters/
│  ├─ data_adapter.py     inbound: Polymarket → bridge, incl. broad ingestion
│  ├─ execution_adapter.py outbound: decisions → orders, + the paper book
│  └─ sizing.py           tick/min-size/cash validation (pure functions)
├─ analytics/             the analytical layer
│  ├─ store.py            raw observations, rollups, settlements, detections
│  ├─ features.py         wallet/market feature generation
│  ├─ ranking.py          dynamic ranking: shrinkage + staleness decay
│  ├─ anomalies.py        the six detectors
│  ├─ feedback.py         journal outcomes → bounded score tilts
│  └─ pipeline.py         the per-cycle analytical pass
└─ bridge/
   ├─ ports.py            ← the seam
   ├─ lean_engine.py      the Quant Bridge as the brain
   ├─ live_features.py    the bridge's own FeatureEngineer, run live
   └─ baseline_engine.py  fallback scoring + the structural exit ladder
scripts/analyze_journal.py
config/config.example.yaml
tests/
```

## Safety notes

- Ships in dry-run; live needs two flags and an env-var key.
- Credentials come from the environment or `.env` only, are never written to the
  config, and are redacted from every log and report. The key is dropped from
  memory once the exchange client has connected.
- Tick size, minimum order size and available cash are enforced before
  submission, from the market's own metadata.
- An order is never sent twice for the same token in a cycle, and a failed order
  is retried only when the failure provably created nothing.
