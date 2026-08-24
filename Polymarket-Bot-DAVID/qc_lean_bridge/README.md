# Autonomous Non-Print Quantitative Research Platform

A local, low-footprint Windows platform that turns **Interactive Brokers Time &
Sales** into **statistically validated, prop-firm-compliant trading strategies** —
and keeps doing it, on its own.

It is not a trading bot. It is a repeatable research *pipeline* that
continuously **generates, validates, ranks, deploys, monitors, and retrains**
strategies from proprietary Non-Print market-structure data.

```
IBKR Time & Sales
      │
      ▼
┌─────────────────────────── Non-Print Data Engine ───────────────────────────┐
│  Bid Non-Print Engine   Ask Non-Print Engine     100 Line-Break Engines      │
│  (independent state)    (independent state)      (1..100 ticks / line)       │
└──────────────┬───────────────────┬──────────────────────┬───────────────────┘
               ▼                    ▼                       ▼
        structural events      research features       line events
               └───────────────────┴───────────────────────┘
                                   ▼
                     Replayable event database (SQLite / TimescaleDB)
                                   ▼
                     Standardized research dataset (CSV + manifest + docs)
                                   ▼
┌──────────────────────────── QuantConnect LEAN Bridge ───────────────────────┐
│  features → discovery → validation → ranking → generated LEAN algorithms     │
└──────────────────────────────────┬──────────────────────────────────────────┘
                                    ▼
            ┌──────────── OpenClaw AI (master controller) ────────────┐
            │  compare vs incumbent → deploy → monitor → retrain      │
            └───────────────┬──────────────────────┬──────────────────┘
                            ▼                       ▼
                  Live Strategy Manager     Prop Firm Risk Manager
                  (deploy / degrade /       (gate on EVERY order:
                   auto-replace)             min-hold, DD, size, …)
                            └───────────┬───────────┘
                                        ▼
                            REST + WebSocket control plane
```

Everything runs **locally on Windows**, async and event-driven, engineered for
**low RAM and low CPU** (see *Performance* below). Every module is
independently testable and replaceable.

---

## The eight modules (maps 1:1 to the brief)

| # | Module | File(s) | What it does |
|---|--------|---------|--------------|
| 1 | **OpenClaw AI** (master controller) | `core/openclaw.py` | Runs and *verifies* every stage, remembers all strategies, compares new vs live, deploys only what passed, detects degradation, triggers retraining, enforces prop rules, writes reports. |
| 2 | **Non-Print Data Engine** | `core/tick_source.py`, `core/nonprint_engine.py` | IBKR Time & Sales (`ib_insync`) → independent **Bid** and **Ask** engines detecting non-print events, liquidity voids, skipped prices, structural gaps, void persistence, structural velocity/acceleration. Incremental, O(1) per tick. |
| 3 | **Market Structure Engine** | `core/linebreak_engine.py` | **100 independent Line Break engines**, resolution 1..100 ticks/line, each with its own structural state — so research can find which resolution currently carries the edge. |
| 4 | **Feature Engineering** | `core/structure_features.py`, `core/features.py` | Builds and **documents** every research feature (the 14 named ones plus supporting structure), exported with a data dictionary. |
| 5 | **LEAN Research** | `core/pipeline.py`, `core/strategy_discovery.py`, `core/optimizers.py`, `core/lean_export.py` | Consumes the exported dataset: feature selection → guided strategy search (random / grid / genetic) → generated QuantConnect algorithms. |
| 6 | **Validation & Risk** | `core/validators.py`, `core/ranking.py`, `core/metrics.py` | Walk-forward, Monte Carlo, out-of-sample, parameter stability, drawdown, Sharpe/Sortino/PF/win-rate/expectancy/recovery. Rejects weak strategies automatically. |
| 7 | **Live Strategy Manager** | `core/live_manager.py` | Deploys approved strategies, monitors drawdown/win-rate/expectancy/degradation, disables deteriorating strategies, promotes stronger validated ones. |
| 8 | **Prop Firm Risk Manager** | `core/prop_firm.py`, `core/risk_management.py` | Enforces account rules **before every order**: daily profit goal/cap, max & EOD drawdown, position size, **min hold time**, scaling, consistency rule. |

Supporting: `core/event_store.py` (replayable DB), `core/events.py` (async bus),
`core/ingest.py` (the engine runner), `core/research_export.py` (dataset hand-off),
`core/strategy_registry.py` (strategy history), `core/api_server.py` (REST + WS),
`core/logging_setup.py` (structured logging).

---

## Quick start

**Windows:** double-click **`Run QC LEAN Bridge.bat`** (installs deps on first
run, launches the GUI via signed Python so Smart App Control allows it).

**Manually:**

```bash
cd qc_lean_bridge
pip install -r requirements.txt        # PyQt6, pandas, numpy, PyYAML
python app.py                          # GUI
```

On a fresh machine with **no IBKR, no Postgres, and no market data**, the
platform still runs end to end: it generates a realistic synthetic tick stream,
runs the engines, exports a dataset, researches it, and can deploy to a paper
live manager. Swap in real IBKR data by starting TWS/IB Gateway (`ib_insync`
installed) and setting `ingest.source: ibkr` — nothing else changes.

### Prove it works right now

```bash
python app.py --selftest     # engines → export → research → LEAN code → prop gate
```

A green self-test exercises the *real* platform path end to end.

---

## Command-line (every stage is independently runnable)

```bash
python app.py --ingest [N]     # Module 2/3: IBKR T&S → engines → DB → dataset CSVs
python app.py --replay         # rebuild engine state from stored ticks (replay proof)
python app.py --cycle          # Module 1: one full OpenClaw research cycle
python app.py --auto           # fully autonomous: cycle + live + API + retrain
python app.py --serve          # REST + WebSocket control plane only
python app.py --live           # live strategy manager only
python app.py --research        # the LEAN bridge only (on existing CSVs)
python app.py --selftest        # end-to-end check, exits 0/1
python app.py --validate        # data + bridge compatibility check
python app.py --benchmark [N]   # throughput / memory baseline
```

---

## Configuration — one file drives everything

`config/default_config.yaml` is the single source of truth (the GUI edits it).
Sections map to modules: `ingest`, `ibkr`, `nonprint`, `structure`, `database`,
`openclaw`, `live`, `api`, then the existing `dataset`, `instrument`,
`prop_constraints`, `discovery`, `validation`, `ranking`. Researching a new
dataset is a config change, never a code change.

### Prop-firm constraints (enforced in backtest, live gate, AND generated LEAN)

| Rule | Default | Where enforced |
|------|---------|----------------|
| Min hold time | 10 s | backtest, live order gate, LEAN |
| Daily profit cap / target | $1,500 / $3,000 | live gate, LEAN |
| Account max drawdown | 1.5% | live gate (halt), LEAN |
| EOD trailing drawdown | $2,000 | live gate (day halt), LEAN |
| Max position size | 5 contracts (10:1 micro) | live gate, LEAN |
| Consistency | 50% min win rate | validation reject, live disable |

All editable in the GUI's Configuration tab.

---

## REST + WebSocket API

Dependency-free (pure `asyncio`), localhost by default (`api.port: 8765`).

```
GET  /api/status                 controller state, cycle, uptime, RAM
GET  /api/strategies             every strategy in the registry
GET  /api/strategies/{id}        one strategy + live performance history
GET  /api/live                   live strategies + prop-firm account state
GET  /api/engine                 engine stats (ticks, voids, lines, DB counts)
GET  /api/cycles                 research cycle audit trail
POST /api/cycle                  run a research cycle now
POST /api/strategies/{id}/deploy | /disable
WS   /ws?topics=structural,feature,live,control,log[,tick]   live push
```

---

## Deploying a generated strategy to QuantConnect

Each `strategies/<ID>/` folder is self-contained: `main.py` (runnable LEAN
algorithm using a real future for execution + a `PythonData` feed of the
structural features for signals, prop rules baked into `OnData`),
`feature_data.csv`, `strategy.json`, and `STRATEGY_REPORT.md`. Create a QC
project, upload the CSV, add `main.py`, set the contract/dates, backtest to
confirm parity, then paper/forward test.

---

## Performance (low RAM, low CPU by design)

Measured on 200,000 synthetic MES ticks through **all 102 engines** (2 non-print
+ 100 line-break) plus feature engineering and SQLite persistence, this machine:

- **~16,500 ticks/sec** end to end
- **~95 MB** peak RAM — flat regardless of run length (bounded pruning, batched
  DB writes, drop-oldest event queues)

Key engineering choices behind that: the 100 line-break engines run as parallel
numpy state arrays with cached trigger prices (a no-op tick is one vectorized
compare, not 100 Python calls); non-print state is pruned to a window around the
market; DB inserts are batched one transaction per 5k rows; the event bus drops
for slow consumers instead of growing.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the full design, data contracts, and
the notable correctness fixes made during integration.

---

## Documentation

- **[ARCHITECTURE.md](ARCHITECTURE.md)** — module design, data contracts, the
  event model, and how the pieces connect.
- **[WORKFLOW.md](WORKFLOW.md)** — the end-to-end research loop.
- **[USER_GUIDE.md](USER_GUIDE.md)** — click-by-click GUI manual.
- **`HOW-TO-RUN.txt`** — the short version.
- **`sample_data/exports/FEATURE_DICTIONARY.md`** — auto-generated definition of
  every exported feature column.

## Notes

- Pure-`numpy` statistics — no SciPy required.
- Optional deps (`ib_insync`, `psycopg2`, `psutil`) are detected at runtime; the
  platform degrades gracefully without them and the frozen `.exe` needs none.
- Works with PyQt6 or PyQt5 (auto-detected).
