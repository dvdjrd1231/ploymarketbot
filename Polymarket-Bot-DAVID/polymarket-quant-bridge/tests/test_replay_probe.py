"""The two tests the frozen ledger cannot answer, run against real series.

`test_adversarial.py` covers what `attack` does with a probe's answers using a
scripted stub. This file covers the answers themselves, which is where the
arithmetic that can flatter a candidate actually lives — and it was written
only after the probe was lifted out of `research.run`, because logic reachable
solely through a 2,000-line function is logic that never gets a test.
"""

from __future__ import annotations

import csv

from pqb.adversarial import FAILED, INCONCLUSIVE, NOT_RUN, SURVIVED
from pqb.config import ResearchConfig
from pqb.research import ReplayProbe


def _series(tmp_path, name: str, prices: list[float], spread: float = 0.0):
    """A minimal exported series: what `rows_from_csv` needs, nothing more."""
    path = tmp_path / f"{name}.csv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "price", "spread", "size", "side"])
        for i, price in enumerate(prices):
            writer.writerow([1_700_000_000 + i * 60, price, spread, 10, "buy"])
    return {"market": name, "csv": path, "token": f"tok-{name}",
            "tokenRaw": f"tok-{name}"}


def _probe(tmp_path, items, volumes=None):
    pool = {item["market"]: item for item in items}
    vols = volumes or {}
    return ReplayProbe(pool, lambda item: 0.0,
                       lambda ids: {m: vols.get(m, 0.0) for m in ids},
                       ResearchConfig())


def _row(market: str, trades: int, pnl: float, ts: float = 0.0):
    return {"market_id": market, "trades": trades, "wins": trades,
            "pnl": pnl, "ts": ts, "drawdown": 0.0}


def _entry(rule_type="sequence", hold=5, direction="up"):
    return {"id": "cand#v1",
            "rule": {"type": rule_type, "hold_bars": hold,
                     "direction": direction}}


# -- placebo ------------------------------------------------------------------

def test_placebo_fails_a_candidate_that_only_captured_drift(tmp_path):
    """THE case the control exists for. Prices rise monotonically, so every
    entry held five bars pays — including a random one. A candidate claiming
    an edge here has discovered the drift, and leave-one-out, temporal split
    and dispersion would all call it broad, stable and replicated.
    """
    items = [_series(tmp_path, f"M{i}", [1.0 + 0.01 * n for n in range(300)])
             for i in range(4)]
    probe = _probe(tmp_path, items)
    # Five bars of a +0.01/bar drift is +0.05 a trade, which is exactly what
    # the candidate claims — and exactly what a random entry also earns.
    ledger = [_row(f"M{i}", 4, 4 * 0.05) for i in range(4)]
    result, detail = probe.placebo(_entry(), {"expectancy": 0.05,
                                              "trades": 16}, ledger)
    assert result == FAILED
    assert "the hold, not the signal" in detail


def test_placebo_passes_a_candidate_that_beats_its_own_markets(tmp_path):
    """A flat, alternating market where random entries earn nothing. A record
    of +0.05 a trade there is not available by accident."""
    prices = [1.0 + (0.02 if n % 2 else -0.02) for n in range(300)]
    items = [_series(tmp_path, f"M{i}", prices) for i in range(4)]
    ledger = [_row(f"M{i}", 4, 4 * 0.05) for i in range(4)]
    result, _ = _probe(tmp_path, items).placebo(
        _entry(), {"expectancy": 0.05, "trades": 16}, ledger)
    assert result == SURVIVED


def test_placebo_is_reproducible(tmp_path):
    """Seeded from the candidate id. §14 wants the same verdict next month,
    and an unseeded control is one you can re-roll until it passes."""
    items = [_series(tmp_path, f"M{i}", [1.0 + 0.003 * n for n in range(300)])
             for i in range(4)]
    ledger = [_row(f"M{i}", 4, 4 * 0.01) for i in range(4)]
    cumulative = {"expectancy": 0.01, "trades": 16}
    first = _probe(tmp_path, items).placebo(_entry(), cumulative, ledger)
    again = _probe(tmp_path, items).placebo(_entry(), cumulative, ledger)
    assert first == again


def test_placebo_declines_rule_types_that_do_not_enter_and_hold(tmp_path):
    """A longshot resolves at settlement. Controlling it against a five-bar
    random hold compares two different experiments and reports the difference
    as a verdict on the candidate."""
    items = [_series(tmp_path, f"M{i}", [1.0] * 300) for i in range(4)]
    ledger = [_row(f"M{i}", 4, 0.2) for i in range(4)]
    result, detail = _probe(tmp_path, items).placebo(
        _entry(rule_type="longshot"), {"expectancy": 0.05}, ledger)
    assert result == NOT_RUN
    assert "different question" in detail


def test_placebo_declines_when_the_markets_have_left_the_pool(tmp_path):
    """Evidence markets are not guaranteed to still be in the pool, and a
    control run on one of them is a statement about that one market."""
    items = [_series(tmp_path, "M0", [1.0 + 0.01 * n for n in range(300)])]
    ledger = [_row("M0", 4, 0.2), _row("GONE", 8, 0.4)]
    result, detail = _probe(tmp_path, items).placebo(
        _entry(), {"expectancy": 0.05}, ledger)
    assert result == NOT_RUN
    assert "still in the pool" in detail


# -- liquidity stress ---------------------------------------------------------

def _liquidity_case(tmp_path, deep_pnl: float, thin_pnl: float):
    items = [_series(tmp_path, f"M{i}", [1.0] * 50) for i in range(4)]
    volumes = {"M0": 1_000_000.0, "M1": 800_000.0,
               "M2": 5_000.0, "M3": 4_000.0}
    ledger = [_row("M0", 5, deep_pnl / 2), _row("M1", 5, deep_pnl / 2),
              _row("M2", 5, thin_pnl / 2), _row("M3", 5, thin_pnl / 2)]
    return _probe(tmp_path, items, volumes).liquidity_stress(
        _entry(), {"expectancy": 0.1}, ledger)


def test_an_edge_only_in_untradable_markets_fails(tmp_path):
    """The specific failure: it pays only where the book is thinnest, which
    is precisely where the position cannot be taken."""
    result, detail = _liquidity_case(tmp_path, deep_pnl=-0.5, thin_pnl=2.0)
    assert result == FAILED
    assert "cannot get filled" in detail


def test_an_edge_that_pays_in_deep_books_survives(tmp_path):
    result, _ = _liquidity_case(tmp_path, deep_pnl=2.0, thin_pnl=1.0)
    assert result == SURVIVED


def test_an_edge_confined_to_deep_books_is_inconclusive_not_a_pass(tmp_path):
    """Takeable, but half the record just went negative. Reporting that as a
    clean survival would be the flattering read."""
    result, detail = _liquidity_case(tmp_path, deep_pnl=2.0, thin_pnl=-0.5)
    assert result == INCONCLUSIVE
    assert "takeable" in detail


def test_liquidity_declines_when_a_half_is_too_thin_to_speak(tmp_path):
    """The real-data case, reproduced: 9 of 12 trades in one market leaves a
    two-trade half, and a two-trade expectancy is a statement about the
    sample rather than about the book."""
    items = [_series(tmp_path, f"M{i}", [1.0] * 50) for i in range(4)]
    volumes = {f"M{i}": float(10 ** (6 - i)) for i in range(4)}
    ledger = [_row("M0", 1, 0.27), _row("M1", 1, -0.02),
              _row("M2", 9, -0.10), _row("M3", 1, -0.02)]
    result, detail = _probe(tmp_path, items, volumes).liquidity_stress(
        _entry(), {"expectancy": 0.01}, ledger)
    assert result == NOT_RUN
    assert "too thin" in detail


def test_liquidity_prefers_a_measured_spread_over_traded_value(tmp_path):
    """Spread measures what a fill costs directly. Volume is the fallback
    because most series cannot quote one — but the detail must name the basis
    used, or the split is not reconstructible."""
    items = [_series(tmp_path, f"M{i}", [1.0] * 50, spread=0.01 * (i + 1))
             for i in range(4)]
    ledger = [_row(f"M{i}", 5, 1.0) for i in range(4)]
    _, detail = _probe(tmp_path, items).liquidity_stress(
        _entry(), {"expectancy": 0.2}, ledger)
    assert detail.startswith("by spread:")


def test_liquidity_falls_back_to_traded_value_and_says_so(tmp_path):
    items = [_series(tmp_path, f"M{i}", [1.0] * 50) for i in range(4)]
    volumes = {f"M{i}": float(10 ** (6 - i)) for i in range(4)}
    ledger = [_row(f"M{i}", 5, 1.0) for i in range(4)]
    _, detail = _probe(tmp_path, items, volumes).liquidity_stress(
        _entry(), {"expectancy": 0.2}, ledger)
    assert detail.startswith("by traded value:")


def test_a_mixed_basis_is_refused_rather_than_ranked(tmp_path):
    """Ranking some markets by spread and others by volume produces an
    ordering that means nothing, so the split declines instead."""
    items = [_series(tmp_path, "M0", [1.0] * 50, spread=0.01),
             _series(tmp_path, "M1", [1.0] * 50, spread=0.02),
             _series(tmp_path, "M2", [1.0] * 50),
             _series(tmp_path, "M3", [1.0] * 50)]
    ledger = [_row(f"M{i}", 5, 1.0) for i in range(4)]
    result, detail = _probe(tmp_path, items).liquidity_stress(
        _entry(), {"expectancy": 0.2}, ledger)
    assert result == NOT_RUN
    assert "no single depth metric" in detail
