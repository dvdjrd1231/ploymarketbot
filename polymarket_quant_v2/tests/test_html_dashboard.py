"""The HTML dashboard: it must render from partial data and never mislead."""

from __future__ import annotations

import json
import re

import pytest

from pqv2.report import html_dashboard as hd


@pytest.fixture
def reports(tmp_path):
    r = tmp_path / "reports"
    r.mkdir()
    (r / "shadow.json").write_text(json.dumps({
        "funnel": {"A": {"received": 0}, "B": {
            "opportunities": 3147, "received": 17268,
            "behavior_matched": 15396, "accepted": 3876,
            "strategy_rejected": 13392, "risk_rejected": 0,
            "portfolio_rejected": 3866, "execution_attempted": 10,
            "execution_successful": 6, "execution_failed": 4,
            "wins": 2, "losses": 4, "pnl": -296.16, "expectancy": -0.4936,
            "top_rejections": [["b.conditions", 11520],
                               ["p.wallet_share", 3620],
                               ["x.unpriced", 4]]}},
        "account": {"equity": 9703.84, "max_drawdown": 0.0347}}))
    (r / "last_pass.json").write_text(json.dumps({
        "wallets": ["0xa", "0xb"], "hypotheses_tested": 150365,
        "bh_threshold": 0.0675,
        "status_histogram": [["INSUFFICIENT_EVIDENCE", 879], ["FAILED", 274],
                             ["VALIDATED", 34]],
        "validated": [{"score": 0.834, "wallet": "0xe91171f655aa",
                       "describe": "copy when price 0.30-0.70",
                       "oos": {"expectancy": 0.4983, "n_filled": 42,
                               "n_markets": 12},
                       "alpha": {"alpha": 0.4082}}],
        "agreement": [{"rule_id": "R1", "describe": "copy when price 0.30-0.70",
                       "wallets_tested": 11, "wallets_positive": 6,
                       "wallets_validated": 2, "mean_alpha": 0.1118,
                       "cross_wallet_t": 0.907, "mean_expectancy": 0.07}],
        "baselines": [{"wallet": "0xaaaa1111", "naive_oos_fills": 606,
                       "naive_oos_expectancy": 0.1198,
                       "naive_fill_rate": 0.941,
                       "pit_evidence_share": 0.0}]}))
    (r / "strategy_a_audit.json").write_text(json.dumps({
        "strategy_a": {"decisions_total": 40820, "executions": 0,
                       "blocking_gate": "v1.learning_mode"},
        "orphaned_evidence": {"validated": [
            {"wallet": "0x84cfffc3f16dcc", "score": 0.7381,
             "oos_p": 5.7e-174, "price_band": [0.7, 0.98], "delay_secs": 300,
             "test_expectancy": 0.2003, "test_fills": 78, "test_markets": 6,
             "test_win_rate": 1.0}],
            "caveats": ["NOT CONNECTED: nothing reads them."]}}))
    return r


def test_renders_a_complete_page(reports):
    h = hd.build(reports)
    assert h.startswith("<!doctype html>")
    assert h.rstrip().endswith("</html>")
    assert "<svg" in h and "<table>" in h


def test_renders_from_no_data_at_all(tmp_path):
    """A dashboard that crashes on an empty run is useless exactly when you
    most need to know why the run was empty."""
    empty = tmp_path / "reports"
    empty.mkdir()
    h = hd.build(empty)
    assert "<!doctype html>" in h
    assert "</html>" in h


def test_renders_with_only_one_report(tmp_path):
    r = tmp_path / "reports"
    r.mkdir()
    (r / "last_pass.json").write_text(json.dumps(
        {"status_histogram": [["FAILED", 3]], "hypotheses_tested": 10}))
    h = hd.build(r)
    assert "Where candidates stopped" in h


def test_every_bar_is_directly_labelled(reports):
    """Relief rule: three light-mode slots sit below 3:1 on the light surface,
    so identity may never rest on colour alone."""
    h = hd.build(reports)
    for svg in re.findall(r"<svg.*?</svg>", h, re.S):
        bars = svg.count("<rect class='bar'")
        labels = svg.count("class='vlab'")
        if bars:
            assert labels >= bars, "a bar is missing its value label"


def test_every_chart_has_a_tooltip_title(reports):
    h = hd.build(reports)
    for svg in re.findall(r"<svg.*?</svg>", h, re.S):
        bars = svg.count("<rect class='bar'")
        if bars:
            assert svg.count("<title>") >= bars


def test_gate_owners_get_fixed_slots_never_cycled():
    """Colour follows the entity, not its rank: a filter that changes the row
    order must not repaint the survivors."""
    assert hd.OWNER_SLOT["STRATEGY_A"] == 1
    assert hd.OWNER_SLOT["STRATEGY_B"] == 2
    assert len(set(hd.OWNER_SLOT.values())) == len(hd.OWNER_SLOT)


def test_dark_mode_is_declared_under_both_scopes(reports):
    """The media query covers the OS setting; the data-theme scope covers the
    toggle, and the toggle must win in both directions."""
    h = hd.build(reports)
    assert "prefers-color-scheme:dark" in h
    assert ':root[data-theme=dark] .app' in h
    assert ':root:where(:not([data-theme=light]))' in h


def test_dark_sequential_ramp_is_stepped_not_flipped(reports):
    """Dark mode is selected for the dark surface, never an automatic inversion
    of the light values."""
    h = hd.build(reports)
    dark = h.split("prefers-color-scheme:dark")[1][:900]
    assert "--seq-1:#184f95" in dark, (
        "the dark ramp must start at the ordinal floor for a dark surface")


def test_status_bar_is_pinned_to_the_bottom(reports):
    """On a short tab the status bar must sit at the bottom of the viewport,
    not float mid-page with the app background showing underneath it.

    Checked as CSS structure because there is no browser here: a flex column
    with `main` growing is what produces the sticky-footer behaviour.
    """
    h = " ".join(hd.build(reports).split())
    assert "min-height:100vh; display:flex; flex-direction:column;" in h
    assert "flex:1 0 auto}" in h, "main must grow to fill the column"
    assert ".statusbar{" in h and "flex:0 0 auto}" in h


def test_no_dual_axis_anywhere(reports):
    h = hd.build(reports)
    assert "twinx" not in h and "y2" not in h


def test_the_page_states_what_validated_does_not_mean(reports):
    """The single most important guard against misreading this dashboard."""
    h = hd.build(reports)
    # Normalise whitespace: the guarantee is that the sentence is present, not
    # that the source happens not to wrap it.
    low = " ".join(h.lower().split())
    assert "paper trading only" in low
    assert "no claim of guaranteed profit" in low
    assert "has traded real money" in low


def test_denominators_accompany_headline_numbers(reports):
    """A number without its denominator is not information.

    "34 validated" means nothing; "34 of 1,535 candidates" does.
    """
    h = " ".join(hd.build(reports).split())
    assert "of 40,820 decisions" in h, "the zero-trade count needs its base"
    # 879 + 274 + 34 from the fixture's status histogram
    assert "of 1,187 candidates" in h, "the validated count needs its base"
    assert "swept this pass" in h, "the wallet count needs its scope"
    assert "the denominator, always reported" in h


def test_orphaned_evidence_is_surfaced_with_its_caveats(reports):
    h = hd.build(reports)
    assert "Validated strategies that nothing reads" in h
    assert "Do not connect these" in h
    assert "NOT CONNECTED" in h


def test_strategy_a_zero_column_is_explained_not_just_shown(reports):
    h = hd.build(reports)
    assert "structurally zero" in h


def test_html_is_escaped(tmp_path):
    r = tmp_path / "reports"
    r.mkdir()
    (r / "last_pass.json").write_text(json.dumps({
        "status_histogram": [], "hypotheses_tested": 1,
        "validated": [{"score": 1.0, "wallet": "<script>x</script>",
                       "describe": "<img onerror=1>",
                       "oos": {"expectancy": 0.1, "n_filled": 1,
                               "n_markets": 1},
                       "alpha": {"alpha": 0.1}}]}))
    h = hd.build(r)
    assert "<script>x</script>" not in h.split("<script>")[-2] if "<script>" in h else True
    assert "&lt;img onerror=1&gt;" in h


def test_write_creates_the_file(reports, tmp_path):
    out = hd.write(reports, tmp_path / "d" / "dashboard.html")
    assert out.exists()
    assert out.read_text(encoding="utf-8").startswith("<!doctype html>")
