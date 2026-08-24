"""The signal API and the one entry point the existing engine may call.

Parts 16-19 and 26 in one file. Three properties matter more than the fields:

* **Disabled means disabled.** `get_signal` checks configuration first and
  returns `NO_SIGNAL` before touching a database, a model or a clock. With
  `enabled: false` the function is a config read and a constant return, so it
  cannot change any decision, cost any latency, or raise.
* **The result is inert.** `WalletStateSignalResult` is a dataclass of numbers
  and strings. It carries no action, no size, no direction, and nothing in it
  is wired to an engine — Part 26's stage ladder is enforced by there being
  nothing to promote, not by a flag somebody could flip.
* **Confidence is measured, not asserted.** `tier` is derived from sample
  size, distance from the decision boundary, data provenance and liquidity.
  It is explicitly NOT a claim that HIGH is profitable; it is a statement
  about how much evidence stands behind this particular case.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .episodes import AGGRESSIVE, PROTECT

LOW, MEDIUM, HIGH = "LOW", "MEDIUM", "HIGH"

# The stage ladder (Part 26). The default is the first rung and nothing in
# this package advances it — only a person editing configuration can.
STAGE_RESEARCH_ONLY = "research_only"
STAGE_BACKTEST = "backtest"
STAGE_WALK_FORWARD = "walk_forward"
STAGE_PAPER = "paper"
STAGE_OBSERVE = "observe"          # exposed as a feature, influences nothing
STAGE_INFLUENCE = "influence"      # requires explicit human approval
STAGES = (STAGE_RESEARCH_ONLY, STAGE_BACKTEST, STAGE_WALK_FORWARD,
          STAGE_PAPER, STAGE_OBSERVE, STAGE_INFLUENCE)


@dataclass
class WalletStateSignalResult:
    """Part 18's structure. Inert by construction.

    Every consumer-facing number is Optional and defaults to None rather than
    0.0. A zero probability and an unknown probability lead to opposite
    decisions, and a consumer that cannot tell them apart will eventually act
    on a hole in the data as though it were a reading.
    """

    wallet: str = ""
    market: str = ""
    timestamp: float = 0.0
    opposite_buy_detected: bool = False
    minutes_since_opposite_buy: Optional[float] = None
    inventory_ratio: Optional[float] = None
    shares_needed_opposite_to_zero: Optional[float] = None
    aggressive_probability: Optional[float] = None
    protect_probability: Optional[float] = None
    frozen_rn1_prediction: str = ""
    generalized_prediction: str = ""
    confidence: str = LOW
    sample_size: int = 0
    expected_value_estimate: Optional[float] = None
    data_quality: str = "unknown"
    liquidity_quality: str = "unknown"
    execution_quality: str = "unknown"
    model_version: str = ""
    stage: str = STAGE_RESEARCH_ONLY
    # Why the consumer is seeing this, in one sentence. Present even on
    # NO_SIGNAL, so "the module returned nothing" is always explained.
    reason: str = ""
    notes: list = field(default_factory=list)

    @property
    def actionable(self) -> bool:
        """Always False below the `influence` stage.

        The property exists so a consumer can ask, and it answers with the
        stage rather than with the prediction — a HIGH-confidence AGGRESSIVE
        signal at `research_only` is still not actionable, and the object says
        so itself rather than relying on the caller to remember.
        """
        return (self.stage == STAGE_INFLUENCE
                and self.generalized_prediction == AGGRESSIVE
                and self.confidence in (MEDIUM, HIGH))

    def to_dict(self) -> dict:
        return {
            "wallet": self.wallet, "market": self.market,
            "timestamp": self.timestamp,
            "oppositeBuyDetected": self.opposite_buy_detected,
            "minutesSinceOppositeBuy": self.minutes_since_opposite_buy,
            "inventoryRatio": self.inventory_ratio,
            "sharesNeededOppositeToZero": self.shares_needed_opposite_to_zero,
            "aggressiveProbability": self.aggressive_probability,
            "protectProbability": self.protect_probability,
            "frozenRN1Prediction": self.frozen_rn1_prediction,
            "generalizedPrediction": self.generalized_prediction,
            "confidence": self.confidence, "sampleSize": self.sample_size,
            "expectedValueEstimate": self.expected_value_estimate,
            "dataQuality": self.data_quality,
            "liquidityQuality": self.liquidity_quality,
            "executionQuality": self.execution_quality,
            "modelVersion": self.model_version, "stage": self.stage,
            "actionable": self.actionable,
            "reason": self.reason, "notes": list(self.notes),
        }


NO_SIGNAL = WalletStateSignalResult(
    reason="wallet state transition research is disabled")


def get_signal(config: Any = None, episode: Any = None,
               horizon_minutes: float = 3.0,
               evidence: Optional[dict] = None,
               quote: Optional[Any] = None,
               model: Optional[Any] = None) -> WalletStateSignalResult:
    """THE entry point. Returns `NO_SIGNAL` unless deliberately enabled.

    The disabled path is the first three lines and touches nothing else: no
    import of a model, no database handle, no clock. That is what makes
    "`enabled: false` produces zero change" a structural fact rather than a
    test result.
    """
    settings = _settings(config)
    if not settings.get("enabled"):
        return NO_SIGNAL
    if episode is None or not getattr(episode, "switched", False):
        return WalletStateSignalResult(
            stage=settings.get("stage", STAGE_RESEARCH_ONLY),
            reason="no opposite-side buy observed for this wallet in this "
                   "market")

    from .classifier import FrozenRN1

    snapshot = episode.snapshot(horizon_minutes)
    out = WalletStateSignalResult(
        wallet=getattr(episode, "wallet", ""),
        market=getattr(episode, "market_id", ""),
        timestamp=snapshot.ts,
        opposite_buy_detected=True,
        minutes_since_opposite_buy=horizon_minutes,
        stage=settings.get("stage", STAGE_RESEARCH_ONLY))

    if not snapshot.valid:
        out.reason = snapshot.invalid_reason or "snapshot unusable"
        out.data_quality = "insufficient"
        return out

    out.inventory_ratio = snapshot.inventory_ratio
    out.shares_needed_opposite_to_zero = \
        snapshot.shares_needed_opposite_to_zero()

    frozen = FrozenRN1().predict(snapshot)
    out.frozen_rn1_prediction = frozen.label
    out.model_version = frozen.model_version

    prediction = frozen
    if model is not None:
        candidate = model.predict(snapshot, None)
        if candidate.valid:
            prediction = candidate
            out.model_version = candidate.model_version
    out.generalized_prediction = prediction.label
    out.aggressive_probability = prediction.aggressive_probability
    out.protect_probability = prediction.protect_probability
    out.reason = prediction.reason

    out.data_quality = _data_quality(snapshot)
    out.liquidity_quality, out.execution_quality = _market_quality(quote)
    evidence = evidence or {}
    out.sample_size = int(evidence.get("sampleSize") or 0)
    out.expected_value_estimate = evidence.get("expectedValue")
    out.confidence = confidence_tier(
        margin=prediction.margin, sample_size=out.sample_size,
        data_quality=out.data_quality, liquidity_quality=out.liquidity_quality,
        out_of_sample_supported=bool(evidence.get("outOfSampleSupported")))
    if out.stage != STAGE_INFLUENCE:
        out.notes.append(
            f"stage '{out.stage}': this result is informational and cannot "
            "influence any trading decision")
    if not evidence:
        out.notes.append(
            "no measured out-of-sample evidence was supplied for this wallet, "
            "so confidence is capped at LOW regardless of the prediction")
    return out


def confidence_tier(margin: float, sample_size: int, data_quality: str,
                    liquidity_quality: str,
                    out_of_sample_supported: bool) -> str:
    """Part 19. Tiers describe EVIDENCE, not expected profit.

    Nothing here encodes "HIGH is profitable". A HIGH signal is one whose
    wallet has a real out-of-sample record, whose case sits well away from the
    decision boundary, and whose price came from a real book. Whether that
    combination makes money is Question B's answer, and it is measured
    elsewhere.
    """
    if not out_of_sample_supported or sample_size < 12:
        return LOW
    if data_quality == "insufficient" or liquidity_quality == "unknown":
        return LOW
    score = 0
    score += 2 if sample_size >= 50 else 1
    score += 2 if margin >= 0.25 else (1 if margin >= 0.10 else 0)
    score += 1 if data_quality == "complete" else 0
    score += 1 if liquidity_quality in ("good", "book") else 0
    if score >= 5:
        return HIGH
    if score >= 3:
        return MEDIUM
    return LOW


def _data_quality(snapshot) -> str:
    if not snapshot.valid:
        return "insufficient"
    if snapshot.shares_needed_opposite_to_zero() is None:
        return "partial"
    if snapshot.events_used < 2:
        return "partial"
    return "complete"


def _market_quality(quote) -> tuple[str, str]:
    if quote is None or not getattr(quote, "available", False):
        return "unknown", "unknown"
    source = getattr(quote, "source", "")
    if source == "book":
        depth = getattr(quote, "depth", None)
        return ("good" if depth else "book"), "book-priced"
    return "print-only", "print-priced (no observed book; spread assumed)"


def _settings(config: Any) -> dict:
    """Read the module's own settings, defensively.

    Any shape is accepted — a config object, a plain dict, or None — and every
    failure mode resolves to DISABLED. A research module that turns itself on
    because a config key was spelled differently is the exact failure Part 30
    is about.
    """
    if config is None:
        return {"enabled": False}
    node = getattr(config, "wallet_state_research", None)
    if node is None and isinstance(config, dict):
        node = config.get("wallet_state_research")
    if node is None:
        return {"enabled": False}
    if isinstance(node, dict):
        return {"enabled": bool(node.get("enabled", False)),
                "stage": str(node.get("stage", STAGE_RESEARCH_ONLY))}
    enabled = bool(getattr(node, "enabled", False))
    integration = bool(getattr(node, "integration_enabled", False))
    stage = str(getattr(node, "stage", STAGE_RESEARCH_ONLY))
    # Belt and braces: the signal path additionally requires
    # `integration_enabled`. `enabled` alone turns the RESEARCH on; making a
    # signal reachable is a second, deliberate decision — the same two-flag
    # discipline that guards live trading in this codebase.
    return {"enabled": enabled and integration, "stage": stage}


def generate(episodes, horizon_minutes: float, config: Any = None,
             evidence_by_wallet: Optional[dict] = None,
             model: Optional[Any] = None) -> list:
    """Batch form, for research and for the observation-mode feature dump."""
    evidence_by_wallet = evidence_by_wallet or {}
    out = []
    for episode in episodes:
        if not getattr(episode, "switched", False):
            continue
        out.append(get_signal(
            config=config, episode=episode, horizon_minutes=horizon_minutes,
            evidence=evidence_by_wallet.get(episode.wallet), model=model))
    return out


def feature_row(signal: WalletStateSignalResult) -> dict:
    """Part 17's named features, for OBSERVATION only.

    Prefixed `wallet_` and emitted as a flat dict so the existing feature
    vector can carry them without knowing anything about this package. They
    are informational columns: nothing in the engine reads them today, and
    Part 26 says nothing should until a person decides otherwise.
    """
    return {
        "wallet_opposite_buy_detected": 1.0 if signal.opposite_buy_detected
        else 0.0,
        "wallet_time_since_opposite_buy": signal.minutes_since_opposite_buy,
        "wallet_inventory_ratio": signal.inventory_ratio,
        "wallet_shares_needed_to_zero": signal.shares_needed_opposite_to_zero,
        "wallet_aggressive_probability": signal.aggressive_probability,
        "wallet_protect_probability": signal.protect_probability,
        "wallet_behavior_confidence": {LOW: 0.25, MEDIUM: 0.6,
                                       HIGH: 1.0}.get(signal.confidence, 0.0),
        "wallet_signal_sample_size": float(signal.sample_size),
        "wallet_signal_quality": {"complete": 1.0, "partial": 0.5,
                                  "insufficient": 0.0,
                                  "unknown": 0.0}.get(signal.data_quality, 0.0),
    }
