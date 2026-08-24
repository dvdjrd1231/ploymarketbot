"""Calibration now records category / time-to-resolution / consensus (§8) and
migrates an existing predictions table to add them."""

from __future__ import annotations

from dataclasses import dataclass

from pqb.decision.calibration import Calibration
from pqb.journal import Journal


@dataclass
class _Estimate:
    probability: float
    market_price: float
    confidence: float = 0.9

    def to_dict(self):
        return {"evidence": []}


@dataclass
class _Opp:
    token_id: str
    market_id: str
    outcome: str
    estimate: _Estimate
    ev_per_dollar: float = 0.05
    acceptable: bool = True


def _journal(tmp_path):
    return Journal(tmp_path / "j.sqlite3", mode="dry_run")


def test_record_stores_the_new_dimensions(tmp_path):
    cal = Calibration(_journal(tmp_path))
    opp = _Opp("tokA", "mktA", "Yes", _Estimate(0.7, 0.6))
    cal.record(opp, acted=True, stake=10.0, category="sports", ttr="1-3d",
               consensus=3)
    rows = cal.journal.query("SELECT category, ttr, consensus FROM predictions")
    assert rows[0]["category"] == "sports"
    assert rows[0]["ttr"] == "1-3d"
    assert rows[0]["consensus"] == 3


def test_migration_adds_columns_to_an_old_table(tmp_path):
    # Simulate a pre-existing predictions table without the new columns.
    j = _journal(tmp_path)
    j.execute("DROP TABLE IF EXISTS predictions")
    j.execute("CREATE TABLE predictions (id INTEGER PRIMARY KEY, ts REAL, "
              "token_id TEXT, market_id TEXT, outcome TEXT, probability REAL, "
              "market_price REAL, ev REAL, confidence REAL, stake REAL, "
              "acted INTEGER, settled REAL, settled_ts REAL, evidence TEXT)")
    # Constructing Calibration must migrate the table in place.
    cal = Calibration(j)
    have = {r["name"] for r in j.query("PRAGMA table_info(predictions)")}
    assert {"category", "ttr", "consensus"} <= have
    # And recording with the new fields then works.
    cal.record(_Opp("t", "m", "Yes", _Estimate(0.7, 0.6)), acted=True,
               category="crypto", ttr="<1d", consensus=1)
    assert cal.journal.query(
        "SELECT category FROM predictions")[-1]["category"] == "crypto"


def test_settling_and_reporting_still_work(tmp_path):
    cal = Calibration(_journal(tmp_path))
    cal.record(_Opp("tokA", "mktA", "Yes", _Estimate(0.8, 0.6)), acted=True,
               category="sports", ttr="1-3d", consensus=2)
    cal.settle({"tokA": 1.0})
    report = cal.report()
    assert report["n"] == 1
