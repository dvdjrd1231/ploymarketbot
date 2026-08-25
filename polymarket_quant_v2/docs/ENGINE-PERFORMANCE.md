# Performance: what was actually slow

Every number here was measured on the client's own machine against the client's
own `intel.sqlite3` (878,650 wallet trades, 47,926 markets, 112 days).

## PROFILE FIRST, and it changed the answer

The plan was a Python core with Rust hot kernels. Profiling moved almost all of
the work out of Rust's reach before a line of it ran.

### Scan: 36.1s to 0.65s (55x), no new language

A 600-market scan took 36.1s. `cProfile` over a 300-market sub-run:

```
47,257 calls in 8.013 seconds
ncalls  tottime  cumtime  function
  2850    7.524    7.524  {method 'execute' of 'sqlite3.Connection'}
   776    0.007    6.653  core/source.py:connect_ro
   300    0.018    7.401  scanner/opportunity.py:_stage1
```

7.5s of 8.0s inside SQLite, and **6.65s of that inside connection setup** - 776
connections at roughly 8.5ms each. The cause was one line:

```python
conn.execute("PRAGMA cache_size = -64000")   # a fresh 64 MB page cache, per connection
```

The queries were never the problem. `pooled_ro()` keeps one read-only
connection per (thread, database), which removes the setup cost and keeps the
page cache warm across queries - the larger of the two wins.

| | before | after |
|---|---|---|
| 300 markets | 8.01s | 0.81s |
| 600 markets | 36.1s | 0.65s |

### Wallet DNA: 37s for 40 wallets, to 7.1s for 120 wallets

Two causes. SQL again, plus a genuine O(n^2):

```
ncalls   tottime  function
  1504    8.359   {method 'execute' of 'sqlite3.Connection'}
 23504    1.196   {built-in method builtins.sum}
```

That `sum` was the leave-one-out alpha baseline, recomputed per trade over a
bucket holding tens of thousands of returns. Precomputing the bucket totals
once per pass makes it O(n). Combined with pooling: three times the wallets in
a fifth of the time.

## Why the Rust crate is not built

`rust/` is real and complete: four coarse-grained kernels (`alpha_excess`,
`block_bootstrap`, `max_drawdown`, `transition_chi2`), chosen because they are
the CPU-bound work that remains after the SQL is fixed, and because each is a
pure function of its inputs so equivalence can be asserted mechanically rather
than argued.

It ships DISABLED. `accel.should_build()` states the trigger as a measurement:

> DO NOT BUILD. Profiling put 83% of scan wall clock in SQLite connection
> setup, not computation; pooling gave 55x. The remaining CPU work is a small
> fraction of runtime.

Build it when a profile shows more than 30% of wall clock inside one of the
four kernels *after* the pooling and algorithmic fixes, and when the substrate
has grown past roughly a year or several venues - because making a
**data-limited** search faster raises the false-discovery rate rather than
finding edge.

**The crate has not been compiled.** No Rust toolchain was available on the
development machine. The Python reference kernels are authoritative and fully
tested; `tests/test_accel.py` skips the Rust equivalence tests with a stated
reason rather than passing silently. Anyone with `cargo` should run
`cd rust && cargo check` before trusting it.

## The accelerator contract

```
RUST ENABLED    use Rust, fall back to Python on ANY error
RUST DISABLED   Python only
RUST SHADOW     run BOTH, compare, report divergence, RETURN PYTHON
```

Rust never changes a decision. In shadow mode its result is discarded after
comparison; in enabled mode any exception falls through to Python. A failed
import cannot take the application down.

## Where the remaining time goes

After both fixes, wall clock is dominated by SQLite reads over a 2.6 GB file
and by the bounded-concurrency agent pool. Neither is the binding constraint on
result quality. The binding constraint is that the substrate is one venue and
112 days, and no amount of speed changes that.
