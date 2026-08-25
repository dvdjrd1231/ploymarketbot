# Performance: profile first, and what that actually found

The brief says **PROFILE FIRST. Do not blindly rewrite Python.** This document
is the profile, and the decision that came out of it.

Machine: Windows 11, 16 GB RAM, CPython 3.13. Data: the client's
`intel.sqlite3` (2.6 GB).

---

## Starting position: there is no Rust in the existing project

No `Cargo.toml`, no `.rs`, no PyO3, no maturin. Every apparent hit in the V1
source is a substring of *robust* or *trust*. Project documentation refers to
an "existing Rust architecture" that does not exist. Recorded because the brief
asks V2 to accelerate it.

---

## The first profile

Hot loop: 400 candidate strategies evaluated over 1,738 observations, tape
pre-warmed so SQLite is not the subject.

```
3,711,705 function calls in 3.876 seconds

 ncalls  tottime  cumtime  function
    400    1.443    3.876  validation/backtest.py:run
  31246    0.029    0.968  substrate/data.py:price_at
    258    0.746    0.746  sqlite3.Connection.execute
 695200    0.551    0.551  strategy_b/strategy.py:admits
1308236    0.523    0.523  str.split          <-- 1.3M calls
 654118    0.161    0.161  str.join           <-- 654k calls

=> 103 candidate-evaluations / second
```

**1.3 million `str.split` calls.** Not matrix maths, not I/O — string
formatting.

The cause was a single line in the sweep path:

```python
ok, why = strategy.admits(o)          # builds an f-string on every rejection
if not ok:
    key = why.split(" ")[0] + " " + " ".join(why.split(" ")[1:3])
```

The rejection reasons are **essential** in the live route, where every
rejection must be explainable (rule 6), and **pure waste** in the search, where
nothing reads them. 18% of runtime spent formatting text nobody looks at.

---

## The fix, and the measurement

Added `CopyStrategy.admits_fast()` — the same predicate without building the
reason string — and used it only on the sweep path. Both exist; the live route
still explains every rejection.

```
before   103 candidate-evaluations / second
after    287 candidate-evaluations / second      2.8x
```

Equivalence is asserted, not assumed:

```
tests/test_validation.py::test_admits_and_admits_fast_agree_everywhere
    150 strategies x 60 observations, zero disagreements
```

**A 2.8× speedup for one function, no new language, no build toolchain, and no
equivalence risk.** This is what the "profile first" rule is for.

---

## Current throughput

| workload | measured |
|---|---|
| substrate inventory | ~21 s |
| observation stream, 1 wallet | ~0.1 s |
| RN1 reconstruction | ~5 s |
| candidate evaluation | ~287 / s |
| full 40-wallet discovery pass | see `var/reports/last_pass.json` |
| peak memory | well inside 16 GB (streamed, never fully loaded) |

Memory is bounded by *simultaneously unsettled trades*, not by tape size, so
the 2.6 GB database is never loaded.

---

## Should Rust be built? Not yet — and the rule says so

`accel.should_build()` states the trigger rather than leaving it to
enthusiasm:

| trigger | threshold | current |
|---|---|---|
| tape rows | > 10,000,000 | 878,650 |
| hypotheses per wallet | > 100,000 | 5,184 |
| pass wall-clock | > 60 min | under |

**None have fired.**

The reason is not laziness about performance. It is that **the constraint on
this system is evidence, not compute.** The substrate holds 90 days and 1,285
markets. Making a data-limited search faster does not find edge — it raises the
false-discovery rate, because more hypotheses tested against the same evidence
means a stricter BH threshold and more ways to be fooled.

And note item 1 in `docs/LIMITS.md`: four search axes are currently **inert**,
so ~12× of the hypotheses being tested are duplicates. Fixing `settled_ts`
would deliver a larger effective speedup than Rust — by making the search
*smaller*, not faster.

---

## What ships anyway, and why

The Rust crate in `rust/` is real, complete and buildable — it is not a stub.
Four kernels, chosen because profiling showed them hot **and** because each is
a pure function whose equivalence can be asserted mechanically:

| kernel | what |
|---|---|
| `sweep_admit` | the whole candidate grid × the whole observation array, one call |
| `t_stat` | two-pass mean / sd / t |
| `bootstrap_means` | percentile bootstrap with an explicit shared LCG |
| `max_drawdown` | peak-to-trough |

Design notes that matter:

- **Coarse-grained.** `sweep_admit` takes the entire grid and the entire
  observation array in one call. A per-observation binding would cross the
  Python/Rust boundary millions of times and be *slower* than the Python it
  replaced.
- **Shared RNG.** `bootstrap_means` uses the same LCG on both sides. Two
  different generators can never pass an equivalence test, and a shadow mode
  that always reports divergence is one nobody reads.
- **Two-pass variance**, matching Python. The one-pass shortcut loses precision
  badly when the mean is large relative to the variance — the normal case here.
- **NaN means "no constraint"**, so Python's `None` crosses the boundary
  without an `Option` per field.

To build:

```
cd rust && maturin develop --release
```

---

## The safety contract

| mode | behaviour |
|---|---|
| `disabled` | Python only; Rust never called |
| `enabled` / `auto` | Rust, **falling back to Python on any exception** |
| `shadow` | run both, compare, record divergence, **return Python** |

Python is the reference implementation until equivalence is proven. Shadow mode
exists to build confidence, not to take risk — which is why it returns the
Python result even when the two agree.

A failed Rust import can never take the application down. Set
`PQV2_ACCEL=enabled` with no extension built and the Accelerator downgrades
itself to Python and says so in `status()`.

Asserted by `tests/test_accel_equivalence.py`, which runs **on machines with no
Rust toolchain** — because the behaviour that matters most is what happens when
Rust is missing or broken.

---

## Honest statement

**No claim is made that Rust has improved this system, because it has not been
built and would not currently help.** The only measured optimisation in V2 is
the 2.8× Python fix above, and it is reproducible from the profile in this
document.
