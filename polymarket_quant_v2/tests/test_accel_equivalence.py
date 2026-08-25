"""Python/Rust equivalence, and the fallback contract.

These run whether or not the Rust extension is built. When it is absent the
equivalence tests skip and the FALLBACK tests still run -- because the
behaviour that matters most is what happens when Rust is missing or broken, and
that must be tested on every machine, not only on ones with a toolchain.
"""

from __future__ import annotations

import pytest

from conftest import make_obs
from pqv2.accel import Accelerator, Divergence, should_build
from pqv2.accel import kernels
from pqv2.config import Settings
from pqv2.strategy_b.strategy import candidates_for

rust = kernels.rust_kernel("sweep_admit")
needs_rust = pytest.mark.skipif(rust is None,
                                reason="Rust extension not built (expected)")


# --- the wire format --------------------------------------------------------

def test_flatten_widths_match_the_declared_layout():
    """A silent column shift would compare the right numbers in the wrong
    places and produce plausible nonsense."""
    obs = [make_obs() for _ in range(3)]
    flat = kernels.flatten_observations(obs)
    assert len(flat) == 3 * kernels.OBS_WIDTH

    strategies = list(candidates_for("w"))[:4]
    ff = kernels.flatten_filters(strategies)
    assert len(ff) == 4 * kernels.FILTER_WIDTH


def test_rust_source_declares_the_same_widths():
    """The Rust constants are the other half of the contract."""
    from pathlib import Path
    src = (Path(__file__).resolve().parent.parent / "rust" / "src" / "lib.rs")
    if not src.exists():
        pytest.skip("rust source not present")
    text = src.read_text(encoding="utf-8")
    assert f"const WIDTH: usize = {kernels.OBS_WIDTH};" in text
    assert f"const FWIDTH: usize = {kernels.FILTER_WIDTH};" in text


def test_none_becomes_nan_meaning_no_constraint():
    s = list(candidates_for("w"))[0]        # naive copy: every filter None
    flat = kernels.flatten_filters([s])
    nans = [v for v in flat if v != v]
    assert len(nans) >= 10


# --- the Python reference ---------------------------------------------------

def test_reference_sweep_matches_the_strategy_object(st):
    """kernels.sweep_admit must agree with CopyStrategy.admits_fast.

    Python is the reference implementation, so this is the test that anchors
    everything: if these two disagree, Rust matching either one is meaningless.
    """
    from pqv2.substrate.state import collect
    obs = collect(st, limit=300)
    strategies = [s for s in list(candidates_for("0xedge"))[:60]]
    flat_obs = kernels.flatten_observations(obs)
    flat_f = kernels.flatten_filters(strategies)
    out = kernels.sweep_admit(flat_obs, flat_f, 1.0, 0.02, 0.98)

    for i, s in enumerate(strategies):
        expected = sum(1 for o in obs
                       if s.admits_fast(o) and 0.02 < o.price < 0.98)
        assert out[i * 4] == expected, s.spec()


def test_reference_t_stat_matches_stats_module():
    from pqv2.validation.stats import t_stat as ref
    xs = [0.1, -0.2, 0.3, 0.05, -0.1, 0.4, 0.2, -0.3]
    _, _, t = kernels.t_stat(xs)
    assert t == pytest.approx(ref(xs), rel=1e-12)


def test_bootstrap_is_deterministic_from_the_seed():
    xs = [0.1, -0.2, 0.3, 0.5, -0.4] * 6
    a = kernels.bootstrap_means(xs, 50, 42)
    b = kernels.bootstrap_means(xs, 50, 42)
    c = kernels.bootstrap_means(xs, 50, 43)
    assert a == b
    assert a != c


# --- equivalence, when Rust is present --------------------------------------

@needs_rust
def test_rust_sweep_matches_python_exactly(st):
    from pqv2.substrate.state import collect
    obs = collect(st, limit=400)
    strategies = list(candidates_for("0xedge"))[:120]
    fo = kernels.flatten_observations(obs)
    ff = kernels.flatten_filters(strategies)
    py = kernels.sweep_admit(fo, ff, 1.005, 0.02, 0.98)
    rs = kernels.rust_kernel("sweep_admit")(fo, ff, 1.005, 0.02, 0.98)
    assert len(py) == len(rs)
    for i, (a, b) in enumerate(zip(py, rs)):
        assert abs(a - b) < 1e-9, f"index {i}: {a} != {b}"


@needs_rust
def test_rust_bootstrap_draws_the_same_indices():
    xs = [0.1, -0.2, 0.3, 0.5, -0.4] * 8
    py = kernels.bootstrap_means(xs, 100, 7)
    rs = kernels.rust_kernel("bootstrap_means")(xs, 100, 7)
    for a, b in zip(py, rs):
        assert abs(a - b) < 1e-12


# --- the fallback contract, which must hold everywhere ----------------------

def test_missing_rust_never_raises():
    """Rule 22: never destroy the application because Rust is unavailable."""
    st = Settings()
    st.accel.mode = "enabled"
    acc = Accelerator(st)
    result = acc.call("k", lambda: 42, None)
    assert result == 42


def test_a_raising_rust_kernel_falls_back_to_python():
    st = Settings()
    st.accel.mode = "enabled"
    acc = Accelerator(st)
    # Pretend the extension loaded. The constructor downgrades `enabled` to
    # `disabled` when no backend is importable, so both must be restored to
    # exercise the fallback path rather than the disabled one.
    acc.backend = object()
    acc.mode = "enabled"

    def boom():
        raise RuntimeError("segfault-ish")

    assert acc.call("k", lambda: 7, boom) == 7
    assert acc.divergences["k"].n_diverged == 1


def test_shadow_mode_returns_python_even_when_rust_disagrees():
    """Shadow exists to build confidence, not to take risk."""
    st = Settings()
    st.accel.mode = "shadow"
    acc = Accelerator(st)
    acc.backend = object()
    out = acc.call("k", lambda: 1.0, lambda: 2.0)
    assert out == 1.0
    assert not acc.divergences["k"].equivalent
    assert acc.divergences["k"].worst == pytest.approx(1.0)


def test_shadow_mode_reports_agreement():
    st = Settings()
    st.accel.mode = "shadow"
    acc = Accelerator(st)
    acc.backend = object()
    acc.call("k", lambda: [1.0, 2.0], lambda: [1.0, 2.0])
    assert acc.divergences["k"].equivalent


def test_disabled_mode_never_calls_rust():
    st = Settings()
    st.accel.mode = "disabled"
    acc = Accelerator(st)
    called = []
    acc.call("k", lambda: 1, lambda: called.append(1))
    assert not called


def test_build_trigger_is_a_rule_not_a_preference():
    small = should_build(tape_rows=878_650, hypotheses_per_wallet=5_184,
                         pass_seconds=120.0)
    assert not small["build"]
    assert "Do not build yet" in small["verdict"]

    big = should_build(tape_rows=50_000_000, hypotheses_per_wallet=500_000,
                       pass_seconds=7200.0)
    assert big["build"]
    assert len(big["triggers"]) == 3
