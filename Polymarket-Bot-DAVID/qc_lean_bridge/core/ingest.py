"""The Non-Print Data Engine runner - async, event-driven, incremental.

One coroutine wires the whole upstream half together:

    TickSource ──► DualNonPrintEngine ──► structural events ──┐
               └─► MultiResolutionLineBreak ─► line events ───┤
                                                              ├─► EventStore
                          StructureFeatureBuilder ─► research row
                                                              └─► EventBus
                                                                   (GUI / WS API)

Design rules that keep RAM and CPU flat no matter how long it runs:
  * strictly incremental - every engine is O(1) per tick, nothing accumulates
  * research rows are EVENT-DRIVEN: a row is emitted when the reference
    line-break engine prints a line, not on a clock (line break charts have no
    time axis - resampling them would destroy the very filtering they provide)
  * the store batches inserts; the bus drops for slow consumers
  * `await asyncio.sleep(0)` every N ticks so the API/GUI stay responsive during
    a multi-million-tick ingest
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from .events import EventBus, TICK, STRUCTURAL, LINE_BREAK, FEATURE
from .event_store import build_store, EventStore
from .linebreak_engine import MultiResolutionLineBreak, LineBreakConfig
from .logging_setup import get_logger, rss_mb
from .nonprint_engine import DualNonPrintEngine, NonPrintConfig
from .structure_features import StructureFeatureBuilder, FeatureConfig
from .tick_source import build_source, TickSource

log = get_logger("ingest")


class NullStore:
    """A store that counts but never persists - used for read-only replay.

    Satisfies the write surface `_drive` calls, so replay runs the identical
    engine path without duplicating rows or opening a second writer on the DB
    it is reading from.
    """

    def __init__(self):
        self.counts = {"ticks": 0, "structural": 0, "lines": 0, "features": 0}
        self.run_id = 0

    def start_run(self, source): return 0
    def finish_run(self, notes=""): return None
    def add_tick(self, t): self.counts["ticks"] += 1
    def add_structural(self, e): self.counts["structural"] += len(e)
    def add_lines(self, e): self.counts["lines"] += len(e)
    def add_feature(self, s): self.counts["features"] += 1
    def flush(self): return None
    def close(self): return None
    def summary(self): return dict(self.counts)


@dataclass
class IngestStats:
    ticks: int = 0
    structural_events: int = 0
    lines: int = 0
    features: int = 0
    seconds: float = 0.0
    rss_mb: float = 0.0
    source: str = ""
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        d = {
            "ticks": self.ticks,
            "structural_events": self.structural_events,
            "line_break_lines": self.lines,
            "feature_rows": self.features,
            "seconds": round(self.seconds, 2),
            "ticks_per_sec": int(self.ticks / self.seconds) if self.seconds > 0 else 0,
            "rss_mb": round(self.rss_mb, 1),
            "source": self.source,
        }
        d.update(self.detail)
        return d


class IngestPipeline:
    """Owns the engines + the store for one ingest (or replay) run."""

    def __init__(self, cfg, bus: EventBus | None = None, logger=None,
                 store: EventStore | None = None):
        self.cfg = cfg
        self.bus = bus or EventBus()
        self.log = logger or (lambda m: None)

        self.nonprint = DualNonPrintEngine(NonPrintConfig.from_config(cfg))
        self.linebreak = MultiResolutionLineBreak(LineBreakConfig.from_config(cfg))
        self.features = StructureFeatureBuilder(FeatureConfig.from_config(cfg))

        self._store = store
        self._owns_store = store is None
        self.stats = IngestStats()
        self._stop = asyncio.Event()
        self.latest: dict | None = None       # last research row (for the API)

    # ------------------------------------------------------------------ store
    @property
    def store(self) -> EventStore:
        if self._store is None:
            self._store = build_store(self.cfg)
        return self._store

    def stop(self) -> None:
        self._stop.set()

    # ------------------------------------------------------------------- run
    async def run(self, max_ticks: int = 0, source: TickSource | None = None,
                  progress=None) -> IngestStats:
        src = source or build_source(self.cfg, max_ticks=max_ticks)
        return await self._drive(src.stream(), src.name, max_ticks, progress,
                                 close=src.close)

    async def replay(self, start: float | None = None, end: float | None = None,
                     progress=None) -> IngestStats:
        """Rebuild all engine state from stored ticks - the replay guarantee.

        Same engine code path as a live run, so a replay reproduces the identical
        structural events and research rows. Writes go to a throwaway NullStore:
        replay VERIFIES reproducibility, it must not re-persist (that would both
        double the stored data and self-lock the reader's connection).
        """
        source_store = self.store
        replay_gen = source_store.replay_ticks(start, end)

        async def _gen():
            for i, tick in enumerate(replay_gen):
                yield tick
                if i % 20_000 == 0:
                    await asyncio.sleep(0)
        return await self._drive(_gen(), "replay", 0, progress,
                                 write_store=NullStore())

    async def _drive(self, stream, source_name: str, max_ticks: int,
                     progress=None, close=None, write_store=None) -> IngestStats:
        cfg_stride = int(self.cfg.get("structure.store_stride", 5))
        store = write_store if write_store is not None else self.store
        run_id = store.start_run(source_name)
        t0 = time.perf_counter()
        n = 0
        last_log = 0

        self.log(f"[Engine] ingest started (source={source_name}, run={run_id})")
        log.info("ingest started", extra={"source": source_name, "run": run_id,
                                          "max_ticks": max_ticks or "all"})
        try:
            async for tick in stream:
                if self._stop.is_set():
                    self.log("[Engine] stop requested")
                    break

                n += 1
                store.add_tick(tick)

                # --- Bid & Ask Non-Print engines (independent state)
                sevents = self.nonprint.on_tick(tick)
                if sevents:
                    store.add_structural(sevents)
                    self.stats.structural_events += len(sevents)
                    for e in sevents:
                        self.bus.publish(STRUCTURAL, e)

                # --- 100 line-break engines
                levents = self.linebreak.on_tick(tick)
                if levents:
                    keep = [e for e in levents if self.linebreak.should_store(e.resolution)]
                    if keep:
                        store.add_lines(keep)
                    self.stats.lines += len(levents)
                    for e in levents:
                        self.bus.publish(LINE_BREAK, e)

                    # --- research row, emitted on a reference-resolution line
                    if self.linebreak.is_snapshot_line(levents):
                        snap = self.features.compute(
                            ts=tick.ts, price=tick.price,
                            nonprint_snap=self.nonprint.snapshot(tick.ts),
                            lb_snap=self.linebreak.snapshot(),
                            bid_size=tick.bid_size, ask_size=tick.ask_size)
                        snap.values.update(self.linebreak.per_resolution(cfg_stride))
                        store.add_feature(snap)
                        self.stats.features += 1
                        self.latest = {"ts": snap.ts, **snap.values}
                        self.bus.publish(FEATURE, snap)

                # Ticks are the highest-volume topic: publish only when someone
                # is actually listening, and never let it stall the engines.
                if self.bus.subscribers:
                    self.bus.publish(TICK, tick)

                if n - last_log >= 100_000:
                    last_log = n
                    el = time.perf_counter() - t0
                    rate = int(n / el) if el > 0 else 0
                    mem = rss_mb()
                    self.log(f"[Engine] {n:,} ticks | {self.stats.features:,} rows | "
                             f"{rate:,}/s | {mem:.0f} MB")
                    log.info("ingest progress", extra={
                        "ticks": n, "rows": self.stats.features,
                        "ticks_per_sec": rate, "rss_mb": round(mem, 1)})
                    if progress:
                        progress(n, max_ticks or 0)
                    await asyncio.sleep(0)

                if max_ticks and n >= max_ticks:
                    break
        finally:
            store.flush()
            self.stats.ticks = n
            self.stats.seconds = time.perf_counter() - t0
            self.stats.rss_mb = rss_mb()
            self.stats.source = source_name
            self.stats.detail = {
                "nonprint": self.nonprint.stats(),
                "linebreak": self.linebreak.stats(),
            }
            store.finish_run(notes=f"source={source_name}")
            if close:
                try:
                    await close()
                except Exception as exc:  # noqa: BLE001
                    log.warning("source close failed", extra={"err": str(exc)})

        s = self.stats
        rate = int(s.ticks / s.seconds) if s.seconds > 0 else 0
        self.log(f"[Engine] OK - {s.ticks:,} ticks -> {s.structural_events:,} structural "
                 f"events, {s.lines:,} lines, {s.features:,} research rows "
                 f"({rate:,} ticks/s, {s.rss_mb:.0f} MB)")
        log.info("ingest complete", extra=s.as_dict())
        return s

    # ---------------------------------------------------------------- export
    def export(self, out_dir=None) -> dict:
        from .research_export import ResearchExporter
        self.store.flush()
        exp = ResearchExporter(self.cfg, self.store, logger=self.log)
        self.log("[Engine] exporting standardized research dataset...")
        return exp.export(out_dir)

    def close(self) -> None:
        if self._store is not None and self._owns_store:
            self._store.close()
            self._store = None


# --------------------------------------------------------------- sync helper
def run_ingest(cfg, max_ticks: int = 0, logger=None, bus: EventBus | None = None,
               do_export: bool = True, progress=None) -> dict:
    """Blocking convenience wrapper (CLI / GUI worker thread)."""
    pipe = IngestPipeline(cfg, bus=bus, logger=logger or (lambda m: None))

    async def _main():
        stats = await pipe.run(max_ticks=max_ticks, progress=progress)
        out = stats.as_dict()
        if do_export:
            out["export"] = pipe.export()
        return out

    try:
        return asyncio.run(_main())
    finally:
        pipe.close()
