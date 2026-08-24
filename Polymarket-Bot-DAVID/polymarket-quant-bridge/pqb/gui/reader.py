"""
Read-only views of what the bot is doing, for the dashboard.

Deliberately separate from the bot itself. The dashboard runs the trading loop
as a **separate process** and only ever *reads* its databases — so a crash in
the interface cannot take the bot down mid-cycle, and closing the window does
not close a position.

Every connection is opened read-only (``mode=ro``). SQLite's WAL mode makes
concurrent reads safe while the bot writes, and read-only is belt-and-braces:
the dashboard is physically unable to corrupt the journal that holds the
doubling baseline and every open position.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


def _connect(path: Path) -> Optional[sqlite3.Connection]:
    if not Path(path).exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _rows(path: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = _connect(path)
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        # A table that does not exist yet is a young install, not an error.
        return []
    finally:
        conn.close()


def _one(path: Path, sql: str, params: tuple = ()) -> dict:
    got = _rows(path, sql, params)
    return got[0] if got else {}


class Reader:
    """Everything the dashboard shows, in the words a person would use."""

    def __init__(self, config):
        self.cfg = config
        self.journal = config.journal_path
        self.intel = config.intel_path

    # -- headline numbers ----------------------------------------------------

    def overview(self) -> dict:
        cycle = _one(self.journal,
                     "SELECT * FROM cycles ORDER BY id DESC LIMIT 1")
        state = self._state()
        paper = state.get("paper.book") or {}
        positions = _rows(self.journal,
                          "SELECT * FROM lifecycles WHERE status='OPEN'")
        closed = _one(self.journal,
                      "SELECT COUNT(*) n, COALESCE(SUM(realized_pnl),0) pnl, "
                      "COALESCE(SUM(CASE WHEN realized_pnl>0 THEN 1 ELSE 0 END),0) wins "
                      "FROM lifecycles WHERE status='CLOSED'")
        # O(1) reads only, on the GUI thread. The old version ran
        # COUNT(DISTINCT wallet) over the raw trade table — a full scan that
        # took seconds once the store grew past a gigabyte, freezing the
        # window every refresh ("Not Responding" after hours of running).
        # The bot now writes these stats to the meta table on its own timer;
        # MAX(id) proxies cover insert-only tables; and the one COUNT kept
        # (wallet_scores) is bounded by the ranked universe, not the tape.
        meta = {r["key"]: r["value"] for r in _rows(
            self.intel, "SELECT key, value FROM meta")}
        fast = _one(self.intel,
                    "SELECT (SELECT COUNT(*) FROM wallet_scores WHERE rank>0)"
                    "       ranked,"
                    "       (SELECT COALESCE(MAX(id),0) FROM anomalies)"
                    "       anomalies,"
                    "       (SELECT COALESCE(MAX(id),0) FROM research_rows)"
                    "       rows")
        intel = {
            "observed": int(meta.get("observed_wallets")
                            or fast.get("ranked") or 0),
            "ranked": int(fast.get("ranked") or 0),
            "anomalies": int(meta.get("anomaly_count")
                             or fast.get("anomalies") or 0),
            "rows": int(fast.get("rows") or 0),
        }

        equity = float(cycle.get("portfolio_value") or 0.0)
        cash = float(cycle.get("balance") or 0.0)
        start = float(self.cfg.mode.paper_starting_balance or 0.0)
        baseline = state.get("doubling.baseline")

        last_ts = float(cycle.get("ts") or 0.0)
        age = (time.time() - last_ts) if last_ts else None

        return {
            "running": age is not None and age < 120,
            "last_cycle_age": age,
            "cycles": _one(self.journal,
                           "SELECT COALESCE(MAX(id),0) n FROM cycles"
                           ).get("n", 0),
            "equity": equity,
            "cash": cash,
            "start_balance": start,
            "profit": equity - start if equity else 0.0,
            "profit_pct": ((equity - start) / start * 100.0) if start and equity else 0.0,
            "fees_paid": float(paper.get("feesPaid") or 0.0),
            "fee_fund": float(paper.get("feeFund") or 0.0),
            "fee_fund_start": float(paper.get("feeFundStart") or 0.0),
            "fee_fund_topups": int(paper.get("feeFundTopups") or 0),
            "realized": float(paper.get("realizedPnl") or 0.0),
            "open_positions": len(positions),
            "closed_trades": int(closed.get("n") or 0),
            "closed_pnl": float(closed.get("pnl") or 0.0),
            "win_rate": (float(closed.get("wins") or 0) / closed["n"] * 100.0
                         if closed.get("n") else 0.0),
            "min_trade_size": float(cycle.get("min_trade_size") or 0.0),
            "baseline": float(baseline) if baseline else None,
            "target": float(baseline) * self.cfg.doubling.multiple if baseline else None,
            "wallets_observed": int(intel.get("observed") or 0),
            "wallets_ranked": int(intel.get("ranked") or 0),
            "anomalies": int(intel.get("anomalies") or 0),
            "research_rows": int(intel.get("rows") or 0),
            "markets": int(cycle.get("markets") or 0),
            "errors": int(cycle.get("errors") or 0),
            "mode": "SIMULATION" if not self.cfg.mode.live else "LIVE",
            "halted": self.cfg.halt_path.exists(),
            "killed": self.cfg.kill_switch_path.exists(),
        }

    def _state(self) -> dict:
        out: dict[str, Any] = {}
        for row in _rows(self.journal, "SELECT key, value FROM engine_state"):
            try:
                out[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                pass
        return out

    # -- tables --------------------------------------------------------------

    def wallets(self, limit: int = 200) -> list[dict]:
        """Ranked wallets, with the behavioral research overlay attached.

        The ranking (wallet_scores) stays the single authority on score,
        confidence and rank; the research pass writes a separate
        fingerprint file, and the two are joined here for display only.
        A wallet with no fingerprint yet simply has no research fields.
        """
        rows = _rows(self.intel,
                     "SELECT * FROM wallet_scores WHERE rank > 0 "
                     "ORDER BY rank LIMIT ?", (limit,))
        research = self.wallet_research().get("wallets") or {}
        if research:
            for row in rows:
                found = research.get(str(row.get("wallet") or "").lower())
                if found:
                    row["research"] = found
        return rows

    def wallet_research(self) -> dict:
        """Per-wallet behavioral fingerprints from the last research pass."""
        import json
        path = self.cfg.data_dir / "wallet_research.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def anomalies(self, limit: int = 200) -> list[dict]:
        return _rows(self.intel,
                     "SELECT * FROM anomalies ORDER BY ts DESC LIMIT ?", (limit,))

    def positions(self) -> list[dict]:
        return _rows(self.journal,
                     "SELECT * FROM lifecycles WHERE status='OPEN' ORDER BY id DESC")

    def trades(self, limit: int = 200) -> list[dict]:
        return _rows(self.journal,
                     "SELECT * FROM lifecycles WHERE status='CLOSED' "
                     "ORDER BY exit_ts DESC LIMIT ?", (limit,))

    def decisions(self, limit: int = 120) -> list[dict]:
        return _rows(self.journal,
                     "SELECT ts, action, outcome, question, score, reason, "
                     "       exit_style, wallet_influence "
                     "  FROM decisions WHERE token_id != '' "
                     " ORDER BY id DESC LIMIT ?", (limit,))

    def anomaly_summary(self) -> list[dict]:
        return _rows(self.intel,
                     "SELECT kind, COUNT(*) n, AVG(strength) strength "
                     "FROM anomalies GROUP BY kind ORDER BY n DESC")

    # -- health --------------------------------------------------------------

    def health(self) -> list[tuple[str, str, str]]:
        """``(what, value, colour)`` for each thing that can be wrong.

        Every row is something a person can act on. "Can it see the market" is
        first because everything else is downstream of it — a bridge that
        cannot reach Gamma reports zero of everything, and without this line
        that looks like a broken bot rather than a broken connection.
        """
        o = self.overview()
        out: list[tuple[str, str, str]] = []

        if not o["running"]:
            out.append(("Bot", "not running", MUTED_C))
        elif o["halted"]:
            out.append(("Bot", "HALTED — records disagree with the exchange", RED_C))
        elif o["killed"]:
            out.append(("Bot", "stopped by you", AMBER_C))
        else:
            out.append(("Bot", "running", GREEN_C))

        if o["markets"]:
            out.append(("Market data", f"{o['markets']} markets visible", GREEN_C))
        else:
            out.append(("Market data", "CANNOT REACH — check connection", RED_C))

        age = o["last_cycle_age"]
        out.append(("Last update",
                    f"{int(age)}s ago" if age is not None else "never",
                    GREEN_C if (age is not None and age < 120) else AMBER_C))
        out.append(("Errors last cycle", str(o["errors"]),
                    GREEN_C if not o["errors"] else RED_C))

        fund_start = o.get("fee_fund_start") or 0.0
        if fund_start > 0:
            fund = o.get("fee_fund") or 0.0
            topups = o.get("fee_fund_topups") or 0
            note = f"${fund:.2f} of ${fund_start:.2f}"
            if topups:
                note += f" — refilled {topups}x at the 10% mark"
            out.append(("Fee fund", note,
                        GREEN_C if fund > 0.10 * fund_start else AMBER_C))

        rows, need = o["research_rows"], 200
        out.append(("Learning progress",
                    f"{rows:,} snapshots" + (" — enough to study" if rows >= need
                                             else f" — needs ~{need}"),
                    GREEN_C if rows >= need else AMBER_C))

        strategies = self.cfg.data_dir / "strategies.json"
        if strategies.exists():
            try:
                n = len(json.loads(strategies.read_text(encoding="utf-8"))
                        .get("strategies") or [])
            except Exception:                            # noqa: BLE001
                n = 0
            out.append(("Trading rules", f"{n} discovered",
                        GREEN_C if n else AMBER_C))
        else:
            out.append(("Trading rules", "none yet — using market quality",
                        AMBER_C))
        return out

    # -- discovery -----------------------------------------------------------

    def discovery(self) -> dict:
        """Discovered strategies and the discovery run's status, for the GUI.

        This is where the Quant Bridge's actual output shows up: the rules it
        found, each with the evidence that kept it — its rank score, Sharpe, the
        OUT-OF-SAMPLE Sharpe (the overfitting check), win rate, and how many
        independent markets it survived on. Read straight from the files the bot
        and the research pass write, so the screen shows what the engine holds.
        """
        import json
        import sqlite3

        status: dict = {}
        strategies: list = []
        dj = self.cfg.data_dir / "discovery.json"
        if dj.exists():
            try:
                status = json.loads(dj.read_text(encoding="utf-8"))
            except Exception:                            # noqa: BLE001
                status = {}
        sj = self.cfg.data_dir / "strategies.json"
        if sj.exists():
            try:
                strategies = (json.loads(sj.read_text(encoding="utf-8"))
                              .get("strategies") or [])
            except Exception:                            # noqa: BLE001
                strategies = []

        # O(1): the GROUP BY over research_rows used to run HERE, on the GUI
        # thread, every refresh — the last of the grow-forever scans behind
        # the multi-hour "Not Responding". The pipeline now computes it on its
        # own 300s timer and this reads the cached number.
        row = _one(self.intel, "SELECT value FROM meta WHERE key='tokens_ready'")
        ready = int(row.get("value") or 0)

        return {"status": status, "strategies": strategies, "tokensReady": ready}

    def motifs(self) -> dict:
        """The family/motif layer's last pass: which STRUCTURES recur.

        Read from the file the research pass writes, exactly like
        `discovery()` reads `strategies.json`. Nothing here is evidence about
        any individual candidate — it is what a structural class has done
        across other candidates on other markets, and the dashboard has to
        say so wherever it shows it.
        """
        import json
        path = self.cfg.data_dir / "motifs.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"motifs": [], "scale": {}}

    # -- results -------------------------------------------------------------

    def performance(self) -> dict:
        """What has actually worked, grouped the way the journal is tagged.

        Reuses :mod:`pqb.analytics.feedback` — the same code the engine learns
        from — so the numbers on screen are the numbers the bot is acting on,
        rather than a second implementation that could drift from it.
        """
        from ..analytics import feedback

        class _ReadOnlyJournal:
            mode = "live" if self.cfg.mode.live else "dry_run"

            @staticmethod
            def query(sql: str, params: tuple = ()) -> list[dict]:
                return _rows(self.journal, sql, params)

        try:
            memory = feedback.build(_ReadOnlyJournal())
        except Exception:                                # noqa: BLE001
            return {"active": False, "groups": {}, "sample": 0}

        followed, overridden = memory.wallet_followed, memory.wallet_overridden
        return {
            "active": memory.active,
            "sample": memory.book_sample,
            "mean_return": memory.book_mean_return,
            "groups": {dim: sorted(stats.values(), key=lambda s: -s.n)
                       for dim, stats in memory.groups.items()},
            "followed": followed,
            "overridden": overridden,
            "bias": memory.wallet_exit_bias()[1],
        }

    def money_management(self) -> dict:
        """The forensic diagnostic, cached.

        Counterfactuals scan the captured price series per token, which is
        indexed but not free, and this runs on the GUI thread. The dashboard's
        refresh timer is seconds and the answer changes only when a trade
        closes, so it is computed on its own cadence — the same fix the
        overview's O(1) reads got when a growing store started freezing the
        window.
        """
        now = time.time()
        cached = getattr(self, "_mm_cache", None)
        if cached is not None and now - cached[0] < 300.0:
            return cached[1]
        from ..analytics import forensics

        try:
            data = forensics.report(
                self.journal, self.intel,
                starting_balance=float(
                    self.cfg.mode.paper_starting_balance or 0.0))
        except Exception:                                # noqa: BLE001
            data = {"available": False, "reason": "diagnostic unavailable"}
        self._mm_cache = (now, data)
        return data

    def counterfactual_by_trade(self) -> dict:
        """``lifecycle_id -> what holding 30 minutes longer would have done``.

        Kept as its own small view because the Closed Trades table needs it
        per row while the Results tab needs only the aggregate, and computing
        the aggregate twice is the expensive half.
        """
        cached = getattr(self, "_cf_cache", None)
        now = time.time()
        if cached is not None and now - cached[0] < 300.0:
            return cached[1]
        from ..analytics import forensics

        out: dict[int, float] = {}
        try:
            trades = forensics.reconstruct(self.journal)
            history = forensics.PriceHistory(self.intel)
            try:
                if history.available:
                    for trade in trades:
                        price = history.price_at(trade.token_id,
                                                 trade.exit_ts + 1_800.0)
                        if price is None or trade.entry_size <= 0:
                            continue
                        hypothetical = ((price - trade.entry_price)
                                        * trade.entry_size - trade.fees)
                        out[trade.lifecycle_id] = \
                            hypothetical - trade.realized_pnl
            finally:
                history.close()
        except Exception:                                # noqa: BLE001
            out = {}
        self._cf_cache = (now, out)
        return out

    def best_and_worst(self) -> dict:
        rows = _rows(self.journal,
                     "SELECT outcome, question, realized_pnl, return_pct, "
                     "       exit_style FROM lifecycles WHERE status='CLOSED' "
                     " ORDER BY realized_pnl DESC")
        if not rows:
            return {}
        return {"best": rows[0], "worst": rows[-1], "n": len(rows)}


GREEN_C, RED_C, AMBER_C, MUTED_C = "#1f9d55", "#c53030", "#b7791f", "#6b7280"
