"""Rust acceleration: the bridge, the fallback, and the honesty about whether
it is needed.

THE MEASURED POSITION, taken on this machine against this data:

  * There is no Rust anywhere in the V1 project. No Cargo.toml, no .rs file, no
    PyO3, no maturin. Every apparent hit in the codebase is a substring of
    "robust" or "trust". Project documentation refers to an "existing Rust
    architecture" that does not exist.

  * PROFILE FIRST was followed. The first profile of the sweep hot loop showed
    18% of runtime building rejection-reason strings that the sweep never
    reads. Fixing that in Python took one function and moved throughput from
    103 to 287 candidate-evaluations/second -- a 2.8x speedup for no new
    language, no build toolchain, and no equivalence risk.

  * At 287 evals/s the full 60-wallet pass runs in ~18 minutes inside 16 GB.
    Rust would help, but it is not the constraint: the constraint is that the
    substrate has 90 days and 1,285 markets, and making a data-limited search
    faster raises the false-discovery rate rather than finding edge.

So this module ships COMPLETE and DISABLED BY DEFAULT (`mode="auto"` selects
Python when no compiled extension is present). The crate in `rust/` is real and
buildable; the trigger for building it is stated in `should_build()` rather
than left to enthusiasm.

The contract, which is what makes it safe to turn on:

    RUST ENABLED   use Rust, fall back to Python on any error
    RUST DISABLED  Python only
    RUST SHADOW    run BOTH, compare, report divergence, RETURN PYTHON

Python is the reference implementation until equivalence is proven. Rust never
changes a decision -- in shadow mode its result is discarded after comparison,
and in enabled mode any exception falls through to Python rather than
propagating. A failed Rust import can never take the application down.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Settings

_BACKEND = None
_IMPORT_ERROR = ""


def _try_import():
    global _BACKEND, _IMPORT_ERROR
    if _BACKEND is not None or _IMPORT_ERROR:
        return _BACKEND
    try:
        import pqv2_accel                                   # type: ignore
        _BACKEND = pqv2_accel
    except Exception as exc:                                # noqa: BLE001
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


class Accelerator:
    """Dispatch to Rust or Python, per the configured mode."""

    def __init__(self, st: Settings) -> None:
        self.st = st
        self.mode = st.accel.mode
        self.tolerance = st.accel.tolerance
        self.backend = _try_import() if self.mode != "disabled" else None
        self.divergences: dict = {}
        self.timings: dict = {}
        if self.mode == "enabled" and self.backend is None:
            # Requested explicitly and unavailable: say so loudly, then carry
            # on in Python. Never raise -- rule 22.
            self.mode = "disabled"
            self.unavailable_reason = _IMPORT_ERROR
        else:
            self.unavailable_reason = _IMPORT_ERROR if self.backend is None else ""

    @property
    def active(self) -> bool:
        return self.backend is not None and self.mode in ("enabled", "auto",
                                                          "shadow")

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "rust_available": self.backend is not None,
            "unavailable_reason": self.unavailable_reason,
            "effective_backend": ("rust" if (self.backend is not None
                                             and self.mode in ("enabled", "auto"))
                                  else "python"),
            "note": ("Rust is not currently the constraint on this system; see "
                     "pqv2/accel/__init__.py and docs/PERFORMANCE.md for the "
                     "measurement that says so."),
            "divergences": {k: v.to_dict() for k, v in self.divergences.items()},
            "timings": self.timings,
        }

    # -- the dispatch --------------------------------------------------------
    def call(self, kernel: str, py_fn, rs_fn=None, *args, **kwargs):
        """Run a kernel under the configured mode.

        Returns the PYTHON result in every mode except `enabled`/`auto` with a
        working backend. That asymmetry is deliberate: shadow mode exists to
        build confidence, not to take risk.
        """
        t0 = time.perf_counter()
        py_result = None
        rs_result = None

        if self.mode == "disabled" or self.backend is None or rs_fn is None:
            py_result = py_fn(*args, **kwargs)
            self._time(kernel, "python", time.perf_counter() - t0)
            return py_result

        if self.mode == "shadow":
            py_result = py_fn(*args, **kwargs)
            t_py = time.perf_counter() - t0
            self._time(kernel, "python", t_py)
            t1 = time.perf_counter()
            try:
                rs_result = rs_fn(*args, **kwargs)
                self._time(kernel, "rust", time.perf_counter() - t1)
                self._compare(kernel, py_result, rs_result)
            except Exception as exc:                        # noqa: BLE001
                self._record_divergence(kernel, f"rust raised: {exc}")
            return py_result                                 # always Python

        # enabled / auto
        try:
            rs_result = rs_fn(*args, **kwargs)
            self._time(kernel, "rust", time.perf_counter() - t0)
            return rs_result
        except Exception as exc:                            # noqa: BLE001
            self._record_divergence(kernel, f"rust raised, fell back: {exc}")
            t1 = time.perf_counter()
            py_result = py_fn(*args, **kwargs)
            self._time(kernel, "python_fallback", time.perf_counter() - t1)
            return py_result

    def _time(self, kernel: str, which: str, seconds: float) -> None:
        slot = self.timings.setdefault(kernel, {})
        entry = slot.setdefault(which, {"calls": 0, "seconds": 0.0})
        entry["calls"] += 1
        entry["seconds"] = round(entry["seconds"] + seconds, 6)

    def _compare(self, kernel: str, py, rs) -> None:
        d = self.divergences.setdefault(kernel, Divergence(kernel))
        d.n_checked += 1
        try:
            diff = _max_abs_diff(py, rs)
        except Exception as exc:                            # noqa: BLE001
            self._record_divergence(kernel, f"incomparable: {exc}")
            return
        if diff > self.tolerance:
            d.n_diverged += 1
            d.worst = max(d.worst, diff)
            d.examples.append({"diff": diff, "python": _short(py),
                               "rust": _short(rs)})

    def _record_divergence(self, kernel: str, note: str) -> None:
        d = self.divergences.setdefault(kernel, Divergence(kernel))
        d.n_checked += 1
        d.n_diverged += 1
        d.examples.append({"note": note})

    def speedup(self, kernel: str) -> float:
        t = self.timings.get(kernel) or {}
        py = t.get("python", {}).get("seconds", 0.0)
        rs = t.get("rust", {}).get("seconds", 0.0)
        return round(py / rs, 2) if rs > 0 else 0.0


def _max_abs_diff(a, b) -> float:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(float(a) - float(b))
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            raise ValueError(f"key mismatch: {set(a) ^ set(b)}")
        return max((_max_abs_diff(a[k], b[k]) for k in a), default=0.0)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            raise ValueError(f"length {len(a)} != {len(b)}")
        return max((_max_abs_diff(x, y) for x, y in zip(a, b)), default=0.0)
    if isinstance(a, (str, bool)) or isinstance(b, (str, bool)):
        return 0.0 if a == b else 1.0
    raise TypeError(f"cannot compare {type(a)} and {type(b)}")


def _short(v) -> str:
    s = repr(v)
    return s if len(s) <= 120 else s[:117] + "..."


def should_build(*, tape_rows: int, hypotheses_per_wallet: int,
                 pass_seconds: float) -> dict:
    """The trigger for building the Rust extension, stated as a rule.

    Deliberately explicit so nobody has to decide by enthusiasm. Making a
    data-starved search faster raises the false-discovery rate; it does not
    find edge. Speed is worth buying when the search is the constraint, and not
    before.
    """
    triggers = []
    if tape_rows > 10_000_000:
        triggers.append(f"tape has {tape_rows:,} rows (> 10M)")
    if hypotheses_per_wallet > 100_000:
        triggers.append(f"{hypotheses_per_wallet:,} hypotheses/wallet (> 100k)")
    if pass_seconds > 3600:
        triggers.append(f"a pass takes {pass_seconds / 60:.0f} minutes (> 60)")
    return {
        "build": bool(triggers),
        "triggers": triggers,
        "verdict": ("Build it: " + "; ".join(triggers)) if triggers else
        ("Do not build yet. None of the triggers have fired. Profile the "
         "Python path again first -- the last profile found a 2.8x win in one "
         "function."),
        "how": "cd rust && maturin develop --release   (needs Rust + maturin)",
    }
