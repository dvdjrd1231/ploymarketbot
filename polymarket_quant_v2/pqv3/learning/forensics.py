"""Post-trade forensics, missed opportunities, and counterfactuals.

The rule this module exists to enforce: **a losing trade must become learning
material, not a line in a PnL curve.** Every closed position at a loss gets a
forensic record with a classification, the evidence state that produced it, the
agent whose call was wrong, and a remedy. Nothing is discarded — not losers,
not winners, not the trades we declined.

Three loops:

  1  `classify_loss`   why did this specific trade fail, and what changes
  2  `analyse_missed`  what did we decline that we should not have
  3  `counterfactual`  what would other choices have produced

Loop 2 is the one most systems skip, and skipping it is how a system becomes
progressively more conservative forever: every false positive is punished by a
real loss, while every false negative is invisible. Without missed-opportunity
analysis the gates only ever ratchet tighter.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..core.pit import StateBuilder
from ..core.source import HistoricalSource

# The classification vocabulary the brief specifies. Fixed, because a free-text
# reason cannot be counted, and the whole value of this table is being able to
# ask "how many of our losses were execution rather than prediction".
CLASSIFICATIONS = (
    "bad_prediction", "bad_timing", "bad_execution", "bad_sizing", "bad_data",
    "bad_news_interpretation", "market_regime_change", "wallet_signal_failure",
    "liquidity_failure", "slippage", "unexpected_event", "model_overfit",
    "false_correlation", "insufficient_evidence", "unknown",
)

REMEDIES = ("parameter", "feature", "risk", "timing", "market_restriction",
            "wallet_restriction", "regime_restriction", "retire", "none")


@dataclass
class LossRecord:
    position_id: str
    strategy_id: str = ""
    classification: str = "unknown"
    predictable: bool = False
    failed_agent: str = ""
    failed_feature: str = ""
    predicted: float = 0.0
    actual: float = 0.0
    narrative: str = ""
    remedy: str = "none"
    evidence: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"position_id": self.position_id, "strategy_id": self.strategy_id,
                "classification": self.classification,
                "predictable": self.predictable,
                "failed_agent": self.failed_agent,
                "failed_feature": self.failed_feature,
                "predicted": round(self.predicted, 5),
                "actual": round(self.actual, 5),
                "narrative": self.narrative, "remedy": self.remedy,
                "evidence": self.evidence}


class Forensics:
    def __init__(self, st: Settings, store, *,
                 source: HistoricalSource | None = None,
                 builder: StateBuilder | None = None) -> None:
        self.st = st
        self.store = store
        self.source = source or HistoricalSource(st)
        self.builder = builder or StateBuilder(st, store, self.source)

    # ------------------------------------------------------------------ loss
    def classify_loss(self, position: dict) -> LossRecord:
        """Reconstruct the decision and determine what actually went wrong.

        Diagnosis order matters: mechanical failures are checked before
        judgement failures. A trade that lost because the fill was 4 cents
        worse than modelled is an execution problem, and calling it a bad
        prediction would send the learning loop off to retune a model that was
        right.
        """
        pid = position["position_id"]
        rec = LossRecord(position_id=pid,
                         strategy_id=position.get("strategy_id") or "")
        rec.actual = float(position.get("realized_pnl") or 0.0)

        dec = self.store.one(
            "SELECT * FROM decisions WHERE market_id=? AND action='TRADE' "
            "  AND ts<=? ORDER BY ts DESC LIMIT 1",
            (position.get("market_id"), int(position.get("opened_ts") or 0)))
        fill = self.store.one(
            "SELECT * FROM fills WHERE decision_id=? ORDER BY ts DESC LIMIT 1",
            ((dec or {}).get("decision_id", ""),))

        if not dec:
            rec.classification = "bad_data"
            rec.narrative = ("no decision record can be matched to this "
                             "position; the audit trail is broken and that is "
                             "itself the finding")
            rec.remedy = "feature"
            return rec

        rec.predicted = float(dec.get("fair_probability") or 0.0)
        entry = float(position.get("entry_price") or 0.0)
        resolution = position.get("resolution")

        # 1 — execution and slippage, before anything about judgement
        if fill:
            slip = float(fill.get("slippage") or 0.0)
            expected = float(fill.get("expected_fill") or 0.0)
            if abs(slip) > 0.02:
                rec.classification = "slippage"
                rec.failed_feature = "fill_price"
                rec.narrative = (
                    f"filled at {fill.get('actual_fill'):.4f} against an "
                    f"expected {expected:.4f} — {slip:+.4f} of slippage, more "
                    f"than the {self.st.costs.slippage_bps}bps modelled")
                rec.remedy = "parameter"
                rec.predictable = True
                rec.evidence.append(f"slippage {slip:+.4f}")
                return rec
            if float(fill.get("size_usdc") or 0) < float(
                    dec.get("size_usdc") or 0) * 0.8:
                rec.classification = "liquidity_failure"
                rec.failed_feature = "available_liquidity"
                rec.narrative = (
                    f"only ${fill.get('size_usdc'):.2f} of the ${dec.get('size_usdc'):.2f} "
                    f"order filled; the edge was measured at a size the market "
                    f"could not supply")
                rec.remedy = "risk"
                rec.predictable = True
                return rec

        # 2 — data quality at decision time
        gates = dec.get("gates") or "{}"
        if '"DATA_VALIDITY"' in str(gates) and '"passed": false' in str(gates):
            rec.classification = "bad_data"
            rec.narrative = "the data-validity gate was already failing"
            rec.remedy = "risk"
            return rec

        # 3 — regime change between entry and exit
        opened, closed = int(position.get("opened_ts") or 0), \
            int(position.get("closed_ts") or 0)
        if opened and closed and closed > opened:
            r0 = self.builder.get(opened, position.get("market_id", ""))
            r1 = self.builder.get(closed, position.get("market_id", ""))
            g0 = r0.regime.get("primary") if r0.regime.ok else None
            g1 = r1.regime.get("primary") if r1.regime.ok else None
            if g0 and g1 and g0 != g1:
                rec.classification = "market_regime_change"
                rec.failed_feature = "regime"
                rec.narrative = (f"regime moved from {g0} at entry to {g1} at "
                                 f"exit; the strategy was validated in {g0}")
                rec.remedy = "regime_restriction"
                rec.predictable = False
                rec.evidence += [f"entry regime {g0}", f"exit regime {g1}"]
                return rec

        # 4 — which agent was most wrong
        rec.failed_agent = self._most_wrong_agent(dec.get("run_id") or "")

        # 5 — prediction quality. Only reached when nothing mechanical explains it.
        if resolution is not None:
            actual_outcome = float(resolution)
            err = abs(rec.predicted - actual_outcome)
            conf = float(dec.get("confidence") or 0.0)
            if err > 0.5 and conf > 0.6:
                rec.classification = "bad_prediction"
                rec.narrative = (
                    f"we assigned {rec.predicted:.2f} at confidence "
                    f"{conf:.2f}; the outcome was {actual_outcome:.0f}. High "
                    f"confidence with a large error is a calibration failure, "
                    f"not variance")
                rec.remedy = "feature"
                rec.predictable = False
                return rec
            if err > 0.5:
                rec.classification = "insufficient_evidence"
                rec.narrative = (
                    f"predicted {rec.predicted:.2f} at confidence {conf:.2f} — "
                    f"the model was wrong but never claimed otherwise. This is "
                    f"the cost of doing business, not a defect")
                rec.remedy = "none"
                return rec

        # 6 — entry price versus the eventual outcome
        if resolution is not None and entry:
            if float(resolution) > 0.5:
                rec.classification = "bad_timing"
                rec.narrative = (f"the outcome resolved YES but the position "
                                 f"still lost; entry at {entry:.4f} was too "
                                 f"expensive or exited early")
                rec.remedy = "timing"
                return rec

        rec.classification = "unknown"
        rec.narrative = ("no mechanical, regime, data or calibration "
                         "explanation fits. Recorded as unknown rather than "
                         "assigned a plausible cause")
        return rec

    def _most_wrong_agent(self, run_id: str) -> str:
        if not run_id:
            return ""
        rows = self.store.query(
            "SELECT agent, stance, confidence FROM agent_outputs "
            " WHERE run_id=? AND stance='FOR' ORDER BY confidence DESC LIMIT 1",
            (run_id,))
        return rows[0]["agent"] if rows else ""

    def run_all_losses(self, *, mode: str = "", limit: int = 200) -> list:
        """Classify every unexamined loss. Idempotent."""
        done = {r["position_id"] for r in self.store.query(
            "SELECT position_id FROM loss_forensics")}
        sql = ("SELECT * FROM positions WHERE status!='OPEN' AND realized_pnl<0")
        params: list = []
        if mode:
            sql += " AND mode=?"
            params.append(mode)
        sql += " ORDER BY closed_ts DESC LIMIT ?"
        params.append(limit)

        out = []
        for pos in self.store.query(sql, params):
            if pos["position_id"] in done:
                continue
            rec = self.classify_loss(pos)
            self.store.insert("loss_forensics", [{
                "position_id": rec.position_id, "strategy_id": rec.strategy_id,
                "classification": rec.classification,
                "predictable": rec.predictable,
                "failed_agent": rec.failed_agent,
                "failed_feature": rec.failed_feature,
                "predicted": rec.predicted, "actual": rec.actual,
                "narrative": rec.narrative, "remedy": rec.remedy}],
                source="forensics")
            out.append(rec)
            if rec.remedy in ("retire", "risk"):
                self.store.alert(
                    "strategy_degradation",
                    f"{rec.classification} on {rec.strategy_id or 'unattributed'}: "
                    f"{rec.narrative}", severity="WARN",
                    subject=rec.strategy_id, source="forensics")
        return out

    # -------------------------------------------------------------- missed
    def analyse_missed(self, *, since_ts: int = 0, limit: int = 200) -> list:
        """For markets we declined, ask whether an opportunity existed.

        Only evaluable where the outcome is now known. A market still open
        cannot tell us whether declining it was right, and counting it either
        way would bias the answer.
        """
        since = since_ts or int(time.time()) - 30 * 86_400
        rows = self.store.query(
            "SELECT * FROM decisions WHERE action='DO_NOT_TRADE' AND ts>=? "
            " ORDER BY ts DESC LIMIT ?", (since, limit))
        done = {r["decision_id"] for r in self.store.query(
            "SELECT decision_id FROM missed_opportunities")}

        out = []
        for d in rows:
            if d["decision_id"] in done or not d["token_id"]:
                continue
            res = self.source.resolution_for(d["token_id"]) \
                if self.source.available else None
            if not res or float(res["price"]) not in (0.0, 1.0):
                continue                       # not yet knowable
            entry = float(d["signal_price"] or 0)
            if entry <= 0:
                continue
            ret = (float(res["price"]) - entry) / entry

            # Would it have been executable at all? A profitable direction we
            # could not have filled is not a missed opportunity, it is a
            # correct rejection, and conflating them teaches the wrong lesson.
            prints = self.source.prints(d["token_id"], int(d["ts"]) + 900,
                                        lookback_secs=900, limit=50)
            executable = bool(prints)
            liquid = sum(n for _, _, n, _ in prints) >= \
                self.st.capital.min_order_usdc / self.st.costs.fill_ratio_assumption

            correct = not (ret > 0.05 and executable and liquid)
            rec = {
                "market_id": d["market_id"], "token_id": d["token_id"],
                "decision_id": d["decision_id"],
                "would_have_returned": round(ret, 5),
                "rejection_gate": d["blocking_gate"] or "",
                "rejection_correct": correct,
                "executable": executable, "liquidity_sufficient": liquid,
                "signal_visible_at_time": True,
                "narrative": self._missed_narrative(d, ret, executable, liquid,
                                                    correct),
                "ts": int(d["ts"])}
            self.store.insert("missed_opportunities", [rec], source="forensics")
            out.append(rec)
        return out

    def _missed_narrative(self, d: dict, ret: float, executable: bool,
                          liquid: bool, correct: bool) -> str:
        gate = d["blocking_gate"] or "no single gate"
        if correct and ret <= 0.05:
            return (f"declined by {gate}; would have returned {ret:+.1%}. "
                    f"Correct rejection.")
        if not executable:
            return (f"declined by {gate}; direction would have returned "
                    f"{ret:+.1%} but nothing printed within 15 minutes, so no "
                    f"fill was available. Correct rejection for the wrong "
                    f"stated reason.")
        if not liquid:
            return (f"declined by {gate}; would have returned {ret:+.1%} but "
                    f"available size was below our minimum order. "
                    f"CAPITAL_INFEASIBLE, not a missed edge.")
        return (f"declined by {gate}; would have returned {ret:+.1%} and was "
                f"both executable and liquid. This is a genuine miss — "
                f"{gate} is a candidate for loosening, subject to what it "
                f"correctly rejected elsewhere.")

    def gate_cost_report(self) -> list:
        """What each gate cost us, and what it saved us.

        The number that decides whether a gate should be loosened. A gate with
        many correct rejections and few misses is earning its place; one with
        the reverse is a tax.
        """
        rows = self.store.query(
            "SELECT rejection_gate gate, "
            "       SUM(CASE WHEN rejection_correct=1 THEN 1 ELSE 0 END) saved, "
            "       SUM(CASE WHEN rejection_correct=0 THEN 1 ELSE 0 END) missed, "
            "       SUM(CASE WHEN rejection_correct=0 "
            "                THEN would_have_returned ELSE 0 END) forgone, "
            "       SUM(CASE WHEN rejection_correct=1 "
            "                THEN -would_have_returned ELSE 0 END) avoided "
            "  FROM missed_opportunities GROUP BY rejection_gate "
            " ORDER BY missed DESC")
        for r in rows:
            n = (r["saved"] or 0) + (r["missed"] or 0)
            r["n"] = n
            r["precision"] = round((r["saved"] or 0) / n, 4) if n else None
            r["forgone"] = round(float(r["forgone"] or 0), 4)
            r["avoided"] = round(float(r["avoided"] or 0), 4)
            r["net"] = round(r["avoided"] - r["forgone"], 4)
            r["verdict"] = ("earning its place" if r["net"] > 0 else
                            "costing more than it saves" if r["net"] < 0
                            else "neutral")
        return rows

    # ------------------------------------------------------- counterfactual
    def counterfactual(self, decision_id: str) -> list:
        """What would other choices have produced?

        Each variant is scored against the same realised outcome, so the
        comparison is fair. Variants that cannot be evaluated are omitted
        rather than scored zero.
        """
        d = self.store.one("SELECT * FROM decisions WHERE decision_id=?",
                           (decision_id,))
        if not d or not d["token_id"] or not self.source.available:
            return []
        res = self.source.resolution_for(d["token_id"])
        if not res or float(res["price"]) not in (0.0, 1.0):
            return []
        outcome = float(res["price"])
        entry = float(d["signal_price"] or 0)
        if entry <= 0:
            return []

        variants: list = []

        def add(name: str, px: float, size: float, note: str) -> None:
            if px <= 0 or size <= 0:
                return
            variants.append({"decision_id": decision_id, "variant": name,
                             "pnl": round(size * (outcome - px) / px, 5),
                             "note": note, "ts": int(d["ts"])})

        traded_size = float(d["size_usdc"] or 0) or self.st.capital.min_order_usdc
        add("as_decided", entry, float(d["size_usdc"] or 0.0),
            "what we actually did" if d["action"] == "TRADE"
            else "we did not trade; this is the counterfactual entry")

        # Timing variants: what a delayed or earlier entry would have paid.
        for delay, label in ((300, "delay_5m"), (1800, "delay_30m"),
                             (3600, "delay_1h")):
            p = self.source.prints(d["token_id"], int(d["ts"]) + delay,
                                   lookback_secs=delay + 60, limit=20)
            later = [x for x in p if x[0] >= int(d["ts"]) + delay]
            if later:
                add(label, later[0][1], traded_size,
                    f"entering {delay}s later at {later[0][1]:.4f}")

        # Sizing variants at the same price.
        for mult, label in ((0.5, "half_size"), (2.0, "double_size")):
            add(label, entry, traded_size * mult,
                f"{mult:g}x the size we chose")

        if variants:
            self.store.insert("counterfactuals", variants, source="forensics")
        return variants


def feature_importance_drift(store, lookback_days: int = 30) -> dict:
    """Which gates and agents are gaining or losing explanatory weight.

    Feeds the LEARNING tab. Computed from what actually blocked decisions and
    what agents actually said, rather than from a model's own feature weights —
    a model's opinion of its features is not evidence about the world.
    """
    now = int(time.time())
    cut = now - lookback_days * 86_400
    prev = cut - lookback_days * 86_400

    def gate_share(a: int, b: int) -> dict:
        rows = store.query(
            "SELECT blocking_gate g, COUNT(*) n FROM decisions "
            " WHERE ts>=? AND ts<? AND blocking_gate!='' GROUP BY g", (a, b))
        total = sum(r["n"] for r in rows) or 1
        return {r["g"]: r["n"] / total for r in rows}

    recent, older = gate_share(cut, now), gate_share(prev, cut)
    keys = set(recent) | set(older)
    rising = sorted(
        ({"gate": k, "recent": round(recent.get(k, 0.0), 4),
          "previous": round(older.get(k, 0.0), 4),
          "delta": round(recent.get(k, 0.0) - older.get(k, 0.0), 4)}
         for k in keys), key=lambda d: -d["delta"])
    return {"window_days": lookback_days,
            "gaining": [r for r in rising if r["delta"] > 0.02][:8],
            "losing": [r for r in rising if r["delta"] < -0.02][-8:],
            "n_gates": len(keys)}
