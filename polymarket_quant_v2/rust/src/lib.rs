//! CPU-bound kernels for the Polymarket Quant Engine V2.
//!
//! Scope discipline: only kernels that PROFILING showed to be hot, and only
//! ones whose Python equivalent is a pure function of its inputs, so that
//! equivalence can be asserted mechanically rather than argued.
//!
//! Everything here is COARSE-GRAINED on purpose. `sweep_admit` evaluates a
//! whole grid of candidates against a whole observation array in one call.
//! A per-observation binding would cross the Python/Rust boundary millions of
//! times and be slower than the Python it replaced.
//!
//! Python remains the reference implementation. These functions must agree
//! with it exactly (integers, counts) or within the configured tolerance
//! (floats). `tests/test_accel_equivalence.py` asserts that against golden
//! data, and `accel.Accelerator` runs them in shadow mode until it does.

use pyo3::prelude::*;

/// One observation, flattened. Field order must match `ObsArray` in
/// `pqv2/accel/kernels.py` -- the Python side builds these arrays and a
/// mismatch would silently compare different columns.
#[derive(Clone, Copy)]
struct Obs {
    price: f64,
    notional: f64,
    rel_notional: f64,
    settled_n: f64,
    win_rate: f64,
    roll_win_rate: f64,
    roll_roi: f64,
    edge_t: f64,
    consec_losses: f64,
    token_repeat: f64,
    market_prints: f64,
    market_move: f64,
    secs_to_settle: f64,
    resolution: f64,
}

const WIDTH: usize = 14;

/// One candidate's filter set. `f64::NAN` means "no constraint", which is how
/// Python's `None` crosses the boundary without an Option per field.
#[derive(Clone, Copy)]
struct Filters {
    min_price: f64,
    max_price: f64,
    min_notional: f64,
    max_notional: f64,
    min_rel_notional: f64,
    min_settled_n: f64,
    min_win_rate: f64,
    min_roll_win_rate: f64,
    min_roll_roi: f64,
    min_edge_t: f64,
    max_consec_losses: f64,
    skip_repeat_token: f64,
    min_market_prints: f64,
    max_market_move: f64,
    min_market_move: f64,
    max_secs_to_settle: f64,
    min_secs_to_settle: f64,
}

const FWIDTH: usize = 17;

#[inline(always)]
fn unset(v: f64) -> bool {
    v.is_nan()
}

/// Mirror of `CopyStrategy.admits_fast`. Any change to that function must be
/// made here in the same commit, or shadow mode will report the divergence.
#[inline]
fn admits(o: &Obs, f: &Filters) -> bool {
    if !unset(f.min_price) && o.price < f.min_price {
        return false;
    }
    if !unset(f.max_price) && o.price > f.max_price {
        return false;
    }
    if !unset(f.min_notional) && o.notional < f.min_notional {
        return false;
    }
    if !unset(f.max_notional) && o.notional > f.max_notional {
        return false;
    }
    if !unset(f.min_rel_notional) && o.rel_notional < f.min_rel_notional {
        return false;
    }
    if !unset(f.min_settled_n) && o.settled_n < f.min_settled_n {
        return false;
    }
    if !unset(f.min_win_rate) && o.win_rate < f.min_win_rate {
        return false;
    }
    if !unset(f.min_roll_win_rate) && o.roll_win_rate < f.min_roll_win_rate {
        return false;
    }
    if !unset(f.min_roll_roi) && o.roll_roi < f.min_roll_roi {
        return false;
    }
    if !unset(f.min_edge_t) && o.edge_t < f.min_edge_t {
        return false;
    }
    if !unset(f.max_consec_losses) && o.consec_losses > f.max_consec_losses {
        return false;
    }
    if f.skip_repeat_token > 0.5 && o.token_repeat > 0.5 {
        return false;
    }
    if !unset(f.min_market_prints) && o.market_prints < f.min_market_prints {
        return false;
    }
    if !unset(f.max_market_move) && o.market_move.abs() > f.max_market_move {
        return false;
    }
    if !unset(f.min_market_move) && o.market_move.abs() < f.min_market_move {
        return false;
    }
    if !unset(f.max_secs_to_settle)
        && (o.secs_to_settle < 0.0 || o.secs_to_settle > f.max_secs_to_settle)
    {
        return false;
    }
    if !unset(f.min_secs_to_settle)
        && (o.secs_to_settle < 0.0 || o.secs_to_settle < f.min_secs_to_settle)
    {
        return false;
    }
    true
}

/// Evaluate every candidate against every observation, hold-to-settlement.
///
/// Returns one row per candidate: `[n_admitted, n_wins, sum_return,
/// sum_sq_return]`. Sums rather than a mean so the Python side computes the
/// same statistic from the same primitives and float association cannot drift
/// between the two implementations.
///
/// Costs are applied identically to `Costs.fill_price`: entry is scaled by
/// `cost_mult`, and a trade whose costed entry leaves `[min_price, max_price]`
/// is not counted -- matching the Python UNFILLED path.
#[pyfunction]
#[pyo3(signature = (obs, filters, cost_mult, min_price, max_price))]
fn sweep_admit(
    obs: Vec<f64>,
    filters: Vec<f64>,
    cost_mult: f64,
    min_price: f64,
    max_price: f64,
) -> PyResult<Vec<f64>> {
    if obs.len() % WIDTH != 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "obs length {} is not a multiple of {}",
            obs.len(),
            WIDTH
        )));
    }
    if filters.len() % FWIDTH != 0 {
        return Err(pyo3::exceptions::PyValueError::new_err(format!(
            "filters length {} is not a multiple of {}",
            filters.len(),
            FWIDTH
        )));
    }

    let n_obs = obs.len() / WIDTH;
    let n_cand = filters.len() / FWIDTH;

    let rows: Vec<Obs> = (0..n_obs)
        .map(|i| {
            let b = i * WIDTH;
            Obs {
                price: obs[b],
                notional: obs[b + 1],
                rel_notional: obs[b + 2],
                settled_n: obs[b + 3],
                win_rate: obs[b + 4],
                roll_win_rate: obs[b + 5],
                roll_roi: obs[b + 6],
                edge_t: obs[b + 7],
                consec_losses: obs[b + 8],
                token_repeat: obs[b + 9],
                market_prints: obs[b + 10],
                market_move: obs[b + 11],
                secs_to_settle: obs[b + 12],
                resolution: obs[b + 13],
            }
        })
        .collect();

    let mut out = Vec::with_capacity(n_cand * 4);
    for c in 0..n_cand {
        let b = c * FWIDTH;
        let f = Filters {
            min_price: filters[b],
            max_price: filters[b + 1],
            min_notional: filters[b + 2],
            max_notional: filters[b + 3],
            min_rel_notional: filters[b + 4],
            min_settled_n: filters[b + 5],
            min_win_rate: filters[b + 6],
            min_roll_win_rate: filters[b + 7],
            min_roll_roi: filters[b + 8],
            min_edge_t: filters[b + 9],
            max_consec_losses: filters[b + 10],
            skip_repeat_token: filters[b + 11],
            min_market_prints: filters[b + 12],
            max_market_move: filters[b + 13],
            min_market_move: filters[b + 14],
            max_secs_to_settle: filters[b + 15],
            min_secs_to_settle: filters[b + 16],
        };

        let mut n_admitted = 0.0f64;
        let mut n_wins = 0.0f64;
        let mut sum_r = 0.0f64;
        let mut sum_r2 = 0.0f64;

        for o in &rows {
            if !admits(o, &f) {
                continue;
            }
            let entry = o.price * cost_mult;
            if !(entry > min_price && entry < max_price) {
                continue;
            }
            let r = (o.resolution - entry) / entry;
            n_admitted += 1.0;
            if r > 0.0 {
                n_wins += 1.0;
            }
            sum_r += r;
            sum_r2 += r * r;
        }
        out.push(n_admitted);
        out.push(n_wins);
        out.push(sum_r);
        out.push(sum_r2);
    }
    Ok(out)
}

/// Mean, sample standard deviation and the t-statistic of a return series.
///
/// Two-pass rather than the one-pass "sum of squares" shortcut: the shortcut
/// loses catastrophic precision when the mean is large relative to the
/// variance, which happens constantly here (a 0.05 entry resolving YES returns
/// +19.0). Python's implementation is two-pass, so this one is too -- matching
/// the reference matters more than saving a pass.
#[pyfunction]
fn t_stat(returns: Vec<f64>) -> PyResult<(f64, f64, f64)> {
    let n = returns.len();
    if n < 5 {
        return Ok((0.0, 0.0, 0.0));
    }
    let nf = n as f64;
    let mean = returns.iter().sum::<f64>() / nf;
    let var = returns.iter().map(|r| (r - mean) * (r - mean)).sum::<f64>() / (nf - 1.0);
    if var <= 0.0 {
        return Ok((mean, 0.0, 0.0));
    }
    let sd = var.sqrt();
    Ok((mean, sd, mean / (sd / nf.sqrt())))
}

/// Percentile bootstrap of the mean, with an explicit LCG so the Python and
/// Rust paths draw the SAME indices from the same seed. Without that,
/// equivalence testing a bootstrap is impossible and shadow mode would report
/// permanent false divergence.
#[pyfunction]
#[pyo3(signature = (returns, draws, seed))]
fn bootstrap_means(returns: Vec<f64>, draws: usize, seed: u64) -> PyResult<Vec<f64>> {
    let n = returns.len();
    if n == 0 {
        return Ok(vec![]);
    }
    let mut state = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
    let mut next = || {
        state = state
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        (state >> 33) as usize
    };
    let mut out = Vec::with_capacity(draws);
    for _ in 0..draws {
        let mut acc = 0.0f64;
        for _ in 0..n {
            acc += returns[next() % n];
        }
        out.push(acc / n as f64);
    }
    out.sort_by(|a, b| a.partial_cmp(b).unwrap());
    Ok(out)
}

/// Maximum drawdown of a cumulative-P&L series.
#[pyfunction]
fn max_drawdown(equity: Vec<f64>) -> PyResult<f64> {
    let mut peak = 0.0f64;
    let mut worst = 0.0f64;
    for e in equity {
        if e > peak {
            peak = e;
        }
        let dd = e - peak;
        if dd < worst {
            worst = dd;
        }
    }
    Ok(worst.abs())
}

#[pymodule]
fn pqv2_accel(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(sweep_admit, m)?)?;
    m.add_function(wrap_pyfunction!(t_stat, m)?)?;
    m.add_function(wrap_pyfunction!(bootstrap_means, m)?)?;
    m.add_function(wrap_pyfunction!(max_drawdown, m)?)?;
    m.add("__doc__", "CPU-bound kernels for the Polymarket Quant Engine V2")?;
    Ok(())
}
