# Architecture

How the platform is built, why, and how the pieces connect. Read this alongside
the module docstrings — each file explains its own design in detail.

## Two halves, one contract

The platform is deliberately split at the CSV boundary:

```
UPSTREAM (the data engine)                DOWNSTREAM (the LEAN bridge)
ticks → structure → features → CSV   ══►  CSV → discovery → validation → LEAN
```

The **contract** between them is a folder of standardized CSVs plus a manifest
and feature dictionary (`core/research_export.py`). Either half can be replaced
without touching the other, which is exactly the modularity the brief asks for.
The downstream bridge existed first; this project added the upstream engine, the
autonomy layer, and the control plane around it.

## The event model (`core/events.py`)

Everything upstream is an immutable event:

```
Tick → StructuralEvent (non-print/void/gap) → LineBreakEvent → FeatureSnapshot → LiveEvent
```

Dataclasses use `slots=True` because at millions of ticks a `__dict__` per event
is the difference between a few hundred MB and several GB. A small async
`EventBus` fans events out to subscribers (DB, live manager, GUI, WebSocket).
Each subscriber owns a **bounded, drop-oldest** queue: a slow consumer sheds its
oldest events instead of growing without bound or back-pressuring the hot ingest
loop. That is what keeps memory flat under a fast feed.

## Module 2 — Non-Print engines (`core/nonprint_engine.py`)

Two independent engines (Bid, Ask) with identical logic and separate state. Each
watches its side's quote ladder and detects, strictly incrementally:

- **Skipped price** — a level the quote jumped over (> 1 tick move).
- **Non-print** — a skipped level with no trade inside the lookback window.
- **Liquidity void** — the level a non-print opened; stays open until traded.
- **Structural gap** — the jump size in ticks.
- **Void persistence** — age of open voids + lifetime of filled voids.
- **Structural velocity / acceleration** — EMA of levels traversed per second.

Memory is bounded by pruning printed-level history and open voids outside a
window around the market. Prices are keyed to the integer tick grid so
comparisons are exact.

## Module 3 — 100 Line-Break engines (`core/linebreak_engine.py`)

Engine *R* prints a line when price travels *R* ticks. All 100 run as **parallel
numpy state arrays**, and each caches its next up/down trigger price. A tick that
triggers nothing (the vast majority) costs **one vectorized comparison over a
100-element array**; only engines that actually triggered are touched in Python.
This is what makes running 100 resolutions on raw ticks cheap. Supports classic
single line break and *k*-line break (e.g. 3-line-break reversals).

The cross-resolution snapshot yields the consensus block: agreement, divergence,
directional consensus, reversal frequency, persistence, compression/expansion,
structural volatility — the raw material for Module 4.

## Module 4 — Feature engineering (`core/structure_features.py`)

Combines the Bid engine, Ask engine, and 100 line-break engines into one
research row containing all **14 named features**, each with a written
definition in `FEATURE_DOCS`, exported as `FEATURE_DICTIONARY.md`. Research rows
are **event-driven**: one row is emitted when a reference-resolution engine
prints a line, never on a clock — line-break charts have no time axis, and
resampling them onto one would destroy the noise-filtering that is their whole
point.

## Replayable database (`core/event_store.py`)

Backends: **SQLite** (default, zero-install, ships in the exe), **PostgreSQL**,
**TimescaleDB** (hypertables on `ts`). `psycopg2` is optional — no Postgres means
automatic fallback to SQLite. Writes are batched (`executemany` per 5k rows) with
periodic commits. `replay_ticks()` streams stored ticks back in time order,
chunked so a multi-million-row replay never materializes in RAM. Replay drives
the *same* engine code through a throwaway `NullStore`, so it reconstructs
byte-identical structure without re-persisting (verified: identical event/line/
row counts on replay).

## Module 1 — OpenClaw (`core/openclaw.py`)

The master controller runs the full cycle and **verifies every stage before the
next begins** (`_verify`) — a stage that silently produced nothing is the failure
mode that quietly wastes a research run. It records every accepted strategy in
the registry with the dataset template it was trained on, compares new research
to the deployed incumbent on the composite rank score (reliability over fattest
backtest), deploys only prop-compliant winners, and retrains when a live strategy
degrades. Cycles run in a worker thread so the event loop (live manager, API)
never stalls.

## Modules 7 & 8 — Live manager + prop gate (`core/live_manager.py`, `core/prop_firm.py`)

The hard problem live is that a discovered rule's threshold is a quantile of a
feature built by the downstream `FeatureEngineer`. If live recomputed that
feature differently, the threshold would mean something else. So live stores the
**dataset template** at deploy time and re-runs the *same* `FeatureEngineer` over
a rolling buffer — same code, same columns, same transforms. Two cadences:
position management (stops/targets/min-hold) runs **every tick**; entry signals
are throttled (rebuilding the feature frame is the expensive part, and a 10 s
min-hold strategy does not need sub-second entries).

The prop gate sits in front of every order: it caps size to the book-wide limit,
refuses exits inside the min-hold window, halts on account/EOD drawdown, stops
new trades at the daily cap, and guards the 50% consistency rule. Every refusal
carries a human-readable reason and is published to the bus.

## Control plane (`core/api_server.py`)

REST + WebSocket implemented directly on `asyncio` (HTTP/1.1 + RFC-6455 by hand,
~200 lines) so the frozen single-file `.exe` needs no web framework. Localhost by
default. The WebSocket samples the tick firehose so a browser is never swamped.

## Notable correctness fixes made during integration

Two real bugs in the pre-existing bridge surfaced once the engines fed it
realistic tick data, and both are fixed:

1. **Timestamp unit bug (critical).** The backtester computed hold time as
   `index.asi8 / 1e9`, assuming nanoseconds. In pandas 2.x a `DatetimeIndex`
   keeps its own resolution, and our sub-second timestamps parse as
   `datetime64[us]` — so `asi8` is *microseconds* and the result was **1000×
   too short**. The 10-second min-hold rule was being evaluated as 10 ms: no
   position could ever satisfy an exit, and the backtester silently produced
   **zero trades**. Fixed by normalizing to nanoseconds explicitly
   (`core/backtester.py`). The min-hold rule is now genuinely enforced (verified:
   every trade holds ≥ 10 s).

2. **Unrealistic search space.** Stop/target were percentages of price; the
   smallest choice (1%) is 50 MES points, which tick-resolution data never
   travels — again, no trades. Retuned to intraday-futures-realistic values
   (0.05–0.6%) in `config/default_config.yaml`.

A third fix improved observability: the ctypes RAM probe truncated the 64-bit
process handle (missing `restype`), so it always returned 0 MB; declaring the
argtypes fixed the low-RAM guardrail reporting.
