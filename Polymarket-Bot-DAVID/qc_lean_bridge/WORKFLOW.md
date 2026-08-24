# Complete Workflow — Engine → Bridge → QuantConnect

This is the **end-to-end** guide: how the research **Engine**, this **Bridge**
app, and **QuantConnect LEAN** connect into one repeatable loop.

> **Update:** the platform now INCLUDES the upstream engine — the Non-Print
> Bid/Ask engines and the 100 Line Break engines (`core/nonprint_engine.py`,
> `core/linebreak_engine.py`), fed by IBKR Time & Sales (`core/tick_source.py`)
> and persisted to a replayable database. The Engine → Bridge hand-off that this
> document once left as **〔FILL IN〕** is implemented in `core/research_export.py`
> and runs automatically inside an OpenClaw cycle (`python app.py --cycle`). See
> **[README.md](README.md)** and **[ARCHITECTURE.md](ARCHITECTURE.md)** for the
> authoritative, current description; the sections below remain useful for
> understanding the LEAN-facing half of the loop.

---

## The big picture

```
 ┌─────────────────────────┐
 │  STAGE 1                 │   Interactive Brokers Time & Sales
 │  YOUR RESEARCH ENGINE    │   ─────────────────────────────────►
 │  (Non-Print Bid/Ask +    │   processes into proprietary
 │  Multi-Res Line Break)   │   market-structure data
 └────────────┬────────────┘
              │  exports
              ▼
 ┌─────────────────────────┐
 │  STAGE 2                 │   bid_nonprint_structure.csv
 │  CSV EXPORT FILES        │   ask_nonprint_structure.csv     ◄── THE HANDOFF
 │  (the "data contract")   │   multi_resolution_linebreak.csv     (how it connects)
 └────────────┬────────────┘
              │  loaded by
              ▼
 ┌─────────────────────────┐
 │  STAGE 3-6               │   Import → Features → Discover →
 │  QC LEAN BRIDGE (this    │   Validate → Rank → Export
 │  app)                    │
 └────────────┬────────────┘
              │  produces
              ▼
 ┌─────────────────────────┐
 │  STAGE 7                 │   strategies\<ID>\main.py  + feature_data.csv
 │  QUANTCONNECT LEAN       │   backtest → paper → live in prop account
 └─────────────────────────┘
```

The **CSV files in Stage 2 are the only connection point.** As long as your
Engine writes CSVs in the agreed format, everything downstream is automatic and
nothing in the app needs changing when you produce new data.

---

## STAGE 1 — Run your Engine and capture data

**Goal:** turn raw market activity into your structural datasets.

1. 〔FILL IN〕 Capture / feed the Interactive Brokers Time & Sales data into your
   Engine the way you normally do.
2. 〔FILL IN〕 Run your Engine to build the Non-Print Bid/Ask structures and the
   Multi-Resolution Line Break structures (resolutions 1–100).
   - *Example placeholder:* `python run_engine.py --date 2026-07-03`
3. Note the folder where your Engine writes its output.

> This stage is entirely your existing process. The Bridge does not touch it —
> it only reads the CSVs your Engine produces.

---

## STAGE 2 — The CSV handoff (the data contract)

This is the piece that makes everything "connect." Your Engine's exports must
follow a simple, consistent shape. **Get this right once and the whole pipeline
just works forever after.**

### The rules
- Put all the CSV exports for one research run in **one folder**.
- Every `.csv` in that folder is loaded and **merged together on the timestamp**,
  so the Bid engine, Ask engine, and each line-break file can be **separate
  files** — they line up by time automatically.
- **Each file must contain:**
  - a **timestamp column** (default name: `timestamp`), and
  - for at least one file, a **price / level column** (default name: `price`) —
    the traded price used for profit/loss.
- **Every other numeric column** is treated automatically as a **structural
  metric** and fed into feature engineering. You don't register them anywhere —
  add or remove columns freely between runs.
- Columns containing "bid" / "ask" in their name are auto-tagged so the Bid/Ask
  imbalance features build themselves.
- Timestamps can be **irregularly spaced** (line-break bars are event-driven) —
  that's expected and handled.

### What a valid file looks like

`bid_nonprint_structure.csv`
```
timestamp,price,bid_structure_state,bid_compression,bid_void_metric,bid_event_count,bid_velocity
2023-01-03 09:30:18,5000.49,0,1.0767,0.1690,0,0.094
2023-01-03 09:30:33,5000.27,6,1.0767,0.1224,0,0.094
2023-01-03 09:30:59,4998.99,5,1.0767,0.1257,1,0.094
```

`multi_resolution_linebreak.csv`
```
timestamp,lb_res1_dir,lb_res1_persistence,lb_res5_dir,lb_res5_persistence, ... lb_res100_persistence
2023-01-03 09:30:18,-1,-1.5,-1,-1.5, ... 1.25
```

`ask_nonprint_structure.csv` — same idea, with your Ask-side columns.

> These three sample files ship with the app (in `sample_data\exports`). Open
> them to see the exact expected layout, then have your Engine export in the
> same shape.

### If your Engine names things differently
No problem — you have two options:
- **Tell the Bridge your names:** in the app's **Configuration**, set the
  timestamp and price column names to match your Engine's headers, and Save.
- **Or map it once:** send me one real export CSV (or a screenshot of its
  headers) and I'll set the mapping in the config permanently, so you never
  touch it.

〔FILL IN〕 *Your Engine's actual column names → we'll confirm the mapping here.*

---

## STAGE 3 — Load the CSVs into the Bridge

1. Open the app (`QC-LEAN-Bridge.exe`, or `Run QC LEAN Bridge.bat` if Windows
   blocks the .exe).
2. Left panel → **Data Source** → **"Browse export folder…"** → pick the folder
   your Engine wrote in Stage 1.
3. Go to the **Configuration** tab → **"Save configuration"**.

That's the entire connection step. (Alternatively, drop the CSVs into
`sample_data\exports`, replacing the samples.)

---

## STAGE 4 — Run the research

Click **`Run Full Pipeline`** on the left. The app runs all six phases:

1. **Load Structural Data** — imports & validates your CSVs.
2. **Engineer Features** — builds research features from your metrics.
3. **Discover Strategies** — searches thousands of rule combinations.
4. **Validate** — in/out-of-sample, walk-forward, Monte Carlo, robustness.
5. **Rank** — sorts survivors by quality.
6. **Export LEAN Code** — writes QuantConnect files for the best ones.

Watch the **Status** list, progress bar, and **Log** tab. A full run takes a few
minutes depending on data size.

*(You can also click the six buttons individually to go step-by-step.)*

---

## STAGE 5 — Review and pick strategies

Open the **Results** tab:
- Top line shows how many strategies were found and how many were **accepted**
  (passed every quality gate).
- The table ranks them best-first (Sharpe, Sortino, profit factor, win rate,
  drawdown, out-of-sample, walk-forward, Monte Carlo, stability).
- **Click a row** for the full plain-English rules, stats, validation evidence,
  and prop-rule compliance.

> Most candidates being rejected is normal and healthy — the strict tests are
> what keep you from deploying something that only worked by luck.

Before running, set your prop-firm rules on the **Configuration** tab
(min hold seconds, daily profit cap, per-trade & account drawdown, max
contracts, consistency). These are enforced in testing **and** baked into the
exported code.

---

## STAGE 6 — Export the QuantConnect code

Exporting (via **Run Full Pipeline**, **Export LEAN Code**, or **Export selected
strategy**) writes, for each accepted strategy, a folder under **`strategies\`**:

```
strategies\S0031\
    main.py             ← the QuantConnect LEAN algorithm (ready to run)
    feature_data.csv    ← the signal feed it reads
    STRATEGY_REPORT.md  ← rules + performance + validation, in plain text
    strategy.json       ← machine-readable rule definition
```

Plus a `reports\` folder with `ranking_summary.csv` and `EXPORT_SUMMARY.md`.

---

## STAGE 7 — Deploy in QuantConnect

1. Create a new QuantConnect project.
2. Upload **`feature_data.csv`** (Object Store or the project data folder).
3. Add **`main.py`**.
4. In `main.py` → `Initialize`, set the correct futures contract and start/end
   dates.
5. **Backtest** in QuantConnect to confirm it matches the Bridge's results.
6. **Paper / forward test**, then deploy to your prop account.

The exported algorithm trades a real futures contract for execution, uses your
structural feature feed for signals, and enforces every prop-firm rule you set.

---

## The repeatable loop (do this each time)

```
new market data ─► run Engine ─► export CSVs ─► Browse to folder in Bridge
     ─► Run Full Pipeline ─► review Results ─► export ─► QuantConnect ─► deploy
```

**Nothing in the app changes between runs.** New data in, new validated
strategies out.

---

## What I need to finalize this

To replace the 〔FILL IN〕 spots with your exact commands and column names, send me:

1. **How you run your Engine** — the command or the click-steps, and the folder
   it writes CSVs to.
2. **One real sample export** from your Engine (any small CSV), or a screenshot
   of its column headers — so I can confirm/lock the timestamp and price mapping.
3. **Your current prop-firm rule numbers**, if different from what's in the app
   now.

With those, I'll finalize this into an exact, no-blanks guide tailored to your
setup.
