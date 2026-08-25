"""The decision engine: evidence -> probability -> debate -> size -> gates -> verdict.

This is the only place in V3 that produces a `TRADE` action, and it produces
one only when all twelve gates pass. Everything else in the system either
supplies evidence to this function or learns from what it decided.

Order of operations matters and is not arbitrary:

    1  build the point-in-time evidence state         (cannot see the future)
    2  read the crash meter                           (may halt before anything)
    3  build the probability ensemble                 (multiple estimators)
    4  run the debate                                 (25 agents, red team veto)
    5  size the trade against THIS bankroll           (may be CAPITAL_INFEASIBLE)
    6  run all twelve gates                           (all of them, always)
    7  record the decision with every reason          (including the rejections)

Sizing happens AFTER the debate and BEFORE the gates because several gates need
a concrete size to judge — "is this trade too big" is unanswerable in the
abstract — while the debate must not be influenced by how much we happen to be
able to afford. An agent that votes FOR more readily on small positions is an
agent that has learned to rationalise.

Every decision is persisted, including `DO_NOT_TRADE`. V1 journalled 40,820
consecutive `DO_NOTHING` decisions with one reason, and the fact that this was
visible in the data is the only reason it was ever diagnosed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from ..agents.debate import Debate, DebateResult, persist as persist_debate
from ..config import Mode, Settings
from ..core.canon import EvidenceState
from ..core.pit import StateBuilder
from ..core.source import HistoricalSource
from ..crash.meter import CrashReading, read as read_crash
from ..execution.simulator import ExecutionSimulator, plan_segments
from ..intelligence.wallets import copy_score
from ..portfolio.capital import (Account, CapitalEngine, Feasibility,
                                 SizingResult, account_from_store)
from ..portfolio.correlation import correlation_key
from ..probability.ensemble import Ensemble, build as build_ensemble
from .gates import GateReport, GateRunner


@dataclass
class Decision:
    decision_id: str
    action: str                          # TRADE | DO_NOT_TRADE
    market_id: str = ""
    token_id: str = ""
    side: str = "BUY"
    mode: str = "RESEARCH"
    strategy_id: str = ""
    run_id: str = ""

    signal_price: float = 0.0
    fair_probability: float = 0.0
    market_probability: float = 0.0
    edge: float = 0.0
    confidence: float = 0.0

    sizing: SizingResult | None = None
    gates: GateReport | None = None
    debate: DebateResult | None = None
    ensemble: Ensemble | None = None
    crash: CrashReading | None = None
    segments: list = field(default_factory=list)
    evidence: EvidenceState | None = None

    reasons_for: list = field(default_factory=list)
    reasons_against: list = field(default_factory=list)
    blocking_gate: str = ""
    ts: int = 0

    @property
    def will_trade(self) -> bool:
        return self.action == "TRADE"

    def to_dict(self) -> dict:
        return {
            "decision_id": self.decision_id, "action": self.action,
            "market_id": self.market_id, "token_id": self.token_id,
            "side": self.side, "mode": self.mode,
            "strategy_id": self.strategy_id, "run_id": self.run_id,
            "signal_price": round(self.signal_price, 5),
            "fair_probability": round(self.fair_probability, 5),
            "market_probability": round(self.market_probability, 5),
            "edge": round(self.edge, 5),
            "confidence": round(self.confidence, 4),
            "blocking_gate": self.blocking_gate,
            "reasons_for": self.reasons_for,
            "reasons_against": self.reasons_against,
            "sizing": self.sizing.to_dict() if self.sizing else None,
            "gates": self.gates.to_dict() if self.gates else None,
            "debate": self.debate.to_dict() if self.debate else None,
            "ensemble": self.ensemble.to_dict() if self.ensemble else None,
            "crash": self.crash.to_dict() if self.crash else None,
            "segments": [{"offset_secs": s.offset_secs, "usdc": s.usdc,
                          "rationale": s.rationale} for s in self.segments],
            "evidence": self.evidence.to_dict() if self.evidence else None,
            "ts": self.ts,
        }


class DecisionEngine:
    def __init__(self, st: Settings, store, *, source: HistoricalSource | None = None,
                 builder: StateBuilder | None = None) -> None:
        self.st = st
        self.store = store
        self.source = source or HistoricalSource(st)
        self.builder = builder or StateBuilder(st, store, self.source)
        self.capital = CapitalEngine(st)
        self.gates = GateRunner(st)
        self.debate = Debate(st)
        self.simulator = ExecutionSimulator(st, self.source)
        self._crash_prior: dict = {}

    # ----------------------------------------------------------------------
    def decide(self, *, market_id: str, token_id: str = "", as_of: int = 0,
               strategy: dict | None = None, wallet_dna: dict | None = None,
               account: Account | None = None,
               sibling_prices: dict | None = None,
               persist: bool = True) -> Decision:
        as_of = as_of or int(time.time())
        d = Decision(decision_id=uuid.uuid4().hex[:16], action="DO_NOT_TRADE",
                     market_id=market_id, token_id=token_id,
                     mode=self.st.mode.value, ts=as_of,
                     strategy_id=(strategy or {}).get("strategy_id", ""))

        ev = self.builder.get(as_of, market_id, token_id)
        d.evidence = ev
        d.token_id = ev.token_id

        # -- 2. crash meter. Reads before anything expensive runs.
        prior = self._crash_prior.get(market_id)
        d.crash = read_crash(ev, prior=prior)
        self._crash_prior[market_id] = d.crash
        if d.crash.blocks_entry:
            d.reasons_against.append(
                f"crash meter {d.crash.level.value} (confidence "
                f"{d.crash.confidence:.2f}): {', '.join(d.crash.drivers)}")
            d.blocking_gate = "CRASH_METER"
            if persist:
                self._persist(d)
            return d

        market_p = float(ev.price.get("last") or 0.0)
        d.market_probability = market_p
        d.signal_price = market_p
        if not (0 < market_p < 1):
            d.reasons_against.append("no usable market price at this timestamp")
            d.blocking_gate = "DATA_VALIDITY"
            if persist:
                self._persist(d)
            return d

        # -- context every downstream consumer shares
        ctx: dict = {
            "market_probability": market_p,
            "strategy": strategy or {},
            "wallet_dna": wallet_dna or {},
            "sibling_prices": sibling_prices or {},
            "band_baseline": self._band_baseline(market_p),
            "price_series": [(t, p) for t, p, _, _ in
                             self.source.prints(token_id or ev.token_id, as_of,
                                                lookback_secs=86_400, limit=600)]
            if self.source.available else [],
            "correlation_key": correlation_key(
                market_id, str(ev.market.get("event_id") or ""),
                str(ev.market.get("question") or "")),
            "loss_lessons": self._loss_lessons(),
            "pass_stats": self._pass_stats(strategy),
            "attribution": self._attribution(),
            "primary_signal_source": (strategy or {}).get("family", ""),
        }

        # -- 3. probability ensemble (first pass, before agent estimates exist)
        ens = build_ensemble(ev, ctx, [])
        d.ensemble = ens
        d.fair_probability = ens.calibrated_probability
        d.edge = ens.edge
        ctx["fair_probability"] = d.fair_probability

        # -- 5a. provisional sizing, so risk/execution agents have something
        # concrete to judge. Recomputed after the debate with the final
        # probability.
        acct = account if account is not None else account_from_store(
            self.store, self.st, self.st.mode.value)
        liquidity = self._available_liquidity(ev)
        ctx["account"] = acct
        ctx["sizing"] = self.capital.size(
            account=acct, probability=d.fair_probability,
            signal_price=market_p, available_liquidity=liquidity,
            confidence=0.5, correlation_key=ctx["correlation_key"])

        # -- COPY_SCORE for whichever profiled wallet is most active here
        ctx["copy_score"] = self._copy_score(ev, ctx)

        # -- 4. debate
        deb = self.debate.run(ev, ctx, subject=f"{market_id}:{ev.token_id}")
        d.debate = deb
        d.run_id = deb.run_id

        # Rebuild the ensemble now that agents have offered estimates. Agent 7's
        # posterior in particular is a genuine independent estimator and should
        # not be discarded just because it was produced after the first pass.
        ens = build_ensemble(ev, ctx, deb.verdicts)
        d.ensemble = ens
        d.fair_probability = ens.calibrated_probability
        d.edge = ens.edge

        # Confidence is the debate's, further cut by estimator disagreement.
        # Two independent measures of uncertainty, both multiplicative.
        d.confidence = round(deb.confidence * (1.0 - ens.disagreement), 4)

        # -- 5b. final sizing
        sizing = self.capital.size(
            account=acct, probability=d.fair_probability,
            signal_price=market_p, available_liquidity=liquidity,
            confidence=d.confidence, correlation_key=ctx["correlation_key"],
            wallet_followed=(ctx["copy_score"] or {}).get("wallet", ""),
            strategy_id=d.strategy_id)
        d.sizing = sizing
        ctx["sizing"] = sizing

        # -- 6. all twelve gates, always
        report = self.gates.run(
            ev=ev, account=acct, sizing=sizing,
            signal_strength=abs(deb.direction),
            fair_probability=d.fair_probability,
            market_probability=market_p,
            confidence=d.confidence,
            strategy=strategy or {},
            red_team=deb.red_team_dict())
        d.gates = report
        d.blocking_gate = report.blocking_gate

        d.reasons_for = [f"[{t['agent']}] {t['thesis']}" for t in deb.theses_for]
        d.reasons_against = ([f"[{t['agent']}] {t['thesis']}"
                              for t in deb.theses_against]
                             + [f"[GATE {g.gate}] {g.reason}"
                                for g in report.blocking])

        if report.passed and sizing.ok and d.confidence > 0:
            d.action = "TRADE"
            d.segments = plan_segments(
                total_usdc=sizing.size_usdc,
                liquidity_per_hour=liquidity,
                urgency=abs(float(ev.price.get("velocity_1h") or 0.0)) * 10,
                st=self.st)

        if persist:
            self._persist(d)
            persist_debate(self.store, deb)
        return d

    # -- helpers ------------------------------------------------------------
    def _available_liquidity(self, ev: EvidenceState) -> float:
        """Measured depth if a book exists, tape flow if not, 0 if neither.

        Returning 0 rather than a guess is what makes LIQUIDITY_VALIDITY able
        to refuse. A default here would silently authorise fills that the
        market cannot supply.
        """
        if ev.order_book.ok:
            ad = ev.order_book.get("ask_depth")
            if ad:
                return float(ad)
        if ev.liquidity.ok:
            return float(ev.liquidity.get("notional_per_hour") or 0.0)
        return 0.0

    def _band_baseline(self, price: float) -> dict:
        from ..intelligence.wallets import _band
        lo, hi = _band(price)
        key = f"band_baseline_{lo:.2f}_{hi:.2f}"
        cached = self.store.get_meta(key, "")
        if cached:
            import json
            try:
                return json.loads(cached)
            except Exception:                                  # noqa: BLE001
                pass
        if not self.source.available:
            return {}
        base = self.source.price_band_baseline(lo, hi)
        import json
        self.store.set_meta(key, json.dumps(base))
        return base

    def _copy_score(self, ev: EvidenceState, ctx: dict) -> dict:
        dna = ctx.get("wallet_dna") or {}
        tops = ev.wallets.get("top") or [] if ev.wallets.ok else []
        best = None
        for t in tops:
            if t["wallet"] in dna:
                cand = dna[t["wallet"]]
                if best is None or cand.alpha_vs_band > best.alpha_vs_band:
                    best = cand
        if best is None:
            return {}
        cs = copy_score(best, ev, ctx)
        cs["wallet"] = best.wallet
        return cs

    def _loss_lessons(self) -> list:
        return self.store.query(
            "SELECT strategy_id, classification, narrative, remedy "
            "  FROM loss_forensics ORDER BY id DESC LIMIT 50")

    def _pass_stats(self, strategy: dict | None) -> dict:
        pid = (strategy or {}).get("pass_id")
        if not pid:
            row = self.store.one(
                "SELECT * FROM research_passes ORDER BY started_ts DESC LIMIT 1")
        else:
            row = self.store.one(
                "SELECT * FROM research_passes WHERE pass_id=?", (pid,))
        return row or {}

    def _attribution(self) -> dict:
        rows = self.store.query(
            "SELECT strategy_id, SUM(realized_pnl) pnl FROM positions "
            " WHERE status!='OPEN' GROUP BY strategy_id")
        return {"by_source": {r["strategy_id"] or "unattributed":
                              round(float(r["pnl"] or 0), 5) for r in rows}}

    def _persist(self, d: Decision) -> None:
        self.store.insert("decisions", [{
            "decision_id": d.decision_id, "run_id": d.run_id,
            "market_id": d.market_id, "token_id": d.token_id,
            "strategy_id": d.strategy_id, "mode": d.mode,
            "action": d.action, "side": d.side,
            "signal_price": d.signal_price,
            "fair_probability": d.fair_probability,
            "market_probability": d.market_probability,
            "edge": d.edge, "confidence": d.confidence,
            "size_usdc": d.sizing.size_usdc if d.sizing else 0.0,
            "size_shares": d.sizing.size_shares if d.sizing else 0.0,
            "expected_value": d.sizing.expected_value if d.sizing else 0.0,
            "max_loss": d.sizing.max_loss if d.sizing else 0.0,
            "gates": d.gates.to_dict() if d.gates else {},
            "blocking_gate": d.blocking_gate,
            "reasons_for": d.reasons_for,
            "reasons_against": d.reasons_against,
            "evidence_ref": f"{d.market_id}@{d.ts}",
            "ts": d.ts,
        }], source="decision_engine")
