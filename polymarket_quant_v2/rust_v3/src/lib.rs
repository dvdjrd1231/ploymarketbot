//! CPU-bound kernels for the Polymarket Quant Bridge V3.
//!
//! # Scope discipline
//!
//! Only kernels that PROFILING showed to be hot, and only ones whose Python
//! equivalent is a pure function of its inputs, so equivalence can be asserted
//! mechanically rather than argued.
//!
//! The measured position on this machine and this data, recorded so the
//! decision to build (or not build) this crate is evidence-based:
//!
//! * A 600-market scan took **36.1s**. The profiler put 7.5s of an 8.0s
//!   sub-run inside `sqlite3.execute`, and 6.65s of THAT inside connection
//!   setup — 776 connections at ~8.5ms each. Pooling the read-only connection
//!   took the same scan to **0.65s**. A 55x speedup, in Python, with no
//!   equivalence risk. Rust would have accelerated the 0.5s that was not the
//!   problem.
//!
//! * A 40-wallet DNA pass took **37s**. Precomputing bucket sums removed an
//!   O(n^2) leave-one-out loop; 120 wallets now take **7.1s**, again dominated
//!   by SQL.
//!
//! So this crate ships COMPLETE and DISABLED BY DEFAULT. The Python side is
//! the reference implementation and stays the reference implementation. The
//! trigger for actually building this is stated in `accel/__init__.py`'s
//! `should_build()` rather than left to enthusiasm.
//!
//! # What is here, and why these
//!
//! Every kernel below is COARSE-GRAINED on purpose: each call does a whole
//! array's worth of work. A per-element binding would cross the Python/Rust
//! boundary millions of times and be slower than the Python it replaced.
//!
//! These four are the ones that stay CPU-bound after the SQL is fixed:
//!
//! * `alpha_excess`      — the wallet-alpha control, over every settled trade
//! * `block_bootstrap`   — robustness testing, thousands of resamples
//! * `max_drawdown`      — equity-curve statistic, called per candidate
//! * `transition_chi2`   — Agent 9's sequence structure test
//!
//! # Contract
//!
//! Python remains authoritative. `accel.Accelerator` runs these in shadow mode
//! — both implementations, compare, report divergence, RETURN PYTHON — until
//! equivalence is proven against golden data by
//! `tests/test_accel_equivalence.py`. In enabled mode any panic or error falls
//! back to Python rather than propagating. Rust never changes a decision.

use pyo3::prelude::*;

/// Leave-one-out excess return against a bucket baseline.
///
/// This is the wallet-alpha control, and it is the one number that decides
/// whether a wallet has skill or merely likes favourites. For each trade `i`
/// with return `r[i]` in bucket `b[i]`:
///
/// ```text
///     excess[i] = r[i] - (bucket_sum[b[i]] - r[i]) / (bucket_n[b[i]] - 1)
/// ```
///
/// The subtraction of `r[i]` is not cosmetic: without it a prolific wallet
/// competes against itself and its measured alpha is pulled toward zero in
/// proportion to how much of the bucket it occupies.
///
/// Buckets with fewer than `min_n` observations are skipped — a baseline drawn
/// from four trades is noise, and comparing against noise manufactures alpha.
///
/// Returns the mean excess, and the count that produced it. The count is
/// returned rather than discarded so the caller can refuse a mean computed
/// from too few comparisons.
#[pyfunction]
#[pyo3(signature = (returns, bucket_ids, bucket_sums, bucket_counts, min_n=10))]
fn alpha_excess(
    returns: Vec<f64>,
    bucket_ids: Vec<usize>,
    bucket_sums: Vec<f64>,
    bucket_counts: Vec<usize>,
    min_n: usize,
) -> PyResult<(f64, usize)> {
    if returns.len() != bucket_ids.len() {
        return Err(pyo3::exceptions::PyValueError::new_err(
            "returns and bucket_ids must be the same length",
        ));
    }
    let mut total = 0.0f64;
    let mut n = 0usize;
    for (i, &r) in returns.iter().enumerate() {
        let b = bucket_ids[i];
        if b >= bucket_counts.len() {
            continue;
        }
        let count = bucket_counts[b];
        if count < min_n || count < 2 {
            continue;
        }
        let baseline = (bucket_sums[b] - r) / (count as f64 - 1.0);
        total += r - baseline;
        n += 1;
    }
    Ok(if n == 0 { (0.0, 0) } else { (total / n as f64, n) })
}

/// Moving-block bootstrap of the mean.
///
/// Blocks rather than individual draws because prediction-market returns are
/// serially dependent — the same market resolves many positions at once, and
/// an i.i.d. bootstrap over dependent data reports a confidence interval far
/// too narrow, which is the most common way a backtest overstates its own
/// significance.
///
/// Deterministic given `seed`: a xorshift64* generator is used rather than a
/// system RNG so a reported p-value can be reproduced exactly. Reproducibility
/// matters more than statistical quality of the generator here, and xorshift64*
/// is more than adequate for resampling indices.
#[pyfunction]
#[pyo3(signature = (values, draws, block_size, seed))]
fn block_bootstrap(values: Vec<f64>, draws: usize, block_size: usize, seed: u64) -> Vec<f64> {
    let n = values.len();
    if n == 0 || draws == 0 {
        return Vec::new();
    }
    let block = block_size.max(1).min(n);
    let n_blocks = (n + block - 1) / block;
    let mut state = if seed == 0 { 0x9E3779B97F4A7C15 } else { seed };
    let mut out = Vec::with_capacity(draws);

    for _ in 0..draws {
        let mut sum = 0.0f64;
        let mut taken = 0usize;
        for _ in 0..n_blocks {
            // xorshift64*
            state ^= state >> 12;
            state ^= state << 25;
            state ^= state >> 27;
            let start = ((state.wrapping_mul(0x2545F4914F6CDD1D)) as usize) % n;
            for k in 0..block {
                if taken >= n {
                    break;
                }
                sum += values[(start + k) % n];
                taken += 1;
            }
        }
        out.push(if taken == 0 { 0.0 } else { sum / taken as f64 });
    }
    out
}

/// Maximum peak-to-trough drawdown of an equity curve, as a fraction of peak.
///
/// Guards the peak against zero and against negative equity: a curve that
/// crosses zero has no meaningful fractional drawdown, and dividing by a peak
/// at or below zero would emit an infinity that then propagates silently into
/// a risk limit.
#[pyfunction]
fn max_drawdown(equity: Vec<f64>) -> f64 {
    let mut peak = f64::NEG_INFINITY;
    let mut worst = 0.0f64;
    for &e in equity.iter() {
        if e > peak {
            peak = e;
        }
        if peak > 0.0 {
            let dd = (peak - e) / peak;
            if dd > worst {
                worst = dd;
            }
        }
    }
    worst
}

/// Chi-square statistic for independence in a two-state transition sequence.
///
/// `symbols` must be 0 (down) or 1 (up); anything else is skipped, which is
/// how flat prints are excluded without the caller having to filter first.
///
/// Returns `(chi2, n_transitions, p_up)`. The critical value at df=1 and
/// alpha=0.05 is 3.84; Agent 9 abstains below it rather than reporting weak
/// structure, because a sequence indistinguishable from independent carries no
/// exploitable information and saying otherwise is how noise becomes a signal.
#[pyfunction]
fn transition_chi2(symbols: Vec<i64>) -> (f64, usize, f64) {
    let s: Vec<i64> = symbols.into_iter().filter(|&x| x == 0 || x == 1).collect();
    if s.len() < 3 {
        return (0.0, 0, 0.0);
    }
    let mut trans = [[0usize; 2]; 2];
    for w in s.windows(2) {
        trans[w[0] as usize][w[1] as usize] += 1;
    }
    let n_up = s.iter().filter(|&&x| x == 1).count();
    let p_up = n_up as f64 / s.len() as f64;
    let n: usize = trans.iter().flatten().sum();

    let mut chi = 0.0f64;
    for a in 0..2 {
        let row: usize = trans[a].iter().sum();
        if row == 0 {
            continue;
        }
        for b in 0..2 {
            let p = if b == 1 { p_up } else { 1.0 - p_up };
            let expected = row as f64 * p;
            if expected > 0.0 {
                let d = trans[a][b] as f64 - expected;
                chi += d * d / expected;
            }
        }
    }
    (chi, n, p_up)
}

#[pymodule]
fn pqv3_accel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(alpha_excess, m)?)?;
    m.add_function(wrap_pyfunction!(block_bootstrap, m)?)?;
    m.add_function(wrap_pyfunction!(max_drawdown, m)?)?;
    m.add_function(wrap_pyfunction!(transition_chi2, m)?)?;
    m.add("__doc__", "CPU-bound kernels for pqv3. Python remains authoritative.")?;
    Ok(())
}
