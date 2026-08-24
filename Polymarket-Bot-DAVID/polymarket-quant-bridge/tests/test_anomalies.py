"""The six detectors, one section per pattern the brief names."""

from __future__ import annotations

import time

import pytest

from pqb.analytics.anomalies import (
    AnomalyConfig, KINDS, build_combos, detect_all, detect_behaviour_drift,
    detect_combo_edge, detect_convergence, detect_lead_lag, detect_market_flow,
    detect_size_spikes,
)
from pqb.analytics.features import build_profiles
from pqb.analytics.ranking import rank_wallets
from pqb.models import MarketIntel

NOW = 1_800_000_000.0


def row(wallet="0xaaa", ts=None, token="T1", side="BUY", price=0.40,
        usdc=100.0, market="M1", outcome="Yes"):
    return {"wallet": wallet, "ts": int(ts if ts is not None else NOW),
            "token_id": token, "side": side, "price": price, "usdc": usdc,
            "market_id": market, "outcome": outcome}


@pytest.fixture
def cfg():
    return AnomalyConfig()


def _intel(rows, resolutions=None, cohort_size=25):
    profiles = build_profiles(rows, resolutions or {}, now=NOW)
    return profiles, rank_wallets(profiles, cohort_size=cohort_size, now=NOW)


# --- 1. a normally quiet wallet suddenly betting big ------------------------

def test_size_spike_fires_on_a_wallets_own_baseline(cfg):
    history = [row(ts=NOW - 86400 + i * 60, usdc=100.0) for i in range(30)]
    spike = row(ts=NOW, usdc=8_000.0)
    profiles, intel = _intel(history + [spike], {"T1": 1.0})

    found = detect_size_spikes([spike], profiles, intel, cfg)
    assert len(found) == 1
    assert found[0].kind == "size_spike"
    assert found[0].z >= cfg.size_z
    assert found[0].wallet == "0xaaa"
    # The wallet's standing travels with the signal rather than gating it, so
    # "mediocre wallet, huge bet" is discoverable as its own pattern.
    assert "walletRank" in found[0].detail
    assert found[0].detail["walletMedianUsdc"] == 100.0


def test_size_spike_is_relative_not_absolute(cfg):
    # The same $8,000 from a wallet that always trades that size is not news.
    history = [row(ts=NOW - 86400 + i * 60, usdc=8_000.0) for i in range(30)]
    current = row(ts=NOW, usdc=8_000.0)
    profiles, intel = _intel(history + [current], {"T1": 1.0})
    assert detect_size_spikes([current], profiles, intel, cfg) == []


def test_size_spike_ignores_dust(cfg):
    history = [row(ts=NOW - 86400 + i * 60, usdc=0.10) for i in range(30)]
    spike = row(ts=NOW, usdc=5.0)
    profiles, intel = _intel(history + [spike], {"T1": 1.0})
    assert detect_size_spikes([spike], profiles, intel, cfg) == []


# --- 2. a lower-ranked wallet repeatedly entering before better ones ---------

def _lead_lag_rows():
    """A weak-but-early wallet, and a strong wallet that follows it."""
    rows = []
    resolutions = {}
    # The follower earns a top rank on its own settled record.
    for i in range(30):
        token = f"hist{i}"
        rows.append(row(wallet="0xfollower", ts=int(NOW - 30 * 86400),
                        token=token, market=f"H{i}"))
        resolutions[token] = 1.0 if i < 22 else 0.0
    # The leader's own record is unremarkable...
    for i in range(20):
        token = f"lead-hist{i}"
        rows.append(row(wallet="0xleader", ts=int(NOW - 30 * 86400),
                        token=token, market=f"L{i}"))
        resolutions[token] = 1.0 if i < 10 else 0.0
    # ...but it is repeatedly first into what the follower later buys.
    for i in range(5):
        token = f"shared{i}"
        rows.append(row(wallet="0xleader", ts=int(NOW - 20_000 - i * 100),
                        token=token, market=f"S{i}"))
        rows.append(row(wallet="0xfollower", ts=int(NOW - 10_000 - i * 100),
                        token=token, market=f"S{i}"))
        resolutions[token] = 1.0
    return rows, resolutions


def test_lead_lag_needs_repetition(cfg):
    rows, resolutions = _lead_lag_rows()
    profiles, intel = _intel(rows, resolutions, cohort_size=1)
    found = detect_lead_lag(rows, intel, cfg)

    leaders = {a.wallet for a in found}
    assert "0xleader" in leaders
    signal = next(a for a in found if a.wallet == "0xleader")
    assert signal.detail["leadEvents"] >= cfg.lead_min_events
    assert signal.detail["distinctTokens"] >= 3


def test_lead_lag_ignores_a_single_early_entry(cfg):
    rows, resolutions = _lead_lag_rows()
    # Strip it back to one shared token: one wallet happening to be early once
    # is noise, and only a sustained pattern separates informed from lucky.
    rows = [r for r in rows if not r["token_id"].startswith("shared")
            or r["token_id"] == "shared0"]
    profiles, intel = _intel(rows, resolutions, cohort_size=1)
    assert [a for a in detect_lead_lag(rows, intel, cfg)
            if a.wallet == "0xleader"] == []


def test_lead_lag_does_not_fire_when_the_best_wallet_leads(cfg):
    """A top wallet being followed by a worse one is the ordinary case."""
    rows, resolutions = _lead_lag_rows()
    swapped = []
    for r in rows:
        r = dict(r)
        if r["token_id"].startswith("shared"):
            r["wallet"] = ("0xleader" if r["wallet"] == "0xfollower"
                           else "0xfollower")
        swapped.append(r)
    profiles, intel = _intel(swapped, resolutions, cohort_size=1)
    assert [a for a in detect_lead_lag(swapped, intel, cfg)
            if a.wallet == "0xfollower"] == []


# --- 3. several wallets converging on one outcome, quickly ------------------

def test_convergence_counts_distinct_wallets(cfg):
    recent = [row(wallet=f"0xw{i}", ts=NOW - i * 60, usdc=200.0)
              for i in range(5)]
    _profiles, intel = _intel(recent, {"T1": 1.0})
    found = detect_convergence(recent, intel, cfg)
    assert len(found) == 1
    assert found[0].kind == "convergence"
    assert found[0].detail["wallets"] == 5
    assert found[0].token_id == "T1"


def test_one_wallet_trading_repeatedly_is_conviction_not_consensus(cfg):
    recent = [row(wallet="0xaaa", ts=NOW - i * 60, usdc=500.0)
              for i in range(20)]
    _profiles, intel = _intel(recent, {"T1": 1.0})
    assert detect_convergence(recent, intel, cfg) == []


def test_convergence_requires_the_cluster_to_be_compressed(cfg):
    # Five wallets over three days is not "unusually quickly".
    recent = [row(wallet=f"0xw{i}", ts=NOW - i * 86400, usdc=500.0)
              for i in range(5)]
    _profiles, intel = _intel(recent, {"T1": 1.0})
    assert detect_convergence(recent, intel, cfg) == []


def test_convergence_ignores_sells(cfg):
    recent = [row(wallet=f"0xw{i}", ts=NOW - i * 60, side="SELL", usdc=500.0)
              for i in range(6)]
    _profiles, intel = _intel(recent, {"T1": 1.0})
    assert detect_convergence(recent, intel, cfg) == []


# --- 4. a wallet behaving unlike its own history ----------------------------

def test_drift_detects_a_cadence_change(cfg):
    history = [row(ts=NOW - 30 * 86400 + i * 86400, usdc=100.0,
                   token=f"h{i}", market=f"M{i}") for i in range(30)]
    burst = [row(ts=NOW - i * 60, usdc=100.0, token="h0", market="M0")
             for i in range(25)]
    profiles, intel = _intel(history + burst, {f"h{i}": 1.0 for i in range(30)})

    found = detect_behaviour_drift(burst, profiles, intel, cfg, now=NOW)
    assert len(found) == 1
    assert found[0].kind == "behaviour_drift"
    assert found[0].detail["rateZ"] > found[0].detail["sizeZ"]


def test_drift_detects_looking_somewhere_new(cfg):
    history = [row(ts=NOW - 30 * 86400 + i * 3600, usdc=100.0,
                   token=f"h{i}", market="M-usual") for i in range(40)]
    elsewhere = [row(ts=NOW - 60, usdc=100.0, token="new", market="M-novel")]
    profiles, intel = _intel(history + elsewhere,
                             {f"h{i}": 1.0 for i in range(40)})
    found = detect_behaviour_drift(elsewhere, profiles, intel, cfg, now=NOW)
    assert found and found[0].detail["marketNovelty"] == 1.0


def test_drift_needs_a_normal_to_deviate_from(cfg):
    thin = [row(ts=NOW - 60, usdc=9_999.0)]
    profiles, intel = _intel(thin, {"T1": 1.0})
    assert detect_behaviour_drift(thin, profiles, intel, cfg, now=NOW) == []


# --- 5. a market taking unusual capital vs its own baseline -----------------

def test_market_flow_uses_the_markets_own_baseline(cfg):
    quiet = MarketIntel(market_id="M-quiet", gross_usdc=50_000.0,
                        baseline_usdc=2_000.0, flow_z=6.0, wallets_active=12)
    busy = MarketIntel(market_id="M-busy", gross_usdc=50_000.0,
                       baseline_usdc=500_000.0, flow_z=0.1, wallets_active=90)
    found = detect_market_flow({"M-quiet": quiet, "M-busy": busy}, cfg)
    # Same absolute flow; only the one that is unusual *for that market* fires.
    assert [a.market_id for a in found] == ["M-quiet"]
    assert found[0].strength > 0


def test_market_flow_ignores_trivial_notional(cfg):
    tiny = MarketIntel(market_id="M", gross_usdc=10.0, flow_z=99.0)
    assert detect_market_flow({"M": tiny}, cfg) == []


# --- 6. wallet x market x timing combinations with a proven record ----------

def _combo_rows():
    """A specialist with a settled, mostly-winning record in one niche.

    Prices vary so the returns do too — a combination whose trades all returned
    exactly the same amount is a special case, tested separately below.
    """
    rows, resolutions = [], {}
    for i in range(12):
        token = f"c{i}"
        rows.append(row(wallet="0xspecialist", ts=int(NOW - 20 * 86400),
                        token=token, market=f"P{i}", price=0.30 + i * 0.01))
        resolutions[token] = 0.0 if i in (3, 9) else 1.0
    return rows, resolutions


def test_combo_edge_needs_settled_history(cfg):
    rows, resolutions = _combo_rows()
    profiles, _ranked = _intel(rows, resolutions)
    categories = {f"P{i}": "Politics" for i in range(12)}
    end_times = {f"P{i}": int(NOW - 20 * 86400 + 3600) for i in range(12)}

    combos = build_combos(profiles, categories, end_times)
    assert combos
    combo = next(iter(combos.values()))
    assert len(combo.returns) == 12
    assert combo.t_stat > cfg.combo_min_t


def test_a_perfectly_consistent_edge_is_significant_not_noise(cfg):
    """Zero dispersion is the most consistent an edge can be.

    Dividing by a zero standard error has no answer, so significance falls back
    to sample size — but it must not fall back to *zero*, which would discard
    the strongest possible record.
    """
    rows, resolutions = [], {}
    for i in range(10):
        token = f"k{i}"
        rows.append(row(wallet="0xexact", ts=int(NOW - 20 * 86400),
                        token=token, market=f"K{i}", price=0.50))
        resolutions[token] = 1.0
    profiles, _ranked = _intel(rows, resolutions)
    categories = {f"K{i}": "Politics" for i in range(10)}
    end_times = {f"K{i}": int(NOW - 20 * 86400 + 3600) for i in range(10)}

    combo = next(iter(build_combos(profiles, categories, end_times).values()))
    assert _stdev_of(combo.returns) == 0.0
    assert combo.t_stat >= cfg.combo_min_t


def _stdev_of(values):
    from pqb.analytics.features import _stdev
    return _stdev(values)


def test_combo_edge_matches_a_live_trade_to_a_proven_combination(cfg):
    rows, resolutions = _combo_rows()
    profiles, intel = _intel(rows, resolutions)
    categories = {f"P{i}": "Politics" for i in range(12)}
    categories["P-live"] = "Politics"
    end_times = {f"P{i}": int(NOW - 20 * 86400 + 3600) for i in range(12)}
    end_times["P-live"] = int(NOW + 3600)

    combos = build_combos(profiles, categories, end_times)
    live = [row(wallet="0xspecialist", ts=NOW, token="live",
                market="P-live", price=0.40)]
    found = detect_combo_edge(live, combos, categories, end_times, intel, cfg)
    assert len(found) == 1
    assert found[0].kind == "combo_edge"
    assert found[0].token_id == "live"
    assert found[0].detail["historicalTrades"] == 12


def test_combo_edge_ignores_unrealised_paper_gains(cfg):
    """Only settled outcomes build a record.

    A mark-to-market estimate would let an open position that merely looks good
    create a 'proven' combination that has never actually paid out.
    """
    rows, _ = _combo_rows()
    profiles = build_profiles(rows, {}, mark_fn=lambda _t: 0.95, now=NOW)
    categories = {f"P{i}": "Politics" for i in range(12)}
    end_times = {f"P{i}": int(NOW) for i in range(12)}
    assert build_combos(profiles, categories, end_times) == {}


# --- the pass ---------------------------------------------------------------

def test_detect_all_is_bounded_and_sorted(cfg):
    cfg.max_per_cycle = 3
    recent = [row(wallet=f"0xw{i}", ts=NOW - i, usdc=400.0, token=f"T{i % 2}")
              for i in range(12)]
    profiles, intel = _intel(recent, {"T0": 1.0, "T1": 1.0})
    markets = {"M1": MarketIntel(market_id="M1", gross_usdc=99_000.0,
                                 flow_z=9.0, baseline_usdc=100.0)}
    found = detect_all(recent, recent, profiles, intel, markets,
                       categories={"M1": "Politics"}, end_times={"M1": int(NOW)},
                       cfg=cfg, now=NOW)
    assert len(found) <= 3
    assert found == sorted(found, key=lambda a: a.strength, reverse=True)


def test_detect_all_respects_the_off_switch(cfg):
    cfg.enabled = False
    assert detect_all([], [], {}, {}, {}, {}, {}, cfg, now=NOW) == []


def test_every_named_kind_is_reachable():
    # The brief names six patterns; each one has a detector and a column.
    assert set(KINDS) == {
        "size_spike", "lead_lag", "convergence", "behaviour_drift",
        "market_flow", "combo_edge",
    }
