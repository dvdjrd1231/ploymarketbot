"""Research engine orchestrator.

Holds the shared state across all six phases so the GUI (or a script) can run
them one at a time or end-to-end. Every method returns a short summary dict and
streams progress through the injected `logger` / `progress` callbacks.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import Config, PROJECT_ROOT
from .data_pipeline import DataPipeline, Dataset
from .features import FeatureEngineer, FeatureSet
from .strategy_discovery import StrategyDiscovery
from .validators import ValidationSuite, ValidationReport
from .ranking import StrategyRanker
from .risk_management import PropConstraints, compliance_report
from .lean_export import export_strategy
from .backtester import BacktestResult


class ResearchEngine:
    def __init__(self, config: Config, logger=None):
        self.cfg = config
        self.log = logger or (lambda m: None)
        self.constraints = PropConstraints.from_config(config)

        self.dataset: Dataset | None = None
        self.feature_set: FeatureSet | None = None
        self.discovered: list[BacktestResult] = []
        self.reports: list[ValidationReport] = []
        self.ranked: list[ValidationReport] = []

    # ------------------------------------------------------------- Phase 1
    def run_data(self) -> dict:
        self.log("[Phase 1] Loading structural datasets...")
        pipe = DataPipeline(self.cfg, logger=self.log)
        issues = pipe.validate()
        self.dataset = pipe.load()
        for msg in issues:
            self.log(f"  note: {msg}")
        summary = self.dataset.summary()
        self.log(f"[Phase 1] OK - {summary['rows']} rows, "
                 f"{summary['metric_columns']} structural metrics.")
        return summary

    # ------------------------------------- Phase 1 compatibility validation
    def validate_compatibility(self) -> dict:
        from .persistence import bridge_compatibility_check
        self.log("[Validate] Checking CSV persistence + Bridge compatibility...")
        report = bridge_compatibility_check(self.cfg, logger=self.log)
        self.log(f"[Validate] files OK {report['files_ok']}/{report['files_checked']}, "
                 f"import {'OK' if report['bridge_import_ok'] else 'FAIL'}, "
                 f"compatible: {report['compatible']}")
        return report

    # ------------------------------------------------------------- Phase 2
    def run_features(self) -> dict:
        if self.dataset is None:
            self.run_data()
        self.log("[Phase 2] Engineering features...")
        eng = FeatureEngineer(self.dataset, logger=self.log)
        self.feature_set = eng.build()
        by_family = {k: len(v) for k, v in self.feature_set.families.items()}
        self.log(f"[Phase 2] OK - {len(self.feature_set.names)} features "
                 f"across {len(by_family)} families.")
        return {"total_features": len(self.feature_set.names), "families": by_family}

    # ------------------------------------------------------------- Phase 3
    def run_discovery(self, progress=None) -> dict:
        if self.feature_set is None:
            self.run_features()
        self.log("[Phase 3] Discovering strategies...")
        disc = StrategyDiscovery(self.feature_set, self.cfg, self.constraints,
                                 logger=self.log, progress=progress)
        self.discovered = disc.run()
        self.log(f"[Phase 3] OK - {len(self.discovered)} viable candidates.")
        top = self.discovered[:1]
        return {
            "candidates": len(self.discovered),
            "best_sharpe": round(top[0].metrics["sharpe"], 3) if top else 0.0,
        }

    # ------------------------------------------------------------- Phase 4
    def run_validation(self, top_n: int = 50, progress=None) -> dict:
        if not self.discovered:
            self.run_discovery()
        self.log(f"[Phase 4] Validating top {top_n} candidates...")
        suite = ValidationSuite(self.feature_set, self.cfg, self.constraints,
                                logger=self.log)
        strategies = [r.strategy for r in self.discovered[:top_n]]
        self.reports = suite.validate_many(strategies, progress=progress)
        accepted = [r for r in self.reports if r.accepted]
        self.log(f"[Phase 4] OK - {len(accepted)}/{len(self.reports)} passed "
                 f"all robustness gates.")
        return {"validated": len(self.reports), "accepted": len(accepted)}

    # ------------------------------------------------------------- Phase 5
    def run_ranking(self) -> dict:
        if not self.reports:
            self.run_validation()
        self.log("[Phase 5] Ranking strategies...")
        ranker = StrategyRanker(self.cfg)
        self.ranked = ranker.rank(self.reports)
        accepted = [r for r in self.ranked if r.accepted]
        self.log(f"[Phase 5] OK - ranked {len(self.ranked)} strategies "
                 f"({len(accepted)} accepted).")
        return {"ranked": len(self.ranked), "accepted": len(accepted)}

    # ------------------------------------------------------------- Phase 6
    def run_export(self, only_accepted: bool = True, top_n: int = 10) -> dict:
        if not self.ranked:
            self.run_ranking()
        out_root = self.cfg.resolve_path("output.strategies_dir", "strategies")
        out_root.mkdir(parents=True, exist_ok=True)
        pool = [r for r in self.ranked if (r.accepted or not only_accepted)][:top_n]
        self.log(f"[Phase 6] Exporting {len(pool)} strategies + LEAN code to "
                 f"{out_root}...")
        paths = []
        for rep in pool:
            p = export_strategy(rep, self.feature_set, self.cfg, self.constraints,
                                out_root, descriptions=self.feature_set.descriptions)
            paths.append(str(p))
            self.log(f"  wrote {p.name}/  (main.py, feature_data.csv, report)")
        self._write_summary(out_root, pool)
        self.log(f"[Phase 6] OK - {len(paths)} strategy packages written.")
        return {"exported": len(paths), "output_dir": str(out_root)}

    # ---------------------------------------------------------------- full
    def run_full(self, top_n_validate=50, top_n_export=10, progress=None) -> dict:
        self.run_data()
        self.run_features()
        self.run_discovery(progress=progress)
        self.run_validation(top_n=top_n_validate, progress=progress)
        self.run_ranking()
        return self.run_export(top_n=top_n_export)

    # --------------------------------------------------------------- tables
    def ranking_table(self) -> pd.DataFrame:
        if not self.ranked:
            return pd.DataFrame()
        rows = []
        for r in self.ranked:
            row = r.to_row()
            row["rank"] = r.full.get("rank", 0)
            row["score"] = round(r.full.get("rank_score", 0.0), 4)
            row["rule"] = r.strategy.describe()
            rows.append(row)
        df = pd.DataFrame(rows)
        cols = ["rank", "id", "accepted", "score", "sharpe", "sortino",
                "profit_factor", "win_rate", "max_dd_pct", "net_profit", "trades",
                "oos_sharpe", "wf_profitable", "mc_profitable", "param_stability", "rule"]
        return df[[c for c in cols if c in df.columns]]

    def _write_summary(self, out_root: Path, pool: list[ValidationReport]):
        df = self.ranking_table()
        reports_dir = self.cfg.resolve_path("output.reports_dir", "reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        if not df.empty:
            df.to_csv(reports_dir / "ranking_summary.csv", index=False)
        # compliance overview for exported pool
        lines = ["# Exported Strategy Summary\n"]
        for rep in pool:
            lines.append(f"- **{rep.strategy.id}** (rank "
                         f"{rep.full.get('rank','-')}, score "
                         f"{rep.full.get('rank_score',0):.3f}) - "
                         f"{'ACCEPTED' if rep.accepted else 'candidate'} - "
                         f"{rep.strategy.describe()}")
        (reports_dir / "EXPORT_SUMMARY.md").write_text("\n".join(lines),
                                                       encoding="utf-8")
