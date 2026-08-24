"""Autonomous Non-Print Quantitative Research Platform - entry point.

GUI:
    python app.py                     # launch the desktop app
    python app.py my_config.yaml      # with a custom config

Headless (each stage is independently runnable - that is the whole design):
    python app.py --ingest [N]        # Module 2/3: IBKR T&S -> engines -> DB -> CSV
    python app.py --replay            # rebuild engines from stored ticks (replay)
    python app.py --cycle             # Module 1: one full OpenClaw research cycle
    python app.py --auto              # fully autonomous: cycle + live + retrain
    python app.py --serve             # REST + WebSocket API only
    python app.py --live              # live strategy manager only
    python app.py --research          # the LEAN bridge only (existing CSVs)
    python app.py --selftest          # end-to-end check, exits 0/1
    python app.py --validate          # data + bridge compatibility check
    python app.py --benchmark [rows]  # throughput / memory baseline

If no data exists, a synthetic tick stream is generated so every command above
runs on a fresh machine with no IBKR, no Postgres and no market data.
"""
from __future__ import annotations

import asyncio
import sys
import os
import traceback
from datetime import datetime

# Ensure the package root is importable when run directly (source mode).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.config import Config, PROJECT_ROOT           # noqa: E402


def _log_path():
    return PROJECT_ROOT / "qc_lean_bridge.log"


def _setup_frozen_logging():
    """In a windowed frozen build there is no console; capture stdout/stderr and
    uncaught exceptions to a log file next to the executable so failures are
    diagnosable."""
    if not getattr(sys, "frozen", False):
        return
    try:
        fh = open(_log_path(), "a", encoding="utf-8", buffering=1)
        fh.write(f"\n===== launch {datetime.now().isoformat(timespec='seconds')} =====\n")
        sys.stdout = fh
        sys.stderr = fh
    except Exception:  # noqa: BLE001
        pass

    def hook(exc_type, exc, tb):
        try:
            with open(_log_path(), "a", encoding="utf-8") as f:
                f.write("UNCAUGHT EXCEPTION:\n")
                traceback.print_exception(exc_type, exc, tb, file=f)
        except Exception:  # noqa: BLE001
            pass
    sys.excepthook = hook


def _setup_logging(cfg: Config, console: bool = True):
    from core.logging_setup import setup
    return setup(log_dir=cfg.resolve_path("logging.dir", "logs"),
                 level=str(cfg.get("logging.level", "INFO")),
                 console=console and not getattr(sys, "frozen", False))


def _ensure_sample_data(cfg: Config):
    """Guarantee the bridge has SOMETHING to research (dataset CSVs)."""
    data_dir = cfg.resolve_path("dataset.data_dir")
    has_csv = data_dir.exists() and any(data_dir.glob("*.csv"))
    if has_csv:
        return
    try:
        from core.sample_data_gen import build
        out = build(out_dir=data_dir)
        print(f"[startup] Generated sample data -> {out}")
    except Exception as exc:  # noqa: BLE001
        print(f"[startup] Could not generate sample data: {exc}")


# ---------------------------------------------------------------- platform
def _ingest(config_path, max_ticks: int) -> int:
    """Module 2 + 3 + export: ticks -> structure -> database -> research CSVs."""
    from core.ingest import run_ingest
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    out = run_ingest(cfg, max_ticks=max_ticks, logger=print, do_export=True)
    exp = out.get("export", {})
    print(f"\n[ingest] {out['ticks']:,} ticks -> "
          f"{out['structural_events']:,} structural events, "
          f"{out['line_break_lines']:,} lines, {out['feature_rows']:,} research rows")
    print(f"[ingest] {out['ticks_per_sec']:,} ticks/s, peak RAM {out['rss_mb']} MB")
    print(f"[ingest] dataset -> {exp.get('output_dir', '-')} "
          f"({exp.get('rows', 0):,} rows)")
    return 0 if out["feature_rows"] > 0 else 1


def _replay(config_path) -> int:
    """Rebuild every engine from stored ticks - proves the DB is replayable."""
    from core.ingest import IngestPipeline
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    pipe = IngestPipeline(cfg, logger=print)
    try:
        stats = asyncio.run(pipe.replay())
        print(f"[replay] {stats.ticks:,} ticks replayed -> "
              f"{stats.features:,} research rows reproduced")
        return 0 if stats.ticks > 0 else 1
    finally:
        pipe.close()


def _cycle(config_path) -> int:
    """One full OpenClaw research cycle."""
    from core.openclaw import OpenClaw
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    oc = OpenClaw(cfg, logger=print)
    res = oc.run_cycle(trigger="cli")
    return 0 if res.ok else 1


def _auto(config_path) -> int:
    """Fully autonomous: research + live + API + retrain-on-degradation."""
    from core.api_server import ApiServer
    from core.openclaw import OpenClaw
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    oc = OpenClaw(cfg, logger=print)

    async def main():
        api = None
        if bool(cfg.get("api.enabled", True)):
            api = ApiServer(cfg, oc.bus, openclaw=oc, logger=print)
            await api.start()
        try:
            await oc.run_forever()
        finally:
            if api:
                await api.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[auto] stopped")
    return 0


def _serve(config_path) -> int:
    """REST + WebSocket API against the current registry/database."""
    from core.api_server import ApiServer
    from core.event_store import build_store
    from core.openclaw import OpenClaw
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    oc = OpenClaw(cfg, logger=print)
    store = build_store(cfg)

    async def main():
        api = ApiServer(cfg, oc.bus, openclaw=oc, store=store, logger=print)
        await api.start()
        print(f"[serve] GET  http://{api.host}:{api.port}/api/status")
        print(f"[serve] WS   ws://{api.host}:{api.port}/ws?topics=live,control,log")
        print("[serve] Ctrl-C to stop")
        await api.serve_forever()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[serve] stopped")
    finally:
        store.close()
    return 0


def _live(config_path) -> int:
    """Live strategy manager against a live/replayed structural feed."""
    from core.openclaw import OpenClaw
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    oc = OpenClaw(cfg, logger=print)
    if not oc.registry.deployed():
        print("[live] no deployed strategies. Run --cycle first "
              "(it deploys what passes validation).")
        return 1

    async def main():
        await asyncio.gather(oc.monitor(), oc._live_feed())

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[live] stopped")
    return 0


def _research(config_path) -> int:
    """The LEAN bridge alone, on whatever CSVs are already exported."""
    from core.pipeline import ResearchEngine
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    _ensure_sample_data(cfg)
    eng = ResearchEngine(cfg, logger=print)
    eng.run_full(top_n_validate=int(cfg.get("openclaw.validate_top_n", 40)),
                 top_n_export=int(cfg.get("openclaw.export_top_n", 5)))
    df = eng.ranking_table()
    print(f"\n[research] ranked {len(df)} strategies, "
          f"{int(df['accepted'].sum()) if 'accepted' in df else 0} accepted")
    return 0 if not df.empty else 1


# ---------------------------------------------------------------- checks
def _selftest(config_path) -> int:
    """End-to-end: data engine -> export -> research -> validate -> LEAN code.

    Exercises the REAL platform path (engines + database + bridge), just small,
    so a green self-test means the whole chain works on this machine.
    """
    from core.ingest import IngestPipeline
    from core.pipeline import ResearchEngine

    cfg = Config.load(config_path)
    _setup_logging(cfg, console=True)

    # Small, deterministic, and independent of IBKR/Postgres.
    ticks = 120_000
    cfg.set("ingest.source", "synthetic")
    cfg.set("ingest.synthetic_ticks", ticks)
    cfg.set("discovery.max_candidates", 150)
    cfg.set("validation.monte_carlo_runs", 100)
    cfg.set("validation.min_trades", 5)

    print("[selftest] 1/3 data engine (non-print + 100 line-break engines)...")
    pipe = IngestPipeline(cfg, logger=print)
    try:
        stats = asyncio.run(pipe.run(max_ticks=ticks))
        exp = pipe.export()
    finally:
        pipe.close()

    engines_ok = (stats.ticks > 0 and stats.structural_events > 0
                  and stats.lines > 0 and stats.features > 50)
    export_ok = exp["rows"] > 0 and not exp["missing_required"]
    print(f"[selftest] engines: {stats.ticks:,} ticks -> "
          f"{stats.structural_events:,} structural events, {stats.lines:,} lines, "
          f"{stats.features:,} rows -> {'OK' if engines_ok else 'FAIL'}")
    print(f"[selftest] export: {exp['rows']:,} rows, "
          f"{len(exp['files'])} files -> {'OK' if export_ok else 'FAIL'}")

    print("[selftest] 2/3 research bridge...")
    eng = ResearchEngine(cfg, logger=print)
    eng.run_data()
    eng.run_features()
    eng.run_discovery()
    eng.run_validation(top_n=10)
    eng.run_ranking()
    res = eng.run_export(top_n=3, only_accepted=False)
    df = eng.ranking_table()
    research_ok = (not df.empty) and res.get("exported", 0) > 0
    print(f"[selftest] research: ranked={len(df)} exported={res.get('exported',0)} "
          f"-> {'OK' if research_ok else 'FAIL'}")

    print("[selftest] 3/3 prop-firm gate...")
    gate_ok = _selftest_gate(cfg)
    print(f"[selftest] prop gate: {'OK' if gate_ok else 'FAIL'}")

    ok = engines_ok and export_ok and research_ok and gate_ok
    print(f"\n[selftest] {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _selftest_gate(cfg) -> bool:
    """The min-hold rule is the one most likely to fail an evaluation - prove it."""
    from core.prop_firm import PropFirmRiskManager
    from core.risk_management import PropConstraints

    c = PropConstraints.from_config(cfg)
    rm = PropFirmRiskManager(c, starting_equity=50_000.0, point_value=5.0)
    t0 = 1_800_000_000.0

    entry = rm.check_entry(t0, "S1", "long", 99)
    if not entry.allowed or entry.contracts > c.max_position_contracts:
        return False                       # must cap size to the prop limit
    rm.on_entry_fill(t0, "S1", "long", 5000.0, entry.contracts, 2.0, 2.0)

    too_early = rm.check_exit(t0 + c.min_hold_seconds - 1.0, "S1")
    if too_early.allowed:
        return False                       # closing inside min-hold must be refused
    allowed = rm.check_exit(t0 + c.min_hold_seconds + 0.5, "S1")
    if not allowed.allowed:
        return False
    rm.on_exit_fill(t0 + c.min_hold_seconds + 0.5, "S1", 5010.0, "target")
    return rm.state.equity > 50_000.0


def _validate(config_path) -> int:
    """Phase 1 - validate CSV persistence + Bridge compatibility; exit 0/1."""
    from core.persistence import bridge_compatibility_check
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    _ensure_sample_data(cfg)
    print("Validating datasets and QC LEAN Bridge compatibility...")
    report = bridge_compatibility_check(cfg, logger=print)
    print(f"\nfiles OK: {report['files_ok']}/{report['files_checked']} | "
          f"bridge import: {'OK' if report['bridge_import_ok'] else 'FAIL'} | "
          f"imported {report['imported_rows']:,} rows, "
          f"{report['imported_metrics']} metrics")
    print(f"COMPATIBLE: {'YES' if report['compatible'] else 'NO'}")
    return 0 if report["compatible"] else 1


def _benchmark(sizes) -> int:
    from core.benchmark import run_benchmark
    if not sizes:
        sizes = [100_000, 1_000_000]
    run_benchmark(sizes=tuple(int(s) for s in sizes), do_discovery=False)
    return 0


# ------------------------------------------------------------------- main
def main():
    _setup_frozen_logging()
    args = list(sys.argv[1:])

    def take(flag: str) -> bool:
        if flag in args:
            args.remove(flag)
            return True
        return False

    def numbers() -> list[int]:
        nums = [int(a) for a in args if a.isdigit()]
        for a in list(args):
            if a.isdigit():
                args.remove(a)
        return nums

    do_ingest = take("--ingest")
    do_replay = take("--replay")
    do_cycle = take("--cycle")
    do_auto = take("--auto")
    do_serve = take("--serve")
    do_live = take("--live")
    do_research = take("--research")
    do_selftest = take("--selftest")
    do_validate = take("--validate")
    do_benchmark = take("--benchmark")

    nums = numbers()
    config_path = args[0] if args else None

    if do_ingest:
        sys.exit(_ingest(config_path, nums[0] if nums else 0))
    if do_replay:
        sys.exit(_replay(config_path))
    if do_cycle:
        sys.exit(_cycle(config_path))
    if do_auto:
        sys.exit(_auto(config_path))
    if do_serve:
        sys.exit(_serve(config_path))
    if do_live:
        sys.exit(_live(config_path))
    if do_research:
        sys.exit(_research(config_path))
    if do_selftest:
        sys.exit(_selftest(config_path))
    if do_validate:
        sys.exit(_validate(config_path))
    if do_benchmark:
        sys.exit(_benchmark(nums))

    # ---- GUI
    cfg = Config.load(config_path)
    _setup_logging(cfg)
    _ensure_sample_data(cfg)

    from gui.qt import QApplication, run_app_exec, BINDING

    print(f"[gui] starting with {BINDING}")
    app = QApplication(sys.argv)
    app.setApplicationName("Non-Print Research Platform")
    from gui.main_window import MainWindow
    win = MainWindow(config_path)
    win.show()
    print("[gui] window shown; entering event loop")
    code = run_app_exec(app)
    print(f"[gui] event loop returned {code}")
    sys.exit(code)


if __name__ == "__main__":
    main()
