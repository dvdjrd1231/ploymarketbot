# Reuse map

Required by section 1 of the brief: for each capability the new system needs,
name the existing component to reuse and the gap.

## Repository availability — checked, not assumed

| Repo the brief names | Available | Stack | Where this project expects it |
|---|---|---|---|
| **polymarketbot** | ✅ | Python 3.13, FastAPI + React | `../ploymarketbot` (note the spelling), or `PQB_POLYMARKETBOT_PATH` |
| **Prop Firm Quant Bridge (LEAN)** | ✅ | **Python 3.10+** research platform | `../qc_lean_bridge` — the **inner** folder of "Advanced Strategy Development for QuantConnect LEAN" — or `PQB_QUANT_BRIDGE_PATH` |
| **paymentor-proxy-admin-panel** | ✅ | **PHP 8.3+**, Laravel 12 / Filament 5 | Not referenced at runtime — patterns only (see below) |

**The bridge arrived, and it is Python — not C#.** Earlier sessions recorded it
as absent and assumed a C# LEAN project; it is in fact a Python research
platform (`core/`, ~8,900 lines) wrapping QuantConnect LEAN concepts: a
non-print tick engine, 100 line-break engines, feature engineering, automated
strategy discovery, walk-forward and Monte-Carlo validation, ranking, a live
strategy manager and a prop-firm risk gate.

That changes the integration from the hardest of the three shapes to the
easiest. Being Python, it is **imported and executed**, not reimplemented and
not messaged across a process boundary — reached through one path shim
([`pqb/quant.py`](../pqb/quant.py)), exactly as `ploymarketbot` is reached
through [`pqb/upstream.py`](../pqb/upstream.py).

The admin panel remains PHP, so that row is still necessarily *pattern* reuse.
This map says which kind each row is.

---

## Capability map

Legend — **Code**: the component is imported and executed. **Pattern**: the
approach is copied, the code cannot be. **New**: written for this project.

| Capability | Reuse | Source | Gap / what was added |
|---|---|---|---|
| **Market-data ingest** | **Code** | `ploymarketbot` `services/price_service.py` — CLOB market WebSocket, REST batch fallback, crossed-book repair from the authoritative touch, per-quote provenance | Gamma's richer fields (category, liquidity, volume, tick size, min order size) are not parsed upstream — added here. `recent_trades` added (no upstream equivalent). |
| **Market metadata / resolution** | **Code** | `ploymarketbot` `services/gamma.py` — cache with a 5s TTL near expiry, in-flight request sharing, closed-market retry, decisive-resolution check | None. Used as-is for settlement. |
| **Account state** | **Code** | `ploymarketbot` `services/data_api.py` (`get_positions`, `get_account_value`) + `trading.py` (`get_usdc_balance`) | Mapped into `PositionView` / `AccountState`; peak/trough path tracking added (needed for trailing exits). |
| **Target-wallet activity** | **Code** | `ploymarketbot` `services/data_api.py` — activity feed with a same-second de-duplication watermark | Upstream keeps one watermark per client instance, so **one client per wallet** is instantiated here to support multiple wallets. |
| **Order execution** | **Code** | `ploymarketbot` `services/trading.py` — `py-clob-client` facade with all calls off the event loop, key normalisation, order-response parsing, account auto-detection | Sizing/tick/min-size validation and the retry classifier are new (`adapters/sizing.py`, `execution_adapter.py`). |
| **Persistence** | **Pattern** | `ploymarketbot` `db.py` — WAL + `synchronous=NORMAL`, one connection under an `RLock`, state-change-only writes | Tables are new: a decision journal is not a trade log. It records decisions that led to nothing, position paths, reconciliation events and engine state. |
| **Logging** | **Pattern** | `ploymarketbot` `applog.py` (rotating file + stream, configured once) | Rewritten as `logs.py` to write to this project's own data directory and to emit one `key=value` event per line with secret redaction. |
| **Scheduling** | **Pattern** | `ploymarketbot` `services/engine.py` — independent asyncio loops so no slow call stalls another concern | The engine itself is **deliberately not reused** (see below). One cycle loop here; LEAN's scheduler replaces it if the bridge drives the clock. |
| **Config & secrets** | **Pattern** | `paymentor` `.env` + encrypted extension settings, read via an accessor, never inline; `.env.example` with blanks | `config.py` with `${env:NAME}` resolution and a redaction path. Config holds env-var *names*, never values. |
| **Deployment / service** | **Pattern** | `paymentor` `scripts/install-debian13.sh` — dedicated user, systemd unit, `Restart=always`, journald | `deploy/pqb.service`, plus systemd hardening (`ProtectSystem=strict`, `ReadWritePaths`) because this process holds a key that moves money. |
| **Backup / restore** | **Pattern** | `paymentor` `scripts/backup.sh` + `restore.sh` + nightly cron + "test a restore" | `scripts/backup.sh` / `restore.sh`. **This was a genuine gap** — the journal holds the doubling baseline and progression index, so losing it rewinds the trade-size progression and orphans every open position. |
| **Security discipline** | **Pattern** | `paymentor` `docs/12-security.md` checklist; idempotency keyed on the provider transaction id | Adopted: no secrets in repo, `.env` chmod 600, duplicate-operation protection (in-flight guard + trade-id de-dup), audit log of every decision. |
| **Documentation shape** | **Pattern** | `paymentor` numbered `docs/`, README status matrix, per-module READMEs, `CORE-TOUCHPOINTS.md` | Adopted: `README.md` + `ARCHITECTURE.md` (reused-vs-new) + this map + `docs/INTEGRATION-PLAN.md`. |
| **"Never edit vendored core"** | **Pattern** | `paymentor` golden rule — layer on top, never modify upstream | Directly adopted. `ploymarketbot` is referenced through **one** file (`pqb/upstream.py`) and **not one upstream file was modified**. |
| **Admin UI** | **Pattern only** | `paymentor` Filament admin (PHP — unusable from Python) | **Gap, deliberately unbuilt.** See below. |
| **Decision / analysis layer** | **Code** | `qc_lean_bridge` `core/pipeline.py` (`ResearchEngine`) — feature engineering, guided rule search, walk-forward + Monte-Carlo validation, ranking | Reached through `pqb/quant.py`. Polymarket features are exported into the CSV shape `DataPipeline` already reads; discovered rules are evaluated live by `bridge/lean_engine.py`. |
| **Research feature engineering** | **Code** | `qc_lean_bridge` `core/features.py` (`FeatureEngineer`) | Reused **twice**, and that is the point: once offline over the exported CSVs, and again live over a rolling window (`bridge/live_features.py`). Rules are discovered over *engineered* columns, so reimplementing them for the live path would mean a validated rule quietly meaning something different in production. |
| **Validation & ranking** | **Code** | `qc_lean_bridge` `core/validators.py`, `core/ranking.py`, `core/metrics.py` | Used as-is via `ResearchEngine.run_full()`. Nothing reimplemented. |
| **Wallet ranking / anomalies** | **New** | — | `pqb/analytics/`. No upstream equivalent: `ploymarketbot` follows a fixed wallet list and the Quant Bridge has no concept of wallets at all. |

---

## Deliberately NOT reused

**`ploymarketbot/services/engine.py`** — the copy engine. It hard-codes
copy-trading rules: Fixed-50%, "copy only the soonest-settling trade", "mirror
the target's exit". The brief's section 7 says the opposite in as many words:
*do not blindly copy target wallets*. Reusing it would import exactly the
behaviour we are told not to build.

**`ploymarketbot`'s `api.py` / `hub.py` / `state.py` / React dashboard** — a web
layer for a UI this project does not have. Kept available for the admin-UI
decision below rather than deleted.

**Everything in `paymentor` except patterns** — PHP cannot be called from a
Python trading loop, and adding a PHP service to host an admin page for a
Python process would be new infrastructure, not reuse.

---

## The admin-UI gap (open decision, not an oversight)

The brief lists "admin UI" as a capability to map. There are three options and
they differ by an order of magnitude in cost:

1. **CLI only (what exists).** `check` / `run` / `status` / `report` / `kill` /
   `resume`, plus journald. Zero new surface. Nothing to secure or authenticate.
2. **Reuse `ploymarketbot`'s dashboard** — same language, same domain, already
   has a WebSocket push store, positions/orders/history tables and an activity
   log. This is the cheapest real UI by a wide margin, and the only option that
   is genuine code reuse.
3. **Filament page in `paymentor`** — a second stack, a second deployment, a
   cross-service API and its authentication. This is the most expensive option
   and reuses the least.

**Recommendation: (1) now, (2) if a UI is wanted.** Option 3 is only right if
the operator must administer the bot from inside the existing panel, and that
requirement has not been stated. Flagged for the client rather than chosen
unilaterally, since it is a scope decision rather than a technical one.

---

## Polymarket API facts re-verified against current docs

Section 1.2 requires checking the live docs rather than relying on memory.
Checked 2026-08-10 against `docs.polymarket.com`:

- **Settlement is USDC on Polygon** (chain id 137). Funds on Ethereum mainnet
  must be bridged first; Polymarket now documents a bridge API
  (`/trading/bridge/deposit`, `supported-assets`, `quote`). An unbridged wallet
  reads as a zero balance and every order fails without naming the cause — so
  this is surfaced in `pqb.cli check` and on every live start.
- **Tick sizes are dynamic**: 0.1 / 0.01 / 0.001 / 0.0001, and they *change*
  when price > 0.96 or < 0.04. Read per market from metadata and per token from
  the book's `tick_size_change` events — already handled, never assumed.
- **Endpoints in use are current**: market and user WebSocket channels, `GET
  /book`, `POST /prices`, `POST /midpoints`, Data API positions and activity.
  Verified `side=BUY` returns the best **bid** and `side=SELL` the best **ask**
  by comparing `POST /prices` against `GET /book` for the same token.
- **`py-clob-client-v2` now exists** alongside the original client this project
  uses via `ploymarketbot`. **Open decision for the client** — migrating is a
  change to the upstream project, not to this one, and should not be done
  silently. Flagged, not actioned.
