"""The dashboard's Discovery view (where discovered strategies show up).

David's question: once the Quant Bridge discovers strategies, where does he SEE
them? Answer: the reader.discovery() feed, rendered in the Discovery tab. This
locks in that it reads both the strategies and the run status the bot writes.
"""

from __future__ import annotations

import json

from pqb.config import Config
from pqb.gui.reader import Reader


def _reader(tmp_path) -> Reader:
    cfg = Config()
    cfg.root = tmp_path
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    return Reader(cfg)


def test_discovery_reads_status_and_strategies(tmp_path):
    r = _reader(tmp_path)
    data_dir = r.cfg.data_dir
    (data_dir / "strategies.json").write_text(json.dumps({"strategies": [
        {"describe": "buy when cohort flow spikes", "score": 0.71,
         "sharpe": 1.4, "oosSharpe": 0.9, "winRate": 0.62, "acceptedOn": 3},
    ]}), encoding="utf-8")
    (data_dir / "discovery.json").write_text(json.dumps({
        "running": False, "lastRun": 123.0, "accepted": 1, "candidates": 40,
    }), encoding="utf-8")

    d = r.discovery()
    assert len(d["strategies"]) == 1
    s = d["strategies"][0]
    assert s["oosSharpe"] == 0.9          # the overfitting column is carried
    assert s["acceptedOn"] == 3
    assert d["status"]["accepted"] == 1
    assert d["status"]["candidates"] == 40


def test_discovery_is_empty_but_safe_before_any_run(tmp_path):
    d = _reader(tmp_path).discovery()
    assert d["strategies"] == []
    assert d["status"] == {}
    assert d["tokensReady"] == 0


def test_malformed_files_do_not_raise(tmp_path):
    r = _reader(tmp_path)
    (r.cfg.data_dir / "strategies.json").write_text("{not json", encoding="utf-8")
    (r.cfg.data_dir / "discovery.json").write_text("garbage", encoding="utf-8")
    d = r.discovery()                     # must degrade, not crash
    assert d["strategies"] == []
    assert d["status"] == {}
