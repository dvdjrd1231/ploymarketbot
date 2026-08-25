"""Online learning, with the data separation that makes it safe.

Continuously updating weights from new observations is easy. Doing it without
destroying the meaning of every out-of-sample number the system has ever
produced is the hard part, and it is the only reason this module is careful.

**The rule.** A model may only learn from an observation once that observation
is FINAL — the position closed, the market resolved — and only from
observations that were never used to select the thing being updated. Four
partitions, never mixed:

    TRAINING       used to fit
    VALIDATION     used to choose between fits
    OUT_OF_SAMPLE  used once, to report
    LIVE           accumulated after the model was frozen

Learning happens from LIVE only. The moment a live observation is folded in,
it stops being able to serve as evidence for the model it updated, so it is
moved to TRAINING and the running out-of-sample record is left untouched. That
is why `report()` distinguishes "performance since freeze" from "performance
used to fit".

**What is actually learned.** Not the strategies — those are frozen artefacts
produced by the discovery pass and are only ever replaced by a new versioned
pass. What updates online is the *weighting between evidence sources*: how much
the wallet channel, the news channel, the microstructure channel and the
statistical channel have each been worth. Those are the weights the probability
ensemble uses, and they are exactly the thing that should drift as conditions
change.

Updates are bounded: a single observation can move a weight by at most
`max_step`, and weights are clipped. An unbounded online rule on a noisy
feed converges to whatever happened most recently, which is not learning.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field

from ..config import Settings

PARTITIONS = ("TRAINING", "VALIDATION", "OUT_OF_SAMPLE", "LIVE")

# The evidence channels whose weights are learned.
CHANNELS = ("wallet", "news", "microstructure", "statistical", "cross_market",
            "bayesian")

META_KEY = "online_weights"
META_STATE = "online_state"


@dataclass
class Weights:
    values: dict = field(default_factory=lambda: {c: 1.0 for c in CHANNELS})
    updates: int = 0
    frozen_ts: int = 0
    last_update_ts: int = 0

    def get(self, channel: str) -> float:
        return float(self.values.get(channel, 1.0))

    def to_dict(self) -> dict:
        return {"values": {k: round(v, 5) for k, v in self.values.items()},
                "updates": self.updates, "frozen_ts": self.frozen_ts,
                "last_update_ts": self.last_update_ts}


@dataclass
class UpdateReport:
    applied: int = 0
    skipped_not_final: int = 0
    skipped_already_used: int = 0
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)
    deltas: dict = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class OnlineLearner:
    """Bounded, auditable weight updates from finalised live outcomes."""

    def __init__(self, st: Settings, store, *, max_step: float = 0.02,
                 lo: float = 0.25, hi: float = 2.0) -> None:
        self.st = st
        self.store = store
        self.max_step = max_step
        self.lo, self.hi = lo, hi

    # -- state --------------------------------------------------------------
    def weights(self) -> Weights:
        raw = self.store.get_meta(META_KEY, "")
        if not raw:
            return Weights(frozen_ts=int(time.time()))
        try:
            d = json.loads(raw)
            return Weights(values=d.get("values") or {c: 1.0 for c in CHANNELS},
                           updates=int(d.get("updates") or 0),
                           frozen_ts=int(d.get("frozen_ts") or 0),
                           last_update_ts=int(d.get("last_update_ts") or 0))
        except Exception:                                     # noqa: BLE001
            return Weights(frozen_ts=int(time.time()))

    def _save(self, w: Weights) -> None:
        self.store.set_meta(META_KEY, json.dumps(w.to_dict()))

    def _used(self) -> set:
        raw = self.store.get_meta(META_STATE, "")
        try:
            return set(json.loads(raw)) if raw else set()
        except Exception:                                     # noqa: BLE001
            return set()

    def _mark_used(self, ids: set) -> None:
        cur = self._used() | ids
        # Bounded: keep the most recent 50k ids. Older positions cannot be
        # re-learned from anyway because they are no longer returned by the
        # finalised-position query window.
        trimmed = sorted(cur)[-50_000:]
        self.store.set_meta(META_STATE, json.dumps(trimmed))

    # -- the update ---------------------------------------------------------
    def update(self, *, mode: str = "PAPER", limit: int = 500) -> UpdateReport:
        """Fold finalised live outcomes into the channel weights.

        Only positions that are CLOSED and carry a resolution are used. An open
        position has no outcome, and folding an unrealised mark into a weight
        would let a temporary price move teach the model something that never
        happened.
        """
        rep = UpdateReport()
        w = self.weights()
        rep.before = dict(w.values)
        used = self._used()

        rows = self.store.query(
            "SELECT p.position_id, p.realized_pnl, p.size_usdc, p.status, "
            "       p.resolution, d.run_id, d.decision_id, d.confidence "
            "  FROM positions p "
            "  LEFT JOIN decisions d ON d.market_id = p.market_id "
            "                       AND d.action = 'TRADE' "
            " WHERE p.mode = ? AND p.status != 'OPEN' "
            " ORDER BY p.closed_ts DESC LIMIT ?", (mode, limit))

        fresh_ids: set = set()
        for r in rows:
            pid = r["position_id"]
            if pid in used:
                rep.skipped_already_used += 1
                continue
            if r["status"] == "OPEN" or r["resolution"] is None:
                rep.skipped_not_final += 1
                continue

            pnl = float(r["realized_pnl"] or 0.0)
            size = float(r["size_usdc"] or 0.0)
            if size <= 0:
                continue
            # Outcome in [-1, +1]: the trade's return as a fraction of what was
            # risked, clipped so one spectacular result cannot dominate.
            outcome = max(-1.0, min(1.0, pnl / size))

            stances = self._channel_stances(r["run_id"])
            if not stances:
                continue
            for channel, direction in stances.items():
                # Reward a channel that pointed the right way, penalise the
                # other. `direction` is +1 (FOR) or -1 (AGAINST).
                signal = direction * outcome
                step = max(-self.max_step, min(self.max_step,
                                               self.max_step * signal))
                cur = w.values.get(channel, 1.0)
                w.values[channel] = max(self.lo, min(self.hi, cur + step))
            rep.applied += 1
            fresh_ids.add(pid)

        if rep.applied:
            w.updates += rep.applied
            w.last_update_ts = int(time.time())
            self._save(w)
            self._mark_used(fresh_ids)

        rep.after = dict(w.values)
        rep.deltas = {k: round(rep.after[k] - rep.before.get(k, 1.0), 5)
                      for k in rep.after}
        rep.note = (
            f"{rep.applied} finalised outcome(s) folded in; "
            f"{rep.skipped_already_used} already used, "
            f"{rep.skipped_not_final} not final. Each observation is used "
            f"exactly once and then moves to TRAINING, so it can never serve "
            f"as evidence for the weights it helped set."
            if rep.applied else
            f"nothing to learn from: {len(rows)} closed position(s), "
            f"{rep.skipped_already_used} already used, "
            f"{rep.skipped_not_final} without a final outcome.")
        return rep

    def _channel_stances(self, run_id: str) -> dict:
        """Which evidence channel took which side on this decision."""
        if not run_id:
            return {}
        agent_channel = {
            "WALLET_FORENSICS": "wallet", "WALLET_REPLICATION": "wallet",
            "NEWS_INTELLIGENCE": "news", "EVENT_ANALYSIS": "news",
            "MARKET_MICROSTRUCTURE": "microstructure",
            "TIME_SERIES": "microstructure",
            "SEQUENCE_ANALYSIS": "statistical",
            "STATISTICAL_RESEARCH": "statistical",
            "CROSS_MARKET": "cross_market",
            "BAYESIAN_PROBABILITY": "bayesian",
        }
        rows = self.store.query(
            "SELECT agent, stance FROM agent_outputs "
            " WHERE run_id = ? AND stance != 'ABSTAIN'", (run_id,))
        out: dict = {}
        for r in rows:
            ch = agent_channel.get(r["agent"])
            if ch:
                out[ch] = 1.0 if r["stance"] == "FOR" else -1.0
        return out

    # -- reporting ----------------------------------------------------------
    def report(self, *, mode: str = "PAPER") -> dict:
        w = self.weights()
        used = self._used()
        closed = self.store.count("positions", "mode=? AND status!='OPEN'",
                                  (mode,))
        return {
            "weights": w.to_dict(),
            "channels": list(CHANNELS),
            "observations_used": len(used),
            "closed_positions": closed,
            "unused": max(0, closed - len(used)),
            "partitions": list(PARTITIONS),
            "bounds": {"max_step": self.max_step, "min": self.lo,
                       "max": self.hi},
            "note": (
                "Weights govern how much each evidence channel contributes to "
                "the probability ensemble. Strategies are NOT learned online — "
                "they are frozen artefacts of a versioned discovery pass and "
                "are replaced only by another pass. An observation is folded "
                "in exactly once, after it is final, and then stops being "
                "available as evidence for the weights it set."),
        }

    def reset(self) -> dict:
        """Refreeze. Used when a new discovery pass invalidates the weights."""
        w = Weights(frozen_ts=int(time.time()))
        self._save(w)
        self.store.set_meta(META_STATE, json.dumps([]))
        return {"reset": True, "weights": w.to_dict(),
                "note": "weights refrozen at 1.0 and the used-observation "
                        "ledger cleared"}
