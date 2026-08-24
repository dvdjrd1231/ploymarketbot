"""Module 1 - OpenClaw AI (Master Controller).

OpenClaw does not invent trading strategies. It MANAGES THE RESEARCH PROCESS:
it runs each stage, refuses to continue when a stage did not really succeed,
remembers every strategy it has ever accepted, checks whether new research beats
what is already live, deploys only what passed, watches the live account, and
retrains when the edge decays.

The full autonomous cycle:

  1  INGEST    IBKR Time & Sales -> Bid/Ask Non-Print engines -> 100 line-break
               engines -> replayable database
  2  EXPORT    standardized research dataset (CSV + manifest + feature dictionary)
  3  RESEARCH  the LEAN bridge: features -> discovery -> validation -> ranking
               -> generated QuantConnect algorithms
  4  COMPARE   new candidates vs the strategies already deployed
  5  DEPLOY    only strategies that passed EVERY gate and beat the incumbent,
               and only if they satisfy the prop-firm rules
  6  MONITOR   live performance; degradation -> disable, promote, RETRAIN

Every stage is VERIFIED before the next one starts (`_verify`), because a stage
that silently produced nothing is the failure mode that quietly wastes a whole
research run. A failed verification aborts the cycle with a reason, and the
reason lands in the cycle report.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import Config, PROJECT_ROOT
from .events import EventBus, LiveEvent, CONTROL, LIVE
from .ingest import IngestPipeline
from .logging_setup import get_logger, rss_mb
from .pipeline import ResearchEngine
from .risk_management import PropConstraints
from .strategy_registry import StrategyRegistry, VALIDATED, DEPLOYED

log = get_logger("openclaw")

IDLE = "idle"
INGESTING = "ingesting"
EXPORTING = "exporting"
RESEARCHING = "researching"
DEPLOYING = "deploying"
MONITORING = "monitoring"
RETRAINING = "retraining"
FAILED = "failed"


class StageError(RuntimeError):
    """A stage ran but did not produce what the next stage needs."""


@dataclass
class CycleResult:
    cycle: int
    trigger: str
    started: str
    ok: bool = False
    stages: dict = field(default_factory=dict)
    deployed: list[str] = field(default_factory=list)
    comparison: dict = field(default_factory=dict)
    error: str = ""
    seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "cycle": self.cycle, "trigger": self.trigger, "started": self.started,
            "ok": self.ok, "stages": self.stages, "deployed": self.deployed,
            "comparison": self.comparison, "error": self.error,
            "seconds": round(self.seconds, 1),
        }


class OpenClaw:
    """The master research manager."""

    def __init__(self, cfg: Config, bus: EventBus | None = None, logger=None):
        self.cfg = cfg
        self.bus = bus or EventBus()
        self.log = logger or (lambda m: None)

        oc = cfg.section("openclaw")
        self.auto_deploy = bool(oc.get("auto_deploy", True))
        self.retrain_on_degradation = bool(oc.get("retrain_on_degradation", True))
        self.cycle_interval_h = float(oc.get("cycle_interval_hours", 12))
        self.max_ticks = int(oc.get("ingest_max_ticks", 200_000))
        self.min_rows = int(oc.get("min_research_rows", 300))
        self.validate_top_n = int(oc.get("validate_top_n", 40))
        self.export_top_n = int(oc.get("export_top_n", 5))
        self.reuse_dataset = bool(oc.get("reuse_existing_dataset", False))

        reg_path = cfg.get("openclaw.registry_path", "data/registry.db")
        p = Path(reg_path)
        self.registry = StrategyRegistry(p if p.is_absolute() else PROJECT_ROOT / p)

        self.constraints = PropConstraints.from_config(cfg)
        self.state = IDLE
        self.stage_detail = ""
        self.cycle = 0
        self.history: list[CycleResult] = []
        self.started_at = time.time()
        self.live = None                    # LiveStrategyManager, once monitoring
        self._retrain_pending = False
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ state
    def _set_state(self, state: str, detail: str = "") -> None:
        self.state = state
        self.stage_detail = detail
        log.info("state", extra={"state": state, "detail": detail})
        self.bus.publish(CONTROL, {"state": state, "detail": detail,
                                   "cycle": self.cycle, "ts": time.time()})

    def status(self) -> dict:
        deployed = self.registry.deployed()
        return {
            "state": self.state,
            "detail": self.stage_detail,
            "cycle": self.cycle,
            "uptime_s": round(time.time() - self.started_at, 1),
            "rss_mb": round(rss_mb(), 1),
            "auto_deploy": self.auto_deploy,
            "retrain_on_degradation": self.retrain_on_degradation,
            "deployed": [r.strategy_id for r in deployed],
            "validated_available": len(self.registry.by_status(VALIDATED)),
            "retrain_pending": self._retrain_pending,
            "last_cycle": self.history[-1].as_dict() if self.history else None,
            "live": self.live.status() if self.live else None,
        }

    # ---------------------------------------------------------------- verify
    def _verify(self, stage: str, ok: bool, detail: str) -> None:
        """A stage is only 'done' when it produced something usable."""
        if ok:
            self.log(f"[OpenClaw] verified {stage}: {detail}")
            log.info("stage verified", extra={"stage": stage, "detail": detail})
            return
        log.error("stage verification FAILED", extra={"stage": stage, "detail": detail})
        raise StageError(f"{stage} verification failed: {detail}")

    # ----------------------------------------------------------------- cycle
    def run_cycle(self, trigger: str = "manual", progress=None) -> CycleResult:
        """One complete research cycle. Blocking; safe to call from a thread."""
        self.cycle = self.registry.start_cycle(trigger)
        res = CycleResult(cycle=self.cycle, trigger=trigger,
                          started=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        t0 = time.perf_counter()
        self.log(f"\n{'='*66}\n[OpenClaw] CYCLE {self.cycle} starting (trigger: {trigger})\n{'='*66}")

        try:
            ingest_stats = self._stage_ingest(res, progress)
            self._stage_export(res, ingest_stats)
            engine = self._stage_research(res, progress)
            accepted = self._stage_record(res, engine)
            self._stage_deploy(res, accepted)
            res.ok = True
            self._set_state(MONITORING if self.live else IDLE, "cycle complete")
        except StageError as exc:
            res.error = str(exc)
            self._set_state(FAILED, str(exc))
            self.log(f"[OpenClaw] CYCLE {self.cycle} ABORTED - {exc}")
        except Exception as exc:  # noqa: BLE001
            res.error = f"{type(exc).__name__}: {exc}"
            self._set_state(FAILED, res.error)
            log.exception("cycle crashed")
            self.log(f"[OpenClaw] CYCLE {self.cycle} FAILED - {res.error}")
        finally:
            res.seconds = time.perf_counter() - t0
            self._retrain_pending = False
            self.history.append(res)
            self.registry.finish_cycle(
                self.cycle,
                ticks=res.stages.get("ingest", {}).get("ticks", 0),
                rows=res.stages.get("ingest", {}).get("feature_rows", 0),
                discovered=res.stages.get("research", {}).get("candidates", 0),
                accepted=res.stages.get("research", {}).get("accepted", 0),
                deployed=len(res.deployed),
                notes=res.error or "ok")
            self._write_report(res)

        self.log(f"[OpenClaw] CYCLE {self.cycle} {'COMPLETE' if res.ok else 'FAILED'} "
                 f"in {res.seconds:.1f}s")
        return res

    # -- stage 1: ingest ---------------------------------------------------
    def _stage_ingest(self, res: CycleResult, progress=None) -> dict:
        data_dir = self.cfg.resolve_path("dataset.data_dir")
        if self.reuse_dataset and any(data_dir.glob("*.csv")):
            self.log("[OpenClaw] reuse_existing_dataset=true - skipping ingest")
            res.stages["ingest"] = {"skipped": True, "reason": "reuse_existing_dataset"}
            return {}

        self._set_state(INGESTING, "IBKR Time & Sales -> structure engines")
        pipe = IngestPipeline(self.cfg, bus=self.bus, logger=self.log)
        try:
            stats = asyncio.run(pipe.run(max_ticks=self.max_ticks, progress=progress))
            out = stats.as_dict()
            res.stages["ingest"] = out
            self._verify("ingest",
                         out["ticks"] > 0 and out["feature_rows"] >= self.min_rows,
                         f"{out['ticks']:,} ticks -> {out['feature_rows']:,} research "
                         f"rows (need >= {self.min_rows})")
            self._ingest_store = pipe.store        # keep open for the export stage
            self._ingest_pipe = pipe
            return out
        except StageError:
            pipe.close()
            raise
        except Exception:
            pipe.close()
            raise

    # -- stage 2: export ---------------------------------------------------
    def _stage_export(self, res: CycleResult, ingest_stats: dict) -> None:
        if res.stages.get("ingest", {}).get("skipped"):
            res.stages["export"] = {"skipped": True}
            return
        self._set_state(EXPORTING, "standardized research dataset")
        try:
            out = self._ingest_pipe.export()
            res.stages["export"] = out
            self._verify("export", out["rows"] > 0 and not out["missing_required"],
                         f"{out['rows']:,} rows -> {out['output_dir']} "
                         f"({len(out['files'])} files)")
        finally:
            self._ingest_pipe.close()

    # -- stage 3: research (the LEAN bridge) -------------------------------
    def _stage_research(self, res: CycleResult, progress=None) -> ResearchEngine:
        self._set_state(RESEARCHING, "features -> discovery -> validation -> ranking")
        engine = ResearchEngine(self.cfg, logger=self.log)
        data = engine.run_data()
        self._verify("data", data["rows"] > 0 and data["metric_columns"] > 0,
                     f"{data['rows']:,} rows, {data['metric_columns']} structural metrics")

        feats = engine.run_features()
        self._verify("features", feats["total_features"] > 0,
                     f"{feats['total_features']} engineered features")

        disc = engine.run_discovery(progress=progress)
        self._verify("discovery", disc["candidates"] > 0,
                     f"{disc['candidates']} viable candidates "
                     f"(best Sharpe {disc['best_sharpe']})")

        val = engine.run_validation(top_n=self.validate_top_n, progress=progress)
        rank = engine.run_ranking()
        export = engine.run_export(only_accepted=True, top_n=self.export_top_n)

        res.stages["research"] = {
            **disc, **val, **rank,
            "lean_exported": export.get("exported", 0),
            "strategies_dir": export.get("output_dir", ""),
        }
        # An honest cycle can find nothing acceptable - that is a RESULT, not a
        # crash. We record it and stop before deploying anything unvalidated.
        self._verify("research", rank["ranked"] > 0,
                     f"{rank['ranked']} ranked, {rank['accepted']} accepted all gates")
        return engine

    # -- stage 4: record + compare ----------------------------------------
    def _stage_record(self, res: CycleResult, engine: ResearchEngine) -> list:
        template = self._dataset_template(engine)
        accepted_reports = [r for r in engine.ranked if r.accepted]
        records = [self.registry.record_accepted(r, template, self.cycle)
                   for r in accepted_reports]

        comparison = self.registry.compare_to_live(records)
        res.comparison = comparison
        res.stages["compare"] = comparison
        self.log(f"[OpenClaw] {len(records)} accepted; best new score "
                 f"{comparison['best_new_score']} vs best live "
                 f"{comparison['best_live_score']} "
                 f"({'IMPROVEMENT' if comparison['better'] else 'no improvement'})")
        return records

    def _dataset_template(self, engine: ResearchEngine) -> dict:
        """What live needs to rebuild the research feature frame identically."""
        ds = engine.dataset
        if ds is None:
            return {}
        return {
            "columns": list(ds.frame.columns),
            "metrics": list(ds.metrics),
            "meta": dict(ds.meta),
            "price_column": self.cfg.get("dataset.price_column", "price"),
        }

    # -- stage 5: deploy ---------------------------------------------------
    def _stage_deploy(self, res: CycleResult, records: list) -> None:
        if not records:
            self.log("[OpenClaw] nothing passed validation - nothing deployed. "
                     "The correct outcome when the data has no edge.")
            res.stages["deploy"] = {"deployed": 0, "reason": "no accepted strategies"}
            return
        if not self.auto_deploy:
            res.stages["deploy"] = {"deployed": 0, "reason": "auto_deploy disabled"}
            self.log("[OpenClaw] auto_deploy is off - strategies validated but not deployed")
            return
        if not res.comparison.get("better", False):
            res.stages["deploy"] = {"deployed": 0,
                                    "reason": "new research did not beat the incumbent"}
            self.log("[OpenClaw] incumbent strategy is still better - keeping it live")
            return

        self._set_state(DEPLOYING, "promoting validated strategies")
        compliant = [r for r in records if self._prop_compliant(r)]
        if not compliant:
            res.stages["deploy"] = {"deployed": 0, "reason": "no prop-compliant strategy"}
            self.log("[OpenClaw] accepted strategies violate prop rules - not deployed")
            return

        if self.live is None:
            self._ensure_live_manager()

        # Retire the incumbents this cycle replaces.
        for old in self.registry.deployed():
            if old.strategy_id not in [r.strategy_id for r in compliant]:
                self.live.disable(old.strategy_id, f"replaced by cycle {self.cycle}")

        for rec in compliant[: self.live.max_live]:
            if self.live.deploy(rec):
                res.deployed.append(rec.strategy_id)

        res.stages["deploy"] = {"deployed": len(res.deployed),
                                "ids": res.deployed}

    def _prop_compliant(self, rec) -> bool:
        """Enforce prop firm rules BEFORE a strategy is ever allowed live."""
        s = rec.strategy_dict()
        c = self.constraints
        problems = []
        if int(s.get("contracts", 1)) > c.max_position_contracts:
            problems.append(f"size {s.get('contracts')} > {c.max_position_contracts}")
        if rec.win_rate < c.min_win_rate:
            problems.append(f"win rate {rec.win_rate:.0%} < consistency floor "
                            f"{c.min_win_rate:.0%}")
        if float(s.get("stop_pct", 0)) > c.per_trade_max_dd_pct:
            problems.append(f"stop {s.get('stop_pct')}% > per-trade cap "
                            f"{c.per_trade_max_dd_pct}%")
        if problems:
            self.log(f"[OpenClaw] {rec.strategy_id} rejected by prop rules: "
                     f"{'; '.join(problems)}")
            return False
        return True

    # -------------------------------------------------------------- live
    def _ensure_live_manager(self):
        if self.live is not None:
            return self.live
        from .live_manager import LiveStrategyManager
        self.live = LiveStrategyManager(
            self.cfg, self.registry, self.bus, logger=self.log,
            on_degradation=self._on_degradation)
        return self.live

    def _on_degradation(self, strategy_id: str, reason: str, replaced: bool) -> None:
        """A live strategy decayed. Retrain unless a replacement already exists."""
        self.log(f"[OpenClaw] degradation on {strategy_id}: {reason} "
                 f"({'replacement promoted' if replaced else 'no replacement available'})")
        self.bus.publish(LIVE, LiveEvent(ts=time.time(), kind="degradation",
                                         strategy_id=strategy_id,
                                         detail={"reason": reason,
                                                 "replaced": replaced}))
        if self.retrain_on_degradation and not replaced:
            self._retrain_pending = True
            self.log("[OpenClaw] retrain scheduled - research will re-run")

    async def monitor(self) -> None:
        """Run the live manager until stopped (module 7 + module 8 enforcement)."""
        live = self._ensure_live_manager()
        for rec in self.registry.deployed():
            live.deploy(rec)
        self._set_state(MONITORING, f"{len(live.strategies)} strategies live")
        await live.run()

    # ------------------------------------------------------------ autonomous
    async def run_forever(self, ingest_live: bool = True) -> None:
        """Fully autonomous mode: cycle, monitor, retrain on degradation.

        Research cycles run in a worker thread so the event loop (live manager,
        API, WebSocket clients) never stalls while a cycle is grinding.
        """
        self._stop.clear()
        live = self._ensure_live_manager()
        tasks = [asyncio.create_task(self.monitor())]
        if ingest_live:
            tasks.append(asyncio.create_task(self._live_feed()))

        try:
            while not self._stop.is_set():
                due = (not self.registry.deployed()) or self._retrain_pending
                trigger = ("degradation" if self._retrain_pending
                           else "scheduled" if not self.registry.deployed()
                           else "interval")
                if due:
                    await asyncio.to_thread(self.run_cycle, trigger)
                    for rec in self.registry.deployed():
                        if rec.strategy_id not in live.strategies:
                            live.deploy(rec)

                try:
                    await asyncio.wait_for(self._stop.wait(),
                                           timeout=max(60.0, self.cycle_interval_h * 3600))
                except asyncio.TimeoutError:
                    self._retrain_pending = True     # scheduled refresh
        finally:
            live.stop()
            for t in tasks:
                t.cancel()
            self._set_state(IDLE, "stopped")

    async def _live_feed(self) -> None:
        """Keep the structural engines fed so the live manager has fresh rows."""
        pipe = IngestPipeline(self.cfg, bus=self.bus, logger=self.log)
        try:
            await pipe.run(max_ticks=0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("live feed stopped", extra={"err": str(exc)})
        finally:
            pipe.close()

    def stop(self) -> None:
        self._stop.set()
        if self.live:
            self.live.stop()

    # --------------------------------------------------------------- report
    def _write_report(self, res: CycleResult) -> None:
        out = self.cfg.resolve_path("output.reports_dir", "reports") / "openclaw"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"cycle_{res.cycle:03d}.json").write_text(
            json.dumps(res.as_dict(), indent=2, default=str), encoding="utf-8")

        s = res.stages
        ing, exp, rsc = s.get("ingest", {}), s.get("export", {}), s.get("research", {})
        lines = [
            f"# OpenClaw Cycle {res.cycle}",
            "",
            f"- **Trigger:** {res.trigger}",
            f"- **Started:** {res.started}",
            f"- **Duration:** {res.seconds:.1f}s",
            f"- **Result:** {'SUCCESS' if res.ok else 'FAILED - ' + res.error}",
            "",
            "## 1. Ingest (Non-Print Data Engine)",
            "",
            f"- Source: `{ing.get('source', '-')}`",
            f"- Ticks processed: {ing.get('ticks', 0):,}",
            f"- Structural events: {ing.get('structural_events', 0):,}",
            f"- Line-break lines: {ing.get('line_break_lines', 0):,}",
            f"- Research rows: {ing.get('feature_rows', 0):,}",
            f"- Throughput: {ing.get('ticks_per_sec', 0):,} ticks/s, "
            f"peak RAM {ing.get('rss_mb', 0)} MB",
            "",
            "## 2. Export",
            "",
            f"- Rows: {exp.get('rows', 0):,}",
            f"- Output: `{exp.get('output_dir', '-')}`",
            "",
            "## 3. Research (QuantConnect LEAN Bridge)",
            "",
            f"- Candidates discovered: {rsc.get('candidates', 0)}",
            f"- Validated: {rsc.get('validated', 0)}",
            f"- **Accepted (passed every gate): {rsc.get('accepted', 0)}**",
            f"- LEAN algorithms exported: {rsc.get('lean_exported', 0)} -> "
            f"`{rsc.get('strategies_dir', '-')}`",
            "",
            "## 4. New vs Previous",
            "",
        ]
        c = res.comparison
        if c:
            lines += [
                f"- Best new score: **{c.get('best_new_score', 0)}** "
                f"(`{c.get('best_new_id', '-')}`)",
                f"- Best live score: {c.get('best_live_score', 0)} "
                f"(`{c.get('best_live_id', '-') or 'none'}`)",
                f"- Improvement: {c.get('improvement', 0):+.4f} -> "
                f"{'DEPLOY' if c.get('better') else 'KEEP INCUMBENT'}",
                "",
            ]
        lines += [
            "## 5. Deployment",
            "",
            f"- Deployed this cycle: {', '.join(res.deployed) if res.deployed else 'none'}",
            f"- Currently live: {', '.join(r.strategy_id for r in self.registry.deployed()) or 'none'}",
            "",
            "## Prop-firm rules enforced",
            "",
            f"- Min hold: {self.constraints.min_hold_seconds:.0f}s",
            f"- Daily profit cap: ${self.constraints.daily_profit_cap:,.0f} "
            f"(target ${self.constraints.daily_profit_target:,.0f})",
            f"- Account max DD: {self.constraints.account_max_dd_pct}%",
            f"- EOD trailing DD: ${self.constraints.eod_max_drawdown:,.0f}",
            f"- Max size: {self.constraints.max_position_contracts} contracts "
            f"({self.constraints.micro_scaling}:1 micro scaling)",
            f"- Consistency: {self.constraints.min_win_rate:.0%} min win rate",
            "",
        ]
        (out / f"cycle_{res.cycle:03d}.md").write_text("\n".join(lines), encoding="utf-8")
        self.log(f"[OpenClaw] report -> {out / f'cycle_{res.cycle:03d}.md'}")
