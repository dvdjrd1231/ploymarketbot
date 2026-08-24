"""Phase 3/5 (Bridge side) - Profiling & benchmark harness.

Establishes a performance baseline for the *Bridge's* own data processing so
optimization work can be measured against it. For each dataset size it times the
per-row stages (import, feature engineering, optional discovery) and records
throughput and peak memory.

Note on scope: the engine-internal profiling in the spec (tick ingestion, the
100 line-break engines, GC pressure, etc.) belongs to the client's Multi-
Resolution Engine, which is a separate codebase. This harness covers everything
downstream of the CSV hand-off, which is what the Bridge controls.

Run:  python -m core.benchmark          (100k + 1M)
      python app.py --benchmark 100000  (custom size via the app)
"""
from __future__ import annotations

import gc
import time
import tracemalloc
from pathlib import Path

from .config import Config
from .data_pipeline import DataPipeline
from .features import FeatureEngineer
from .risk_management import PropConstraints


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run_benchmark(sizes=(100_000, 1_000_000), config: Config | None = None,
                  logger=print, do_discovery: bool = False,
                  discovery_budget: int = 100) -> list[dict]:
    import tempfile
    from .sample_data_gen import build as build_sample

    cfg = config or Config.load()
    results: list[dict] = []

    for size in sizes:
        logger(f"\n=== Benchmark: {size:,} line-break bars ===")
        tmp = Path(tempfile.mkdtemp(prefix=f"qcbench_{size}_"))
        logger(f"  generating synthetic dataset ({size:,} rows x 3 files)...")
        t_gen0 = time.perf_counter()
        build_sample(out_dir=tmp, n=size)
        gen_secs = time.perf_counter() - t_gen0

        bench_cfg = Config(cfg.data.copy())
        bench_cfg.set("dataset.data_dir", str(tmp))

        gc.collect()
        tracemalloc.start()

        # --- Stage: import / persistence read ---
        t0 = time.perf_counter()
        ds = DataPipeline(bench_cfg, logger=lambda m: None).load()
        t_import = time.perf_counter() - t0
        rows = len(ds.frame)

        # --- Stage: feature engineering (the O(N) per-row work) ---
        t0 = time.perf_counter()
        fs = FeatureEngineer(ds, logger=lambda m: None).build()
        t_features = time.perf_counter() - t0
        n_features = len(fs.names)

        # --- Stage: discovery (optional, small budget) ---
        t_discovery = 0.0
        if do_discovery:
            from .strategy_discovery import StrategyDiscovery
            bench_cfg.set("discovery.max_candidates", discovery_budget)
            t0 = time.perf_counter()
            StrategyDiscovery(fs, bench_cfg,
                              PropConstraints.from_config(bench_cfg)).run()
            t_discovery = time.perf_counter() - t0

        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        total = t_import + t_features + t_discovery
        rec = {
            "rows": rows,
            "features": n_features,
            "gen_secs": gen_secs,
            "import_secs": t_import,
            "features_secs": t_features,
            "discovery_secs": t_discovery,
            "total_secs": total,
            "import_rows_per_sec": rows / t_import if t_import else 0,
            "features_rows_per_sec": rows / t_features if t_features else 0,
            "throughput_rows_per_sec": rows / total if total else 0,
            "peak_python_mem": peak,
        }
        results.append(rec)

        logger(f"  rows in                : {rows:,}")
        logger(f"  import                 : {t_import:8.2f}s  "
               f"({rec['import_rows_per_sec']:,.0f} rows/s)")
        logger(f"  feature engineering    : {t_features:8.2f}s  "
               f"({rec['features_rows_per_sec']:,.0f} rows/s, {n_features} features)")
        if do_discovery:
            logger(f"  discovery ({discovery_budget:>4} cand): {t_discovery:8.2f}s")
        logger(f"  end-to-end throughput  : {rec['throughput_rows_per_sec']:,.0f} rows/s")
        logger(f"  peak Python memory     : {_fmt_bytes(peak)}")

        # cleanup temp data
        del ds, fs
        gc.collect()
        for p in tmp.glob("*"):
            try:
                p.unlink()
            except Exception:  # noqa: BLE001
                pass
        try:
            tmp.rmdir()
        except Exception:  # noqa: BLE001
            pass

    _print_scaling(results, logger)
    return results


def _print_scaling(results: list[dict], logger):
    if len(results) < 2:
        return
    logger("\n=== Scaling (should stay ~linear, O(N)) ===")
    base = results[0]
    for r in results:
        ratio_rows = r["rows"] / base["rows"] if base["rows"] else 1
        ratio_time = r["total_secs"] / base["total_secs"] if base["total_secs"] else 1
        logger(f"  {r['rows']:>10,} rows: {ratio_rows:5.1f}x data -> "
               f"{ratio_time:5.1f}x time  "
               f"({'linear' if ratio_time <= ratio_rows * 1.3 else 'super-linear!'})")


if __name__ == "__main__":
    run_benchmark(sizes=(100_000, 1_000_000))
