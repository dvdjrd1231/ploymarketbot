"""
The orchestration loop — the glue, and deliberately the only glue.

Per cycle:

    kill-switch check -> refresh universe (periodically)
      -> gather features / account / positions / wallet signals
      -> settle anything the market resolved
      -> hydrate position history (peaks, reductions) from the journal
      -> doubling-rule check
      -> engine.evaluate(context)
      -> journal every decision
      -> execute the actionable ones, exits before entries
      -> journal every execution and lifecycle transition
      -> reconcile (on its own timer)

The ordering is not incidental. Exits execute before entries so a cycle that
frees capital can spend it in the same pass. Reconciliation runs last so it
judges settled state rather than racing an order it just placed. And decisions
are journalled *before* execution, so a crash mid-order still leaves evidence of
what was intended.

Nothing in here decides anything. Every judgement belongs to the engine behind
``bridge.ports.DecisionEngine``; this file only supplies it with a complete world
and faithfully carries out and records what comes back.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Optional

import httpx

from .adapters.data_adapter import PolymarketDataAdapter
from .adapters.execution_adapter import PaperBook, PolymarketExecutionAdapter
from .analytics.pipeline import IntelPipeline
from .analytics.store import IntelStore
from .bridge.ports import DecisionEngine, load_engine
from .config import Config
from .doubling import DoublingRule
from .features import token_features
from .journal import Journal
from .logs import Log
from .models import (
    Action, AccountState, BridgeContext, Decision, ExecutionReport,
    MarketFeatures, MarketStatus, PositionView,
)
from .reconcile import Reconciler
from .upstream import PolymarketError, TradingClient, clob_available

# The universe is re-resolved on this cadence rather than every cycle: market
# metadata moves in minutes, and a Gamma sweep per cycle would be the loop's
# dominant cost for no additional information.
UNIVERSE_REFRESH_SECONDS = 300

# Raw observations are pruned on this cadence rather than every cycle: the
# delete scans by timestamp and there is nothing to gain from running it more
# often than the horizon it enforces moves.
PRUNE_INTERVAL_SECONDS = 3_600

# How often, and how widely, we look up how observed markets settled. A market
# resolves once, so checking often buys nothing; the batch is sized to a few
# Gamma requests so the sweep never dominates a cycle.
SETTLE_SWEEP_SECONDS = 300
SETTLE_SWEEP_BATCH = 60


class BootAborted(Exception):
    """The operator pressed Stop while the bot was still starting up.

    A shutdown request, not a failure: callers treat it exactly like a
    clean stop from the main loop."""


class Runner:
    def __init__(self, config: Config, log: Log):
        self.config = config
        self.log = log
        # Wall-clock boot moment, stamped BEFORE anything touches disk.
        # This is the line between a STALE stop file (a previous run's
        # leftover, cleared) and a FRESH one (the operator pressing Stop
        # during our own boot, honoured). It used to be stamped AFTER the
        # journal was created — so a Stop pressed the instant the journal
        # appeared carried an mtime older than the wall and was eaten as
        # stale. Under load that window stretched to seconds, which is
        # exactly when Stop presses landed in it.
        self._boot_wall = time.time()
        self.journal = Journal(
            config.journal_path,
            mode="live" if config.mode.live else "dry_run")
        self.http: Optional[httpx.AsyncClient] = None
        self.client: Optional[TradingClient] = None
        self.data: Optional[PolymarketDataAdapter] = None
        self.execution: Optional[PolymarketExecutionAdapter] = None
        self.paper: Optional[PaperBook] = None
        self.engine: Optional[DecisionEngine] = None
        self.reconciler: Optional[Reconciler] = None
        self.doubling = DoublingRule(
            store=self.journal,
            progression=config.doubling.progression,
            multiple=config.doubling.multiple,
            enabled=config.doubling.enabled,
        )
        self.intel_store: Optional[IntelStore] = None
        self.intel: Optional[IntelPipeline] = None
        self._stopping = asyncio.Event()
        self._cycle = 0
        self._last_universe = 0.0
        self._last_capture = 0.0
        self._last_prune = 0.0
        self._last_settle = 0.0
        # Trades observed this cycle, and the weighted evidence derived from
        # them. Reset every cycle so stale activity can never influence a
        # decision it did not belong to.
        # This cycle's market features, so the mark guard can consult the
        # trade tape without re-fetching anything.
        self._markets_now: dict = {}
        self._observed_trades: list = []
        self._derived_signals: list = []
        self._kill_announced = ""
        self._force_flatten = False
        # Daily-loss tracking (risk §3). Anchored to the portfolio value at the
        # start of each UTC day; the loss since then blocks new entries.
        self._day_date = ""
        self._day_anchor_value = 0.0
        self._entry_block_announced = ""
        # This cycle's capital-preservation verdict. Neutral until the first
        # cycle computes one, so a code path that reads it before the loop
        # starts sees "no caps" rather than an attribute error.
        from . import riskpolicy as _riskpolicy
        self._preservation = _riskpolicy.PolicyVerdict()
        # Auto-discovery: run the Quant Bridge's strategy discovery from inside
        # the bot so the dashboard user never needs a command line. Heavy, so it
        # runs on its own timer in a thread and writes a status file the GUI reads.
        self._last_discover = 0.0
        self._discovering = False
        self._last_backfill = 0.0
        self._backfilling = False
        # The non-print engine feed (created in start() when enabled): the
        # structural layer between the exchange feed and the brain.
        self.nonprint_feed = None
        # Liquidation-cascade capture (research only) — created in start().
        self.cascade_monitor = None
        # The Market-State layer (events -> state -> LEAN): created in start().
        self.market_state = None

    # ==================================================================
    # setup / teardown
    # ==================================================================

    async def start(self) -> None:
        """Boot, abortable: a Stop pressed DURING startup is honoured.

        Startup does real network work (market discovery, a possible
        auto-backfill kickoff) and can take a while on a slow connection.
        The STOP file used to be polled only once the main loop was
        reached, so a Stop pressed mid-boot silently waited startup out —
        the exact "button that does nothing" this codebase keeps killing.
        The boot body now races a stop-watcher; a fresh STOP cancels boot
        cleanly and raises :class:`BootAborted` for the caller to treat as
        a normal shutdown.
        """
        watcher = asyncio.ensure_future(self._stop_watch_during_boot())
        body = asyncio.ensure_future(self._start_body())
        try:
            done, _ = await asyncio.wait(
                {watcher, body}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and not body.done():
                body.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await body
                self._stopping.set()
                self.log.info("Stop requested during startup — aborting "
                              "boot.")
                raise BootAborted()
            return body.result()
        finally:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher

    async def _stop_watch_during_boot(self) -> None:
        """Resolve when a FRESH stop file appears (stale ones — older than
        our own boot — are the previous run's leftovers, not a request)."""
        path = self.config.stop_path
        while True:
            with contextlib.suppress(OSError):
                if path.exists() and path.stat().st_mtime >= self._boot_wall:
                    with contextlib.suppress(OSError):
                        path.unlink()
                    return
            await asyncio.sleep(1.0)

    async def _start_body(self) -> None:
        cfg = self.config
        self.log.event("boot", mode="live" if cfg.mode.live else "dry_run",
                       cycle=cfg.engine.cycle_seconds,
                       markets=len(cfg.markets.track),
                       wallets=len(cfg.wallets),
                       engine=cfg.engine.implementation,
                       build=cfg.build_version())
        nested = cfg.nested_install()
        if nested is not None:
            self.log.warning(
                "UPDATE DID NOT INSTALL: a newer copy of the bot is nested "
                "inside this folder — you are running the OLD version",
                nested=str(nested))

        # The code-enforced live gate (§3). Before anything connects for live,
        # the paper record must prove an edge. A failure downgrades the session
        # to paper here — arming live on an unproven model is precisely what the
        # gate exists to make impossible, so it happens in code, not a checklist.
        if cfg.mode.live and cfg.live_gate.require:
            from .decision.live_gate import evaluate as evaluate_gate
            gate = evaluate_gate(cfg.journal_path, cfg.live_gate)
            self.log.event("live_gate", passed=gate.passed, **gate.metrics)
            if not gate.passed:
                cfg.mode.dry_run = True        # force paper for this session
                self.log.error(
                    "LIVE BLOCKED by the calibration gate — running PAPER "
                    "instead. Reasons: " + "; ".join(gate.reasons))
            else:
                self.log.warning("Live gate PASSED: " + ", ".join(
                    f"{k}={v}" for k, v in gate.metrics.items()))

        if not cfg.mode.live:
            self.log.info(
                "DRY-RUN: decisions are journalled, orders are simulated. "
                "Set mode.dry_run=false AND mode.allow_live=true to trade.")
        else:
            # Surfaced explicitly, every live start: a wallet funded on
            # Ethereum mainnet shows a zero balance here and every order is
            # rejected, with nothing in the error to suggest the real cause.
            self.log.warning(
                "LIVE MODE. Polymarket settles in USDC on POLYGON (chain "
                f"{cfg.polymarket.chain_id}). Funds held on Ethereum mainnet "
                "must be bridged to Polygon first — an unbridged wallet reads "
                "as a zero balance and every order fails.")

        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=12.0),
            limits=httpx.Limits(max_connections=32,
                                max_keepalive_connections=16,
                                keepalive_expiry=30.0),
            headers={"User-Agent": "PolymarketQuantBridge/0.1"},
        )

        if cfg.mode.live:
            self.client = await self._connect_live()
        else:
            self.paper = PaperBook(
                store=self.journal,
                starting_cash=cfg.mode.paper_starting_balance,
                reset=cfg.mode.reset_paper_on_start,
                fee_per_trade=cfg.engine.portfolio.fee_per_trade_usdc,
                fee_fund=cfg.engine.portfolio.fee_fund_usdc,
            )
            self.log.event("paper.book", cash=self.paper.cash,
                           positions=len(self.paper.positions))

        self.data = PolymarketDataAdapter(cfg, self.log, http=self.http,
                                          client=self.client)
        if cfg.intel.enabled:
            self.intel_store = IntelStore(cfg.intel_path)
            self.intel = IntelPipeline(
                store=self.intel_store, log=self.log,
                anomaly_config=cfg.intel.anomalies,
                pinned={w.address.lower(): (w.label or w.address[:10])
                        for w in cfg.wallets if w.address.strip()},
                cohort_size=cfg.intel.cohort_size,
                lookback_days=cfg.intel.lookback_days,
                refresh_seconds=cfg.intel.refresh_seconds,
                mark_fn=self._mark_for,
            )
            self.log.event("intel.ready", db=str(cfg.intel_path),
                           **self.intel_store.stats())
        if cfg.market_state.enabled:
            from .bridge.market_state import MarketStateTracker
            self.market_state = MarketStateTracker(cfg.market_state)
            self.log.event("market_state.ready")

        if cfg.research.enabled and cfg.research.nonprint_enabled:
            from .bridge.nonprint_feed import NonPrintFeed
            self.nonprint_feed = NonPrintFeed(
                print_lookback_s=cfg.research.nonprint_print_lookback_s,
                void_max_age_s=cfg.research.nonprint_void_max_age_s,
                log=self.log)
            self.log.event("nonprint.ready",
                           available=self.nonprint_feed.available)

        # The liquidation-cascade hypothesis capture (operator's candidate):
        # observes derivatives forced flow and the 5-minute BTC market,
        # records the event study, and feeds liq_* columns into discovery.
        # Research only — it cannot trade, gate, or block a cycle.
        self.cascade_monitor = None
        if cfg.cascade.enabled and self.http is not None:
            try:
                from .analytics.cascade_monitor import CascadeMonitor
                self.cascade_monitor = CascadeMonitor(
                    cfg.cascade, cfg.data_dir / "cascade.sqlite3", self.http,
                    cfg.polymarket.gamma_host, log=self.log,
                    fee=cfg.engine.portfolio.fee_per_trade_usdc,
                    assumed_spread=cfg.research.assumed_spread)
                self.cascade_monitor.start()
                self.log.event("cascade.ready", symbol=cfg.cascade.symbol)
            except Exception as exc:                      # noqa: BLE001
                self.cascade_monitor = None
                self.log.warning("Cascade capture unavailable",
                                 error=repr(exc))

        self.execution = PolymarketExecutionAdapter(
            cfg, self.log, self.journal, client=self.client, paper=self.paper,
            price_fn=self._touch_price)
        self.engine = load_engine(
            cfg.engine.implementation,
            engine_config=cfg.engine, config=cfg, journal=self.journal,
            log=self.log, intel_store=self.intel_store,
        )
        self.reconciler = Reconciler(cfg, self.log, self.journal, self.data,
                                     paper=self.paper)

        # A fresh install self-provisions its historical dataset. Without
        # this, discovery has nothing to study until someone knows to run
        # `pqb.cli backfill` by hand — and the operator's Discovery tab sits
        # at zero with no explanation.
        if self._needs_backfill():
            asyncio.ensure_future(self._auto_backfill())

        await self.data.start()

        # Reconcile before the first decision: on restart the exchange is the
        # authority on what we hold, and deciding from a stale belief is exactly
        # the failure this ordering prevents. A divergence found HERE is the
        # most serious kind — it means the process died holding something other
        # than what it recorded — so it halts on exactly the same terms as one
        # found mid-run.
        if cfg.reconciliation.on_startup:
            result = await self.reconciler.run(self._mark_for)
            if result.should_halt and cfg.reconciliation.halt_on_mismatch:
                self._engage_halt(result, self.log)

        # Then pin whatever that established we hold, and re-resolve the
        # universe around it before any decision is made.
        if self._pin_open_positions():
            await self.data.refresh_universe()
        self._last_universe = time.time()

        # Prime the books for everything we hold before the first cycle. The
        # stream takes a moment to deliver, and cycle 1 can legitimately decide
        # to exit — a decision that needs a real bid, not a stale mark.
        held = sorted(self.data.pinned_tokens)
        if held:
            await self.data.refresh_prices(held)
            self.log.event("prices.primed", tokens=len(held))

    async def _connect_live(self) -> TradingClient:
        ok, err = clob_available()
        if not ok:
            raise PolymarketError(
                f"Live mode needs the trading library: {err}. "
                "pip install -r requirements.txt")
        pm = self.config.polymarket
        if not pm.private_key:
            raise PolymarketError(
                "Live mode requires a private key from the environment.")
        client = await TradingClient(
            host=pm.clob_host, chain_id=pm.chain_id,
            private_key=pm.private_key, signature_type=pm.signature_type,
            funder_address=pm.funder_address,
        ).connect()
        # The key is not needed again; drop the process's only other reference.
        self.config.polymarket.private_key = ""
        self.log.event("exchange.connected", address=client.address,
                       funder=client.funder_address,
                       signature_type=pm.signature_type)
        return client

    async def stop(self) -> None:
        self._stopping.set()
        if getattr(self, "cascade_monitor", None) is not None:
            with contextlib.suppress(Exception):
                await self.cascade_monitor.stop()
        if self.data is not None:
            with contextlib.suppress(Exception):
                await self.data.stop()
        if self.http is not None:
            with contextlib.suppress(Exception):
                await self.http.aclose()
        if self.intel_store is not None:
            with contextlib.suppress(Exception):
                self.intel_store.close()
        with contextlib.suppress(Exception):
            self.journal.close()
        self.log.event("shutdown", cycles=self._cycle)

    # ==================================================================
    # main loop
    # ==================================================================

    def _shutdown_requested(self) -> bool:
        """Has someone asked the process to exit?

        Checked between cycles as well as inside the sleep, so a stop is acted
        on within a second even though a cycle can take several. The file is
        removed as it is honoured, so it can never stop the *next* run too.
        """
        path = self.config.stop_path
        if not path.exists():
            return False
        with contextlib.suppress(OSError):
            path.unlink()
        self.log.info("Shutdown requested — finishing this cycle and exiting.")
        return True

    async def run_forever(self) -> None:
        # A stale marker from a PREVIOUS run would otherwise stop this one
        # before it had done anything — but only a file older than our own
        # boot is stale. One written after boot is the operator pressing Stop
        # while we were still starting up, and deleting that request is how a
        # Stop button silently does nothing.
        with contextlib.suppress(OSError):
            path = self.config.stop_path
            if path.exists() and path.stat().st_mtime < self._boot_wall:
                path.unlink()

        while not self._stopping.is_set():
            if self._shutdown_requested():
                break
            started = time.perf_counter()
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.log.error("Cycle failed", error=f"{type(exc).__name__}: {exc}")
            elapsed = time.perf_counter() - started
            await self._sleep(max(1.0, self.config.engine.cycle_seconds - elapsed))

    async def _sleep(self, seconds: float) -> None:
        """Idle between cycles, waking early if asked to stop.

        Polled in short slices rather than one long wait so a shutdown request
        is noticed within a second. Most of a cycle's wall-clock is spent in
        here, so without this a stop would routinely appear to hang for the
        length of the whole cycle interval.
        """
        deadline = time.monotonic() + max(0.0, seconds)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopping.wait(),
                                       timeout=min(1.0, remaining))
            if self._stopping.is_set() or self._shutdown_requested():
                self._stopping.set()
                return

    async def run_cycle(self) -> dict:
        self._cycle += 1
        cycle_id = f"{int(time.time())}-{self._cycle:06d}"
        log = self.log.bind(cycle=cycle_id)
        started = time.perf_counter()
        errors = 0

        gate = self._gate_state()

        # Pin what we hold before resolving the universe, so a position can
        # never be filtered out of its own market's price feed.
        new_pins = self._pin_open_positions()
        blind = not self.data.universe
        if (new_pins or blind
                or time.time() - self._last_universe > UNIVERSE_REFRESH_SECONDS):
            with contextlib.suppress(Exception):
                await self.data.refresh_universe()
            # Retry every cycle while the universe is empty rather than waiting
            # out the normal interval. An empty universe means the bridge can
            # see nothing at all, so five minutes of patience is five minutes
            # of guaranteed inactivity.
            self._last_universe = 0.0 if not self.data.universe else time.time()

        # Reconcile BEFORE deciding, not after. A divergence found afterwards
        # would already have been traded on, which is precisely what the
        # halt-on-mismatch rule exists to prevent.
        if self.reconciler.due():
            try:
                result = await self.reconciler.run(self._mark_for)
                if result.should_halt and \
                        self.config.reconciliation.halt_on_mismatch:
                    self._engage_halt(result, log)
                    gate = "halt"
            except Exception as exc:
                errors += 1
                log.error("Reconciliation failed", error=repr(exc))

        markets, account, positions, signals, wallet_positions = \
            await self._gather(log)

        # A cycle that can see no markets is not a healthy cycle, and must not
        # report itself as one. Everything downstream — quotes, captured
        # features, decisions, wallet scoring — is starved by this, so it is
        # counted as an error and said plainly rather than left as a WARNING
        # buried above an `errors=0` summary.
        if not markets:
            errors += 1
            log.error(
                "No markets visible — the bridge is blind this cycle. Nothing "
                "can be decided, captured or scored until market data returns. "
                "Usually the connection, a firewall or a VPN.",
                universe=len(self.data.universe))

        # Simulated positions in resolved markets are realised here rather than
        # left for an exit order: once a market resolves there is no book to sell
        # into, and the settlement price is the only correct exit price.
        if self.paper is not None:
            await self._settle_simulated(markets, positions, log)
            positions = self._local_positions(markets)
            account = self._paper_account()

        self._hydrate(positions)

        # The analytical pass: ingest -> rank -> detect. Before the engine is
        # called, because its output is part of what the engine is given.
        await self._run_intel(markets, log)

        drawdown = self._portfolio_drawdown(account.portfolio_value)
        # A halted bridge must not start a flatten either: closing out is still
        # trading, and we have just established that we do not know what we
        # hold. The operator investigates, then decides.
        doubling_flatten = (False if gate == "halt" else
                            self._doubling_check(account.portfolio_value,
                                                 positions, log))
        flattening = doubling_flatten or self._force_flatten
        flatten_reason = "doubling" if doubling_flatten else ""
        if gate == "flatten":
            flattening = True
            flatten_reason = "kill_switch"

        # CAPITAL PRESERVATION, computed BEFORE the context is built rather
        # than beside the risk block below. The verdict has to reach the engine
        # in the same cycle it is computed, because a policy that can only
        # block is a cliff: it lets the account run at full size right up to
        # the halt and then stops dead. The ramp is what makes the halt rare.
        self._preservation = self._preservation_verdict(account, positions)
        if self._preservation.reasons:
            log.info("capital preservation: "
                     + "; ".join(self._preservation.reasons))

        nonprint = self._feed_nonprint(markets)
        market_state = self._feed_market_state(markets)
        tape_wallets = self._tape_wallets()
        from .analytics import regime as regime_mod
        regime = regime_mod.measure(markets)

        context = BridgeContext(
            cycle_id=cycle_id, ts=time.time(), account=account,
            markets=markets, positions=positions,
            nonprint=nonprint, regime=regime, market_state=market_state,
            tape_wallets=tape_wallets,
            wallet_signals=list(signals) + self._derived_signals,
            wallet_positions=wallet_positions,
            min_trade_size=self.doubling.min_trade_size,
            flattening=flattening, flatten_reason=flatten_reason,
            portfolio_drawdown=drawdown,
            tracked_wallets=self._weighable_wallets(),
            wallet_intel=self.intel.intel if self.intel else {},
            market_intel=self.intel.market_intel if self.intel else {},
            anomalies=list(self.intel.anomalies) if self.intel else [],
            observed_wallets=self.intel.observed_wallets if self.intel else 0,
            cascade=(self.cascade_monitor.feature_aggregates()
                     if getattr(self, "cascade_monitor", None) else {}),
            capital_scale=float(self._preservation.scale),
            capital_notes=list(self._preservation.reasons),
        )

        # Captured with the context in hand so the stored row contains exactly
        # what the engine saw — wallet evidence and anomalies included. A
        # research series built from market data alone could never discover
        # whether those columns predict anything.
        self._capture_research(context, log)
        # Discovery runs on its own hourly timer, in a thread — it never blocks
        # this cycle. This is what makes a dashboard-only user get discovered
        # strategies without ever running a command.
        await self._maybe_discover(log)

        decisions = self.engine.evaluate(context)
        actionable = 0
        for decision in decisions:
            market = markets.get(decision.market_id)
            self.journal.record_decision(decision, cycle_id, market)
            self._log_decision(log, decision)

        # Risk §3: a breach blocks new ENTRIES for the rest of the cycle (and,
        # for daily loss, the day) while still letting exits through. Computed
        # once per cycle from the reconciled account and positions.
        entry_block = self._entry_block(account, positions)
        if entry_block and self._entry_block_announced != entry_block:
            log.error(f"RISK LIMIT — entries blocked: {entry_block}")
            self._entry_block_announced = entry_block

        # Exits first: capital they release is spendable by entries this cycle.
        # ``available`` moves with each fill — sizing every order against the
        # opening balance would let one cycle commit more cash than exists.
        available = account.balance
        for decision in sorted(decisions, key=_execution_order):
            if not decision.is_actionable:
                continue
            actionable += 1
            if gate == "halt":
                # Decisions are still made and journalled while halted, so the
                # operator can see what the engine wanted to do — but nothing
                # reaches the exchange.
                log.warning("Suppressed by halt",
                            action=decision.action.value,
                            token=decision.token_id[:12])
                continue
            if entry_block and decision.action is Action.BUY:
                log.warning("Entry blocked by risk limit",
                            token=decision.token_id[:12], reason=entry_block)
                continue
            try:
                report = await self._act(decision, context, log, available)
            except Exception as exc:
                errors += 1
                log.error("Execution failed",
                          token=decision.token_id[:12],
                          error=f"{type(exc).__name__}: {exc}")
                continue
            if report is not None and report.ok:
                available += (-report.filled_usdc
                              if decision.action is Action.BUY
                              else report.filled_usdc)

        if (flattening and gate != "halt" and not self._force_flatten
                and gate != "flatten"):
            self._try_complete_doubling(log)

        summary = {
            "markets": len(markets),
            "positions": len(positions),
            "walletSignals": len(signals),
            "decisions": len(decisions),
            "actionable": actionable,
            "portfolioValue": account.portfolio_value,
            "balance": account.balance,
            "minTradeSize": self.doubling.min_trade_size,
            "flattening": flattening,
            "durationMs": (time.perf_counter() - started) * 1000.0,
            "errors": errors,
        }
        self.journal.record_cycle(cycle_id, summary)
        log.event("cycle", **{
            "markets": summary["markets"],
            "positions": summary["positions"],
            "signals": summary["walletSignals"],
            "wallets": context.observed_wallets or None,
            "ranked": self._weighable_wallets() if self.intel else None,
            "anomalies": len(context.anomalies) or None,
            "decisions": summary["decisions"],
            "actionable": actionable,
            "equity": round(account.portfolio_value, 4),
            "cash": round(account.balance, 4),
            "minSize": self.doubling.min_trade_size,
            "ms": round(summary["durationMs"], 1),
            "errors": errors,
        })
        return summary

    # ==================================================================
    # gathering
    # ==================================================================

    async def _gather(self, log: Log):
        """Fetch every input concurrently; a failing source degrades to empty."""
        live = self.config.mode.live
        results = await asyncio.gather(
            self.data.market_features(with_trades=True),
            self.data.wallet_signals(),
            self.data.wallet_positions(),
            # Positions and the cash balance are two calls, not one, so the
            # account can be assembled from the positions we already have rather
            # than fetching them a second time inside the adapter.
            self.data.exchange_positions() if live else _immediate([]),
            self.data.client.get_usdc_balance() if live and self.data.client
            else _immediate(0.0),
            return_exceptions=True,
        )
        markets, signals, wallet_positions, positions, balance = results
        if isinstance(markets, BaseException):
            log.error("Market features unavailable", error=repr(markets))
            markets = {}
        if isinstance(signals, BaseException):
            signals = []
        if isinstance(wallet_positions, BaseException):
            wallet_positions = []
        if isinstance(positions, BaseException):
            log.error("Positions unavailable", error=repr(positions))
            positions = []
        if isinstance(balance, BaseException):
            log.error("Balance unavailable", error=repr(balance))
            balance = 0.0

        if live:
            account = AccountState(
                address=self.data.wallet_address,
                balance=float(balance),
                position_value=sum(p.market_value for p in positions),
                unrealized_pnl=sum(p.unrealized_pnl for p in positions),
                updated_ts=time.time(),
            )
        else:
            positions = self._local_positions(markets)
            account = self._paper_account()
        return markets, account, positions, signals, wallet_positions

    # ==================================================================
    # the analytical pass
    # ==================================================================

    async def _run_intel(self, markets: dict[str, MarketFeatures],
                         log: Log) -> None:
        """Ingest broadly, record how markets settled, re-rank, detect.

        Every step is individually guarded. The analytical layer is an input to
        the decision, not a precondition for making one: if ranking fails, the
        bridge should decide with the previous cycle's ranks and a logged
        warning, not stop trading.
        """
        if self.intel is None or self.data is None:
            return
        now = time.time()

        self._observed_trades, self._derived_signals = [], []
        try:
            observed = await self.data.observe_trades()
            if observed:
                new = self.intel_store.record_trades(observed)
                log.event("intel.ingest", observed=len(observed), new=new,
                          wallets=len({t.wallet for t in observed}))
                self._observed_trades = observed
        except Exception as exc:
            log.error("Trade ingestion failed", error=repr(exc))

        try:
            self.intel.record_resolutions(markets, self._resolved_price)
        except Exception as exc:
            log.warning("Resolution capture failed", error=repr(exc))

        # Learn how markets *outside* the tracked universe settled. Without
        # this the only settlements ever recorded are those of the 25 markets
        # we happen to be watching, almost no observed wallet ever gets a
        # scoreable trade, and the ranking stays permanently empty.
        if now - self._last_settle > SETTLE_SWEEP_SECONDS:
            self._last_settle = now
            try:
                pending = self.intel_store.markets_without_resolution(
                    limit=SETTLE_SWEEP_BATCH)
                if pending:
                    settled = await self.data.settlements(pending)
                    for token_id, (market_id, price) in settled.items():
                        self.intel_store.record_resolution(token_id, market_id,
                                                           price)
                    log.event("intel.settlements", checked=len(pending),
                              settled=len({m for m, _ in settled.values()}),
                              outcomes=len(settled))
            except Exception as exc:
                log.warning("Settlement sweep failed", error=repr(exc))

        try:
            self.intel.refresh(now=now)
        except Exception as exc:
            log.error("Wallet ranking failed", error=repr(exc))

        try:
            self.intel.detect(markets, now=now)
        except Exception as exc:
            log.error("Anomaly detection failed", error=repr(exc))

        # Turn what was just observed into weighted evidence the engine can
        # actually use. Done after ranking so each signal carries the wallet's
        # current earned weight rather than last cycle's.
        try:
            tokens = {t for m in markets.values() for t in m.quotes}
            self._derived_signals = self.intel.signals_from(
                self._observed_trades, tokens)
            if self._derived_signals:
                ranked = {w for w, i in self.intel.intel.items() if i.rank}
                log.event("intel.signals", derived=len(self._derived_signals),
                          wallets=len({s.wallet for s in self._derived_signals}),
                          fromRanked=sum(1 for s in self._derived_signals
                                         if s.wallet in ranked))
        except Exception as exc:
            log.error("Signal derivation failed", error=repr(exc))

        if now - self._last_prune > PRUNE_INTERVAL_SECONDS:
            self._last_prune = now
            try:
                removed = self.intel_store.prune(self.config.intel.retention_days)
                if removed:
                    log.event("intel.pruned", rows=removed,
                              retentionDays=self.config.intel.retention_days)
            except Exception as exc:
                log.warning("Intel pruning failed", error=repr(exc))
            # Long-run memory hygiene: the universe rotates, and per-token
            # engines for markets that went quiet hours ago are pure leak.
            try:
                dropped = 0
                if self.nonprint_feed is not None:
                    dropped += self.nonprint_feed.prune(now)
                if self.market_state is not None:
                    dropped += self.market_state.prune(now)
                if dropped:
                    log.event("engines.pruned", tokens=dropped)
            except Exception as exc:                     # noqa: BLE001
                log.warning("Engine pruning failed", error=repr(exc))

    def _resolved_price(self, market_id: str,
                        token_id: str) -> Optional[float]:
        info = self.data.gamma.cached(market_id) if self.data else None
        return info.resolved_price(token_id) if info else None

    def _weighable_wallets(self) -> int:
        """How many wallets carry evidence the engine can actually weigh.

        Not ``len(config.wallets)`` any more. Ingestion is broad and ranking is
        derived, so a run with an empty ``wallets:`` list can still have
        thousands of observed and hundreds of ranked wallets — and reporting
        zero there would tell the engine the wallet term is undefined and
        disable it entirely.
        """
        if self.intel is None:
            return len(self.config.wallets)
        ranked = sum(1 for w in self.intel.intel.values() if w.rank)
        return ranked or len(self.config.wallets)

    def _capture_research(self, context: BridgeContext, log: Log) -> None:
        """Write this cycle's feature vectors for later strategy discovery."""
        if self.intel_store is None or not self.config.research.enabled:
            return
        now = time.time()
        if now - self._last_capture < self.config.research.capture_seconds:
            return

        rows = []
        book_raw: list[tuple[float, str, str, dict]] = []
        for market in context.markets.values():
            if not market.tradable:
                continue
            intel = context.market_intel.get(market.market_id)
            for token_id, quote in market.quotes.items():
                # A row whose price is a Gamma fallback is not an observation of
                # a tradable market. Storing it would teach discovery to fit
                # against prices nothing was ever quoting.
                if quote.source == "none":
                    continue
                rows.append((
                    now, token_id, market.market_id, quote.outcome,
                    market.category,
                    token_features(market, quote, context, market_intel=intel),
                ))
                # The raw book look, verbatim, at capture cadence — the third
                # leg of RAW + normalized + snapshot preservation.
                book_raw.append((now, market.market_id, token_id,
                                 quote.to_dict()))
        if not rows:
            # Nothing usable this pass — almost always a cold start, where the
            # book stream has not delivered yet and every quote is still a
            # metadata fallback. The clock is deliberately NOT advanced here:
            # doing so would let one barren attempt burn a whole capture
            # interval, and on a fresh start that repeats, starving the series
            # that strategy discovery is later run over. Retry next cycle.
            return
        try:
            written = self.intel_store.record_research_rows(rows)
            self.intel_store.record_raw_events("book", book_raw)
            self._last_capture = now
            log.debug(f"captured {written} research rows")
        except Exception as exc:
            log.warning("Research capture failed", error=repr(exc))

    def _needs_backfill(self) -> bool:
        cfg = self.config.research
        if not (cfg.enabled and cfg.auto_backfill) or self.intel_store is None:
            return False
        try:
            have = int(self.intel_store.stats().get("resolutions") or 0)
        except Exception:                                 # noqa: BLE001
            return False
        return have < cfg.auto_backfill_min_resolutions

    def _needs_series_refill(self, exported: int,
                             breadth_blocked: int = 0,
                             holdout_series: int = 0) -> bool:
        """A discovery pass came back data-starved: too few studiable series,
        OR candidates queuing on independent-market BREADTH with a thin
        holdout pool (the operator's OOS-breadth rule: when insufficient
        OOS markets exist, expand the eligible-market search).

        This is the UPGRADED-install path the resolution-count trigger cannot
        see: a store full of legacy settled-tail tapes satisfies the fresh
        -install check while the uncertainty band drops nearly every series
        it holds. Without this, that machine's board sits empty forever and
        backfill stays convinced there is nothing to do.
        """
        cfg = self.config.research
        if not (cfg.enabled and cfg.auto_backfill) or self.intel_store is None:
            return False
        if self._backfilling:
            return False
        starved = (exported < cfg.auto_backfill_min_series
                   or (breadth_blocked >= 3 and holdout_series < 6))
        if not starved:
            return False
        return (time.time() - self._last_backfill
                >= cfg.auto_backfill_cooldown_seconds)

    async def _auto_backfill(self) -> None:
        """Pull the settled-market history a fresh install is missing.

        Runs alongside the trading loop on the shared HTTP client, logs its
        progress, and hands off to the existing hourly discovery — which is
        what turns the pulled history into Discovery-tab strategies without
        any operator command.
        """
        from .analytics.backfill import run as backfill_run

        if self._backfilling:
            return
        self._backfilling = True
        self._last_backfill = time.time()
        cfg = self.config
        self.log.event("backfill.auto_start",
                       markets=cfg.research.auto_backfill_markets)
        # The Discovery tab must never sit empty and silent while this runs —
        # an unexplained empty tab reads as "something was not done correctly".
        # Progress lands in the same status file the tab already reads.
        self._write_discovery({
            "running": True, "phase": "backfill",
            "note": "Building the historical dataset - pulling settled "
                    "markets..."})
        try:
            result = await backfill_run(
                self.http, self.intel_store, log=self.log,
                gamma_host=cfg.polymarket.gamma_host,
                data_host=cfg.polymarket.data_api_host,
                markets=cfg.research.auto_backfill_markets,
                trades_per_market=cfg.research.auto_backfill_trades,
                uncertain_lo=cfg.research.uncertain_min_price,
                uncertain_hi=cfg.research.uncertain_max_price,
                max_volume=cfg.research.backfill_max_volume,
                min_volume=cfg.research.backfill_min_volume,
                progress=lambda m: self._write_discovery(
                    {"running": True, "phase": "backfill",
                     "note": m.strip()}))
            self.log.event("backfill.auto_done",
                           marketsUsed=result.markets_used,
                           trades=result.trades_written,
                           settled=result.resolutions_written)
            self._write_discovery({
                "running": True, "phase": "research",
                "note": f"Dataset ready ({result.markets_used} settled "
                        "markets) - starting strategy research..."})
            # Discovery should not wait out its hourly timer with fresh
            # history sitting there.
            self._last_discover = 0.0
        except Exception as exc:                          # noqa: BLE001
            self.log.warning("Auto-backfill failed; will retry next start",
                             error=repr(exc))
        finally:
            self._backfilling = False

    def _feed_nonprint(self, markets: dict) -> dict:
        """Run this cycle's tape and book through the non-print engine.

        Trades first (they are the prints, fed oldest-first with the current
        book attached), then the quote update, then one snapshot per token —
        which token_features merges into every row both capture and the live
        engine read. Returns {} untouched when the feed is off or unavailable,
        and never lets a feed error cost the cycle: structure is an input, not
        a dependency.
        """
        feed = self.nonprint_feed
        if feed is None or not feed.available:
            return {}
        now = time.time()
        snapshots: dict[str, dict[str, float]] = {}
        try:
            for market in markets.values():
                tick = market.tick_size or 0.01
                trades = sorted((market.recent_trades or []),
                                key=lambda r: r.get("ts") or 0)
                for row in trades:
                    token = str(row.get("tokenId") or "")
                    quote = market.quote(token) if token else None
                    if quote is None:
                        continue
                    feed.feed_trade(
                        token, ts=float(row.get("ts") or now),
                        price=float(row.get("price") or 0.0),
                        bid=quote.bid or 0.0, ask=quote.ask or 0.0,
                        bid_size=quote.bid_depth, ask_size=quote.ask_depth,
                        tick_size=quote.tick_size or tick)
                for token, quote in market.quotes.items():
                    feed.feed_quote(
                        token, ts=now, bid=quote.bid or 0.0,
                        ask=quote.ask or 0.0, bid_size=quote.bid_depth,
                        ask_size=quote.ask_depth,
                        tick_size=quote.tick_size or tick)
                    snap = feed.snapshot(token, now)
                    if snap:
                        snapshots[token] = snap
        except Exception as exc:                          # noqa: BLE001
            self.log.warning("Non-print feed error this cycle",
                             error=repr(exc))
        return snapshots

    def _tape_wallets(self) -> dict:
        """Per-token wallet participation from this cycle's observed trades.

        Distinct printing wallets and the largest wallet's share of the
        notional — computed from the same observed-trade feed the intel layer
        ingests, and by the same formula the historical dataset uses.
        """
        per_token: dict[str, dict[str, float]] = {}
        for trade in self._observed_trades:
            token = str(getattr(trade, "token_id", "") or "")
            wallet = str(getattr(trade, "wallet", "") or "")
            usdc = float(getattr(trade, "usdc", 0.0) or 0.0)
            if not token or not wallet:
                continue
            per_token.setdefault(token, {}).setdefault(wallet, 0.0)
            per_token[token][wallet] += usdc
        out: dict[str, dict] = {}
        for token, wallets in per_token.items():
            total = sum(wallets.values())
            out[token] = {
                "count": float(len(wallets)),
                "concentration": (max(wallets.values()) / total
                                  if total > 0 else 0.0),
            }
        return out

    def _feed_market_state(self, markets: dict) -> dict:
        """Run this cycle's events through the Market-State layer.

        Same event sources as everything else — the streamed book and the
        trade tape — normalized into the tracker, one snapshot per token out.
        Never lets a state error cost the cycle.
        """
        tracker = self.market_state
        if tracker is None:
            return {}
        now = time.time()
        snapshots: dict[str, dict[str, float]] = {}
        raw: list[tuple[float, str, str, dict]] = []
        try:
            for market in markets.values():
                for row in sorted((market.recent_trades or []),
                                  key=lambda r: r.get("ts") or 0):
                    token = str(row.get("tokenId") or "")
                    if not token:
                        continue
                    price = float(row.get("price") or 0.0)
                    size = float(row.get("size") or 0.0)
                    fed = tracker.feed_trade(
                        token, ts=float(row.get("ts") or now), price=price,
                        notional=price * size,
                        is_buy=str(row.get("side") or "").upper().startswith("B"))
                    if fed:
                        # Raw preservation (his step 9): exactly the events the
                        # state layer consumed, verbatim, replayable later.
                        raw.append((float(row.get("ts") or now),
                                    market.market_id, token, dict(row)))
                for token, quote in market.quotes.items():
                    price = quote.mid or quote.last or 0.0
                    tracker.feed_book(token, ts=now, price=float(price or 0.0),
                                      bid_depth=quote.bid_depth,
                                      ask_depth=quote.ask_depth)
                    snap = tracker.snapshot(token, now)
                    if snap:
                        snapshots[token] = snap
            if raw and self.intel_store is not None:
                self.intel_store.record_raw_events("trade", raw)
        except Exception as exc:                          # noqa: BLE001
            self.log.warning("Market-state feed error this cycle",
                             error=repr(exc))
        return snapshots

    async def _maybe_discover(self, log: Log) -> None:
        """Kick off strategy discovery on its own timer, off the trading loop.

        This is what makes discovery happen for a dashboard-only user: the bot
        runs it automatically once enough history is captured, rather than
        waiting for someone to type `pqb.cli research`. It is CPU-heavy, so it
        runs in a worker thread and a guard ensures only one is ever in flight —
        a slow research pass must never stall a trading cycle.
        """
        rc = self.config.research
        if not (rc.enabled and rc.auto_discover) or self.intel_store is None:
            return
        if self._discovering:
            return
        now = time.time()
        if now - self._last_discover < rc.discover_seconds:
            return
        self._last_discover = now
        self._discovering = True
        asyncio.ensure_future(self._run_discovery(log))

    async def _run_discovery(self, log: Log) -> None:
        import pqb.research as research

        rc = self.config.research
        self._write_discovery({"running": True, "phase": "research",
                               "note": "Researching historical and captured "
                                       "series...",
                               "startedTs": time.time()})
        try:
            result = await asyncio.to_thread(
                research.run, self.config, self.intel_store, None,
                rc.min_rows, rc.max_tokens, rc.min_tokens)
            self._write_discovery({
                "running": False, "lastRun": time.time(),
                "accepted": result.accepted, "candidates": result.candidates,
                "tokensExported": result.tokens_exported,
                "tokensResearched": result.tokens_researched,
                "skippedReason": result.skipped_reason,
                "errors": result.errors[:5],
                "funnel": result.funnel,
            })
            if result.skipped_reason:
                log.info(f"Auto-discovery skipped: {result.skipped_reason}")
            else:
                log.event("research.discovered", accepted=result.accepted,
                          candidates=result.candidates,
                          tokens=result.tokens_researched)
            # Profit attribution rides the same hourly cadence: read-only
            # research over the journal, written where the CLI and any
            # future GUI surface can read it. Never blocks, never gates.
            try:
                from .analytics.attribution import write_report
                write_report(self.config.data_dir / "journal.sqlite3",
                             self.config.data_dir / "attribution.json")
            except Exception as exc:                      # noqa: BLE001
                log.warning("Attribution report failed", error=repr(exc))
            # ...and the money-management diagnostic (§26), on the same
            # cadence and with the same rules: read-only over the journal,
            # written where the dashboard and the CLI read it, and incapable
            # of changing a stop, an exit, a size or a strategy. What it
            # produces is a report and a list of QUESTIONS; the answer "no
            # change recommended" is the one it gives most often, and that is
            # the intended behaviour rather than a missing feature.
            try:
                from .analytics.forensics import report as forensic_report

                data = forensic_report(
                    self.config.data_dir / "journal.sqlite3",
                    self.config.intel_path,
                    starting_balance=float(
                        self.config.mode.paper_starting_balance or 0.0))
                (self.config.data_dir / "forensics.json").write_text(
                    json.dumps(data, indent=1, default=str), encoding="utf-8")
                proposals = [h for h in (data.get("hypotheses") or [])
                             if h.get("status") == "PROPOSED"]
                log.event("forensics.report",
                          closedTrades=int((data.get("account") or {})
                                           .get("closedTrades") or 0),
                          hypotheses=len(proposals),
                          hurting=str((data.get("exitAttribution") or {})
                                      .get("destroyingMostValue") or ""))
            except Exception as exc:                      # noqa: BLE001
                log.warning("Forensic report failed", error=repr(exc))
            # A data-starved pass re-collects instead of silently repeating:
            # a legacy store's settled-tail tapes are dropped by the
            # uncertainty band, and only a fresh uncertain-period sweep can
            # replace them. Cooldown-bounded; the sweep pokes discovery to
            # rerun the moment new history lands.
            if self._needs_series_refill(
                    result.tokens_exported,
                    breadth_blocked=int(
                        result.funnel.get("blockedOnBreadth") or 0),
                    holdout_series=int(
                        result.funnel.get("holdoutSeries") or 0)):
                log.event("backfill.refill_start",
                          seriesExported=result.tokens_exported,
                          breadthBlocked=int(
                              result.funnel.get("blockedOnBreadth") or 0),
                          floor=self.config.research.auto_backfill_min_series)
                self._write_discovery({
                    "running": True, "phase": "backfill",
                    "note": f"Only {result.tokens_exported} studiable "
                            "series - re-collecting settled markets' "
                            "uncertain periods..."})
                asyncio.ensure_future(self._auto_backfill())
        except Exception as exc:                          # noqa: BLE001
            log.warning("Auto-discovery failed", error=repr(exc))
            self._write_discovery({"running": False, "lastRun": time.time(),
                                   "skippedReason": f"error: {exc}"})
        finally:
            self._discovering = False

    def _write_discovery(self, status: dict) -> None:
        """Persist a small discovery status file for the dashboard to read."""
        import json
        try:
            path = self.config.data_dir / "discovery.json"
            path.write_text(json.dumps(status), encoding="utf-8")
        except OSError:
            pass

    def _local_positions(self, markets: dict[str, MarketFeatures]
                         ) -> list[PositionView]:
        if self.paper is None:
            return []
        views = self.paper.views(self._mark_for)
        for view in views:
            market = markets.get(view.market_id)
            if market is not None and market.end_ts:
                view.end_ts = market.end_ts
        return views

    def _paper_account(self) -> AccountState:
        if self.paper is None:
            return AccountState()
        value = self.paper.value(self._mark_for)
        return AccountState(
            address="PAPER", balance=self.paper.cash, position_value=value,
            realized_pnl=self.paper.realized_pnl,
            unrealized_pnl=sum(p.unrealized_pnl
                               for p in self.paper.views(self._mark_for)),
            updated_ts=time.time(),
        )

    async def _touch_price(self, token_id: str, side) -> Optional[float]:
        """A confirmed executable price, fetched over REST if the book is cold.

        Handed to the execution adapter so an order is never sized from a price
        nobody is quoting.
        """
        if self.data is None:
            return None
        return await self.data.prices.price_now(token_id, side)

    def _mark_for(self, token_id: str) -> Optional[float]:
        """What a position in this token is worth right now.

        **The order book is not trusted once a market stops being active.** As a
        market closes, the book thins out and can leave a stale resting quote
        behind. That is how a position of 191 shares bought at $0.13 came to be
        marked at $1.00 — a +669% paper gain on a token that settled at zero
        minutes later, which inflated the whole account by $167 for five
        minutes.

        That is not just a cosmetic reporting error. Portfolio value is what the
        doubling rule watches, so a false spike past 2x the baseline would close
        every position and advance the trade size on the strength of a number
        that was never real.

        So for a market that is closing or resolved, the settlement price is
        used when it is known, and otherwise the last actual trade — a price
        somebody genuinely paid — in preference to a quote nobody is honouring.
        """
        if self.data is None:
            return None

        info = None
        market_id = self._market_of(token_id)
        if market_id:
            info = self.data.gamma.cached(market_id)

        if info is not None and (info.resolved or info.closed):
            settled = info.resolved_price(token_id)
            if settled is not None:
                return settled
            last = self._last_trade_price(market_id, token_id)
            if last is not None and last > 0:
                return last

        price, _ = self.data.prices.mark(token_id, 0.0)
        return price if price > 0 else None

    def _market_of(self, token_id: str) -> str:
        """Which market a held token belongs to, from what we already know."""
        if self.paper is not None:
            position = self.paper.positions.get(str(token_id))
            if position is not None and position.market_id:
                return position.market_id
        row = self.journal.find_open_lifecycle(str(token_id))
        return str(row["market_id"]) if row and row["market_id"] else ""

    def _last_trade_price(self, market_id: str,
                          token_id: str) -> Optional[float]:
        market = self._markets_now.get(market_id) if self._markets_now else None
        if market is None:
            return None
        prints = [t for t in market.recent_trades
                  if str(t.get("tokenId") or "") == str(token_id)]
        if not prints:
            return None
        newest = max(prints, key=lambda t: float(t.get("ts") or 0))
        try:
            return float(newest.get("price") or 0.0) or None
        except (TypeError, ValueError):
            return None

    def _hydrate(self, positions: list[PositionView]) -> None:
        """Attach each position's stored path (peak, trough, reductions).

        Trailing-drawdown and reduce-once logic are decisions about a position's
        *history*, and the engine is stateless by contract — so the history is
        read from the journal and handed over with the position.
        """
        for position in positions:
            row = self.journal.find_open_lifecycle(position.token_id)
            if row is None:
                continue
            position.lifecycle_id = row["id"]
            price = position.cur_price or position.avg_price
            position.peak_price = max(row["peak_price"] or 0.0, price)
            position.trough_price = min(row["trough_price"] or price, price)
            if not position.opened_ts and row["entry_ts"]:
                position.opened_ts = int(row["entry_ts"])
            # Consumed by the engine's reduce-once check.
            setattr(position, "reduced_count", row["reduced_count"] or 0)
            self.journal.track_evolution(position)

    def _pin_open_positions(self) -> bool:
        """Tell the data adapter which markets we are holding.

        Read from the journal's open lifecycles rather than the previous
        cycle's position list, because that is the one source already
        reconciled against the exchange and it is populated before the first
        cycle runs — so a restart pins its inherited positions immediately,
        not one cycle late.
        """
        rows = self.journal.open_lifecycles()
        markets = {r["market_id"] for r in rows if r["market_id"]}
        tokens = {r["token_id"] for r in rows if r["token_id"]}
        if self.paper is not None:
            for position in self.paper.positions.values():
                if position.market_id:
                    markets.add(position.market_id)
                tokens.add(position.token_id)
        return self.data.pin(markets, tokens)

    def _portfolio_drawdown(self, portfolio_value: float) -> float:
        peak = float(self.journal.get_state("portfolio.peak", 0.0) or 0.0)
        if portfolio_value > peak:
            self.journal.set_state("portfolio.peak", portfolio_value)
            return 0.0
        if peak <= 0:
            return 0.0
        return max(0.0, (peak - portfolio_value) / peak)

    async def _settle_simulated(self, markets: dict[str, MarketFeatures],
                                positions: list[PositionView],
                                log: Log) -> None:
        for position in list(positions):
            market = markets.get(position.market_id)
            if market is None or market.status is MarketStatus.ACTIVE:
                continue
            info = self.data.gamma.cached(position.market_id)
            final = info.resolved_price(position.token_id) if info else None
            if final is None:
                # Closed but not decisively resolved: leave it to the engine,
                # which will try to exit into whatever book remains. Settling on
                # a guess would fabricate P&L.
                continue
            pnl = self.paper.sell(position.token_id, final, position.size)
            if position.lifecycle_id is not None:
                self.journal.close_lifecycle(
                    lifecycle_id=position.lifecycle_id, exit_price=final,
                    exit_size=position.size, realized_pnl=pnl,
                    reason=f"Market resolved at {final:.2f}.",
                    exit_style="resolution")
            log.event("position.settled", token=position.token_id[:12],
                      final=final, size=position.size, pnl=round(pnl, 4),
                      cash=round(self.paper.cash, 4))

    # ==================================================================
    # acting on decisions
    # ==================================================================

    async def _act(self, decision: Decision, context: BridgeContext,
                   log: Log,
                   available_cash: float) -> Optional[ExecutionReport]:
        market = context.markets.get(decision.market_id)
        position = next((p for p in context.positions
                         if p.token_id == decision.token_id), None)
        if decision.action is not Action.BUY and position is None:
            log.warning("Skipping an exit for a position that is gone",
                        token=decision.token_id[:12])
            return None

        report = await self.execution.execute(
            decision, market, position,
            available_cash=available_cash,
            min_trade_size=context.min_trade_size,
        )
        self.journal.record_execution(
            report, side="BUY" if decision.action is Action.BUY else "SELL")

        if not report.ok:
            return report
        if decision.action is Action.BUY:
            self._on_entry(decision, report, market, log)
        else:
            self._on_exit(decision, report, position, log)
        return report

    def _on_entry(self, decision: Decision, report: ExecutionReport,
                  market: Optional[MarketFeatures], log: Log) -> None:
        lifecycle_id = self.journal.open_lifecycle(decision, report, market)
        if self.paper is not None:
            self.paper.link_lifecycle(decision.token_id, lifecycle_id)
        log.event("position.opened", token=decision.token_id[:12],
                  outcome=decision.outcome, price=report.avg_price,
                  shares=report.filled_size,
                  usdc=round(report.filled_usdc, 4),
                  score=decision.score, lifecycle=lifecycle_id,
                  wallets=decision.wallet_influence or None)

    def _on_exit(self, decision: Decision, report: ExecutionReport,
                 position: Optional[PositionView], log: Log) -> None:
        if position is None:
            return
        entry = position.avg_price
        pnl = (report.avg_price - entry) * report.filled_size
        lifecycle_id = decision.lifecycle_id or position.lifecycle_id
        partial = (decision.action is Action.REDUCE
                   or report.filled_size < position.size - 1e-9)

        if lifecycle_id is not None:
            if partial:
                self.journal.record_reduction(
                    lifecycle_id, report.filled_size, report.avg_price, pnl)
            else:
                self.journal.close_lifecycle(
                    lifecycle_id=lifecycle_id, exit_price=report.avg_price,
                    exit_size=report.filled_size, realized_pnl=pnl,
                    reason=decision.reason,
                    exit_style=decision.exit_style or "exit",
                    exit_decision_id=decision.journal_id)
        log.event("position.reduced" if partial else "position.closed",
                  token=decision.token_id[:12], price=report.avg_price,
                  shares=report.filled_size, pnl=round(pnl, 4),
                  style=decision.exit_style or "exit",
                  lifecycle=lifecycle_id)

    def _log_decision(self, log: Log, decision: Decision) -> None:
        if decision.action is Action.NOTHING and not decision.token_id:
            log.debug(f"gate: {decision.reason}")
            return
        log.event("decision", action=decision.action.value,
                  token=decision.token_id[:12] or None,
                  outcome=decision.outcome or None,
                  score=decision.score, style=decision.exit_style or None,
                  wallets=decision.wallet_influence or None,
                  reason=decision.reason)

    # ==================================================================
    # doubling rule
    # ==================================================================

    def _doubling_check(self, portfolio_value: float,
                        positions: list[PositionView], log: Log) -> bool:
        rule = self.doubling
        if not rule.enabled:
            return False
        if rule.initialize(portfolio_value):
            log.event("doubling.baseline", baseline=rule.baseline,
                      target=rule.target, minSize=rule.min_trade_size)
        if rule.flattening:
            return True
        if rule.should_trigger(portfolio_value) and \
                rule.begin_flatten(portfolio_value):
            log.event("doubling.triggered", value=portfolio_value,
                      baseline=rule.baseline, target=rule.target,
                      positions=len(positions))
            self.journal.record_reconciliation(
                "doubling.triggered", "", {"baseline": rule.baseline},
                {"portfolioValue": portfolio_value},
                "flattening all positions", rule.status().to_dict())
            # Resting orders must not survive the flatten: one filling mid-close
            # would re-open exposure the rule is trying to clear.
            asyncio.ensure_future(self.execution.cancel_all())
            return True
        return False

    def _try_complete_doubling(self, log: Log) -> None:
        """Advance the progression only once the account is genuinely flat.

        Called after this cycle's exits, so a mass close that filled partially
        simply leaves the rule in FLATTENING and is retried next cycle — the
        minimum trade size never advances against a half-closed book.
        """
        rule = self.doubling
        if not rule.flattening:
            return
        open_positions = (len(self.paper.positions) if self.paper is not None
                          else None)
        if open_positions is None:
            # Live: trust the exchange, not our belief, before advancing.
            open_positions = len(self.journal.open_lifecycles())
        pending = self.execution.pending_count
        if not rule.can_complete(open_positions, pending):
            log.event("doubling.waiting", positions=open_positions,
                      pending=pending)
            return
        value = (self._paper_account().portfolio_value if self.paper is not None
                 else float(self.journal.get_state("account.balance", 0.0) or 0.0))
        previous = rule.min_trade_size
        if rule.complete_flatten(value, open_positions, pending):
            log.event("doubling.completed", baseline=rule.baseline,
                      minSizeFrom=previous, minSizeTo=rule.min_trade_size,
                      index=rule.index, atEnd=rule.at_end_of_progression)

    # ==================================================================
    # kill switch
    # ==================================================================

    def _roll_day(self, portfolio_value: float) -> None:
        """Anchor the daily-loss baseline at the first cycle of each UTC day."""
        today = time.strftime("%Y-%m-%d", time.gmtime())
        if today != self._day_date:
            self._day_date = today
            self._day_anchor_value = portfolio_value
            self._entry_block_announced = ""

    def _entry_block(self, account, positions) -> str:
        """Reason new ENTRIES are blocked this cycle, or ``""`` if clear.

        Exits are never blocked by this — the whole point of a risk stop is to
        let a position be reduced, not to trap capital in it. So the run loop
        applies this only to BUY decisions.
        """
        risk = self.config.risk
        self._roll_day(account.portfolio_value)

        # CAPITAL PRESERVATION. Evaluated first because its caps are fractions
        # of the account and therefore bind before the absolute dollar limits
        # below on a shrunken balance — which is the whole reason it exists.
        # The verdict is also stashed so the engine can shrink stakes with it
        # in the same cycle; a cap that only blocks is a cliff, and a ramp that
        # only shrinks never stops anything.
        # The verdict was computed at the top of the cycle so the engine could
        # size with it; re-using it here keeps one decision per cycle rather
        # than two that could disagree.
        preservation = getattr(self, "_preservation", None)
        if preservation is not None and preservation.blocked:
            return "capital preservation: " + preservation.block_reason

        if (risk.max_daily_loss_usdc > 0 and self._day_anchor_value > 0):
            loss = self._day_anchor_value - account.portfolio_value
            if loss >= risk.max_daily_loss_usdc:
                return (f"daily loss ${loss:,.2f} has reached the limit "
                        f"${risk.max_daily_loss_usdc:,.2f} — no new entries "
                        "until the next UTC day")

        if risk.max_open_exposure_usdc > 0:
            rows = positions.values() if isinstance(positions, dict) else positions
            exposure = sum((p.size * (self._mark_for(p.token_id) or p.avg_price))
                           for p in rows)
            if exposure >= risk.max_open_exposure_usdc:
                return (f"open exposure ${exposure:,.2f} has reached the limit "
                        f"${risk.max_open_exposure_usdc:,.2f} — no new entries "
                        "until exits reduce it")
        return ""

    def _preservation_verdict(self, account, positions):
        """The capital-preservation verdict for this cycle.

        Peak equity is tracked in persisted engine state rather than recomputed
        from the journal: a restart must not reset the peak, or the drawdown
        ramp would silently release itself every time the process bounces —
        which is precisely when it is most needed.
        """
        from . import riskpolicy

        cfg = getattr(self.config.risk, "preservation", None)
        if cfg is None:
            return riskpolicy.PolicyVerdict()
        equity = float(account.portfolio_value or 0.0)
        peak = float(self.journal.get_state("risk.peak_equity", 0.0) or 0.0)
        if equity > peak:
            peak = equity
            self.journal.set_state("risk.peak_equity", peak)
        rows = positions.values() if isinstance(positions, dict) else positions
        # No market metadata is threaded in here on purpose. `PositionView`
        # carries `market_id`, which is the STRONGEST cluster key there is —
        # two positions in one market are the same bet whatever their category
        # says — and the category cap degrades to the market cap rather than
        # inventing a lookup this method would have to keep in sync.
        views = riskpolicy.positions_from(rows, {}, mark_for=self._mark_for)
        return riskpolicy.evaluate(cfg, equity, float(account.balance or 0.0),
                                   views, peak_equity=peak)

    def _gate_state(self) -> str:
        """``off`` | ``halt`` | ``flatten`` — may anything reach the exchange?

        Two independent files can close the gate, and both are files rather
        than signals or API calls: they work from any shell, survive the
        operator's SSH session dropping, need no redeploy, and leave an audit
        trail of exactly when trading was stopped.

          * ``KILL``  — the operator's switch. ``flatten`` also closes out.
          * ``HALT``  — engaged automatically by reconciliation. Never flattens:
                        closing out is still trading, and a halt means we do not
                        currently know what we hold.

        HALT wins over a plain kill, and a kill-flatten is only honoured when
        no halt is in force.
        """
        halted = self.config.halt_path.exists()
        kill_path = self.config.kill_switch_path
        killed = kill_path.exists()

        if not halted and not killed:
            if self._kill_announced:
                self.log.event("gate.cleared", previous=self._kill_announced)
                self._kill_announced = ""
            return "off"

        if halted:
            state = "halt"
        else:
            try:
                body = kill_path.read_text(encoding="utf-8").strip().lower()
            except OSError:
                body = ""
            state = "flatten" if "flatten" in body else "halt"

        if self._kill_announced != state:
            source = "RECONCILIATION HALT" if halted else "KILL SWITCH"
            detail = ("closing all positions" if state == "flatten"
                      else "no orders will be sent")
            self.log.error(f"{source} ACTIVE ({state}) — {detail}",
                           file=str(self.config.halt_path if halted
                                    else kill_path))
            self._kill_announced = state
            # Resting orders must not survive either kind of stop.
            asyncio.ensure_future(self.execution.cancel_all())
        return state

    def _engage_halt(self, result, log: Log) -> None:
        """Stop trading because the exchange and the bridge disagree.

        Writes a HALT marker that persists across restarts: a divergence the
        operator has not looked at must not be cleared by bouncing the process.
        """
        path = self.config.halt_path
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "engagedTs": time.time(),
            "reason": "reconciliation mismatch",
            "summary": result.summary(),
            "result": result.to_dict(),
        }
        path.write_text(json.dumps(payload, indent=2, default=str),
                        encoding="utf-8")
        self.journal.record_reconciliation(
            "halt.engaged", "", None, result.to_dict(),
            "trading halted pending operator review", payload)
        log.error(
            "TRADING HALTED — the exchange and the bridge disagree. "
            "No orders will be sent until an operator clears this.",
            mismatches=len(result.critical), detail=result.summary(),
            file=str(path), clear_with="python -m pqb.cli resume")


def _execution_order(decision: Decision) -> int:
    """Exits before reductions before entries."""
    return {Action.EXIT: 0, Action.SELL: 1, Action.REDUCE: 2,
            Action.BUY: 3}.get(decision.action, 9)


async def _immediate(value):
    return value
