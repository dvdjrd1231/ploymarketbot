"""The LEAN bridge (§56, §57) — a translation layer that decides nothing.

The existing engine must be able to consume this research without knowing how
it was computed, and without depending on the RN1 project's web/Vercel
implementation. So the adapter takes a `WalletStateSignalResult` and returns a
flat feature object with named fields, and that is the entire contract.

Three properties, all structural rather than promised:

* **Disabled returns nothing.** `get_alpha_features` calls the same
  `signal.get_signal` gate, so `enabled: false` — or `enabled: true` with
  `integration_enabled: false` — yields `NO_ALPHA_FEATURES`, a frozen
  singleton whose every value is None.
* **Nothing is actionable.** There is no side, size, weight, order or
  direction in the structure. A consumer that wanted to trade on it would
  have to invent the decision itself, which is exactly where that decision
  belongs.
* **Every value carries its provenance.** `information_cutoff_timestamp`
  travels with the features, so a consumer can assert for itself that it is
  not reading tomorrow's news.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .signal import (HIGH, LOW, MEDIUM, NO_SIGNAL, STAGE_INFLUENCE,
                     WalletStateSignalResult, get_signal)

_CONFIDENCE_SCORE = {LOW: 0.25, MEDIUM: 0.60, HIGH: 1.00}


@dataclass(frozen=True)
class WalletStateAlphaFeatures:
    """§56's feature object. Immutable: a feature vector is an observation.

    Every field is Optional and defaults to None rather than 0.0. A zero
    probability and an unknown probability lead to opposite decisions, and a
    consumer that cannot tell them apart will eventually act on a hole in the
    data as though it were a reading.
    """

    aggressive_probability: Optional[float] = None
    protect_probability: Optional[float] = None
    directional_probability: Optional[float] = None
    wallet_state_confidence: Optional[float] = None
    inventory_ratio: Optional[float] = None
    opposite_transition_probability: Optional[float] = None
    wallet_historical_state_score: Optional[float] = None
    market_liquidity_score: Optional[float] = None
    execution_quality_score: Optional[float] = None
    data_quality_score: Optional[float] = None
    model_version: str = ""
    signal_timestamp: Optional[float] = None
    information_cutoff_timestamp: Optional[float] = None
    # Present so a consumer never has to infer it from the values.
    available: bool = False
    stage: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "AggressiveProbability": self.aggressive_probability,
            "ProtectProbability": self.protect_probability,
            "DirectionalProbability": self.directional_probability,
            "WalletStateConfidence": self.wallet_state_confidence,
            "InventoryRatio": self.inventory_ratio,
            "OppositeTransitionProbability":
                self.opposite_transition_probability,
            "WalletHistoricalStateScore": self.wallet_historical_state_score,
            "MarketLiquidityScore": self.market_liquidity_score,
            "ExecutionQualityScore": self.execution_quality_score,
            "DataQualityScore": self.data_quality_score,
            "ModelVersion": self.model_version,
            "SignalTimestamp": self.signal_timestamp,
            "InformationCutoffTimestamp": self.information_cutoff_timestamp,
            "Available": self.available, "Stage": self.stage,
            "Reason": self.reason,
        }


NO_ALPHA_FEATURES = WalletStateAlphaFeatures(
    available=False,
    reason="wallet state transition research is disabled or not integrated")


_QUALITY_SCORE = {"complete": 1.0, "partial": 0.5, "insufficient": 0.0,
                  "unknown": 0.0}
_LIQUIDITY_SCORE = {"good": 1.0, "book": 0.8, "print-only": 0.3,
                    "unknown": 0.0}
_EXECUTION_SCORE = {"book-priced": 1.0,
                    "print-priced (no observed book; spread assumed)": 0.4,
                    "unknown": 0.0}


def from_signal(result: WalletStateSignalResult) -> WalletStateAlphaFeatures:
    """Translate a signal result. Pure; no configuration, no I/O."""
    if result is None or result is NO_SIGNAL or not result.opposite_buy_detected:
        return NO_ALPHA_FEATURES
    aggressive = result.aggressive_probability
    protect = result.protect_probability
    # DIRECTIONAL is not emitted by the two-class post-opposite model: by the
    # time that signal exists the wallet is already two-sided, so directional
    # is not a live possibility. None, not zero — the difference is the whole
    # reason these fields are Optional.
    directional = None
    return WalletStateAlphaFeatures(
        aggressive_probability=aggressive,
        protect_probability=protect,
        directional_probability=directional,
        wallet_state_confidence=_CONFIDENCE_SCORE.get(result.confidence),
        inventory_ratio=result.inventory_ratio,
        # The signal fires ON the transition, so the probability that it
        # happens is 1 by observation rather than by prediction. Stated as
        # such rather than dressed up as a model output.
        opposite_transition_probability=1.0,
        wallet_historical_state_score=(
            float(result.sample_size) if result.sample_size else None),
        market_liquidity_score=_LIQUIDITY_SCORE.get(result.liquidity_quality),
        execution_quality_score=_EXECUTION_SCORE.get(result.execution_quality),
        data_quality_score=_QUALITY_SCORE.get(result.data_quality),
        model_version=result.model_version,
        signal_timestamp=result.timestamp,
        information_cutoff_timestamp=result.timestamp,
        available=True, stage=result.stage, reason=result.reason)


def from_v1_prediction(prediction: Any, wallet: str = "",
                       market: str = "", stage: str = "") -> \
        WalletStateAlphaFeatures:
    """Translate a frozen Strategy Model V1 prediction.

    V1 fires at the first BUY, so there is no inventory ratio yet and no
    observed transition — both are None rather than zero, and
    `opposite_transition_probability` is left None because V1 predicts the
    eventual MODE, not whether a transition occurs.
    """
    if prediction is None or not getattr(prediction, "valid", False):
        return NO_ALPHA_FEATURES
    probabilities = getattr(prediction, "probabilities", {}) or {}
    return WalletStateAlphaFeatures(
        aggressive_probability=probabilities.get("AGGRESSIVE_OPPOSITE"),
        protect_probability=probabilities.get("PROTECT_REBALANCE"),
        directional_probability=probabilities.get("DIRECTIONAL"),
        # A rule emits certainty, not calibrated confidence. Reporting 1.0
        # here would claim a calibration nothing has measured.
        wallet_state_confidence=None,
        inventory_ratio=None,
        opposite_transition_probability=None,
        model_version=getattr(prediction, "model_version", ""),
        signal_timestamp=getattr(prediction, "prediction_ts", None),
        information_cutoff_timestamp=getattr(
            prediction, "information_cutoff_ts", None),
        available=True, stage=stage,
        reason=getattr(prediction, "reason", ""))


def get_alpha_features(config: Any = None, episode: Any = None,
                       horizon_minutes: float = 3.0,
                       evidence: Optional[dict] = None,
                       quote: Optional[Any] = None,
                       model: Optional[Any] = None
                       ) -> WalletStateAlphaFeatures:
    """The single call the existing engine may make. Off by default.

    Goes through `signal.get_signal`, so the two-flag gate is enforced in one
    place rather than duplicated here — a second copy of a safety check is a
    second thing that can be wrong.
    """
    result = get_signal(config=config, episode=episode,
                        horizon_minutes=horizon_minutes, evidence=evidence,
                        quote=quote, model=model)
    if result is NO_SIGNAL:
        return NO_ALPHA_FEATURES
    return from_signal(result)


def may_influence(features: WalletStateAlphaFeatures) -> bool:
    """Whether a consumer is permitted to let these change a decision.

    Always False below the `influence` stage, whatever the probabilities say.
    Exposed as a function so a consumer asks rather than remembers, and so the
    answer lives beside the features instead of in the caller's discipline.
    """
    return bool(features.available and features.stage == STAGE_INFLUENCE)
