# Polymarket Copy Trading Bot — Local Web Application

The existing desktop bot, migrated to a local browser application. Same trading
logic, rebuilt around asynchronous I/O and WebSockets so nothing on screen is
ever stale and nothing ever freezes.

Runs entirely on your PC. The server binds to `127.0.0.1` — it is not reachable
from your network or the internet.

---

## Quick start

Double-click **`Start Bot.bat`**, or:

```
pip install -r requirements.txt
python run.py
```

The first run installs the dashboard's dependencies and builds it (needs
[Node.js 18+](https://nodejs.org) — for the build only, not to run the bot).
Your browser opens on <http://localhost:8765>.

To stop: `Ctrl+C` in the console window.

Running it 24/7 on a Linux VPS instead? See [DEPLOY.md](DEPLOY.md).

---

## What changed, and why the stalling is gone

The desktop build ran one loop, every three seconds, on one thread. That single
loop fetched prices, read the balance, checked settlements, polled the target
wallet **and** rebuilt every table. Any slow HTTP call delayed all of it, and
the table rebuild ran on the UI thread — which is what you saw as freezing,
lagging numbers and prices that did not match Polymarket.

| Concern | Before | Now |
|---|---|---|
| Market prices | REST poll every 3s, per position | **WebSocket order-book stream** — the same book Polymarket renders, applied on arrival |
| Trade P/L | Recomputed on the 3s tick | Recomputed from streamed marks and pushed ~2.5×/second |
| Portfolio P/L | Same 3s tick | Continuous, derived from live marks |
| Wallet balance | Same 3s tick, blocking | Own task, never blocks anything else |
| Order status | Not tracked | **Authenticated CLOB user channel** — fills and cancels arrive as they happen |
| Position sync | None | Reconciled against the exchange every 30s |
| Countdowns | Redrawn by a UI timer | Absolute timestamps ticked locally, exact to the second |
| UI updates | Whole tables rebuilt every second | Push-driven; only changed rows repaint |
| Blocking calls | On the UI thread | Signing/REST dispatched to a thread pool; the event loop never blocks |

Concretely, the engine is now five independent asyncio tasks that cannot stall
one another:

```
_push_loop        ~400ms   recompute marks, broadcast to the browser
_target_loop      15s      look for new trades on the followed wallet
_account_loop     5s       wallet balance / buying power
_market_loop      20s      market metadata + settlement detection
_reconcile_loop   30s      compare our book against the exchange's
```

Prices reach `_push_loop` by push, not by polling, so a mark is current to the
millisecond and reading it costs nothing.

---

## Trading rules — unchanged

Every rule from the desktop bot is preserved exactly:

- **Mode 1 — Proportional.** Mirrors the same fraction of your bankroll (cash +
  open positions) that the target used of theirs. Several positions at once.
- **Mode 2 — Fixed 50%.** One position at a time, each using 50% of available
  balance, so profits compound automatically.
- **Rule 1.** Among several qualifying trades, only the soonest-settling one is
  copied, keeping capital recycling quickly.
- **Rule 2.** The copied notional never exceeds the target's dollar amount.
- **Entry-price rule.** A trade is only entered if the executable price is the
  **same or better** than the target's entry; otherwise it is skipped and the
  reason is logged.
- **De-duplication.** Every target trade id is processed at most once.
- **Expiry filters.** Never enter a market already settling; optionally ignore
  anything settling beyond N days.
- **Exit mirroring.** When the target sells a position you copy, your copy is
  closed too (toggleable).

Only trades that happen *after* you press Start are copied — existing history on
the target wallet is baselined and ignored.

---

## Paper trading

Fully simulated against live public prices. No private key, no funds, no real
orders. Each new session **starts from a clean dashboard** — previous simulated
trades, positions and P/L are cleared automatically — unless you tick *Restore
previous session*.

The virtual balance behaves like the real thing: cost is deducted on entry and
proceeds are credited on settlement, so Fixed-50% compounding is visible.

---

## Security

- The private key is **write-only**. You type it once; it goes straight to the
  Windows Credential Manager via `keyring`. No API route can read it back, and
  it never appears in a page, a log, or the config file. The dashboard only ever
  learns that *a* key exists.
- If `keyring` is unavailable, the fallback is **Windows DPAPI** (`secret.bin`,
  decryptable only by your Windows user on this machine). If neither is
  available the app refuses to store a key rather than obfuscating it weakly.
- The server binds to localhost only. CORS is restricted to the local dev
  origins — no wildcard, because these routes move money.
- An existing key stored by the desktop app is picked up automatically (same
  credential entry), so you do not need to re-enter it.

**Live trading is armed only when both Paper trading and Dry-run are off.** The
dashboard shows a red banner and a `LIVE` badge whenever that is the case.

---

## The dashboard

- **Positions** — open copies with live price, cost, value, P/L, return and a
  per-second countdown. The dot beside each price shows its source: green for
  the live stream, amber for a REST top-up. Entry prices are green when the fill
  honoured the entry-price rule.
- **Orders** — every order with its live exchange status: pending, live,
  matched, filled, cancelled or rejected. Pending orders can be cancelled.
- **History** — settled, skipped and failed trades, each with the reason. Knowing
  *why* a trade was skipped is as useful as seeing the ones taken.
- **Target wallet** — what the followed wallet is doing, plus feed diagnostics.
- **Activity log** — every decision as it happens, filterable and searchable.
- **Settings** — everything above, plus credentials and performance tuning.

The header pills are the honesty indicators: **Server** (this page's socket),
**Prices** (the Polymarket book stream), **Exchange** (the order channel).

---

## Layout

```
PolymarketBot_web/
├─ run.py                  Launcher: builds the UI, starts the server
├─ Start Bot.bat           Double-click entry point
├─ requirements.txt
├─ backend/
│  ├─ main.py              FastAPI app, serves the dashboard + API
│  ├─ api.py               REST routes + the dashboard WebSocket
│  ├─ state.py             Singletons and engine lifecycle
│  ├─ hub.py               Broadcast hub (coalescing, non-blocking)
│  ├─ db.py                SQLite: trades, orders, log
│  ├─ models.py            Shared data models
│  ├─ settings_store.py    Configuration
│  ├─ secret_store.py      Keyring / DPAPI credential storage
│  ├─ applog.py            Logging → file + DB + browser
│  └─ services/
│     ├─ engine.py         The copy engine (all trading rules)
│     ├─ price_service.py  WebSocket order books + REST safety net
│     ├─ user_stream.py    Authenticated order/trade channel
│     ├─ trading.py        py-clob-client, off the event loop
│     ├─ gamma.py          Market metadata / settlement
│     └─ data_api.py       Target activity + on-chain positions
└─ frontend/               React + TypeScript + Vite
   └─ src/
      ├─ App.tsx
      ├─ lib/live.ts       WebSocket store (slice-level subscriptions)
      ├─ lib/clock.ts      Shared one-second countdown ticker
      └─ components/
```

Data lives in `%APPDATA%\PolymarketBotWeb\` (database, config, `bot.log`).

---

## Development

```
# terminal 1 — API with auto-reload
python run.py --reload --no-browser

# terminal 2 — dashboard with hot reload on http://localhost:5173
cd frontend
npm run dev
```

Other flags: `--port N`, `--rebuild`, `--no-browser`.
API reference at `/api/docs` while the server is running.

---

## Troubleshooting

**"Trading library not loaded"** — `pip install -r requirements.txt` into the
same Python that runs the app. Paper trading works without it.

**Prices pill shows `retrying`** — the WebSocket dropped; it reconnects with
backoff and REST polling covers the gap, so prices stay correct meanwhile. The
Target wallet tab shows the last error.

**"No secure credential store available"** — `pip install keyring`, then restart.

**Connected but balance is $0.00** — your USDC is probably in a Polymarket
email/browser wallet. Paste that address into the funder field and press
*Connect & auto-detect account*.

**Nothing is being copied** — check the Activity log. Every skip states its
reason: the price moved past the target's entry, the market was already
settling, the minimum could not be met, or Fixed-50% already holds a position.
