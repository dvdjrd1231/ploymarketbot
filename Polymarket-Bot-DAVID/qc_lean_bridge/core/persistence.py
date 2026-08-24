"""Phase 1 - CSV Persistence & QC LEAN Bridge Compatibility.

Robust save/load for the engine's output datasets with:
  * a sidecar manifest (schema + row count + SHA-256 checksum) written next to
    each CSV, so tampering or truncation is detected on load;
  * schema-consistency validation against the expected shape of each dataset
    type (Engine Capture, Engine Backtest, Engine Multi-Resolution);
  * a serialize -> deserialize round-trip integrity check;
  * a Bridge-compatibility check that actually runs the importer end-to-end.

Dataset types are flexible on purpose (the engine's exact columns vary): each
type declares only the columns it MUST have; every other numeric column is
accepted and flows through to feature engineering unchanged.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd


# --------------------------------------------------------- dataset schemas
# Each type lists its REQUIRED columns; anything else numeric is allowed.
DATASET_SCHEMAS: dict[str, dict] = {
    "engine_capture": {
        # price is required at the FOLDER level (>=1 file), not per-file, since
        # a Bid file and an Ask file are captured separately.
        "required": ["timestamp"],
        "min_extra_numeric": 1,
        "description": "Raw captured Bid/Ask non-print structure stream.",
    },
    "engine_backtest": {
        "required": ["timestamp", "price"],
        "description": "Replayable historical dataset for backtesting.",
    },
    "engine_multi_resolution": {
        "required": ["timestamp"],
        "min_extra_numeric": 1,   # at least one resolution/metric column
        "description": "Multi-Resolution Line Break structures (1-100).",
    },
    "generic": {
        "required": ["timestamp"],
        "description": "Any structural dataset with a timestamp column.",
    },
}


@dataclass
class Manifest:
    dataset_type: str
    columns: list[str]
    dtypes: dict[str, str]
    row_count: int
    sha256: str
    timestamp_column: str = "timestamp"
    created_at: str = ""
    schema_version: int = 1

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)


@dataclass
class ValidationResult:
    ok: bool
    dataset_type: str
    path: str
    issues: list[str] = field(default_factory=list)
    rows: int = 0
    columns: int = 0

    def summary(self) -> str:
        status = "OK" if self.ok else "FAIL"
        head = f"[{status}] {Path(self.path).name} ({self.dataset_type}): " \
               f"{self.rows} rows, {self.columns} cols"
        if self.issues:
            head += "\n    - " + "\n    - ".join(self.issues)
        return head


# ----------------------------------------------------------------- helpers
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _manifest_path(csv_path: Path) -> Path:
    return csv_path.with_suffix(csv_path.suffix + ".manifest.json")


# ------------------------------------------------------------------ save
def save_dataset(df: pd.DataFrame, path: str | Path,
                 dataset_type: str = "generic",
                 timestamp_column: str = "timestamp") -> Manifest:
    """Write `df` to CSV plus a manifest sidecar. If the frame is indexed by a
    DatetimeIndex it is written as the timestamp column."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    out = df.copy()
    if isinstance(out.index, pd.DatetimeIndex):
        out.index.name = timestamp_column
        out.to_csv(path)
    else:
        out.to_csv(path, index=False)

    sha = _sha256_file(path)
    # columns as they appear in the written CSV (index included if datetime)
    written = pd.read_csv(path, nrows=0)
    manifest = Manifest(
        dataset_type=dataset_type,
        columns=list(written.columns),
        dtypes={c: str(t) for c, t in zip(out.columns, out.dtypes)},
        row_count=int(len(out)),
        sha256=sha,
        timestamp_column=timestamp_column,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    _manifest_path(path).write_text(manifest.to_json(), encoding="utf-8")
    return manifest


# ------------------------------------------------------------------ load
def load_dataset(path: str | Path, verify: bool = True) -> tuple[pd.DataFrame, list[str]]:
    """Load a CSV, verifying against its manifest if one exists.
    Returns (dataframe, issues)."""
    path = Path(path)
    issues: list[str] = []
    df = pd.read_csv(path)

    mpath = _manifest_path(path)
    if verify and mpath.exists():
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        # checksum integrity
        actual_sha = _sha256_file(path)
        if actual_sha != manifest.get("sha256"):
            issues.append("checksum mismatch - file changed since it was saved")
        # row count
        if int(manifest.get("row_count", -1)) != len(df):
            issues.append(f"row-count mismatch: manifest "
                          f"{manifest.get('row_count')} vs file {len(df)}")
        # schema consistency
        expected_cols = manifest.get("columns", [])
        if list(df.columns) != expected_cols:
            missing = set(expected_cols) - set(df.columns)
            extra = set(df.columns) - set(expected_cols)
            if missing:
                issues.append(f"missing columns vs manifest: {sorted(missing)}")
            if extra:
                issues.append(f"unexpected columns vs manifest: {sorted(extra)}")
    return df, issues


# -------------------------------------------------------- schema validation
def validate_schema(df: pd.DataFrame, dataset_type: str,
                    timestamp_column: str = "timestamp") -> list[str]:
    schema = DATASET_SCHEMAS.get(dataset_type, DATASET_SCHEMAS["generic"])
    issues: list[str] = []
    cols_lower = {c.lower() for c in df.columns}

    for req in schema.get("required", []):
        want = timestamp_column if req == "timestamp" else req
        if want.lower() not in cols_lower:
            issues.append(f"required column '{want}' is missing")

    min_extra = schema.get("min_extra_numeric", 0)
    if min_extra:
        numeric = df.select_dtypes(include=[np.number]).shape[1]
        if numeric < min_extra:
            issues.append(f"expected >= {min_extra} numeric metric column(s), "
                          f"found {numeric}")
    return issues


# ------------------------------------------------- round-trip integrity
def round_trip_check(df: pd.DataFrame, dataset_type: str = "generic",
                     tmp_dir: str | Path | None = None) -> tuple[bool, list[str]]:
    """Save then reload and confirm the data survives serialization intact."""
    import tempfile
    issues: list[str] = []
    tmp_dir = Path(tmp_dir) if tmp_dir else Path(tempfile.gettempdir())
    tmp = tmp_dir / f"_roundtrip_{dataset_type}.csv"
    try:
        save_dataset(df, tmp, dataset_type=dataset_type)
        reloaded, load_issues = load_dataset(tmp, verify=True)
        issues += load_issues

        original = df.reset_index() if isinstance(df.index, pd.DatetimeIndex) else df.copy()
        # align on shape
        if reloaded.shape[0] != original.shape[0]:
            issues.append(f"row count changed: {original.shape[0]} -> {reloaded.shape[0]}")
        # compare numeric columns tolerantly
        for col in original.columns:
            if col not in reloaded.columns:
                continue
            a = original[col]
            b = reloaded[col]
            if pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b):
                av = pd.to_numeric(a, errors="coerce").fillna(0).to_numpy(dtype=float)
                bv = pd.to_numeric(b, errors="coerce").fillna(0).to_numpy(dtype=float)
                if not np.allclose(av, bv, rtol=1e-6, atol=1e-6):
                    issues.append(f"numeric drift in column '{col}'")
            else:
                if not (a.astype(str).values == b.astype(str).values).all():
                    issues.append(f"value change in column '{col}'")
    except Exception as exc:  # noqa: BLE001
        issues.append(f"round-trip error: {exc}")
    finally:
        for p in (tmp, _manifest_path(tmp)):
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    return (len(issues) == 0), issues


# --------------------------------------------- per-file & folder validation
def validate_file(path: str | Path, dataset_type: str = "generic",
                  timestamp_column: str = "timestamp") -> ValidationResult:
    path = Path(path)
    df, load_issues = load_dataset(path, verify=True)
    schema_issues = validate_schema(df, dataset_type, timestamp_column)
    rt_ok, rt_issues = round_trip_check(df, dataset_type)
    issues = load_issues + schema_issues
    if not rt_ok:
        issues += [f"round-trip: {i}" for i in rt_issues]
    return ValidationResult(
        ok=(len(issues) == 0),
        dataset_type=dataset_type,
        path=str(path),
        issues=issues,
        rows=len(df),
        columns=df.shape[1],
    )


def infer_dataset_type(path: str | Path) -> str:
    name = Path(path).stem.lower()
    if "multi" in name or "resolution" in name or "linebreak" in name or "line_break" in name:
        return "engine_multi_resolution"
    if "backtest" in name or "replay" in name:
        return "engine_backtest"
    if "capture" in name or "nonprint" in name or "bid" in name or "ask" in name:
        return "engine_capture"
    return "generic"


# ------------------------------------------------- bridge compatibility
def bridge_compatibility_check(config, logger=None) -> dict:
    """End-to-end: validate every CSV in the data folder, then confirm the QC
    LEAN Bridge importer can actually load them. Returns a report dict."""
    from .data_pipeline import DataPipeline
    log = logger or (lambda m: None)

    data_dir = config.resolve_path("dataset.data_dir")
    ts_col = config.get("dataset.timestamp_column", "timestamp")
    files = sorted(data_dir.glob("*.csv")) if data_dir.exists() else []

    file_results = []
    for fp in files:
        dtype = infer_dataset_type(fp)
        res = validate_file(fp, dtype, ts_col)
        file_results.append(res)
        log("  " + res.summary())

    # Bridge import
    import_ok, import_issues, rows, metrics = True, [], 0, 0
    try:
        pipe = DataPipeline(config, logger=log)
        import_issues = pipe.validate()
        ds = pipe.load()
        rows, metrics = len(ds.frame), len(ds.metrics)
    except Exception as exc:  # noqa: BLE001
        import_ok = False
        import_issues = [f"FATAL import error: {exc}"]

    all_files_ok = all(r.ok for r in file_results) if file_results else False
    return {
        "data_dir": str(data_dir),
        "files_checked": len(file_results),
        "files_ok": sum(1 for r in file_results if r.ok),
        "all_files_ok": all_files_ok,
        "bridge_import_ok": import_ok and not any(
            i.startswith("FATAL") for i in import_issues),
        "imported_rows": rows,
        "imported_metrics": metrics,
        "import_notes": import_issues,
        "file_results": file_results,
        "compatible": all_files_ok and import_ok and rows > 0,
    }
