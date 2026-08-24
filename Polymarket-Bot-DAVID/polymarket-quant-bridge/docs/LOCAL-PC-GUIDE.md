# Running the bot on your own PC

A plain-English guide for testing the Polymarket Quant Bridge on a Windows
computer. No technical knowledge needed — you double-click numbered files in
order.

**It starts in simulation.** It trades with pretend money and cannot spend a
penny of yours until two settings are deliberately changed. You can leave it
running for days in this mode and nothing is at risk.

---

## What you need

- **Windows 10 or 11**
- **Python 3.10 or newer** — free, one download (step 2 below)
- **An internet connection**
- About **15 minutes**, then leave it running

It uses roughly 300–500 MB of memory — about the same as a browser tab or two.

---

## Step 1 — Put the three folders side by side

The bot is one folder, but it needs two companion folders next to it. This is
the single most common thing to get wrong, so do it first.

Put them anywhere you like — Desktop, Documents, a drive — as long as all three
sit **inside the same folder**, like this:

```
Polymarket Bot\
├── polymarket-quant-bridge\      <-- the bot
├── ploymarketbot\                (connects to Polymarket)
└── qc_lean_bridge\               (the Quant Bridge - the brain)
```

Note the spelling of `ploymarketbot` — that is how the folder is named.

If your Quant Bridge is still inside its outer folder called *"Advanced
Strategy Development for QuantConnect LEAN"*, that is fine — the setup finds it
either way.

---

## Step 2 — Install Python (once)

Skip this if you already have Python 3.10+.

1. Go to **https://www.python.org/downloads/**
2. Click the big yellow **Download Python** button.
3. Run the installer.
4. **Tick the box that says "Add python.exe to PATH"** at the bottom of the
   first screen. This matters — without it the setup cannot find Python.
5. Click **Install Now**, then **Close**.

---

## Step 3 — Run the setup

Open:

```
polymarket-quant-bridge\deploy\windows\
```

Double-click **`1-SETUP.bat`**.

A black window opens and works through the setup. First time it downloads about
100 MB, so it can take a few minutes. Leave the window open.

When it finishes you should see:

```
 ============================================================
  SETUP COMPLETE
 ============================================================

  The bot is in SIMULATION mode.
```

Press any key to close it.

> If it says a folder is missing, go back to Step 1 — the three folders are not
> side by side yet. Fix it and run the setup again; running it twice is safe.

You only ever do this once.

---

## Step 4 — Open the dashboard

Double-click **`START-DASHBOARD.vbs`**.

No black command window — it opens the dashboard directly. Then press
**Start bot** in the top right.

> If Windows blocks `.vbs` files, use **`0-DASHBOARD.bat`** instead. Same
> window; it just flashes a console for a moment on the way.

### What you are looking at

**Overview** — the headline numbers:

| Card | Means |
|---|---|
| **Account value** | What it is worth now, against the $100 you started with |
| **Profit / loss** | Including fees paid so far |
| **Open positions** | Trades it currently has money in |
| **Completed trades** | Finished trades and how many won |
| **Traders watched** | Traders it found by itself — you configured none |
| **Traders ranked** | How many it has scored on measured results |
| **Unusual events** | Odd behaviour it flagged |
| **Trade size now** | Current step of your progression, and where it doubles |

Under the cards is a plain-English note telling you what it is doing right now
and what to expect next. That note changes as it learns.

The other tabs:

- **Wallets** — the ranking it worked out on its own.
- **Unusual activity** — what it spotted, and how unusual, for that particular
  trader or market.
- **Open positions** / **Closed trades** — what it holds and what each finished
  trade made or lost, after fees.
- **Activity** — every decision, newest first, in one line each.

Everything refreshes automatically every few seconds.

**Two stop buttons, top right.** *Stop bot* shuts it down cleanly. The red
**STOP TRADING** halts new orders within seconds even while it is running, and
can also close everything.

**`positions=0` at the start is correct.** It is deliberately cautious until it
has learned something. Do not judge it in the first hour.

---

## If you prefer no window at all

Everything in the dashboard is also available as numbered files, and the bot
can run without the dashboard open.

| File | What it does |
|---|---|
| **`2-START-BOT.bat`** | Runs the bot in a plain text window |
| **`3-STATUS.bat`** | Overall state and open positions |
| **`4-WALLETS.bat`** | The wallet ranking |
| **`5-ANOMALIES.bat`** | Unusual behaviour, with the numbers |
| **`6-RESULTS.bat`** | What has made or lost money |
| **`7-FIND-STRATEGIES.bat`** | Studies collected data for trading rules |
| **`8-STOP-TRADING.bat`** | Emergency stop |
| **`9-RESUME.bat`** | Lets it trade again after a stop |

The dashboard and these files are interchangeable — you can start the bot from
one and stop it from the other.

---

## What to expect, and when

The bot learns from live market data, and that data **can only be collected as
it happens** — it cannot be downloaded after the fact. So the first day is
mostly it watching and recording.

| After | What changes |
|---|---|
| **Minutes** | It is watching markets and finding wallets. `4-WALLETS.bat` already shows a ranking. |
| **1 hour** | Enough history for its deeper analysis to switch on. |
| **3–4 hours** | Enough data for `7-FIND-STRATEGIES.bat` to do a real study. Before this it will politely say there is not enough data — that is normal, not a fault. |
| **1–3 days** | Markets start settling, so it can score wallets on what actually happened rather than on current prices. This is when the ranking gets genuinely good. |
| **About a week** | Enough completed trades for it to start learning from its own results. |

**Leave it running.** Every restart is fine — it remembers everything — but it
only learns while it is on.

---

## How to be certain it is not spending money

Three independent ways to check:

1. **`3-STATUS.bat`** — near the top it says the account is `PAPER`.
2. Open `config\config.yaml` in Notepad. Near the top:
   ```yaml
   mode:
     dry_run: true
     allow_live: false
   ```
   Both of those must be changed **and** a wallet key added before a single real
   order is possible. One of them alone does nothing.
3. The startup lines say `mode=dry_run`, and every simulated order is logged as
   `SIMULATED_FILLED`.

Nothing in this guide can cause a real trade. Going live is a separate,
deliberate step you should do with your developer on the call.

---

## Stopping it

- **Normal stop:** close the bot window, or press `Ctrl+C` in it. It saves
  everything on the way out.
- **Emergency stop while it runs:** double-click **`8-STOP-TRADING.bat`**. It
  takes effect within seconds and works even if the bot window is unresponsive.
  Option 1 stops new orders; option 2 also closes everything open.

---

## If something goes wrong

| What you see | What to do |
|---|---|
| "Cannot find the ploymarketbot folder" | The three folders are not side by side. See Step 1. |
| "Python is not installed" | Do Step 2, and make sure you ticked **Add python.exe to PATH**. |
| "Python 3.10 or newer is required" | Install a newer Python from python.org. |
| Package install fails | Check your internet, then run `1-SETUP.bat` again. |
| `markets=0` on some cycles | Polymarket briefly rate-limited us. Harmless — it recovers on its own. |
| `ranked=0` early on | Normal in the first minutes. It fills in as it collects data. |
| "no token has 200+ captured rows yet" | The bot has not run long enough yet. Give it 3–4 hours. |
| Windows SmartScreen warning | Click **More info → Run anyway**. These are plain text files; you can open any of them in Notepad to see exactly what they do. |

Nothing here can lose data. The bot's memory lives in the `state` folder and
survives restarts.

---

## Mac or Linux

The same thing, typed instead of clicked. From inside `polymarket-quant-bridge`:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install -r ../ploymarketbot/requirements.txt
cp config/config.example.yaml config/config.yaml
.venv/bin/python -m pqb.cli check     # verify
.venv/bin/python -m pqb.cli run       # start
```

Then `status`, `wallets`, `anomalies`, `report`, `research`, `kill`, `resume`
in place of `run`.

---

## When you are ready for a real server

This guide is for testing on your own PC. For running it continuously — where
it survives reboots, backs itself up nightly and studies the data on a
schedule — see **`docs/VPS-GUIDE.md`** in the same folder.
