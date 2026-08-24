"""A feature has to move meaningfully before it may carry a rule.

The audit finding: discovery reads thresholds off each feature's own
quantiles, and the only thing standing between a feature and a rule was
``np.nanstd(v) == 0``. Dust has a non-zero standard deviation, so a velocity
column that is a run of exact zeros with floating-point noise on top passed,
its 90th percentile landed inside the noise, and the delivered library
contains the result:

    SHORT when np_ask_quote_vel > 3.8e-10; stop 2.9%, target 9.6%; size 1204

That threshold is a statement about rounding. These tests pin the floors that
keep it out, and — just as important — pin the features that must still get
through, because the cheap ways to block dust also block rare-event columns
and binary flags, which are legitimate.

The bridge has no test suite of its own, so its selector is exercised from
here, where the suite actually runs.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("pandas")

from pqb.quant import QuantBridgeNotFound, ensure_on_path

try:
    ensure_on_path()
    from core.strategy_discovery import StrategyDiscovery
except (QuantBridgeNotFound, ImportError) as exc:      # pragma: no cover
    pytest.skip(f"quant bridge unavailable: {exc}", allow_module_level=True)


N = 1000


def _selector(frame: pd.DataFrame, price: pd.Series, **discovery):
    """A StrategyDiscovery with just enough wired up to select features."""

    class _FeatureSet:
        pass

    fs = _FeatureSet()
    fs.frame = frame
    fs.price = price
    fs.names = list(frame.columns)

    sd = StrategyDiscovery.__new__(StrategyDiscovery)
    sd.fs = fs
    sd.d = {"max_features_scanned": 40, **discovery}
    sd.log = lambda _m: None
    return sd


def _price(rng: np.random.RandomState) -> pd.Series:
    return pd.Series(np.cumsum(rng.randn(N)) * 0.01 + 0.5)


# -- what must be rejected ----------------------------------------------------

def test_the_dust_column_that_produced_the_shipped_rule_is_rejected():
    """96% exact zeros with 1e-10 noise on the rest — the real column."""
    seen = 0
    for seed in range(40):
        rng = np.random.RandomState(seed)
        price = _price(rng)
        frame = pd.DataFrame({
            "np_ask_quote_vel": np.concatenate(
                [np.zeros(960), rng.randn(40) * 1e-10]),
        })
        if "np_ask_quote_vel" in _selector(frame, price)._select_features():
            seen += 1
    assert seen == 0, f"dust column selected in {seen}/40 series"


def test_uniform_float_noise_is_rejected():
    rng = np.random.RandomState(3)
    price = _price(rng)
    frame = pd.DataFrame({"np_bid_quote_vel": rng.randn(N) * 1e-12})
    assert _selector(frame, price)._select_features() == []


def test_a_constant_column_is_rejected():
    rng = np.random.RandomState(4)
    price = _price(rng)
    frame = pd.DataFrame({"outcome_count": np.full(N, 2.0)})
    assert _selector(frame, price)._select_features() == []


def test_selecting_nothing_is_an_allowed_answer():
    """Better an empty search than a search fitted to whatever dust remains."""
    rng = np.random.RandomState(5)
    price = _price(rng)
    frame = pd.DataFrame({
        "a": np.full(N, 1.0),
        "b": rng.randn(N) * 1e-14,
        "c": np.concatenate([np.zeros(995), rng.randn(5) * 1e-9]),
    })
    assert _selector(frame, price)._select_features() == []


# -- what must still get through ----------------------------------------------

def test_a_genuine_predictor_is_kept():
    rng = np.random.RandomState(6)
    price = _price(rng)
    fwd = (price.shift(-1) - price).fillna(0.0).values
    frame = pd.DataFrame({"good": fwd * 3 + rng.randn(N) * 0.005})
    assert _selector(frame, price)._select_features() == ["good"]


def test_a_small_scale_feature_is_not_punished_for_its_units():
    """1e-9 is dust or a unit choice depending on the spread, not the scale."""
    rng = np.random.RandomState(7)
    price = _price(rng)
    fwd = (price.shift(-1) - price).fillna(0.0).values
    frame = pd.DataFrame({"tiny_units": (fwd * 3 + rng.randn(N) * 0.005) * 1e-9})
    assert _selector(frame, price)._select_features() == ["tiny_units"]


def test_a_rare_event_column_can_still_qualify():
    """A cascade firing on a minority of bars is worth trading, not dust."""
    rng = np.random.RandomState(8)
    price = _price(rng)
    fwd = (price.shift(-1) - price).fillna(0.0).values
    frame = pd.DataFrame({
        "liq_cascade": np.where(rng.rand(N) < 0.15, 1.0, 0.0) + fwd * 4,
    })
    assert _selector(frame, price)._select_features() == ["liq_cascade"]


def test_a_binary_flag_is_thresholdable_and_not_excluded_by_cardinality():
    rng = np.random.RandomState(9)
    price = _price(rng)
    fwd = (price.shift(-1) - price).fillna(0.0).values
    flag = (fwd > 0).astype(float)
    frame = pd.DataFrame({"quote_is_live": flag})
    assert _selector(frame, price)._select_features() == ["quote_is_live"]


# -- the mechanism ------------------------------------------------------------

def test_the_effect_floor_scales_with_the_evidence_the_feature_offers():
    """A mostly-flat column speaks about its departures, not the whole series."""
    flat_for_most = np.concatenate([np.zeros(960), np.arange(1.0, 41.0)])
    broad = np.arange(float(N))

    assert StrategyDiscovery._informative_rows(flat_for_most, 0.995, 2) == 40
    assert StrategyDiscovery._informative_rows(broad, 0.995, 2) == N


def test_floors_can_be_turned_off():
    rng = np.random.RandomState(10)
    price = _price(rng)
    frame = pd.DataFrame({
        "np_ask_quote_vel": np.concatenate(
            [np.zeros(960), rng.randn(40) * 1e-10]),
    })
    kept = _selector(frame, price, min_feature_effect=0.0,
                     feature_effect_sigma=0.0, max_feature_flat_share=0.0,
                     min_feature_distinct=0)._select_features()
    assert kept == ["np_ask_quote_vel"]


def test_jitter_below_the_scale_tolerance_is_not_counted_as_variation():
    zeros_with_jitter = np.zeros(N)
    zeros_with_jitter[500] = 1.0          # sets the scale
    zeros_with_jitter[:400] = 1e-18       # dust far below tolerance
    assert StrategyDiscovery._informative_rows(zeros_with_jitter, 0.995, 2) == 0
