"""The JSON API behind the dashboard.

Every endpoint returns data that came out of the store or out of a computation
over the store. There is no endpoint that returns a hard-coded number, and
there is no endpoint that returns a plausible default when its table is empty —
an empty table returns zero rows plus a `note` saying why, because a dashboard
that shows 0 with an explanation is useful and one that shows 0 silently is
indistinguishable from one that is broken.

Everything passes through `secrets.scrub` on the way out. That is belt and
braces — no credential is ever loaded into a structure that reaches here — but
the cost is one pass over a small dict and the failure mode it prevents is
unrecoverable.

`mode` is stamped on every payload that reports money, so a reader can never
mistake paper PnL for realised PnL.
"""

from __future__ import annotations

import time

from ..agents.debate import agent_accuracy
from ..agents.registry import catalogue as agent_catalogue
from ..config import Mode, Settings
from ..core.canon import jsonable
from ..core.source import HistoricalSource
from ..decision.gates import gate_catalogue
from ..ingest.settled_ts import coverage as settled_coverage
from ..intelligence.wallets import cohorts, rank
from ..learning.forensics import Forensics, feature_importance_drift
from ..portfolio.capital import account_from_store
from ..portfolio.correlation import aggregate_exposure
from ..secrets import scrub, status as secret_status, wallet_banner


class Api:
    def __init__(self, st: Settings, store, engine=None) -> None:
        self.st = st
        self.store = store
        self.engine = engine                 # `runtime.Engine`, may be None
        self.source = HistoricalSource(st)

    # ------------------------------------------------------------------ util
    def _mode(self) -> str:
        return self.st.mode.value

    def _dna(self) -> dict:
        return self.engine.wallet_dna if self.engine else {}

    def _note(self, rows, what: str, fix: str) -> str:
        return "" if rows else f"no {what} yet — {fix}"

    # -------------------------------------------------------------- sections
    def overview(self) -> dict:
        mode = self._mode()
        acct = account_from_store(self.store, self.st, mode)
        closed = self.store.query(
            "SELECT realized_pnl FROM positions WHERE mode=? AND status!='OPEN'",
            (mode,))
        pnls = [float(r["realized_pnl"] or 0) for r in closed]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        gross_win, gross_loss = sum(wins), -sum(losses)

        return {
            "mode": mode,
            "live_authorized": self.st.live_authorized,
            "starting_capital": self.st.capital.starting_capital,
            "account_value": round(acct.equity, 4),
            "available_cash": round(acct.available_cash, 4),
            "reserved": round(self.st.capital.starting_capital
                              * self.st.capital.reserve_fraction, 4),
            "position_value": round(acct.position_value, 4),
            "realized_pnl": round(acct.realized_pnl, 4),
            "unrealized_pnl": round(acct.unrealized_pnl, 4),
            "total_pnl": round(acct.total_pnl, 4),
            "return_pct": round(acct.return_pct, 5),
            "drawdown": round(acct.drawdown, 5),
            "open_positions": acct.open_positions,
            "completed_trades": len(pnls),
            "win_rate": round(len(wins) / len(pnls), 4) if pnls else None,
            "expectancy": round(sum(pnls) / len(pnls), 5) if pnls else None,
            "profit_factor": round(gross_win / gross_loss, 4)
            if gross_loss > 0 else None,
            "max_drawdown": round(acct.drawdown, 5),
            "validated_strategies": self.store.count(
                "strategies", "status IN ('APPROVED','LIVE')"),
            "active_strategies": self.store.count(
                "strategies", "status IN ('PAPER','SHADOW','APPROVED','LIVE')"),
            "markets_scanned": int(self.store.get_meta("last_scan_markets", "0")),
            "wallets_monitored": len(self._dna()),
            "news_events_detected": self.store.count("news_items"),
            "opportunities_detected": self.store.count(
                "decisions", "ts > ?", (int(time.time()) - 86_400,)),
            "opportunities_rejected": self.store.count(
                "decisions", "action='DO_NOT_TRADE' AND ts > ?",
                (int(time.time()) - 86_400,)),
            "paper_trades": self.store.count("fills", "mode='PAPER'"),
            "live_trades": self.store.count("fills", "mode='LIVE'"),
            "wallet_status": wallet_banner(),
            "note": ("no completed trades yet; every performance figure above "
                     "is null rather than zero, because zero would read as a "
                     "measured result") if not pnls else "",
        }

    def markets(self) -> dict:
        rows = self.store.query(
            "SELECT market_id, question, category, event_id, close_ts, status "
            "  FROM markets ORDER BY close_ts LIMIT 300")
        if not rows and self.source.available:
            now = int(time.time())
            rows = [{"market_id": m["market_id"], "question": m["question"],
                     "category": "", "event_id": "", "close_ts": 0,
                     "status": "TAPE_ONLY", "prints": m["prints"],
                     "notional": round(float(m["notional"] or 0), 2)}
                    for m in self.source.active_markets(now, 7 * 86_400, 200)]
        return {"markets": rows, "n": len(rows),
                "note": self._note(rows, "market metadata",
                                   "run `pqv3 sync-markets` (needs collectors "
                                   "enabled) or `pqv3 scan` to read the tape")}

    def wallets(self) -> dict:
        dna = self._dna()
        if not dna:
            return {"wallets": [], "n": 0, "cohorts": {},
                    "note": "wallet DNA has not been built — run `pqv3 dna`"}
        ordered = rank(dna, by="alpha")
        graph = (self.engine.wallet_graph.to_dict()
                 if self.engine and getattr(self.engine, "wallet_graph", None)
                 else None)
        return {"n": len(dna),
                "wallets": [d.to_dict() for d in ordered[:200]],
                "cohorts": cohorts(dna),
                "graph": graph,
                "note": ("ranked by alpha over the price band, never by win "
                         "rate — this dataset's favourite-longshot bias makes "
                         "win rate a measure of price preference, not skill")}

    def wallet_detail(self, wallet: str) -> dict:
        dna = self._dna().get(wallet)
        trades = self.source.wallet_trades(wallet, limit=500) \
            if self.source.available else []
        chain = self.store.query(
            "SELECT * FROM chain_events WHERE wallet=? ORDER BY ts DESC LIMIT 50",
            (wallet.lower(),))
        return {"wallet": wallet,
                "dna": dna.to_dict() if dna else None,
                "trades": trades, "n_trades": len(trades),
                "chain_events": chain,
                "note": "" if dna else
                "no DNA profile for this wallet — run `pqv3 dna`"}

    def leaderboard(self) -> dict:
        dna = self._dna()
        if not dna:
            return {"boards": {}, "note": "run `pqv3 dna` first"}
        return {"boards": {k: [d.to_dict() for d in rank(dna, by=k)[:15]]
                           for k in ("alpha", "expectancy", "risk_adjusted",
                                     "consistency", "win_rate",
                                     "capital_efficiency", "diversity")},
                "cohorts": cohorts(dna),
                "note": "several orderings, because no single one is correct"}

    def opportunities(self) -> dict:
        last = self.engine.last_scan if self.engine else None
        return {"scan": last.to_dict() if last else None,
                "note": self._note(last, "scan",
                                   "run `pqv3 scan` or start the engine")}

    def news(self) -> dict:
        rows = self.store.query(
            "SELECT n.*, COUNT(l.market_id) linked FROM news_items n "
            "  LEFT JOIN news_market_links l ON l.news_id = n.id "
            " GROUP BY n.id ORDER BY n.capture_ts DESC LIMIT 100")
        return {"items": rows, "n": len(rows),
                "history_days": self.store.history_span_days(
                    "news_items", "capture_ts"),
                "note": self._note(
                    rows, "news",
                    "configure `collectors.news_feeds` and enable collectors. "
                    "News history cannot be backfilled — it accumulates from "
                    "the moment collection starts")}

    def events(self) -> dict:
        rows = self.store.query(
            "SELECT id, title, source_class, confirmation, event_ts, ts, "
            "       capture_ts, (capture_ts - ts) publication_lag "
            "  FROM news_items WHERE event_ts > 0 "
            " ORDER BY capture_ts DESC LIMIT 100")
        return {"events": rows, "n": len(rows),
                "note": self._note(rows, "timestamped events",
                                   "no feed has supplied an event time distinct "
                                   "from its publication time")}

    def blockchain(self) -> dict:
        rows = self.store.query(
            "SELECT * FROM chain_events ORDER BY ts DESC LIMIT 100")
        return {"events": rows, "n": self.store.count("chain_events"),
                "last_block": self.store.get_meta("chain_last_block", "0"),
                "history_days": self.store.history_span_days("chain_events"),
                "note": self._note(rows, "chain data",
                                   "set `collectors.chain_rpc` and enable "
                                   "collectors")}

    def microstructure(self) -> dict:
        rows = self.store.query(
            "SELECT token_id, COUNT(*) snapshots, AVG(spread) avg_spread, "
            "       AVG(bid_depth+ask_depth) avg_depth, AVG(imbalance) avg_imb, "
            "       MAX(capture_ts) last_ts FROM book_snapshots "
            " GROUP BY token_id ORDER BY snapshots DESC LIMIT 100")
        span = self.store.history_span_days("book_snapshots")
        return {"tokens": rows, "n": len(rows), "history_days": span,
                "min_history_days": self.st.collectors.min_history_days,
                "gated": span < self.st.collectors.min_history_days,
                "note": self._note(
                    rows, "order-book snapshots",
                    "enable collectors. This is the one data class that CANNOT "
                    "be backfilled: depth, spread, partial fills and queue "
                    "position for past markets are gone. Everything "
                    "microstructural is measured from the moment capture "
                    "starts")}

    def strategies(self) -> dict:
        rows = self.store.query(
            "SELECT * FROM strategies ORDER BY status, ts DESC LIMIT 200")
        by_status: dict = {}
        for r in rows:
            by_status.setdefault(r["status"], []).append(r)
        return {"strategies": rows, "by_status":
                {k: len(v) for k, v in by_status.items()},
                "ladder": ["DISCOVERED", "TESTING", "VALIDATING", "SHADOW",
                           "PAPER", "APPROVED", "LIVE", "DEGRADED",
                           "SUSPENDED", "RETIRED"],
                "note": self._note(rows, "strategies",
                                   "run `pqv3 discover`")}

    def _inversion(self) -> dict:
        """Latest inversion pass, read back from the store."""
        import json as _json
        rows = self.store.query(
            "SELECT * FROM research_passes ORDER BY started_ts DESC LIMIT 20")
        for r in rows:
            d = _json.loads(r["detail"] or "{}")
            if d.get("kind") == "inversion":
                hyp = self.store.query(
                    "SELECT statement, outcome, p_value, effect, n, params "
                    "  FROM hypotheses WHERE pass_id=? "
                    " ORDER BY effect DESC LIMIT 60", (r["pass_id"],))
                return {"pass_id": r["pass_id"], "by_verdict":
                        d.get("by_verdict", {}), "notes": d.get("notes", []),
                        "bh_threshold": r["bh_threshold"],
                        "tests": r["tested"], "conditions": r["distinct_tested"],
                        "readings": hyp}
        return {}

    def discovery(self) -> dict:
        import json as _json
        passes = self.store.query(
            "SELECT * FROM research_passes ORDER BY started_ts DESC LIMIT 20")
        latest = _json.loads(passes[0]["detail"] or "{}") if passes else {}
        hyp = self.store.query(
            "SELECT outcome, COUNT(*) n FROM hypotheses GROUP BY outcome")
        total = self.store.count("hypotheses")
        return {"passes": passes,
                "hypotheses_total": total,
                "by_outcome": {r["outcome"]: r["n"] for r in hyp},
                "effective_search_space": sum(
                    int(p["distinct_tested"] or 0) for p in passes),
                "raw_tests": sum(int(p["tested"] or 0) for p in passes),
                "latest": latest,
                "distinct_findings": latest.get("distinct_findings"),
                "finding_groups": latest.get("finding_groups") or [],
                "screen": latest.get("screen") or {},
                "pass_notes": latest.get("notes") or [],
                "inversion": self._inversion(),
                "inert_features": (latest.get("search_space") or {}).get(
                    "inert_features") or [],
                "note": self._note(
                    passes, "research passes",
                    "run `pqv3 discover`. Every pass records its full "
                    "denominator so a p-value can never be quoted without the "
                    "search that produced it")}

    def agents(self) -> dict:
        cat = agent_catalogue()
        last = self.store.query(
            "SELECT agent, stance, confidence, thesis, objections, ts "
            "  FROM agent_outputs ORDER BY id DESC LIMIT 400")
        latest: dict = {}
        for r in last:
            latest.setdefault(r["agent"], r)
        for c in cat:
            r = latest.get(c["name"])
            c["latest"] = r
            c["accuracy"] = agent_accuracy(self.store, c["name"])
        return {"agents": cat,
                "n_adversarial": sum(1 for c in cat if c["adversarial"]),
                "recent": last[:60],
                "note": self._note(last, "agent output", "run `pqv3 scan`")}

    def validation(self) -> dict:
        rows = self.store.query(
            "SELECT blocking_gate g, COUNT(*) n FROM decisions "
            " WHERE blocking_gate != '' GROUP BY g ORDER BY n DESC")
        f = Forensics(self.st, self.store, source=self.source)
        return {"gates": gate_catalogue(),
                "blocking_counts": rows,
                "gate_cost": f.gate_cost_report(),
                "settled_ts": settled_coverage(self.store),
                "note": ("`gate_cost` compares what each gate saved against "
                         "what it cost. A gate is only worth keeping if it "
                         "avoided more than it forwent")}

    def backtest(self) -> dict:
        """Backtest results, read from the strategies the pass persisted."""
        import json as _json
        rows = self.store.query(
            "SELECT * FROM strategies ORDER BY expectancy DESC LIMIT 60")
        out = []
        modelled = False
        for r in rows:
            p = _json.loads(r["params"] or "{}")
            oos = p.get("out_of_sample") or {}
            ct = p.get("capital_test") or {}
            v = p.get("verdict") or {}
            if ct.get("hold_model") == "MODELLED":
                modelled = True
            out.append({
                "strategy_id": r["strategy_id"], "version": r["version"],
                "statement": p.get("statement", ""),
                "status": v.get("status"), "lifecycle": r["status"],
                "evidence_quality": r["evidence_quality"],
                "oos_n": oos.get("n"), "oos_markets": oos.get("markets"),
                "expectancy": oos.get("expectancy"),
                "alpha_vs_baseline": oos.get("alpha_vs_baseline"),
                "baseline": oos.get("baseline_expectancy"),
                "win_rate": oos.get("win_rate"),
                "p_value": oos.get("p_value"),
                "capital_trades": ct.get("trades"),
                "capital_signals": ct.get("signals"),
                "capital_return": ct.get("total_return"),
                "capital_fill_rate": ct.get("fill_rate"),
                "hold_model": ct.get("hold_model"),
                "capital_note": ct.get("note"),
                "walkforward_positive": (p.get("walkforward") or {}).get(
                    "positive_share"),
                "robustness_survival": (p.get("robustness") or {}).get(
                    "survival"),
                "caveats": v.get("caveats") or [],
            })
        return {"results": out, "n": len(out),
                "capital_modelled": modelled,
                "note": self._note(out, "backtest results",
                                   "run `pqv3 discover`")
                or ("the $100 capital columns are MODELLED, not measured: this "
                    "database's settlement timestamps are degenerate. "
                    "Out-of-sample expectancy is unaffected."
                    if modelled else "")}

    def paper(self) -> dict:
        return self._trading_view("PAPER")

    def live(self) -> dict:
        v = self._trading_view("LIVE")
        v["authorization"] = {
            "live_authorized": self.st.live_authorized,
            "wallet": wallet_banner(),
            "history": self.store.query(
                "SELECT action, granted, actor, note, ts FROM authorizations "
                " ORDER BY id DESC LIMIT 20"),
            "requirements": self._live_requirements(),
        }
        return v

    def _trading_view(self, mode: str) -> dict:
        acct = account_from_store(self.store, self.st, mode)
        return {"mode": mode,
                "account": acct.to_dict(),
                "positions": self.store.query(
                    "SELECT * FROM positions WHERE mode=? "
                    " ORDER BY opened_ts DESC LIMIT 200", (mode,)),
                "fills": self.store.query(
                    "SELECT * FROM fills WHERE mode=? ORDER BY ts DESC LIMIT 200",
                    (mode,)),
                "note": self._note(
                    self.store.count("fills", "mode=?", (mode,)),
                    f"{mode.lower()} fills",
                    "nothing has traded in this mode")}

    def _live_requirements(self) -> list:
        """What must be true before a human is asked to authorise LIVE.

        Displayed on the LIVE tab before the authorisation control. Each row is
        a measured fact, so the reader is deciding from evidence rather than
        from a green light someone hard-coded.
        """
        span_book = self.store.history_span_days("book_snapshots")
        cov = settled_coverage(self.store)
        approved = self.store.count("strategies", "status='APPROVED'")
        paper_fills = self.store.count("fills", "mode='PAPER'")
        return [
            {"requirement": "at least one APPROVED strategy",
             "met": approved > 0, "actual": approved},
            {"requirement": "paper trading has executed fills",
             "met": paper_fills >= 30, "actual": paper_fills},
            {"requirement": "order-book history for execution modelling",
             "met": span_book >= self.st.collectors.min_history_days,
             "actual": f"{span_book}d of "
                       f"{self.st.collectors.min_history_days}d"},
            {"requirement": "usable settlement timestamps",
             "met": cov["pit_features_enabled"],
             "actual": f"{cov['usable']}/{cov['total']}"},
            {"requirement": "wallet credential present",
             "met": wallet_banner() == "WALLET CONNECTED",
             "actual": wallet_banner()},
            {"requirement": "drawdown below the hard stop",
             "met": account_from_store(self.store, self.st, "PAPER").drawdown
             < self.st.capital.hard_stop_drawdown,
             "actual": f"{account_from_store(self.store, self.st, 'PAPER').drawdown:.1%}"},
        ]

    def portfolio(self) -> dict:
        mode = self._mode()
        acct = account_from_store(self.store, self.st, mode)
        pos = self.store.query(
            "SELECT p.*, m.question, m.event_id FROM positions p "
            "  LEFT JOIN markets m ON m.market_id = p.market_id "
            " WHERE p.mode=? AND p.status='OPEN'", (mode,))
        return {"account": acct.to_dict(),
                "exposure": acct.exposure.to_dict(),
                "buckets": aggregate_exposure(pos),
                "limits": {
                    "max_fraction_per_trade": self.st.capital.max_fraction_per_trade,
                    "max_fraction_per_market": self.st.capital.max_fraction_per_market,
                    "max_fraction_correlated": self.st.capital.max_fraction_correlated,
                    "max_fraction_one_wallet_copy":
                        self.st.capital.max_fraction_one_wallet_copy,
                    "max_open_positions": self.st.capital.max_open_positions,
                    "reserve_fraction": self.st.capital.reserve_fraction,
                    "per_trade_usdc": round(
                        acct.equity * self.st.capital.max_fraction_per_trade, 2),
                    "min_order_usdc": self.st.capital.min_order_usdc},
                "note": ("buckets group positions by their true underlying "
                         "event, which is the view in which three correlated "
                         "bets stop looking diversified")}

    def risk(self) -> dict:
        mode = self._mode()
        acct = account_from_store(self.store, self.st, mode)
        crash = (self.engine.last_crash.to_dict()
                 if self.engine and self.engine.last_crash else None)
        return {"account": acct.to_dict(),
                "drawdown": round(acct.drawdown, 5),
                "hard_stop": self.st.capital.hard_stop_drawdown,
                "halted": acct.drawdown >= self.st.capital.hard_stop_drawdown,
                "crash_meter": crash,
                "note": ("the crash meter reports the strongest single input "
                         "as the level and the corroboration as the "
                         "confidence, so one alarming reading is visible "
                         "rather than averaged away")}

    def activity(self) -> dict:
        return {"discoveries": self.store.query(
            "SELECT kind, headline, measured, why, action, priority, "
            "       importance, impact, urgency, surfaced, acked, ts "
            "  FROM discoveries ORDER BY acked, priority DESC, id DESC "
            " LIMIT 60"),
            "discoveries_note": (
                "§40. Ranked by importance x expected economic impact x "
                "urgency — three ESTIMATES, bounded 0..1, whose only job is "
                "to order this queue. Nothing downstream reads them, and no "
                "gate or sizing decision sees them. Rows below the floor are "
                "recorded but were never shown"),
            "decisions": self.store.query(
            "SELECT decision_id, ts, market_id, action, blocking_gate, "
            "       confidence, edge, size_usdc, mode FROM decisions "
            " ORDER BY ts DESC LIMIT 200"),
            "alerts": self.store.query(
                "SELECT * FROM alerts ORDER BY id DESC LIMIT 100"),
            "n_decisions": self.store.count("decisions")}

    def losses(self) -> dict:
        rows = self.store.query(
            "SELECT f.*, p.market_id, p.entry_price, p.exit_price, p.size_usdc "
            "  FROM loss_forensics f "
            "  LEFT JOIN positions p ON p.position_id = f.position_id "
            " ORDER BY f.id DESC LIMIT 200")
        by_class = self.store.query(
            "SELECT classification, COUNT(*) n, AVG(actual) avg_loss "
            "  FROM loss_forensics GROUP BY classification ORDER BY n DESC")
        return {"losses": rows, "by_classification": by_class,
                "n": self.store.count("loss_forensics"),
                "note": self._note(
                    rows, "loss forensics",
                    "no losing trade has been recorded and examined. Every "
                    "loss gets a forensic record automatically")}

    def learning(self) -> dict:
        from ..learning.online import OnlineLearner
        f = Forensics(self.st, self.store, source=self.source)
        return {"online": OnlineLearner(self.st, self.store).report(
                    mode=self._mode()),
                "drift": feature_importance_drift(self.store),
                "missed": self.store.query(
                    "SELECT * FROM missed_opportunities "
                    " ORDER BY id DESC LIMIT 100"),
                "gate_cost": f.gate_cost_report(),
                "counterfactuals": self.store.query(
                    "SELECT variant, COUNT(*) n, AVG(pnl) avg_pnl "
                    "  FROM counterfactuals GROUP BY variant "
                    " ORDER BY avg_pnl DESC"),
                "strategy_flow": self.store.query(
                    "SELECT status, COUNT(*) n FROM strategies GROUP BY status"),
                "note": ("missed-opportunity analysis is what stops the gates "
                         "ratcheting tighter forever: without it, every false "
                         "positive is punished and every false negative is "
                         "invisible")}

    def system(self) -> dict:
        from ..bootstrap import v2_status
        health = self.store.health()
        inv = self.source.inventory()
        return {
            "mode": self._mode(),
            "collectors_enabled": self.st.collectors.enabled,
            "health": health,
            "v1_data": inv,
            "v2": v2_status(),
            "store": {"path": str(self.store.path),
                      "schema_version": self.store.get_meta("schema_version"),
                      "tables": {t: self.store.count(t) for t in (
                          "markets", "book_snapshots", "news_items",
                          "chain_events", "resolution_times", "decisions",
                          "agent_outputs", "fills", "positions", "strategies",
                          "hypotheses", "loss_forensics",
                          "missed_opportunities", "alerts")}},
            "secrets": [s.__dict__ for s in secret_status()],
            "wallet": wallet_banner(),
            "engine": self.engine.status() if self.engine else
            {"running": False, "note": "engine not started"},
            "settled_ts": settled_coverage(self.store),
            "note": ("every count here is a SELECT, not a cached figure. "
                     "Freshly installed, most of them are zero and that is the "
                     "accurate answer")}

    def doctrine(self) -> dict:
        """The operating charter, and the boundary it is held to.

        Rendered as its own page rather than buried in a docstring because §0
        and §41 only bind anything if a reader can see both halves at once:
        what the charter instructs, and what this installation can actually
        do. The second half is probed at request time.
        """
        from ..agents import doctrine as doc
        limit = self.st.agents.llm_context_limit
        return {"status": doc.status(limit),
                "sections": [s.to_dict() for s in doc.sections()],
                "capabilities": doc.capabilities(self.st, self.store,
                                                 self.engine),
                "condensed": doc.CONDENSED,
                "text": doc.charter(),
                "console_turns": self.store.count("console_turns"),
                "note": ("the charter is read from docs/MASTER-SYSTEM-PROMPT.md "
                         "at request time. Editing that file changes what the "
                         "embedded model is instructed, with no code change")}

    def chat(self, text: str, *, run: str = "", confirm: str = "",
             narrate: bool = True) -> dict:
        """One console turn. Not a GET route — see `app.do_POST`."""
        from ..agents.console import Console
        c = Console(self.st, self.store, api=self, engine=self.engine)
        return c.ask(text, run=run, confirm=confirm, narrate=narrate)

    def chat_history(self, limit: int = 50) -> dict:
        from ..agents.console import Console
        c = Console(self.st, self.store, api=self, engine=self.engine)
        rows = c.history(limit)
        return {"turns": rows, "n": len(rows),
                "note": self._note(rows, "console history",
                                   "nothing has been asked yet")}

    # ---------------------------------------------------------------- router
    ROUTES = ("overview", "markets", "wallets", "leaderboard", "opportunities",
              "news", "events", "blockchain", "microstructure", "strategies",
              "discovery", "agents", "validation", "backtest", "paper", "live",
              "portfolio", "risk", "activity", "losses", "learning", "system",
              "doctrine")

    def get(self, name: str, **kw) -> dict:
        if name == "wallet_detail":
            return scrub(jsonable(self.wallet_detail(kw.get("wallet", ""))))
        if name not in self.ROUTES:
            return {"error": f"unknown section '{name}'",
                    "available": list(self.ROUTES)}
        payload = getattr(self, name)()
        payload.setdefault("generated_ts", int(time.time()))
        return scrub(jsonable(payload))

    def all(self) -> dict:
        return {n: self.get(n) for n in self.ROUTES}
