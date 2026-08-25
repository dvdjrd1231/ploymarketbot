"""Kernel correctness and the accelerator contract.

The Rust extension is not built in CI, so the equivalence tests skip when it is
absent — and say so, rather than passing silently. What is always asserted is
that the Python reference kernels are correct and that the accelerator's
contract holds regardless of whether a backend exists.
"""

from __future__ import annotations

import math

import pytest

from pqv3.accel import Accelerator, default, should_build
from pqv3.accel import kernels as K

HAS_RUST = False
try:
    import pqv3_accel                                        # noqa: F401
    HAS_RUST = True
except Exception:                                            # noqa: BLE001
    pass

needs_rust = pytest.mark.skipif(
    not HAS_RUST,
    reason="pqv3_accel not built — see accel.should_build(); the Python "
           "reference is authoritative and is tested unconditionally")


# ----------------------------------------------------- reference correctness
def test_alpha_excess_removes_the_trades_own_contribution():
    """Without leave-one-out, a wallet competes against itself."""
    returns = [1.0, 0.0, 0.0, 0.0]
    # One bucket holding all four; sum 1.0, n 4.
    out, n = K.alpha_excess(returns, [0, 0, 0, 0], [1.0], [4], min_n=2)
    assert n == 4
    # trade 0: 1.0 - (1.0-1.0)/3 = 1.0 ; others: 0 - (1.0-0)/3 = -1/3
    assert out == pytest.approx((1.0 - 1.0 / 3 * 3) / 4)


def test_alpha_excess_skips_thin_buckets():
    out, n = K.alpha_excess([0.5, 0.2], [0, 0], [0.7], [2], min_n=10)
    assert (out, n) == (0.0, 0), "a baseline from 2 trades was used"


def test_alpha_excess_is_zero_for_a_wallet_matching_its_bucket():
    """The control's whole purpose: no skill must read as no alpha."""
    returns = [0.2] * 50
    out, n = K.alpha_excess(returns, [0] * 50, [0.2 * 200], [200], min_n=10)
    assert abs(out) < 1e-9, "a wallet identical to its bucket showed alpha"


def test_max_drawdown_basic():
    assert K.max_drawdown([10, 20, 10, 40]) == pytest.approx(0.5)
    assert K.max_drawdown([]) == 0.0
    assert K.max_drawdown([5, 6, 7]) == 0.0


def test_max_drawdown_never_returns_infinity():
    """An infinity here would propagate silently into a risk limit."""
    for curve in ([0.0, -5.0, 3.0], [-1.0, -2.0], [0.0, 0.0]):
        v = K.max_drawdown(curve)
        assert math.isfinite(v), f"{curve} produced {v}"


def test_transition_chi2_on_independent_noise_is_small():
    seq = [(i * 7 + 3) % 2 for i in range(200)]
    chi, n, p = K.transition_chi2(seq)
    assert n == 199
    assert 0.4 < p < 0.6


def test_transition_chi2_detects_perfect_alternation():
    chi, n, p = K.transition_chi2([0, 1] * 100)
    assert chi > 3.84, "perfect alternation read as independent"


def test_transition_chi2_ignores_flat_prints():
    chi_a, n_a, _ = K.transition_chi2([0, 1, 0, 1, 0, 1])
    chi_b, n_b, _ = K.transition_chi2([0, 5, 1, 0, 1, 9, 0, 1])
    assert n_a == n_b, "non-binary symbols were not filtered"


def test_block_bootstrap_is_deterministic_given_a_seed():
    vals = [0.1, -0.2, 0.3, 0.05, -0.15, 0.22, 0.0, 0.4]
    a = K.block_bootstrap(vals, 50, 4, 12345)
    b = K.block_bootstrap(vals, 50, 4, 12345)
    assert a == b, "a reported p-value would not be reproducible"
    c = K.block_bootstrap(vals, 50, 4, 999)
    assert a != c


def test_block_bootstrap_mean_is_near_the_sample_mean():
    vals = [0.1] * 40
    out = K.block_bootstrap(vals, 100, 5, 7)
    assert all(abs(x - 0.1) < 1e-9 for x in out)


def test_block_bootstrap_handles_empty_input():
    assert K.block_bootstrap([], 10, 3, 1) == []
    assert K.block_bootstrap([1.0], 0, 3, 1) == []


# ------------------------------------------------------------- the contract
def test_accelerator_works_with_no_backend():
    a = Accelerator(mode="disabled")
    assert a.active is False
    assert a.call("max_drawdown", [10, 5]) == pytest.approx(0.5)


def test_auto_mode_falls_back_to_python_cleanly():
    a = Accelerator(mode="auto")
    assert a.call("max_drawdown", [10, 20, 10]) == pytest.approx(0.5)
    st = a.status()
    assert st["kernels"]
    assert "authoritative" in st["note"]


def test_unknown_kernel_raises():
    with pytest.raises(KeyError):
        Accelerator(mode="disabled").call("does_not_exist", [])


def test_attribute_style_dispatch():
    a = Accelerator(mode="disabled")
    assert a.max_drawdown([4, 2]) == pytest.approx(0.5)
    with pytest.raises(AttributeError):
        _ = a.nonexistent_kernel


def test_should_build_states_a_measured_verdict():
    out = should_build()
    assert out["criteria"] and len(out["criteria"]) >= 3
    assert "DO NOT BUILD" in out["current_verdict"]
    assert "55x" in out["current_verdict"] or "pooling" in out["current_verdict"]


# ------------------------------------------------------------- equivalence
@needs_rust
@pytest.mark.parametrize("curve", [
    [10, 20, 10, 40], [1, 2, 3], [], [0.0, -5.0, 3.0], [100, 1, 100, 1]])
def test_rust_max_drawdown_matches(curve):
    assert pqv3_accel.max_drawdown(curve) == pytest.approx(
        K.max_drawdown(curve), abs=1e-12)


@needs_rust
def test_rust_alpha_excess_matches():
    returns = [0.1 * i for i in range(60)]
    ids = [i % 3 for i in range(60)]
    sums = [sum(r for r, b in zip(returns, ids) if b == k) for k in range(3)]
    counts = [sum(1 for b in ids if b == k) for k in range(3)]
    a = K.alpha_excess(returns, ids, sums, counts, 10)
    b = pqv3_accel.alpha_excess(returns, ids, sums, counts, 10)
    assert a[1] == b[1]
    assert a[0] == pytest.approx(b[0], abs=1e-12)


@needs_rust
def test_rust_bootstrap_reproduces_the_python_sequence():
    """Same generator, same seed, same values — not merely same distribution."""
    vals = [0.1, -0.2, 0.3, 0.05, -0.15, 0.22, 0.0, 0.4]
    a = K.block_bootstrap(vals, 200, 3, 4242)
    b = pqv3_accel.block_bootstrap(vals, 200, 3, 4242)
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert x == pytest.approx(y, abs=1e-12)


@needs_rust
def test_shadow_mode_returns_python_and_records_divergence():
    a = Accelerator(mode="shadow")
    out = a.call("max_drawdown", [10, 20, 10, 40])
    assert out == pytest.approx(K.max_drawdown([10, 20, 10, 40]))
    st = a.status()
    assert st["returns_rust_results"] is False
    assert st["divergences"]["max_drawdown"]["equivalent"], (
        "Rust diverged from the Python reference")
