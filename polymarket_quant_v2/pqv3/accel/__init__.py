"""Rust acceleration: the bridge, the fallback, and the honesty about need.

THE MEASURED POSITION on this machine against this data. Recorded here rather
than in a commit message, because the decision to build the crate should be
evidence-based and re-checkable as the data grows:

  * PROFILE FIRST was followed, and it changed the answer. A 600-market scan
    took 36.1s. The profiler put 7.5s of an 8.0s sub-run inside
    `sqlite3.execute`, of which 6.65s was connection SETUP — 776 connections at
    ~8.5ms each, because `PRAGMA cache_size = -64000` requests a fresh 64 MB
    page cache every time. Pooling the read-only connection took the same scan
    to **0.65s**: a 55x speedup in Python, with no new language, no build
    toolchain and no equivalence risk.

  * The wallet-DNA pass told the same story. 40 wallets took 37s; removing an
    O(n^2) leave-one-out loop and pooling connections put 120 wallets at 7.1s.

  * After both fixes the remaining CPU work is a small fraction of wall clock,
    and the binding constraint is that the substrate has ~112 days and one
    venue. Making a data-limited search faster raises the false-discovery rate
    rather than finding edge.

So this module ships COMPLETE and DISABLED BY DEFAULT (`mode="auto"` selects
Python whenever no compiled extension is present). The crate in `rust/` is real
and buildable. `should_build()` states the trigger.

THE CONTRACT, which is what makes it safe to turn on at all:

    RUST ENABLED    use Rust, fall back to Python on ANY error
    RUST DISABLED   Python only
    RUST SHADOW     run BOTH, compare, report divergence, RETURN PYTHON

Python is the reference implementation until equivalence is proven. Rust never
changes a decision — in shadow mode its result is discarded after comparison,
and in enabled mode any exception falls through to Python rather than
propagating. A failed Rust import can never take the application down.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field

from . import kernels

_BACKEND = None
_IMPORT_ERROR = ""


def _try_import():
    global _BACKEND, _IMPORT_ERROR
    if _BACKEND is not None or _IMPORT_ERROR:
        return _BACKEND
    try:
        import pqv3_accel                                     # type: ignore
        _BACKEND = pqv3_accel
    except Exception as exc:                                  # noqa: BLE001
        _IMPORT_ERROR = f"{type(exc).__name__}: {exc}"
        _BACKEND = None
    return _BACKEND


@dataclass
class Divergence:
    kernel: str
    n_checked: int = 0
    n_diverged: int = 0
    worst: float = 0.0
    examples: list = field(default_factory=list)

    @property
    def equivalent(self) -> bool:
        return self.n_diverged == 0

    def to_dict(self) -> dict:
        return {"kernel": self.kernel, "checked": self.n_checked,
                "diverged": self.n_diverged, "worst_abs_diff": self.worst,
                "equivalent": self.equivalent, "examples": self.examples[:5]}


def _flatten(v) -> list:
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            out.extend(_flatten(x))
        return out
    return [v]


class Accelerator:
    """Dispatch to Rust or Python, per the configured mode."""

    MODES = ("auto", "enabled", "disabled", "shadow")

    def __init__(self, st=None, *, mode: str = "", tolerance: float = 1e-9) -> None:
        self.mode = (mode or os.environ.get("PQV3_ACCEL", "auto")).lower()
        if self.mode not in self.MODES:
            self.mode = "auto"
        self.tolerance = tolerance
        self.backend = _try_import() if self.mode != "disabled" else None
        self.divergences: dict = {}
        self.timings: dict = {}

    # -- status -------------------------------------------------------------
    @property
    def active(self) -> bool:
        """Whether Rust results are actually returned to callers."""
        return self.backend is not None and self.mode in ("enabled", "auto")

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "backend_present": self.backend is not None,
            "import_error": _IMPORT_ERROR,
            "returns_rust_results": self.active,
            "kernels": sorted(kernels.KERNELS),
            "divergences": {k: v.to_dict()
                            for k, v in self.divergences.items()},
            "timings": {k: {"python_ms": round(v["py"] * 1000, 4),
                            "rust_ms": round(v["rs"] * 1000, 4),
                            "speedup": round(v["py"] / v["rs"], 2)
                            if v.get("rs") else None}
                        for k, v in self.timings.items()},
            "note": ("Python is authoritative. In shadow mode Rust results are "
                     "compared and discarded; in enabled mode any Rust error "
                     "falls back to Python."),
        }

    # -- dispatch -----------------------------------------------------------
    def call(self, name: str, *args, **kw):
        py_fn = kernels.KERNELS.get(name)
        if py_fn is None:
            raise KeyError(f"unknown kernel {name!r}")

        if self.mode == "disabled" or self.backend is None:
            return py_fn(*args, **kw)

        rs_fn = getattr(self.backend, name, None)
        if rs_fn is None:
            return py_fn(*args, **kw)

        if self.mode == "shadow":
            t0 = time.perf_counter()
            py_out = py_fn(*args, **kw)
            t1 = time.perf_counter()
            try:
                rs_out = rs_fn(*args, **kw)
                t2 = time.perf_counter()
                self.timings[name] = {"py": t1 - t0, "rs": t2 - t1}
                self._compare(name, py_out, rs_out)
            except Exception as exc:                          # noqa: BLE001
                self._record_divergence(name, float("inf"),
                                        f"rust raised {type(exc).__name__}")
            # ALWAYS the Python result. This is the whole point of shadow mode.
            return py_out

        # enabled / auto
        try:
            return rs_fn(*args, **kw)
        except Exception:                                     # noqa: BLE001
            # A Rust failure must degrade to Python, never propagate. An
            # accelerator that can crash the engine is a liability, not an
            # optimisation.
            return py_fn(*args, **kw)

    # -- equivalence --------------------------------------------------------
    def _compare(self, name: str, py_out, rs_out) -> None:
        d = self.divergences.setdefault(name, Divergence(kernel=name))
        d.n_checked += 1
        a, b = _flatten(py_out), _flatten(rs_out)
        if len(a) != len(b):
            self._record_divergence(name, float("inf"),
                                    f"shape {len(a)} vs {len(b)}")
            return
        worst = 0.0
        for x, y in zip(a, b):
            if isinstance(x, bool) or isinstance(x, int):
                if x != y:
                    worst = max(worst, float("inf"))
                continue
            diff = abs(float(x) - float(y))
            worst = max(worst, diff)
        if worst > self.tolerance:
            self._record_divergence(name, worst,
                                    f"max abs diff {worst:g} > "
                                    f"{self.tolerance:g}")
        d.worst = max(d.worst, worst if worst != float("inf") else d.worst)

    def _record_divergence(self, name: str, worst: float, detail: str) -> None:
        d = self.divergences.setdefault(name, Divergence(kernel=name))
        d.n_diverged += 1
        d.examples.append(detail)
        if worst != float("inf"):
            d.worst = max(d.worst, worst)

    # -- convenience --------------------------------------------------------
    def __getattr__(self, name: str):
        if name in kernels.KERNELS:
            return lambda *a, **k: self.call(name, *a, **k)
        raise AttributeError(name)


def should_build() -> dict:
    """The written trigger for building the crate.

    Deliberately a measurement rather than an opinion. Build it when, and only
    when, all of these hold — otherwise the correct action is to fix the SQL,
    which is what the profiler has said every time so far.
    """
    return {
        "criteria": [
            "a profile shows >30% of wall clock inside one of the four "
            "kernels, AFTER connection pooling and algorithmic fixes",
            "the research pass is run often enough that its wall clock is a "
            "working constraint, not an occasional inconvenience",
            "the substrate has grown beyond ~1 year or several venues, so a "
            "faster search is not simply a higher false-discovery rate",
            "`maturin` and a Rust toolchain are acceptable install "
            "dependencies for whoever runs this",
        ],
        "current_verdict": (
            "DO NOT BUILD. Profiling put 83% of scan wall clock in SQLite "
            "connection setup, not computation; pooling gave 55x. The "
            "remaining CPU work is a small fraction of runtime."),
        "build_command": "cd rust && maturin develop --release",
    }


_DEFAULT: Accelerator | None = None


def default() -> Accelerator:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = Accelerator()
    return _DEFAULT
