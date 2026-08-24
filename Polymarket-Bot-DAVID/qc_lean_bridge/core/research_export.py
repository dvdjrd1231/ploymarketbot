"""Standardized research dataset export - the hand-off to the LEAN bridge.

The engines produce structure; this writes it out in the EXACT shape the bridge's
`DataPipeline` already consumes, so the two halves of the platform stay decoupled
and either can be replaced on its own:

    exports/
      bid_nonprint_structure.csv      timestamp, price, bid_*      (Bid engine)
      ask_nonprint_structure.csv      timestamp, ask_*             (Ask engine)
      multi_resolution_linebreak.csv  timestamp, lb_res{R}_*       (100 engines)
      structure_features.csv          timestamp, price, the 14 features
      FEATURE_DICTIONARY.md           written definition of every column
      MANIFEST.json                   rows + SHA-256 per file (integrity/audit)

Why the split: the bridge tags columns Bid/Ask by filename+token to build its
imbalance families, and prefixes each file's columns with the file stem, so a
column can never collide. Rows are streamed from the database in chunks - a
20-million-tick research run exports without ever holding the frame in RAM.
"""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .logging_setup import get_logger
from .structure_features import StructureFeatureBuilder, FEATURE_DOCS

log = get_logger("export")

REQUIRED_FEATURES = [
    "structural_velocity", "structural_acceleration", "void_size",
    "void_persistence", "reversal_frequency", "persistence_length",
    "compression", "expansion", "resolution_agreement", "resolution_divergence",
    "structural_volatility", "trend_persistence", "liquidity_imbalance",
    "directional_consensus",
]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S.%f")[:-3]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _CsvWriter:
    """Incremental CSV writer that fixes its columns from the first row."""

    def __init__(self, path: Path, columns: list[str]):
        self.path = path
        self.columns = columns
        self.rows = 0
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh)
        self._w.writerow(["timestamp"] + columns)

    def write(self, ts: float, row: dict) -> None:
        self._w.writerow([_iso(ts)] + [
            _fmt(row.get(c, "")) for c in self.columns])
        self.rows += 1

    def close(self) -> None:
        self._fh.close()


def _fmt(v) -> str:
    if v == "" or v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.6f}".rstrip("0").rstrip(".") or "0"
    return str(v)


class ResearchExporter:
    """Streams feature snapshots from the store into the bridge's export folder."""

    def __init__(self, cfg, store, logger=None):
        self.cfg = cfg
        self.store = store
        self.log = logger or (lambda m: None)

    def export(self, out_dir: Path | None = None, limit: int = 0) -> dict:
        out_dir = Path(out_dir) if out_dir else self.cfg.resolve_path("dataset.data_dir")
        out_dir.mkdir(parents=True, exist_ok=True)

        rows = self.store.feature_rows(limit=limit)
        if not rows:
            raise RuntimeError(
                "No feature snapshots in the database. Run the data engine first "
                "(Ingest / --ingest) so the engines can produce research rows.")

        first = rows[0]
        all_cols = [c for c in first.keys() if c not in ("ts",)]
        bid_cols = ["price"] + sorted(c for c in all_cols if c.startswith("bid_"))
        ask_cols = sorted(c for c in all_cols if c.startswith("ask_"))
        lb_cols = sorted(c for c in all_cols if c.startswith("lb_res")) + \
                  sorted(c for c in all_cols if c.startswith("res_"))
        feat_cols = ["price"] + [c for c in REQUIRED_FEATURES if c in all_cols] + \
                    [c for c in ("void_imbalance", "nonprint_imbalance",
                                 "quote_size_imbalance", "spread_ticks")
                     if c in all_cols]

        writers = {
            "bid_nonprint_structure.csv": _CsvWriter(out_dir / "bid_nonprint_structure.csv", bid_cols),
            "ask_nonprint_structure.csv": _CsvWriter(out_dir / "ask_nonprint_structure.csv", ask_cols),
            "multi_resolution_linebreak.csv": _CsvWriter(out_dir / "multi_resolution_linebreak.csv", lb_cols),
            "structure_features.csv": _CsvWriter(out_dir / "structure_features.csv", feat_cols),
        }

        for row in rows:
            ts = row["ts"]
            for w in writers.values():
                w.write(ts, row)

        manifest = {
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "rows": len(rows),
            "start": _iso(rows[0]["ts"]),
            "end": _iso(rows[-1]["ts"]),
            "instrument": self.cfg.get("instrument.symbol", "MES"),
            "tick_size": self.cfg.get("instrument.tick_size", 0.25),
            "resolutions": f"{self.cfg.get('structure.min_resolution', 1)}.."
                           f"{self.cfg.get('structure.max_resolution', 100)}",
            "files": {},
        }
        for name, w in writers.items():
            w.close()
            p = out_dir / name
            manifest["files"][name] = {
                "rows": w.rows,
                "columns": len(w.columns) + 1,
                "sha256": _sha256(p),
                "bytes": p.stat().st_size,
            }
            self.log(f"  wrote {name}: {w.rows:,} rows x {len(w.columns)+1} cols")

        StructureFeatureBuilder.write_docs(out_dir / "FEATURE_DICTIONARY.md")
        (out_dir / "MANIFEST.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8")

        missing = [f for f in REQUIRED_FEATURES if f not in all_cols]
        if missing:
            log.warning("required features absent from export",
                        extra={"missing": ",".join(missing)})

        log.info("research dataset exported",
                 extra={"dir": str(out_dir), "rows": len(rows),
                        "files": len(writers)})
        return {
            "output_dir": str(out_dir),
            "rows": len(rows),
            "files": list(writers.keys()),
            "features_documented": len(FEATURE_DOCS),
            "missing_required": missing,
            "manifest": str(out_dir / "MANIFEST.json"),
        }

    def export_ticks(self, path: Path | None = None, limit: int = 0) -> dict:
        """Dump raw Time & Sales - re-importable by CsvTickSource for replay."""
        path = Path(path) if path else (
            self.cfg.resolve_path("dataset.data_dir").parent / "ticks.csv")
        path.parent.mkdir(parents=True, exist_ok=True)
        n = 0
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["ts", "price", "size", "bid", "bid_size", "ask", "ask_size"])
            for t in self.store.replay_ticks():
                w.writerow([f"{t.ts:.6f}", t.price, t.size, t.bid, t.bid_size,
                            t.ask, t.ask_size])
                n += 1
                if limit and n >= limit:
                    break
        log.info("ticks exported", extra={"path": str(path), "ticks": n})
        return {"path": str(path), "ticks": n}
