# QC LEAN Bridge — User Guide

A step-by-step guide to running the app, from opening it to getting
QuantConnect-ready strategy code out the other end. No coding required.

> **New controls in this version.** The left panel now has, above the six
> research-pipeline buttons:
> - **Data Engine + Export** — runs the Non-Print + 100 Line Break engines on
>   IBKR (or synthetic/replayed) ticks and writes the research dataset. Do this
>   first if you want fresh structural data; then run the pipeline.
> - **Run Full Research Cycle** (OpenClaw) — does everything end to end: ingest →
>   export → research → validate → compare vs the deployed strategy → deploy the
>   winner, and writes a cycle report under `reports/openclaw/`.
>
> You can still run the six numbered pipeline steps by hand exactly as before.
> For headless/automated use and the REST+WebSocket API, see **[README.md](README.md)**.

---

## Contents
1. [What this app does](#1-what-this-app-does)
2. [Opening the app](#2-opening-the-app)
3. [Loading YOUR data](#3-loading-your-data)
4. [Running the research](#4-running-the-research)
5. [Reading the results](#5-reading-the-results)
6. [Prop-firm rules & settings](#6-prop-firm-rules--settings)
7. [Getting a strategy into QuantConnect](#7-getting-a-strategy-into-quantconnect)
8. [Doing it again with new data](#8-doing-it-again-with-new-data)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. What this app does

You give it the CSV exports from your Non-Print Bid/Ask + Multi-Resolution Line
Break engine. It automatically:

1. **Imports** your data,
2. **Builds features** from your structural metrics,
3. **Discovers** thousands of candidate trading strategies,
4. **Validates** them (in/out-of-sample, walk-forward, Monte Carlo, robustness),
5. **Ranks** the survivors, and
6. **Exports** a ready-to-run QuantConnect LEAN algorithm for each good one —
   with your prop-firm rules already built in.

You mostly press one button. The app does the rest.

---

## 2. Opening the app

Pick whichever works on your PC:

- **Single program file** — double-click **`QC-LEAN-Bridge.exe`**.
  It's one self-contained file; copy it anywhere and run it.

- **Launcher (if Windows blocks the .exe)** — double-click
  **`Run QC LEAN Bridge.bat`**. This runs the same app through Python (which
  Windows trusts). The first run installs what it needs (takes a minute and
  needs internet); after that it opens instantly.

> If you double-click the .exe and Windows says *"Smart App Control blocked an
> app…"*, that's a Windows security feature blocking unsigned programs. Use the
> `.bat` launcher instead — it's the same app. (See Troubleshooting.)

When it opens you'll see a control panel on the left and three tabs on the
right: **Results**, **Configuration**, and **Log**.

The very first time, the app loads with built-in **sample data** so you can try
the full run immediately before pointing it at your own files.

---

## 3. Loading YOUR data

This is the only setup step. Two ways — either is fine:

### Option A — point the app at your export folder (recommended)
1. On the left, under **Data Source**, click **"Browse export folder…"**.
2. Select the folder where your engine saves its CSV files.
3. Click **"Save configuration"** (on the **Configuration** tab, or it saves the
   folder for you).

### Option B — drop files into the samples folder
Copy your CSV files into the `sample_data\exports` folder, replacing the sample
files already there.

### What the app expects in the CSVs
- Every `.csv` in the folder is loaded and **merged together on the timestamp**
  (Bid engine, Ask engine, and each line-break resolution can be separate files).
- Each file needs:
  - a **time column** (default name: `timestamp`), and
  - a **price/level column** (default name: `price`).
- **Everything else** — structure states, compression, void, velocity,
  resolution columns, and so on — is picked up **automatically** as research
  input. You don't list them anywhere.
- Irregular time gaps between line-break bars are expected and handled.

> **If your columns are named differently** (e.g. your time column isn't called
> `timestamp`, or price is a "level" column), open the **Configuration** and set
> the names — or just send one sample CSV to your developer and they'll set the
> mapping once so you never touch it.

---

## 4. Running the research

On the left you have the six phases as buttons, plus one big button.

**The easy way:** click **`Run Full Pipeline`**. It runs all six phases in order
and fills the **Results** tab when done. The progress bar and **Status** list
show where it is; the **Log** tab shows a running commentary.

**Running phases one at a time** (optional) — the buttons, in order:
1. **Load Structural Data** — imports and checks your CSVs.
2. **Engineer Features** — builds the research features.
3. **Discover Strategies** — searches many rule combinations.
4. **Validate (robustness)** — stress-tests the best candidates.
5. **Rank Strategies** — sorts them by quality.
6. **Export LEAN Code** — writes QuantConnect files for the top strategies.

Each phase remembers the previous one, so you can stop and inspect between steps.
A big run can take a few minutes depending on data size and settings.

---

## 5. Reading the results

Open the **Results** tab. The line at the top says how many strategies were
found and how many were **accepted** (passed every quality test).

**The table** — one row per strategy, best first. Key columns:

| Column | Meaning (higher is better unless noted) |
| --- | --- |
| `rank` / `score` | Overall quality ranking and composite score |
| `accepted` | `True` = passed every validation gate |
| `sharpe`, `sortino` | Risk-adjusted return (downside-only for Sortino) |
| `profit_factor` | Gross win ÷ gross loss |
| `win_rate` | Share of winning trades (prop consistency rule) |
| `max_dd_pct` | Worst equity drop, % (lower is better) |
| `net_profit`, `trades` | Total profit and number of trades |
| `oos_sharpe` | Sharpe on unseen out-of-sample data |
| `wf_profitable` | Fraction of walk-forward windows that were profitable |
| `mc_profitable` | Fraction of Monte Carlo runs that ended profitable |
| `param_stability` | How stable it stays when settings are nudged |

**Click any row** to see a full plain-English breakdown underneath: the exact
entry/exit rules, all the stats, the validation evidence, and whether it meets
your prop-firm rules.

> It's normal for **most** candidates to be rejected. Strict validation is the
> point — it filters out strategies that only look good by luck. A handful of
> accepted strategies from thousands tested is a healthy result.

**To save one strategy's files:** select its row and click
**"Export selected strategy"**. Or use **Export LEAN Code** / **Run Full
Pipeline** to export the whole top list at once.

---

## 6. Prop-firm rules & settings

Open the **Configuration** tab. Under **Prop Firm Constraints**, set your
program's rules — these are enforced during testing **and** written into the
exported QuantConnect code, so live behavior matches the research:

- **Min hold seconds** — every trade stays open at least this long (e.g. 10).
- **Daily profit cap ($)** — stop taking trades once daily profit hits this.
- **Per-trade max DD (%)** — hard stop-loss limit on any single trade.
- **Account max DD (%)** — halt all trading if account equity drops this far.
- **EOD trailing DD ($)** — stop for the day if intraday equity falls this much.
- **Max contracts** — largest position size allowed.
- **Min win rate (consistency)** — reject strategies below this win rate.

Other useful settings on the same tab:
- **Instrument & Account** — symbol, point value ($ per point), commission,
  starting equity.
- **Strategy Discovery** — how hard to search (`Max candidates`), and how many
  features to scan.
- **Validation Gates** — how strict to be (min trades, min Sharpe, max drawdown,
  and how many top candidates to validate).

Click **"Save configuration"** to apply (this resets results so you can re-run
cleanly). Click **"Reload from file"** to undo unsaved changes.

---

## 7. Getting a strategy into QuantConnect

After exporting, look in the **`strategies`** folder next to the app. Each
accepted strategy gets its own folder (e.g. `strategies\S0031\`) containing:

- **`main.py`** — the QuantConnect LEAN algorithm, ready to run.
- **`feature_data.csv`** — the signal data the algorithm reads.
- **`STRATEGY_REPORT.md`** — full rules + performance + validation, in plain text.
- **`strategy.json`** — the machine-readable rule definition.

There's also a **`reports`** folder with `ranking_summary.csv` (every strategy in
a spreadsheet) and `EXPORT_SUMMARY.md`.

**To run one in QuantConnect:**
1. Create a new project in QuantConnect.
2. Upload **`feature_data.csv`** (Object Store or the project's data folder).
3. Add **`main.py`** to the project.
4. Set the correct contract and start/end dates near the top of `main.py`
   (`Initialize`).
5. Backtest in QuantConnect to confirm it matches, then paper/forward test.

The exported algorithm uses a real futures contract for execution plus your
structural feature feed for signals, with all your prop rules enforced.

---

## 8. Doing it again with new data

This is designed to be repeated whenever your engine produces fresh data:

1. Export new data from your engine.
2. Put it in your data folder (or Browse to it) — **no settings to change**.
3. Click **`Run Full Pipeline`**.
4. Review the new **Results** and export the winners.

Same button, new strategies. Nothing in the app needs editing to handle new
datasets.

---

## 9. Troubleshooting

**"Smart App Control blocked an app…" when opening the .exe**
Windows is blocking the unsigned program and gives no "run anyway" button. Use
**`Run QC LEAN Bridge.bat`** instead — same app, allowed because it runs through
signed Python. (To make the .exe itself pass, it must be signed with a purchased
code-signing certificate.)

**"Failed to load Python DLL …"**
You're using an old folder-style build and copied only the .exe out of its
folder. Use the current **single-file** `QC-LEAN-Bridge.exe` — it's one file
with nothing else to keep alongside it.

**"No CSV files found in …"**
The data folder is empty or wrong. Click **"Browse export folder…"**, pick the
folder with your CSVs, then **Save configuration**.

**"Could not resolve a price column"**
Your price/level column isn't named `price`. Set the correct name in the
Configuration (or ask your developer to map it once).

**"0 accepted" strategies**
That just means nothing passed the strict tests on this data. Options: feed more
data, or on the **Configuration** tab loosen the **Validation Gates** (e.g. lower
`Min Sharpe`, lower `Min trades`) — but looser gates mean less reliable results.

**The window seems frozen during a run**
Big runs take time. Watch the **Log** tab and progress bar — it's working. Let it
finish.

**Something errored and I need details**
Check the **Log** tab in the app, or the file `qc_lean_bridge.log` next to the
program.

---

*Tip: ask your developer to pre-set your data's column mapping once. After that,
your whole workflow is just: Browse to your data → Run Full Pipeline → review →
export.*
