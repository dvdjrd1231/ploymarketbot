"""Main application window - drives the whole 6-phase research workflow."""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd

from .qt import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QFormLayout,
    QPushButton, QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox,
    QTextEdit, QPlainTextEdit, QTabWidget, QTableWidget, QTableWidgetItem,
    QGroupBox, QProgressBar, QFileDialog, QMessageBox, QSplitter, QHeaderView,
    QFrame, QCheckBox, QSizePolicy, QApplication,
    Qt, QFont, ALIGN_CENTER, ALIGN_LEFT, ORIENT_H, ORIENT_V, RESIZE_STRETCH,
    RESIZE_CONTENTS, RESIZE_INTERACTIVE, SELECT_ROWS, SELECT_SINGLE, EDIT_NONE,
    FRAME_HLINE, FRAME_SUNKEN, BINDING,
)
from .workers import Worker
from core.config import Config, PROJECT_ROOT
from core.pipeline import ResearchEngine
from core.risk_management import PropConstraints, compliance_report


PHASES = ["Data", "Features", "Discovery", "Validation", "Ranking", "Export"]


class MainWindow(QMainWindow):
    def __init__(self, config_path: str | None = None):
        super().__init__()
        self.setWindowTitle("QC LEAN Bridge - Structural Strategy Research")
        self.resize(1280, 820)

        self.config_path = config_path
        self.cfg = Config.load(config_path)
        self.engine = ResearchEngine(self.cfg)
        self.worker: Worker | None = None
        self._phase_status = {p: "pending" for p in PHASES}

        self._build_ui()
        self._load_config_into_form()
        self.log(f"Ready. Qt binding: {BINDING}. "
                 f"Config: {self.cfg.source_path}")
        self.log("Tip: click 'Run Full Pipeline' to execute all six phases, "
                 "or run them one at a time.")

    # ==================================================================== UI
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        splitter = QSplitter(ORIENT_H)
        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_tabs())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([340, 940])
        root.addWidget(splitter)

        self._apply_style()

    # -------------------------------------------------------- left control
    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMaximumWidth(360)
        lay = QVBoxLayout(panel)
        lay.setSpacing(10)

        title = QLabel("QC LEAN Bridge")
        title.setObjectName("appTitle")
        sub = QLabel("Line-Break Structural Strategy Research")
        sub.setObjectName("appSub")
        lay.addWidget(title)
        lay.addWidget(sub)
        lay.addWidget(self._hline())

        # Data source
        ds_box = QGroupBox("Data Source")
        ds_lay = QVBoxLayout(ds_box)
        self.data_dir_edit = QLineEdit()
        browse = QPushButton("Browse export folder...")
        browse.clicked.connect(self._browse_data_dir)
        validate_btn = QPushButton("Validate Data && Compatibility")
        validate_btn.clicked.connect(self.on_validate_data)
        ds_lay.addWidget(QLabel("Engine CSV export folder:"))
        ds_lay.addWidget(self.data_dir_edit)
        ds_lay.addWidget(browse)
        ds_lay.addWidget(validate_btn)
        lay.addWidget(ds_box)

        # Data engine (upstream: IBKR T&S -> structure engines -> DB -> CSV)
        eng_box = QGroupBox("Data Engine (Non-Print + 100 Line Break)")
        eng_lay = QVBoxLayout(eng_box)
        self.ingest_ticks = QSpinBox()
        self.ingest_ticks.setRange(0, 100_000_000)
        self.ingest_ticks.setSingleStep(50_000)
        self.ingest_ticks.setValue(int(self.cfg.get("openclaw.ingest_max_ticks", 200000)))
        self.ingest_ticks.setToolTip("0 = stream until stopped (live)")
        eng_lay.addWidget(QLabel("Ticks to ingest (0 = unlimited):"))
        eng_lay.addWidget(self.ingest_ticks)
        self.ingest_btn = QPushButton("Run Data Engine + Export")
        self.ingest_btn.clicked.connect(self.on_ingest)
        eng_lay.addWidget(self.ingest_btn)
        lay.addWidget(eng_box)

        # Pipeline buttons
        run_box = QGroupBox("Research Pipeline (LEAN Bridge)")
        run_lay = QVBoxLayout(run_box)
        self.phase_buttons = {}
        specs = [
            ("Data", "1. Load Structural Data", self.on_data),
            ("Features", "2. Engineer Features", self.on_features),
            ("Discovery", "3. Discover Strategies", self.on_discovery),
            ("Validation", "4. Validate (robustness)", self.on_validation),
            ("Ranking", "5. Rank Strategies", self.on_ranking),
            ("Export", "6. Export LEAN Code", self.on_export),
        ]
        for key, label, handler in specs:
            btn = QPushButton(label)
            btn.clicked.connect(handler)
            run_lay.addWidget(btn)
            self.phase_buttons[key] = btn
        run_lay.addWidget(self._hline())
        self.run_all_btn = QPushButton("Run Full Pipeline")
        self.run_all_btn.setObjectName("runAll")
        self.run_all_btn.clicked.connect(self.on_full)
        run_lay.addWidget(self.run_all_btn)
        lay.addWidget(run_box)

        # OpenClaw master controller (full autonomous cycle)
        oc_box = QGroupBox("OpenClaw AI (Master Controller)")
        oc_lay = QVBoxLayout(oc_box)
        self.cycle_btn = QPushButton("Run Full Research Cycle")
        self.cycle_btn.setObjectName("runAll")
        self.cycle_btn.setToolTip("Ingest -> Export -> Research -> Validate -> "
                                  "Compare -> Deploy, end to end")
        self.cycle_btn.clicked.connect(self.on_cycle)
        oc_lay.addWidget(self.cycle_btn)
        lay.addWidget(oc_box)

        # Status list
        self.status_labels = {}
        st_box = QGroupBox("Status")
        st_lay = QFormLayout(st_box)
        for p in PHASES:
            lbl = QLabel("pending")
            lbl.setObjectName("statusPending")
            self.status_labels[p] = lbl
            st_lay.addRow(f"{p}:", lbl)
        lay.addWidget(st_box)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        lay.addWidget(self.progress)

        lay.addStretch(1)
        return panel

    # -------------------------------------------------------- right tabs
    def _build_right_tabs(self) -> QWidget:
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_results_tab(), "Results")
        self.tabs.addTab(self._build_config_tab(), "Configuration")
        self.tabs.addTab(self._build_log_tab(), "Log")
        return self.tabs

    def _build_results_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)

        header = QHBoxLayout()
        self.results_summary = QLabel("No results yet. Run the pipeline.")
        self.results_summary.setObjectName("resultsSummary")
        header.addWidget(self.results_summary)
        header.addStretch(1)
        self.export_sel_btn = QPushButton("Export selected strategy")
        self.export_sel_btn.clicked.connect(self._export_selected)
        self.export_sel_btn.setEnabled(False)
        header.addWidget(self.export_sel_btn)
        lay.addLayout(header)

        split = QSplitter(ORIENT_V)

        self.results_table = QTableWidget()
        self.results_table.setEditTriggers(EDIT_NONE)
        self.results_table.setSelectionBehavior(SELECT_ROWS)
        self.results_table.setSelectionMode(SELECT_SINGLE)
        self.results_table.itemSelectionChanged.connect(self._on_select_strategy)
        self.results_table.setAlternatingRowColors(True)
        split.addWidget(self.results_table)

        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setObjectName("detail")
        split.addWidget(self.detail)
        split.setSizes([480, 260])

        lay.addWidget(split)
        return w

    def _build_config_tab(self) -> QWidget:
        outer = QWidget()
        lay = QVBoxLayout(outer)

        grid = QGridLayout()

        # Prop constraints
        prop_box = QGroupBox("Prop Firm Constraints (enforced in backtest + LEAN)")
        pf = QFormLayout(prop_box)
        self.f_min_hold = self._dspin(0, 3600, 0.5)
        self.f_daily_cap = self._dspin(0, 1e6, 50)
        self.f_per_trade_dd = self._dspin(0, 100, 0.5)
        self.f_acct_dd = self._dspin(0, 100, 0.1)
        self.f_eod_dd = self._dspin(0, 1e6, 50)
        self.f_max_contracts = self._ispin(1, 100)
        self.f_min_win = self._dspin(0, 1, 0.01)
        pf.addRow("Min hold seconds:", self.f_min_hold)
        pf.addRow("Daily profit cap ($):", self.f_daily_cap)
        pf.addRow("Per-trade max DD (%):", self.f_per_trade_dd)
        pf.addRow("Account max DD (%):", self.f_acct_dd)
        pf.addRow("EOD trailing DD ($):", self.f_eod_dd)
        pf.addRow("Max contracts:", self.f_max_contracts)
        pf.addRow("Min win rate (consistency):", self.f_min_win)
        grid.addWidget(prop_box, 0, 0)

        # Instrument / account
        instr_box = QGroupBox("Instrument & Account")
        inf = QFormLayout(instr_box)
        self.f_symbol = QLineEdit()
        self.f_point_value = self._dspin(0.01, 1000, 0.5)
        self.f_commission = self._dspin(0, 100, 0.01)
        self.f_start_equity = self._dspin(0, 1e8, 1000)
        inf.addRow("Symbol:", self.f_symbol)
        inf.addRow("Point value ($):", self.f_point_value)
        inf.addRow("Commission/contract ($):", self.f_commission)
        inf.addRow("Starting equity ($):", self.f_start_equity)
        grid.addWidget(instr_box, 0, 1)

        # Discovery
        disc_box = QGroupBox("Strategy Discovery")
        df = QFormLayout(disc_box)
        self.f_method = QComboBox()
        self.f_method.addItems(["random", "grid", "genetic"])
        self.f_max_cand = self._ispin(10, 100000)
        self.f_max_feat = self._ispin(2, 500)
        self.f_seed = self._ispin(0, 1000000)
        df.addRow("Search method:", self.f_method)
        df.addRow("Max candidates:", self.f_max_cand)
        df.addRow("Max features scanned:", self.f_max_feat)
        df.addRow("Random seed:", self.f_seed)
        grid.addWidget(disc_box, 1, 0)

        # Validation
        val_box = QGroupBox("Validation Gates")
        vf = QFormLayout(val_box)
        self.f_is_frac = self._dspin(0.1, 0.95, 0.05)
        self.f_wf_windows = self._ispin(2, 20)
        self.f_mc_runs = self._ispin(50, 20000)
        self.f_min_trades = self._ispin(1, 10000)
        self.f_min_sharpe = self._dspin(-5, 20, 0.1)
        self.f_max_dd = self._dspin(0, 100, 1)
        self.f_val_top_n = self._ispin(1, 1000)
        vf.addRow("In-sample fraction:", self.f_is_frac)
        vf.addRow("Walk-forward windows:", self.f_wf_windows)
        vf.addRow("Monte Carlo runs:", self.f_mc_runs)
        vf.addRow("Min trades:", self.f_min_trades)
        vf.addRow("Min Sharpe:", self.f_min_sharpe)
        vf.addRow("Max drawdown (%):", self.f_max_dd)
        vf.addRow("Validate top-N candidates:", self.f_val_top_n)
        grid.addWidget(val_box, 1, 1)

        lay.addLayout(grid)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save configuration")
        save_btn.clicked.connect(self._save_config_from_form)
        reload_btn = QPushButton("Reload from file")
        reload_btn.clicked.connect(self._reload_config)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(reload_btn)
        btn_row.addStretch(1)
        note = QLabel("Saving rebuilds the research engine (clears results).")
        note.setObjectName("appSub")
        btn_row.addWidget(note)
        lay.addLayout(btn_row)
        lay.addStretch(1)
        return outer

    def _build_log_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setObjectName("logView")
        mono = QFont("Consolas" if os.name == "nt" else "Monospace")
        mono.setStyleHint(QFont.StyleHint.TypeWriter if BINDING == "PyQt6"
                          else QFont.TypeWriter)
        self.log_view.setFont(mono)
        lay.addWidget(self.log_view)
        clear = QPushButton("Clear log")
        clear.clicked.connect(lambda: self.log_view.clear())
        lay.addWidget(clear, alignment=ALIGN_LEFT)
        return w

    # ------------------------------------------------------------- helpers
    def _hline(self) -> QFrame:
        line = QFrame()
        line.setFrameShape(FRAME_HLINE)
        line.setFrameShadow(FRAME_SUNKEN)
        return line

    def _dspin(self, lo, hi, step) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(4 if step < 0.1 else 2)
        return s

    def _ispin(self, lo, hi) -> QSpinBox:
        s = QSpinBox()
        s.setRange(lo, hi)
        return s

    # ==================================================================== log
    def log(self, msg: str):
        self.log_view.appendPlainText(str(msg))
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _set_status(self, phase: str, state: str):
        self._phase_status[phase] = state
        lbl = self.status_labels[phase]
        lbl.setText(state)
        lbl.setObjectName({
            "pending": "statusPending",
            "running": "statusRunning",
            "done": "statusDone",
            "error": "statusError",
        }.get(state, "statusPending"))
        lbl.setStyleSheet("")   # force re-eval of object-name style
        self._reapply_status_style(lbl, state)

    def _reapply_status_style(self, lbl, state):
        colors = {"pending": "#8a8f98", "running": "#e0a800",
                  "done": "#2e9e4f", "error": "#d64545"}
        lbl.setStyleSheet(f"color:{colors.get(state,'#8a8f98')}; font-weight:600;")

    # ==================================================================== run
    def _busy(self, busy: bool):
        for b in self.phase_buttons.values():
            b.setEnabled(not busy)
        self.run_all_btn.setEnabled(not busy)
        for b in (getattr(self, "ingest_btn", None), getattr(self, "cycle_btn", None)):
            if b is not None:
                b.setEnabled(not busy)
        if busy:
            self.progress.setRange(0, 0)   # indeterminate until progress arrives
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)

    def _start(self, name: str, fn):
        if self.worker and self.worker.isRunning():
            QMessageBox.warning(self, "Busy", "A phase is already running.")
            return
        self._busy(True)
        if name in PHASES:
            self._set_status(name, "running")
        self.worker = Worker(name, fn)
        self.worker.log.connect(self.log)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(self._on_done)
        self.worker.failed.connect(self._on_failed)
        self.worker.start()

    def _on_progress(self, done: int, total: int):
        if total <= 0:
            return
        self.progress.setRange(0, total)
        self.progress.setValue(done)

    def _on_failed(self, name: str, err: str):
        self._busy(False)
        if name in PHASES:
            self._set_status(name, "error")
        self.log(f"[ERROR in {name}] {err}")
        QMessageBox.critical(self, f"{name} failed", err.split(chr(10))[0])

    def _on_done(self, name: str, result):
        self._busy(False)
        if name in PHASES:
            self._set_status(name, "done")
        if name == "Full":
            for p in PHASES:
                self._set_status(p, "done")
        self._refresh_after(name, result)

    # ------------------------------------------------- phase closures
    def _phase(self, method_name, use_progress=False, **kwargs):
        def fn(log_cb, progress_cb):
            self.engine.log = log_cb
            method = getattr(self.engine, method_name)
            if use_progress:
                return method(progress=progress_cb, **kwargs)
            return method(**kwargs)
        return fn

    def on_validate_data(self):
        def fn(log_cb, progress_cb):
            self.engine.log = log_cb
            return self.engine.validate_compatibility()
        self._start("Validate", fn)

    def on_ingest(self):
        max_ticks = self.ingest_ticks.value()

        def fn(log_cb, progress_cb):
            from core.ingest import run_ingest
            return run_ingest(self.cfg, max_ticks=max_ticks, logger=log_cb,
                              do_export=True, progress=progress_cb)
        self._start("Ingest", fn)

    def on_cycle(self):
        def fn(log_cb, progress_cb):
            from core.openclaw import OpenClaw
            oc = OpenClaw(self.cfg, logger=log_cb)
            res = oc.run_cycle(trigger="gui", progress=progress_cb)
            return res.as_dict()
        self._start("Cycle", fn)

    def on_data(self):
        self._start("Data", self._phase("run_data"))

    def on_features(self):
        self._start("Features", self._phase("run_features"))

    def on_discovery(self):
        self._start("Discovery", self._phase("run_discovery", use_progress=True))

    def on_validation(self):
        top_n = self.f_val_top_n.value()
        self._start("Validation",
                    self._phase("run_validation", use_progress=True, top_n=top_n))

    def on_ranking(self):
        self._start("Ranking", self._phase("run_ranking"))

    def on_export(self):
        self._start("Export",
                    self._phase("run_export", only_accepted=False, top_n=10))

    def on_full(self):
        top_n = self.f_val_top_n.value()

        def fn(log_cb, progress_cb):
            self.engine.log = log_cb
            return self.engine.run_full(top_n_validate=top_n, top_n_export=10,
                                        progress=progress_cb)
        for p in PHASES:
            self._set_status(p, "running")
        self._start("Full", fn)

    # ------------------------------------------------- post-run refresh
    def _refresh_after(self, name: str, result):
        if name == "Validate" and isinstance(result, dict):
            compatible = result.get("compatible")
            msg = (f"Files OK: {result.get('files_ok')}/{result.get('files_checked')}\n"
                   f"Bridge import: {'OK' if result.get('bridge_import_ok') else 'FAILED'}\n"
                   f"Imported: {result.get('imported_rows', 0):,} rows, "
                   f"{result.get('imported_metrics', 0)} structural metrics\n\n"
                   f"Compatible with the Bridge: {'YES' if compatible else 'NO'}")
            if compatible:
                QMessageBox.information(self, "Data Validation", msg)
            else:
                QMessageBox.warning(self, "Data Validation", msg)
            return
        if name == "Ingest" and isinstance(result, dict):
            exp = result.get("export", {})
            self.log(f"[Ingest] {result.get('ticks', 0):,} ticks -> "
                     f"{result.get('structural_events', 0):,} structural events, "
                     f"{result.get('line_break_lines', 0):,} lines, "
                     f"{result.get('feature_rows', 0):,} research rows "
                     f"({result.get('ticks_per_sec', 0):,}/s, "
                     f"{result.get('rss_mb', 0)} MB)")
            if exp:
                self.log(f"[Ingest] dataset -> {exp.get('output_dir', '-')} "
                         f"({exp.get('rows', 0):,} rows). Now run the pipeline.")
                QMessageBox.information(
                    self, "Data Engine",
                    f"Ingested {result.get('ticks', 0):,} ticks and exported "
                    f"{exp.get('rows', 0):,} research rows to:\n"
                    f"{exp.get('output_dir', '-')}\n\nRun the research pipeline next.")
            return
        if name == "Cycle" and isinstance(result, dict):
            self._rebuild_engine_after_cycle()
            ok = result.get("ok")
            dep = ", ".join(result.get("deployed", [])) or "none"
            QMessageBox.information(
                self, "OpenClaw Cycle",
                f"Cycle {result.get('cycle')} "
                f"{'completed' if ok else 'failed: ' + result.get('error', '')}.\n"
                f"Deployed: {dep}\nDuration: {result.get('seconds', 0):.1f}s")
            self._populate_results()
            return
        if isinstance(result, dict):
            self.log(f"[{name}] result: {result}")
        if name in ("Ranking", "Full", "Export", "Validation"):
            self._populate_results()
        if name == "Export" or name == "Full":
            out = result.get("output_dir") if isinstance(result, dict) else None
            if out:
                self.log(f"Strategy packages + LEAN code written to: {out}")

    def _rebuild_engine_after_cycle(self):
        """After an OpenClaw cycle, surface its ranking summary in the table."""
        summary = self.cfg.resolve_path("output.reports_dir", "reports") / "ranking_summary.csv"
        if not summary.exists():
            return
        try:
            df = pd.read_csv(summary)
        except Exception as exc:  # noqa: BLE001
            self.log(f"Could not load cycle results: {exc}")
            return
        accepted = int(df["accepted"].sum()) if "accepted" in df else 0
        self.results_summary.setText(
            f"OpenClaw cycle: {len(df)} strategies ranked - {accepted} accepted. "
            f"Full reports in reports/openclaw/.")
        self._fill_table(self.results_table, df)
        self._cycle_df = df

    def _populate_results(self):
        if getattr(self, "engine", None) is None:
            return
        df = self.engine.ranking_table()
        if df.empty:
            if hasattr(self, "_cycle_df"):
                return          # keep the cycle results already shown
            return
        accepted = int(df["accepted"].sum()) if "accepted" in df else 0
        self.results_summary.setText(
            f"{len(df)} strategies ranked - {accepted} accepted "
            f"(passed all robustness gates). Click a row for details.")
        self._fill_table(self.results_table, df)

    def _fill_table(self, table: QTableWidget, df: pd.DataFrame):
        display_cols = [c for c in df.columns if c != "rule"]
        table.setColumnCount(len(display_cols))
        table.setRowCount(len(df))
        table.setHorizontalHeaderLabels(display_cols)
        for r in range(len(df)):
            for c, col in enumerate(display_cols):
                val = df.iloc[r][col]
                if isinstance(val, float):
                    text = f"{val:,.3f}" if abs(val) < 1000 else f"{val:,.1f}"
                else:
                    text = str(val)
                item = QTableWidgetItem(text)
                item.setTextAlignment(ALIGN_CENTER)
                if col == "accepted":
                    item.setForeground(
                        Qt.GlobalColor.darkGreen if BINDING == "PyQt6" else Qt.darkGreen
                    ) if bool(val) else None
                table.setItem(r, c, item)
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(RESIZE_CONTENTS)
        if table.columnCount():
            hdr.setSectionResizeMode(table.columnCount() - 1, RESIZE_STRETCH)
        self._results_df = df

    def _on_select_strategy(self):
        rows = self.results_table.selectionModel().selectedRows() \
            if self.results_table.selectionModel() else []
        if not rows:
            return
        idx = rows[0].row()
        if not hasattr(self, "_results_df") or idx >= len(self._results_df):
            return
        row = self._results_df.iloc[idx]
        sid = row["id"]
        rep = next((r for r in self.engine.ranked if r.strategy.id == sid), None)
        if rep is None:
            return
        self.export_sel_btn.setEnabled(True)
        self._selected_report = rep
        self.detail.setHtml(self._detail_html(rep))

    def _detail_html(self, rep) -> str:
        s = rep.strategy
        f = rep.full
        c = self.engine.constraints
        size = min(s.contracts, c.max_position_contracts)
        consistency_met = f.get("win_rate", 0) >= c.min_win_rate
        badge = ("<span style='color:#2e9e4f;font-weight:700'>ACCEPTED</span>"
                 if rep.accepted else
                 "<span style='color:#d64545;font-weight:700'>NOT ACCEPTED</span>")
        reasons = ("<br>".join(f"&bull; {x}" for x in rep.reasons)
                   if rep.reasons else "All gates passed.")
        return f"""
        <h2>{s.id} &nbsp; {badge}</h2>
        <p><b>Rule:</b> {s.describe()}</p>
        <table cellpadding='4'>
          <tr><td><b>Net profit</b></td><td>${f.get('net_profit',0):,.2f}</td>
              <td><b>Trades</b></td><td>{f.get('trades',0)}</td></tr>
          <tr><td><b>Sharpe</b></td><td>{f.get('sharpe',0):.3f}</td>
              <td><b>Sortino</b></td><td>{f.get('sortino',0):.3f}</td></tr>
          <tr><td><b>Profit factor</b></td><td>{f.get('profit_factor',0):.3f}</td>
              <td><b>Win rate</b></td><td>{f.get('win_rate',0)*100:.1f}%</td></tr>
          <tr><td><b>Max DD</b></td><td>{f.get('max_drawdown_pct',0):.2f}%</td>
              <td><b>Recovery</b></td><td>{f.get('recovery_factor',0):.2f}</td></tr>
        </table>
        <p><b>Validation</b><br>
        IS Sharpe {rep.in_sample.get('sharpe',0):.2f} &rarr; OOS Sharpe
        {rep.out_sample.get('sharpe',0):.2f}
        (overfit degradation {rep.overfitting.get('degradation',0)*100:.0f}%)<br>
        Walk-forward {rep.walk_forward.get('pct_profitable',0)*100:.0f}% windows profitable;
        Monte Carlo {rep.monte_carlo.get('pct_profitable',0)*100:.0f}% paths profitable;
        Parameter stability {rep.robustness.get('stability',0):.2f}</p>
        <p><b>Prop compliance:</b> size {size}/{c.max_position_contracts} contracts,
        min-hold {c.min_hold_seconds:.0f}s enforced,
        consistency {'met' if consistency_met else 'NOT met'}</p>
        <p><b>Gate result:</b><br>{reasons}</p>
        """

    def _export_selected(self):
        rep = getattr(self, "_selected_report", None)
        if rep is None:
            return
        from core.lean_export import export_strategy
        out_root = self.cfg.resolve_path("output.strategies_dir", "strategies")
        out_root.mkdir(parents=True, exist_ok=True)
        p = export_strategy(rep, self.engine.feature_set, self.cfg,
                            self.engine.constraints, out_root,
                            descriptions=self.engine.feature_set.descriptions)
        self.log(f"Exported {rep.strategy.id} -> {p}")
        QMessageBox.information(self, "Exported",
                                f"Strategy {rep.strategy.id} written to:\n{p}")

    # ==================================================================== config
    def _browse_data_dir(self):
        start = self.data_dir_edit.text() or str(PROJECT_ROOT)
        d = QFileDialog.getExistingDirectory(self, "Select engine export folder",
                                             start)
        if d:
            self.data_dir_edit.setText(d)

    def _load_config_into_form(self):
        c = self.cfg
        self.data_dir_edit.setText(c.get("dataset.data_dir", ""))
        self.f_min_hold.setValue(float(c.get("prop_constraints.min_hold_seconds", 10)))
        self.f_daily_cap.setValue(float(c.get("prop_constraints.daily_profit_cap", 1500)))
        self.f_per_trade_dd.setValue(float(c.get("prop_constraints.per_trade_max_dd_pct", 10)))
        self.f_acct_dd.setValue(float(c.get("prop_constraints.account_max_dd_pct", 1.5)))
        self.f_eod_dd.setValue(float(c.get("prop_constraints.eod_max_drawdown", 2000)))
        self.f_max_contracts.setValue(int(c.get("prop_constraints.max_position_contracts", 5)))
        self.f_min_win.setValue(float(c.get("prop_constraints.min_win_rate", 0.5)))

        self.f_symbol.setText(str(c.get("instrument.symbol", "MES")))
        self.f_point_value.setValue(float(c.get("instrument.point_value", 5)))
        self.f_commission.setValue(float(c.get("instrument.commission_per_contract", 0.62)))
        self.f_start_equity.setValue(float(c.get("account.starting_equity", 50000)))

        self.f_method.setCurrentText(str(c.get("discovery.method", "random")))
        self.f_max_cand.setValue(int(c.get("discovery.max_candidates", 400)))
        self.f_max_feat.setValue(int(c.get("discovery.max_features_scanned", 40)))
        self.f_seed.setValue(int(c.get("discovery.random_seed", 7)))

        self.f_is_frac.setValue(float(c.get("validation.in_sample_fraction", 0.7)))
        self.f_wf_windows.setValue(int(c.get("validation.walk_forward_windows", 4)))
        self.f_mc_runs.setValue(int(c.get("validation.monte_carlo_runs", 500)))
        self.f_min_trades.setValue(int(c.get("validation.min_trades", 30)))
        self.f_min_sharpe.setValue(float(c.get("validation.min_sharpe", 1.0)))
        self.f_max_dd.setValue(float(c.get("validation.max_drawdown_pct", 25)))
        self.f_val_top_n.setValue(50)

    def _save_config_from_form(self):
        c = self.cfg
        c.set("dataset.data_dir", self.data_dir_edit.text().strip())
        c.set("prop_constraints.min_hold_seconds", self.f_min_hold.value())
        c.set("prop_constraints.daily_profit_cap", self.f_daily_cap.value())
        c.set("prop_constraints.per_trade_max_dd_pct", self.f_per_trade_dd.value())
        c.set("prop_constraints.account_max_dd_pct", self.f_acct_dd.value())
        c.set("prop_constraints.eod_max_drawdown", self.f_eod_dd.value())
        c.set("prop_constraints.max_position_contracts", self.f_max_contracts.value())
        c.set("prop_constraints.min_win_rate", self.f_min_win.value())

        c.set("instrument.symbol", self.f_symbol.text().strip())
        c.set("instrument.point_value", self.f_point_value.value())
        c.set("instrument.commission_per_contract", self.f_commission.value())
        c.set("account.starting_equity", self.f_start_equity.value())

        c.set("discovery.method", self.f_method.currentText())
        c.set("discovery.max_candidates", self.f_max_cand.value())
        c.set("discovery.max_features_scanned", self.f_max_feat.value())
        c.set("discovery.random_seed", self.f_seed.value())

        c.set("validation.in_sample_fraction", self.f_is_frac.value())
        c.set("validation.walk_forward_windows", self.f_wf_windows.value())
        c.set("validation.monte_carlo_runs", self.f_mc_runs.value())
        c.set("validation.min_trades", self.f_min_trades.value())
        c.set("validation.min_sharpe", self.f_min_sharpe.value())
        c.set("validation.max_drawdown_pct", self.f_max_dd.value())

        path = c.save()
        self.engine = ResearchEngine(self.cfg)   # rebuild -> clears results
        for p in PHASES:
            self._set_status(p, "pending")
        self.results_table.clearContents()
        self.results_table.setRowCount(0)
        self.detail.clear()
        self.export_sel_btn.setEnabled(False)
        self.log(f"Configuration saved to {path}. Engine rebuilt.")
        QMessageBox.information(self, "Saved", f"Configuration saved to:\n{path}")

    def _reload_config(self):
        self.cfg = Config.load(self.config_path)
        self.engine = ResearchEngine(self.cfg)
        self._load_config_into_form()
        self.log("Configuration reloaded from file. Engine rebuilt.")

    # ==================================================================== style
    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { font-size: 13px; }
            QLabel#appTitle { font-size: 20px; font-weight: 700; }
            QLabel#appSub { color: #8a8f98; font-size: 11px; }
            QLabel#resultsSummary { font-weight: 600; }
            QPushButton { padding: 7px 10px; border-radius: 6px;
                          background: #3a6ea5; color: white; border: none; }
            QPushButton:hover { background: #4d84c0; }
            QPushButton:disabled { background: #b7c4d1; color: #eef; }
            QPushButton#runAll { background: #2e9e4f; font-weight: 700;
                                 padding: 10px; }
            QPushButton#runAll:hover { background: #37b85c; }
            QGroupBox { font-weight: 600; border: 1px solid #cfd6dd;
                        border-radius: 6px; margin-top: 10px; padding-top: 8px; }
            QGroupBox::title { subcontrol-origin: margin; left: 8px;
                               padding: 0 4px; }
            QTableWidget { gridline-color: #e2e6ea; }
            QHeaderView::section { background: #eef2f6; padding: 4px;
                                   border: none; font-weight: 600; }
            QTextEdit#detail, QPlainTextEdit#logView {
                background: #1e1e1e; color: #d4d4d4; border: none; }
            QTextEdit#detail { color: #222; background: #fafbfc; }
            QProgressBar { border: 1px solid #cfd6dd; border-radius: 5px;
                           text-align: center; height: 18px; }
            QProgressBar::chunk { background: #3a6ea5; border-radius: 4px; }
        """)
