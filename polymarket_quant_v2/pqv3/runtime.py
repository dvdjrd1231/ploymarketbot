"""The engine: startup sequence, background loops, and shared state.

Implements the fifteen-step startup the brief specifies, in order, with each
step recording its own result rather than throwing. A system whose job is to
tell you what is broken must be able to start while things are broken.

Threading model is deliberately dull. One thread per loop, all daemon, all
guarded by a stop event, all writing through the same `Store` (which is
thread-local per connection and WAL-backed, so concurrent readers do not block
the dashboard). No async runtime, because the workload is SQLite reads and
occasional HTTP, and an event loop would add a scheduling layer without
removing a blocking call.

`live_authorized` is never set here. It is set by an explicit human action
through `authorize_live`, which writes a row to `authorizations` with the
system state at the moment of the decision attached. A mode that can be entered
by a background loop is not a gate.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from .config import Mode, Settings
from .core.pit import StateBuilder
from .core.source import HistoricalSource
from .core.store import Store
from .crash.meter import CrashReading
from .decision.decide import DecisionEngine
from .ingest.collectors import run_all as run_collectors
from .ingest.settled_ts import SettlementTimeCollector, coverage
from .intelligence.wallets import WalletIntelligence
from .learning.forensics import Forensics
from .regime.detect import calibrate as calibrate_regime, load_thresholds
from .scanner.opportunity import Scanner


@dataclass
class StartupStep:
    n: int
    name: str
    ok: bool = False
    detail: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {"step": self.n, "name": self.name, "ok": self.ok,
                "detail": self.detail, "elapsed_ms": self.elapsed_ms}


class Engine:
    def __init__(self, st: Settings) -> None:
        self.st = st.ensure_dirs()
        self.store = Store(self.st)
        self.source = HistoricalSource(self.st)
        self.builder = StateBuilder(self.st, self.store, self.source)
        self.decision = DecisionEngine(self.st, self.store, source=self.source,
                                       builder=self.builder)
        self.scanner = Scanner(self.st, self.store, source=self.source,
                               builder=self.builder)
        self.forensics = Forensics(self.st, self.store, source=self.source,
                                   builder=self.builder)

        self.wallet_dna: dict = {}
        self.wallet_graph = None
        self.last_scan = None
        self.last_crash: CrashReading | None = None
        self.last_backtest: dict | None = None
        self.startup: list = []
        self.started_ts = 0

        self._stop = threading.Event()
        self._threads: list = []

    # ------------------------------------------------------------- startup
    def start(self, *, build_dna: bool = True, max_wallets: int = 120) -> list:
        """The fifteen-step sequence. Each step records rather than raises."""
        self.startup = []
        self.started_ts = int(time.time())

        def step(n: int, name: str, fn):
            s = StartupStep(n, name)
            t0 = time.perf_counter()
            try:
                s.detail = fn() or ""
                s.ok = True
            except Exception as e:                            # noqa: BLE001
                s.ok = False
                s.detail = f"{type(e).__name__}: {e}"
            s.elapsed_ms = int((time.perf_counter() - t0) * 1000)
            self.startup.append(s)
            return s

        step(1, "verify configuration", self._verify_config)
        step(2, "verify database", self._verify_db)
        step(3, "verify data sources", self._verify_sources)
        step(4, "verify historical datasets", self._verify_history)
        step(5, "synchronize markets", self._sync_markets)
        step(6, "synchronize wallets",
             lambda: self._sync_wallets(build_dna, max_wallets))
        step(7, "synchronize blockchain data", self._sync_chain)
        step(8, "synchronize news and events", self._sync_news)
        step(9, "build feature state", self._build_features)
        step(10, "start agents", self._check_agents)
        step(11, "run validation", self._run_validation)
        step(12, "start opportunity scanner", self._start_scanner)
        step(13, "start shadow/paper engine", self._start_paper)
        step(14, "confirm live execution disabled", self._confirm_live_off)
        step(15, "start health monitoring", self._start_health)
        return self.startup

    # -- steps --------------------------------------------------------------
    def _verify_config(self) -> str:
        c = self.st.capital
        issues = []
        # The $100 collision, checked at startup rather than discovered at the
        # first trade.
        per_trade = c.starting_capital * c.max_fraction_per_trade
        if per_trade < c.min_order_usdc:
            issues.append(
                f"per-trade cap ${per_trade:.2f} is below the "
                f"${c.min_order_usdc:.2f} venue minimum: NO trade can be sized "
                f"at this bankroll")
        if per_trade < c.min_shares * 0.5:
            issues.append(
                f"per-trade cap ${per_trade:.2f} cannot buy {c.min_shares:g} "
                f"shares above $0.50; entries in the upper price band will be "
                f"CAPITAL_INFEASIBLE")
        detail = (f"starting capital ${c.starting_capital:.2f}, per-trade cap "
                  f"${per_trade:.2f}, reserve "
                  f"{c.reserve_fraction:.0%}, mode {self.st.mode.value}")
        if issues:
            for i in issues:
                self.store.alert("config", i, severity="WARN", source="startup")
            return detail + " | " + " | ".join(issues)
        return detail

    def _verify_db(self) -> str:
        v = self.store.get_meta("schema_version")
        n = sum(self.store.count(t) for t in ("decisions", "fills", "positions"))
        return f"schema v{v} at {self.store.path.name}, {n} trading rows"

    def _verify_sources(self) -> str:
        parts = [f"V1 tape: {'present' if self.source.available else 'ABSENT'}"]
        parts.append(f"collectors: "
                     f"{'enabled' if self.st.collectors.enabled else 'disabled'}")
        parts.append(f"news feeds: {len(self.st.collectors.news_feeds)}")
        parts.append(f"chain RPC: "
                     f"{'set' if self.st.collectors.chain_rpc else 'unset'}")
        return "; ".join(parts)

    def _verify_history(self) -> str:
        inv = self.source.inventory()
        if not inv.get("available"):
            return "no historical dataset"
        n = self.source.use_settlement_times(self.store)
        cov = coverage(self.store)
        return (f"{inv['wallet_trades']:,} wallet trades over "
                f"{inv['tape_days']}d, {inv['markets']:,} markets; "
                f"settlement timestamps: {n} V3-measured, "
                f"{cov['usable']}/{cov['total']} usable")

    def _sync_markets(self) -> str:
        if not self.st.collectors.enabled:
            n = self.store.count("markets")
            return (f"collectors disabled; {n} markets from previous runs. "
                    f"Market metadata (event grouping, close times, "
                    f"categories) is required for correlation limits and "
                    f"cross-market checks")
        from .ingest.collectors import MarketCollector
        r = MarketCollector(self.st, self.store).run()
        return f"{r.status}: {r.detail}"

    def _sync_wallets(self, build: bool, max_wallets: int) -> str:
        if not self.source.available:
            return "no tape; wallet DNA unavailable"
        cands = self.source.candidate_wallets()
        if not build:
            return f"{len(cands)} candidate wallets; DNA build skipped"
        wi = WalletIntelligence(self.st, self.source)
        self.wallet_dna = wi.build(max_wallets=max_wallets)
        with_alpha = sum(1 for d in self.wallet_dna.values()
                         if d.alpha_vs_band > 0)
        # The relationship graph, so "twenty wallets agree" can be discounted
        # to the number of INDEPENDENT opinions that actually represents.
        from .intelligence import graph as G
        self.wallet_graph = G.build(self.st, self.source,
                                    wallets=list(self.wallet_dna))
        return (f"{len(self.wallet_dna)} DNA profiles from {len(cands)} "
                f"candidates; {with_alpha} show positive alpha over their "
                f"price band; graph has {len(self.wallet_graph.edges)} edges "
                f"and {len(self.wallet_graph.clusters)} cluster(s)")

    def _sync_chain(self) -> str:
        if not self.st.collectors.chain_rpc:
            return ("no chain RPC configured; the blockchain layer stays empty "
                    "and Agent 3 abstains rather than concluding calm")
        from .ingest.collectors import ChainCollector
        r = ChainCollector(self.st, self.store).run()
        return f"{r.status}: {r.detail}"

    def _sync_news(self) -> str:
        if not self.st.collectors.news_feeds:
            return ("no news feeds configured; the news layer stays empty and "
                    "Agent 4 abstains. News history cannot be backfilled")
        from .ingest.collectors import NewsCollector
        r = NewsCollector(self.st, self.store).run()
        return f"{r.status}: {r.detail}"

    def _build_features(self) -> str:
        th = load_thresholds(self.store)
        if not th.calibrated and self.source.available:
            th = calibrate_regime(self.store, self.source)
        cal = self.store.get_meta("regime_calibration_sample", "0")
        return (f"regime thresholds "
                f"{'calibrated on ' + cal + ' tokens' if th.calibrated else 'DEFAULT (uncalibrated)'}")

    def _check_agents(self) -> str:
        from .agents.registry import AGENTS, ADVERSARIAL
        prov = self.st.agents.llm_provider or "none"
        return (f"{len(AGENTS)} agents, {len(ADVERSARIAL)} adversarial; "
                f"LLM provider: {prov} (agents are deterministic; the LLM is "
                f"narrative-only and never produces a number)")

    def _run_validation(self) -> str:
        n = self.forensics.run_all_losses()
        m = self.forensics.analyse_missed()
        return (f"{len(n)} new loss forensics, {len(m)} missed-opportunity "
                f"records")

    def _start_scanner(self) -> str:
        if not self.source.available:
            return "no tape; scanner idle"
        self.last_scan = self.scanner.scan(wallet_dna=self.wallet_dna, top_n=25)
        self.store.set_meta("last_scan_markets",
                            str(self.last_scan.markets_scanned))
        return (f"{self.last_scan.markets_scanned} markets scanned, "
                f"{self.last_scan.markets_eligible} eligible, "
                f"{len(self.last_scan.opportunities)} ranked, "
                f"{self.last_scan.markets_dropped_at_stage1} dropped below the "
                f"cut ({self.last_scan.elapsed_ms}ms)")

    def _start_paper(self) -> str:
        if self.st.mode in (Mode.PAPER, Mode.SHADOW):
            return f"{self.st.mode.value} engine armed"
        return (f"mode is {self.st.mode.value}; paper execution not armed. "
                f"Switch with `pqv3 mode PAPER`")

    def _confirm_live_off(self) -> str:
        if self.st.mode is Mode.LIVE and not self.st.live_authorized:
            self.st.mode = Mode.PAPER
            self.store.alert("safety", "LIVE mode requested without human "
                             "authorization; forced back to PAPER",
                             severity="ERROR", source="startup")
            return "LIVE requested without authorization — forced to PAPER"
        return (f"live execution DISABLED "
                f"(live_authorized={self.st.live_authorized})")

    def _start_health(self) -> str:
        return f"{len(self.store.health())} collectors tracked"

    # ------------------------------------------------------------ the loops
    def run_loops(self) -> None:
        """Start the continuous research loop and the collector loop."""
        self._stop.clear()
        specs = [("research", self._research_loop, 60),
                 ("collectors", self._collector_loop,
                  self.st.collectors.orderbook_interval_secs)]
        for name, fn, interval in specs:
            t = threading.Thread(target=self._loop, args=(name, fn, interval),
                                 name=f"pqv3-{name}", daemon=True)
            t.start()
            self._threads.append(t)

    def _loop(self, name: str, fn, interval: int) -> None:
        while not self._stop.is_set():
            try:
                fn()
            except Exception as e:                            # noqa: BLE001
                self.store.record_health(f"loop:{name}", "ERROR",
                                         error=f"{type(e).__name__}: {e}")
            self._stop.wait(interval)

    def _research_loop(self) -> None:
        """SCAN -> ANALYZE -> DEBATE -> LEARN, once per interval."""
        if not self.source.available:
            return
        self.last_scan = self.scanner.scan(wallet_dna=self.wallet_dna, top_n=15)
        self.store.set_meta("last_scan_markets",
                            str(self.last_scan.markets_scanned))
        for o in self.last_scan.opportunities[:5]:
            # The candidate's own print time, not `now`. On a live feed these
            # are within seconds of each other; on stale history they are not,
            # and using `now` would collapse every decision into a
            # DATA_VALIDITY rejection that hides what the other eleven gates
            # would have said.
            d = self.decision.decide(market_id=o.market_id,
                                     token_id=o.token_id, as_of=o.as_of,
                                     wallet_dna=self.wallet_dna)
            if d.crash:
                self.last_crash = d.crash
            if d.will_trade and self.st.mode in (Mode.PAPER, Mode.SHADOW):
                self._record_paper(d)
        self.forensics.run_all_losses(limit=25)
        self.forensics.analyse_missed(limit=25)
        self.store.record_health("loop:research", "OK", success=True,
                                 detail=f"{self.last_scan.markets_scanned} "
                                        f"markets")

    def _collector_loop(self) -> None:
        if not self.st.collectors.enabled:
            return
        run_collectors(self.st, self.store)

    def _record_paper(self, d) -> None:
        """Simulate the fill and open a paper position.

        Uses the same `ExecutionSimulator` the backtest uses. Two different
        fill models — one for research, one for paper — is how paper results
        drift away from backtest results for reasons nobody can find.
        """
        fill = self.decision.simulator.simulate(
            token_id=d.token_id, signal_ts=d.ts,
            signal_price=d.signal_price,
            size_usdc=d.sizing.size_usdc if d.sizing else 0.0,
            book_layer=d.evidence.order_book if d.evidence else None)
        if fill.status.value in ("UNFILLED", "REJECTED"):
            self.store.insert("fills", [{
                "fill_id": fill.fill_id, "decision_id": d.decision_id,
                "mode": self.st.mode.value, "market_id": d.market_id,
                "token_id": d.token_id, "signal_price": d.signal_price,
                "expected_fill": fill.expected_fill, "actual_fill": 0.0,
                "size_usdc": 0.0, "uncertainty": fill.uncertainty + [fill.reason],
                "ts": d.ts}], source="paper")
            return
        self.store.insert("fills", [{
            "fill_id": fill.fill_id, "decision_id": d.decision_id,
            "mode": self.st.mode.value, "market_id": d.market_id,
            "token_id": d.token_id, "signal_price": fill.signal_price,
            "expected_fill": fill.expected_fill,
            "actual_fill": fill.actual_fill, "slippage": fill.slippage,
            "latency_ms": fill.latency_ms, "market_impact": fill.market_impact,
            "size_usdc": fill.size_usdc, "size_shares": fill.size_shares,
            "fees": fill.fees, "uncertainty": fill.uncertainty,
            "ts": fill.fill_ts}], source="paper")
        self.store.insert("positions", [{
            "position_id": fill.fill_id, "mode": self.st.mode.value,
            "market_id": d.market_id, "token_id": d.token_id,
            "strategy_id": d.strategy_id,
            "opened_ts": fill.fill_ts, "entry_price": fill.actual_fill,
            "size_usdc": fill.size_usdc, "size_shares": fill.size_shares,
            "status": "OPEN",
            "correlation_key": (d.evidence.market.get("event_id") or
                                d.market_id) if d.evidence else d.market_id,
            "ts": fill.fill_ts}], source="paper")
        self.store.alert("trade", f"{self.st.mode.value} entry "
                                  f"${fill.size_usdc:.2f} @ {fill.actual_fill:.4f}",
                         subject=d.market_id, source="paper")

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()

    # ---------------------------------------------------------------- state
    def status(self) -> dict:
        return {"running": bool(self._threads) and not self._stop.is_set(),
                "started_ts": self.started_ts,
                "uptime_secs": int(time.time()) - self.started_ts
                if self.started_ts else 0,
                "mode": self.st.mode.value,
                "live_authorized": self.st.live_authorized,
                "threads": [t.name for t in self._threads if t.is_alive()],
                "wallet_profiles": len(self.wallet_dna),
                "startup": [s.to_dict() for s in self.startup]}

    # -------------------------------------------------------- authorisation
    def authorize_live(self, *, granted: bool, actor: str = "human",
                       note: str = "") -> dict:
        """The only path into LIVE. Records the state at the moment of consent.

        The snapshot matters: an authorisation granted while three requirements
        were unmet is a different event from one granted when all were met, and
        six months later the row is the only record of which happened.
        """
        from .server.api import Api
        api = Api(self.st, self.store, self)
        snapshot = {"requirements": api._live_requirements(),
                    "overview": api.overview()}
        unmet = [r["requirement"] for r in snapshot["requirements"]
                 if not r["met"]]
        self.store.insert("authorizations", [{
            "action": "LIVE", "granted": granted, "actor": actor,
            "note": note, "snapshot": snapshot}], source="human")
        if granted:
            self.st.live_authorized = True
            self.store.alert("authorization",
                             f"LIVE authorized by {actor}. Unmet requirements "
                             f"at time of consent: {unmet or 'none'}",
                             severity="WARN", source="human")
        else:
            self.st.live_authorized = False
            self.st.mode = Mode.PAPER
        return {"live_authorized": self.st.live_authorized,
                "mode": self.st.mode.value, "unmet_requirements": unmet}
