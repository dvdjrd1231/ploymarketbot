"""Research export and cross-token aggregation."""

from __future__ import annotations

import csv
import json
import time

import pytest

from pqb.features import FEATURE_NAMES
from pqb.research import (
    MANIFEST, aggregate, export_token_series, load_strategies, save,
    signature_of,
)


def capture(store, token: str, rows: int, start: float, market: str = "M1"):
    store.record_research_rows([
        (start + i * 60, token, market, "Yes", "Politics",
         {"price": 0.40 + (i % 10) / 100.0, "flow_z": float(i % 3),
          "liquidity": 50_000.0})
        for i in range(rows)
    ])


# --- export -----------------------------------------------------------------

def test_each_token_gets_its_own_directory(intel_store, tmp_path):
    now = time.time()
    capture(intel_store, "TOKEN-A", 250, now, market="M1")
    capture(intel_store, "TOKEN-B", 300, now, market="M2")

    written = export_token_series(intel_store, tmp_path, min_rows=200)
    assert len(written) == 2
    directories = {row["path"] for row in written}
    assert len(directories) == 2
    # One CSV per directory: the bridge merges every CSV in a directory into
    # one frame, so two tokens in one directory would be joined on timestamp
    # into a single nonsense series.
    for row in written:
        from pathlib import Path
        assert len(list(Path(row["path"]).parent.glob("*.csv"))) == 1


def test_thin_tokens_are_skipped_not_exported_thin(intel_store, tmp_path):
    now = time.time()
    capture(intel_store, "RICH", 250, now)
    capture(intel_store, "THIN", 50, now, market="M2")
    written = export_token_series(intel_store, tmp_path, min_rows=200)
    assert [row["tokenId"] for row in written] == ["RICH"]

    manifest = json.loads((tmp_path / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["belowMinRows"] == 1
    assert manifest["tokensCaptured"] == 2
    assert manifest["tokensEligible"] == 1
    assert manifest["minRows"] == 200


def test_the_token_cap_is_reported_not_silent(intel_store, tmp_path):
    # Researching 2 of 5 eligible tokens and saying nothing would read
    # afterwards as though everything had been covered.
    now = time.time()
    for i in range(5):
        capture(intel_store, f"T{i}", 250, now, market=f"M{i}")
    notes = []
    written = export_token_series(intel_store, tmp_path, min_rows=200,
                                  max_tokens=2, log=notes.append)
    assert len(written) == 2
    manifest = json.loads((tmp_path / MANIFEST).read_text(encoding="utf-8"))
    assert manifest["cappedOut"] == 3
    assert any("not researched" in note for note in notes)


def test_csv_has_the_timestamp_and_every_feature_column(intel_store, tmp_path):
    capture(intel_store, "T", 220, time.time())
    written = export_token_series(intel_store, tmp_path, min_rows=200)
    with open(written[0]["path"], newline="", encoding="utf-8") as handle:
        reader = csv.reader(handle)
        header = next(reader)
        first = next(reader)
    assert header == ["timestamp", *FEATURE_NAMES]
    assert len(first) == len(header)
    # The bridge keys on `timestamp` and prices P&L off `price`.
    assert "price" in header
    assert float(first[header.index("price")]) > 0


def test_missing_features_are_written_as_zero_not_blank(intel_store, tmp_path):
    # Blanks are forward-filled by the bridge, which would carry a stale value
    # across a gap and present it as a fresh observation.
    intel_store.record_research_rows([
        (time.time() + i, "T", "M1", "Yes", "Politics", {"price": 0.5})
        for i in range(210)
    ])
    written = export_token_series(intel_store, tmp_path, min_rows=200)
    with open(written[0]["path"], newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["flow_z"] == "0.0"
    assert all(value != "" for value in rows[0].values())


def test_export_with_nothing_captured_is_empty_not_an_error(intel_store,
                                                            tmp_path):
    assert export_token_series(intel_store, tmp_path, min_rows=200) == []


# --- cross-token aggregation ------------------------------------------------

class FakeStrategy:
    def __init__(self, feature, direction="long", threshold=1.0):
        self.direction = direction
        self.entry_feature = feature
        self.entry_op = ">"
        self.entry_threshold = threshold
        self.filter_feature = None
        self.filter_op = None

    def to_dict(self):
        return {"direction": self.direction, "entry_feature": self.entry_feature,
                "entry_op": self.entry_op,
                "entry_threshold": self.entry_threshold,
                "filter_feature": None, "filter_op": None,
                "filter_threshold": None}

    def describe(self):
        return f"{self.direction} {self.entry_feature}"


class FakeReport:
    def __init__(self, strategy, accepted=True, **metrics):
        self.strategy = strategy
        self.accepted = accepted
        self.full = {"rank_score": 0.5, "sharpe": 1.2, "oos_sharpe": 1.0,
                     "win_rate": 0.55, "trades": 40, **metrics}


def test_a_rule_must_survive_on_several_tokens():
    shared = "flow_z"
    per_token = [
        ("A", [FakeReport(FakeStrategy(shared)),
               FakeReport(FakeStrategy("only_here_a"))]),
        ("B", [FakeReport(FakeStrategy(shared))]),
    ]
    kept = aggregate(per_token, min_tokens=2)
    assert len(kept) == 1
    assert kept[0].rule["entry_feature"] == shared
    assert kept[0].accepted_on == 2
    assert set(kept[0].tokens) == {"A", "B"}


def test_ranked_candidates_survive_without_bridge_acceptance():
    """Research eligibility is not trade eligibility (the operator's spec).

    The bridge's acceptance gates are futures-grade — under honest Polymarket
    costs they accept zero per token, and requiring them here zeroed the whole
    pipeline. A ranked candidate flows on as a RESEARCH candidate; only the
    frozen OOS replay can make it tradable."""
    per_token = [
        ("A", [FakeReport(FakeStrategy("flow_z"), accepted=True)]),
        ("B", [FakeReport(FakeStrategy("flow_z"), accepted=False)]),
    ]
    kept = aggregate(per_token, min_tokens=2)
    assert len(kept) == 1
    assert kept[0].accepted_on == 2       # ranked on both tokens


def test_result_carries_the_funnel():
    """Every pass must be able to say which stage emptied — '14,000 trades
    and nothing on the board' has to be answerable from the status file."""
    from pqb.research import ResearchResult

    result = ResearchResult()
    result.funnel["rankedCandidates"] = 320
    result.funnel["zeroedAt"] = "tradable"
    data = result.to_dict()
    assert data["funnel"]["rankedCandidates"] == 320
    assert data["funnel"]["zeroedAt"] == "tradable"


def test_threshold_variants_from_one_token_are_one_vote():
    # Discovery sets thresholds from each token's own quantiles, so the same
    # idea lands on a different number every time. Several variants of one idea
    # on one token is still one token's worth of evidence.
    per_token = [
        ("A", [FakeReport(FakeStrategy("flow_z", threshold=1.0)),
               FakeReport(FakeStrategy("flow_z", threshold=2.0)),
               FakeReport(FakeStrategy("flow_z", threshold=3.0))]),
    ]
    assert aggregate(per_token, min_tokens=2) == []
    kept = aggregate(per_token, min_tokens=1)
    assert len(kept) == 1
    assert kept[0].accepted_on == 1


def test_signature_ignores_thresholds_but_not_direction():
    long_rule = FakeStrategy("flow_z", direction="long", threshold=1.0)
    same_idea = FakeStrategy("flow_z", direction="long", threshold=9.0)
    opposite = FakeStrategy("flow_z", direction="short", threshold=1.0)
    assert signature_of(long_rule.to_dict()) == signature_of(same_idea.to_dict())
    assert signature_of(long_rule.to_dict()) != signature_of(opposite.to_dict())


def test_ordering_prefers_broader_confirmation():
    per_token = [
        ("A", [FakeReport(FakeStrategy("wide")), FakeReport(FakeStrategy("narrow"))]),
        ("B", [FakeReport(FakeStrategy("wide")), FakeReport(FakeStrategy("narrow"))]),
        ("C", [FakeReport(FakeStrategy("wide"))]),
    ]
    kept = aggregate(per_token, min_tokens=2)
    assert [s.rule["entry_feature"] for s in kept] == ["wide", "narrow"]


# --- persistence ------------------------------------------------------------

def test_absent_strategy_file_is_an_empty_list(tmp_path):
    assert load_strategies(tmp_path / "nope.json") == []


def test_save_records_the_feature_contract(tmp_path):
    path = tmp_path / "strategies.json"
    save(path, [])
    payload = json.loads(path.read_text(encoding="utf-8"))
    # So a stale strategy file can be recognised as stale.
    assert payload["featureColumns"] == list(FEATURE_NAMES)


# --- the bridge override contract -------------------------------------------

def _prop_keys_the_bridge_reads() -> set:
    """Every `prop_constraints.*` key PropConstraints.from_config looks up.

    Read out of the source rather than hard-coded, so this test tracks the
    bridge instead of drifting alongside it.
    """
    import inspect
    import re
    from pqb.quant import load
    source = inspect.getsource(load().PropConstraints.from_config)
    return set(re.findall(r'c\.get\(\s*"([a-z_]+)"', source))


def test_override_keys_exist_in_the_bridge(tmp_path):
    """A misspelled override does not error — it silently keeps the default.

    That is how a 1.5% futures account-drawdown halt ended up applied to a
    prediction market and stopped every backtest before it took a position.
    """
    pytest.importorskip("pandas")
    from pqb.quant import available
    ok, why = available()
    if not ok:
        pytest.skip(f"Quant Bridge unavailable: {why.splitlines()[0]}")

    from pqb.config import Config
    from pqb.research import _bridge_overrides
    overrides = _bridge_overrides(tmp_path, tmp_path, Config())
    ours = {k.split(".", 1)[1] for k in overrides if k.startswith("prop_")}
    theirs = _prop_keys_the_bridge_reads()
    assert theirs, "could not read the bridge's constraint keys"
    assert ours <= theirs, f"overrides the bridge ignores: {sorted(ours - theirs)}"


def test_profit_caps_are_disabled_by_being_unreachable(tmp_path):
    from pqb.config import Config
    from pqb.research import _UNREACHABLE, _bridge_overrides
    overrides = _bridge_overrides(tmp_path, tmp_path, Config())
    # Zero would be satisfied on the first bar and block every entry: the
    # backtester caps the day when daily_pnl >= daily_profit_cap.
    assert overrides["prop_constraints.daily_profit_cap"] == _UNREACHABLE
    assert overrides["prop_constraints.daily_profit_target"] == _UNREACHABLE
    # These two genuinely do document 0 as "disabled".
    assert overrides["prop_constraints.max_hold_seconds"] == 0.0
    assert overrides["prop_constraints.eod_max_drawdown"] == 0.0
