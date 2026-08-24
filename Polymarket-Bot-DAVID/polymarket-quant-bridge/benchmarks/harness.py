"""Benchmark harness (Phase 12) — measure before claiming.

Built first, and deliberately, because the first attempt at measuring the
index change produced this:

    run 1: 45.18s   run 2: 28.52s   run 3: 29.41s

on an unchanged configuration. On a 2.4 GB SQLite database the OS file cache
dominates, so a single before/after pair can show any result you like — a ~9 s
saving disappears inside ~16 s of noise. Two things fix that, and both are
implemented here:

  INTERLEAVING   A/B/A/B rather than AAA/BBB. Cache state and thermal drift
                 move slowly, so alternating cancels them; running all of A
                 then all of B attributes the drift to the change.

  MEDIAN OF N    not mean, and not min. The mean is dragged by a single cold
                 outlier; the min flatters whichever arm happened to run
                 warmest. The median of >= 5 is stable against both.

It also caught a real methodology error: dropping an index to measure "before"
did nothing, because opening the store re-created it from the schema. The
harness reports the *observed configuration* alongside the timing so that a
result which silently measured the wrong thing is visible rather than believed.

Usage:

    python -m benchmarks.harness list
    python -m benchmarks.harness run sql_redemptions --reps 5
    python -m benchmarks.harness ab sql_redemptions --reps 5
"""

from __future__ import annotations

import argparse
import gc
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class Arm:
    """One side of an experiment: the timed call, plus untimed state changes.

    Keeping setup/teardown out of `run` is what makes an A/B honest -- see
    `time_once`.
    """

    run: Callable[[], object]
    setup: Callable[[], object] | None = None
    teardown: Callable[[], object] | None = None

    def __call__(self) -> object:
        return self.run()


@dataclass
class Timing:
    """The result of timing one arm of an experiment."""

    label: str
    samples: list[float] = field(default_factory=list)
    note: str = ""

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0

    @property
    def spread(self) -> float:
        """Max - min, as a fraction of the median. High means untrustworthy."""
        if len(self.samples) < 2 or self.median <= 0:
            return 0.0
        return (max(self.samples) - min(self.samples)) / self.median

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "median_s": round(self.median, 4),
            "min_s": round(min(self.samples), 4) if self.samples else 0.0,
            "max_s": round(max(self.samples), 4) if self.samples else 0.0,
            "spread": round(self.spread, 3),
            "n": len(self.samples),
            "note": self.note,
        }


def time_once(fn: Callable[[], object],
              setup: Callable[[], object] | None = None,
              teardown: Callable[[], object] | None = None) -> float:
    """One timed call, with GC quiesced so a collection cycle is not measured.

    `setup` and `teardown` run OUTSIDE the timed region. This is not a
    convenience -- the first version of this harness timed an arm that dropped
    an index, ran the query, then re-created the index inside the same block.
    The rebuild cost ~5.7 s and was attributed to the query, reporting 55x for
    a change that is really 25x. Any state a workload must restore belongs
    here, never inside `fn`.
    """
    if setup is not None:
        setup()
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        start = time.perf_counter()
        fn()
        return time.perf_counter() - start
    finally:
        if was_enabled:
            gc.enable()
        if teardown is not None:
            teardown()


def _as_arm(fn) -> Arm:
    return fn if isinstance(fn, Arm) else Arm(fn)


def measure(fn: Callable[[], object], reps: int = 5, warmup: int = 1,
            label: str = "") -> Timing:
    """Warm up, then time `reps` times.

    The warm-up is discarded rather than averaged in: the first call pays for
    page cache, lazy imports and SQLite's own preparation, none of which recur
    in the steady state this is trying to describe.
    """
    arm = _as_arm(fn)
    for _ in range(warmup):
        arm()
    t = Timing(label=label or getattr(arm.run, "__name__", "workload"))
    for _ in range(reps):
        t.samples.append(time_once(arm.run, arm.setup, arm.teardown))
    return t


def compare(a: Callable[[], object], b: Callable[[], object], *,
            reps: int = 5, warmup: int = 1,
            label_a: str = "A", label_b: str = "B") -> dict:
    """Interleaved A/B. See the module docstring for why this matters."""
    arm_a, arm_b = _as_arm(a), _as_arm(b)
    for _ in range(warmup):
        arm_a(); arm_b()

    ta, tb = Timing(label=label_a), Timing(label=label_b)
    for _ in range(reps):
        ta.samples.append(time_once(arm_a.run, arm_a.setup, arm_a.teardown))
        tb.samples.append(time_once(arm_b.run, arm_b.setup, arm_b.teardown))

    speedup = (ta.median / tb.median) if tb.median > 0 else 0.0
    noisy = max(ta.spread, tb.spread)
    verdict = "RELIABLE"
    if noisy > 0.5:
        verdict = "NOISY - spread exceeds 50% of median; do not quote this"
    elif abs(speedup - 1.0) < noisy:
        verdict = "INCONCLUSIVE - the difference is inside the noise"

    return {
        "a": ta.as_dict(), "b": tb.as_dict(),
        "speedup": round(speedup, 2),
        "verdict": verdict,
    }


# ---------------------------------------------------------------------------
# Workloads. Each returns a zero-argument callable, plus an optional A/B pair.
# ---------------------------------------------------------------------------

def _intel_path() -> Path:
    """The store the app itself would open, resolved the same way it resolves it."""
    from pqb import config as pqb_config

    cfg = pqb_config.load(ROOT / "config" / "config.yaml")
    return Path(cfg.intel_path)


REDEMPTIONS_SQL = (
    "SELECT wallet, market_id, MIN(ts) ts FROM wallet_trades "
    "WHERE event_type='REDEEM' AND market_id != '' GROUP BY wallet, market_id"
)
SHORTLIST_SQL = (
    "SELECT wallet, COUNT(*) n FROM wallet_trades WHERE market_id != '' "
    "GROUP BY wallet HAVING n >= 5 ORDER BY n DESC LIMIT 40"
)


def workload_sql(sql: str, index: str | None = None, ddl: str | None = None):
    """A query, optionally with a named index dropped/created around the arms.

    Returns (with_index, without_index, describe). The describe callable reports
    the *observed* plan, so a run that silently measured the wrong configuration
    is caught rather than trusted.
    """
    import sqlite3

    path = _intel_path()
    conn = sqlite3.connect(str(path))

    def run() -> object:
        return conn.execute(sql).fetchall()

    def drop_index() -> None:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.commit()

    def create_index() -> None:
        conn.execute(ddl)
        conn.commit()

    def describe() -> str:
        return conn.execute("EXPLAIN QUERY PLAN " + sql).fetchone()[-1]

    # The un-indexed arm is the same query; only the surrounding state differs,
    # and that state is moved into setup/teardown so it is not timed.
    alt = Arm(run, setup=drop_index, teardown=create_index) if (index and ddl) else None
    return Arm(run), alt, describe


def workload_bootstrap():
    """The Monte Carlo hot path — bottleneck #2, and the best Rust candidate."""
    import random

    from pqb.wallet_state_research.backtest import bootstrap_roi

    rng = random.Random(7)
    rois = [rng.uniform(-0.5, 0.5) for _ in range(100)]

    def run() -> object:
        return bootstrap_roi(rois)

    return Arm(run), None, lambda: "bootstrap_roi(n=100, iterations=2000)"


def workload_events():
    """Event loading + the signed_shares/is_buy property path — bottleneck #4."""
    from pqb.wallet_state_research.events import load_events

    path = _intel_path()

    def run() -> object:
        evs = load_events(path)
        # Touch the properties the state machine touches, so the cost of
        # is_buy/signed_shares is inside the measurement rather than beside it.
        return sum(1 for e in evs if e.is_buy) + sum(e.signed_shares for e in evs)

    return Arm(run), None, lambda: f"load_events({path.name}) + property scan"


WORKLOADS: dict[str, Callable] = {
    "sql_redemptions": lambda: workload_sql(
        REDEMPTIONS_SQL, "idx_wt_event_type",
        "CREATE INDEX IF NOT EXISTS idx_wt_event_type "
        "ON wallet_trades(event_type, wallet, market_id, ts)"),
    "sql_shortlist": lambda: workload_sql(
        SHORTLIST_SQL, "idx_wt_wallet_market",
        "CREATE INDEX IF NOT EXISTS idx_wt_wallet_market "
        "ON wallet_trades(wallet, market_id)"),
    "bootstrap": workload_bootstrap,
    "events": workload_events,
}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="benchmarks.harness")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list")

    r = sub.add_parser("run")
    r.add_argument("workload", choices=sorted(WORKLOADS))
    r.add_argument("--reps", type=int, default=5)
    r.add_argument("--json", action="store_true")

    ab = sub.add_parser("ab", help="Interleaved A/B against the un-indexed arm.")
    ab.add_argument("workload", choices=sorted(WORKLOADS))
    ab.add_argument("--reps", type=int, default=5)
    ab.add_argument("--json", action="store_true")

    args = p.parse_args(argv)

    if args.cmd == "list":
        for name in sorted(WORKLOADS):
            print(f"  {name}")
        return 0

    run, alt, describe = WORKLOADS[args.workload]()

    if args.cmd == "run":
        t = measure(run, reps=args.reps, label=args.workload)
        out = t.as_dict()
        out["observed"] = describe()
        print(json.dumps(out, indent=2) if args.json else _fmt_one(out))
        return 0

    if alt is None:
        print(f"workload {args.workload!r} has no A/B arm; use `run`.")
        return 1
    res = compare(alt, run, reps=args.reps,
                  label_a="without index", label_b="with index")
    res["observed"] = describe()
    print(json.dumps(res, indent=2) if args.json else _fmt_ab(res))
    return 0


def _fmt_one(d: dict) -> str:
    return (f"{d['label']}\n"
            f"  median {d['median_s']:.4f}s  (min {d['min_s']:.4f} "
            f"max {d['max_s']:.4f}, n={d['n']}, spread {d['spread']:.1%})\n"
            f"  observed: {d['observed']}")


def _fmt_ab(d: dict) -> str:
    a, b = d["a"], d["b"]
    return (f"  {a['label']:16s} median {a['median_s']:8.4f}s  spread {a['spread']:.1%}\n"
            f"  {b['label']:16s} median {b['median_s']:8.4f}s  spread {b['spread']:.1%}\n"
            f"  speedup {d['speedup']}x\n"
            f"  verdict {d['verdict']}\n"
            f"  observed: {d['observed']}")


if __name__ == "__main__":
    raise SystemExit(main())
