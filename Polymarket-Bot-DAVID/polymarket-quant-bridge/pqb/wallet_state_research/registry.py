"""The model registry, the historical benchmarks, and the quarantine (§46).

Three jobs, and the third is the one that matters most.

1. **Provenance.** Every frozen model carries the window it was fitted on, the
   window it was validated on, the holdout it has not seen, its thresholds,
   its label definition, its universe and its prospective boundary. A number
   quoted without those is not a result, it is a rumour.

2. **Immutability.** A registered version is never overwritten. `register`
   raises on a collision rather than replacing, so "V2 beat V1" cannot quietly
   become "V1 was edited until it lost".

3. **Quarantine.** The handoff is explicit that a contaminated forward
   validator produced ~40.48% by replaying old conditions as fresh
   predictions, and that this must never appear in a performance table again.
   Deleting it would lose the lesson; leaving it loose invites its return. So
   it is registered as a first-class QUARANTINED record whose metrics are not
   readable through the normal path — `metrics()` raises for it, by name, with
   the reason.

Candidate A and Candidate B are registered the same way, as SOURCE BENCHMARKS:
real prior findings, preserved verbatim, never merged into a training set and
never reproduced automatically.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

STATUS_FROZEN = "FROZEN"
STATUS_SOURCE_BENCHMARK = "SOURCE_BENCHMARK"
STATUS_DISCOVERY = "DISCOVERY"
STATUS_QUARANTINED = "QUARANTINED"


class ContaminatedResultError(RuntimeError):
    """Raised when quarantined metrics are requested. Loud on purpose."""


@dataclass
class ModelRecord:
    """One versioned model, with everything §46 asks to be recorded."""

    version: str
    name: str = ""
    status: str = STATUS_FROZEN
    created_ts: float = field(default_factory=time.time)
    training_window: str = ""
    validation_window: str = ""
    holdout_window: str = ""
    features: tuple = ()
    thresholds: dict = field(default_factory=dict)
    hyperparameters: dict = field(default_factory=dict)
    wallet_universe: str = ""
    market_universe: str = ""
    data_version: str = ""
    execution_version: str = ""
    label_definition: str = ""
    random_seed: Optional[int] = None
    prospective_boundary: str = ""
    source_metrics: dict = field(default_factory=dict)
    quarantine_reason: str = ""
    notes: str = ""

    @property
    def quarantined(self) -> bool:
        return self.status == STATUS_QUARANTINED

    def metrics(self) -> dict:
        """The model's reported metrics. Raises for quarantined records."""
        if self.quarantined:
            raise ContaminatedResultError(
                f"{self.version} is QUARANTINED and its metrics must never be "
                f"reported as performance. {self.quarantine_reason}")
        return dict(self.source_metrics)

    def to_dict(self, include_quarantined_metrics: bool = False) -> dict:
        out = {
            "version": self.version, "name": self.name,
            "status": self.status, "createdTs": self.created_ts,
            "trainingWindow": self.training_window,
            "validationWindow": self.validation_window,
            "holdoutWindow": self.holdout_window,
            "features": list(self.features),
            "thresholds": dict(self.thresholds),
            "hyperparameters": dict(self.hyperparameters),
            "walletUniverse": self.wallet_universe,
            "marketUniverse": self.market_universe,
            "dataVersion": self.data_version,
            "executionVersion": self.execution_version,
            "labelDefinition": self.label_definition,
            "randomSeed": self.random_seed,
            "prospectiveBoundary": self.prospective_boundary,
            "notes": self.notes,
        }
        if self.quarantined:
            out["quarantineReason"] = self.quarantine_reason
            out["metrics"] = "WITHHELD — quarantined"
            if include_quarantined_metrics:
                # Only ever for an audit of what was excluded, and labelled
                # as such at every level so it cannot be copied into a table
                # by accident.
                out["withheldMetricsForAuditOnly"] = dict(self.source_metrics)
        else:
            out["metrics"] = dict(self.source_metrics)
        return out


class ModelRegistry:
    """Versioned models. Append-only."""

    def __init__(self) -> None:
        self._records: dict[str, ModelRecord] = {}

    def register(self, record: ModelRecord) -> ModelRecord:
        if record.version in self._records:
            raise ValueError(
                f"{record.version} is already registered. A historical model "
                "version is never overwritten (§46) — register the new model "
                "under a new version and report them side by side.")
        self._records[record.version] = record
        return record

    def get(self, version: str) -> ModelRecord:
        if version not in self._records:
            raise KeyError(f"unknown model version: {version}")
        return self._records[version]

    def versions(self) -> list:
        return sorted(self._records)

    def by_status(self, status: str) -> list:
        return [r for r in self._records.values() if r.status == status]

    def usable(self) -> list:
        """Everything that may appear in a performance table."""
        return [r for r in self._records.values() if not r.quarantined]

    def to_dict(self) -> dict:
        return {
            "models": [r.to_dict() for r in
                       sorted(self._records.values(), key=lambda r: r.version)],
            "quarantined": [r.version for r in
                            self.by_status(STATUS_QUARANTINED)],
            "note": ("A quarantined record's metrics raise on access rather "
                     "than returning a number. The record is kept so the "
                     "lesson survives; the number is unreachable so it cannot "
                     "return to a table."),
        }


def default_registry() -> ModelRegistry:
    """The registry as the handoff defines it."""
    from .classifier import (RN1_INVENTORY_RATIO_THRESHOLD,
                             RN1_SHARES_NEEDED_THRESHOLD)
    from .episodes import LABEL_RATIO_BOUNDARY
    from .strategy_v1 import (PROSPECTIVE_BOUNDARY_UTC,
                              V1_AGGRESSIVE_CAPITAL_MIN,
                              V1_AGGRESSIVE_PRICE_MAX,
                              V1_DIRECTIONAL_PRICE_MIN, V1_MODEL_VERSION)

    registry = ModelRegistry()
    label_definition = (
        f"final oppositeShares/originalShares >= {LABEL_RATIO_BOUNDARY} => "
        "AGGRESSIVE_OPPOSITE; two-sided below it => PROTECT_REBALANCE; "
        "effectively one-sided => DIRECTIONAL. A research labelling boundary "
        "only — no claim that any wallet uses 1.40.")

    registry.register(ModelRecord(
        version=V1_MODEL_VERSION,
        name="RN1 Strategy Model V1 — frozen entry-time three-state rule",
        status=STATUS_FROZEN,
        features=("initialPrice", "initialObservedCapital"),
        thresholds={"aggressivePriceMax": V1_AGGRESSIVE_PRICE_MAX,
                    "aggressiveCapitalMin": V1_AGGRESSIVE_CAPITAL_MIN,
                    "directionalPriceMin": V1_DIRECTIONAL_PRICE_MIN},
        label_definition=label_definition,
        prospective_boundary=PROSPECTIVE_BOUNDARY_UTC.isoformat(),
        training_window="none — the rule was frozen by the source project, "
                        "not fitted here",
        wallet_universe="any", market_universe="any",
        notes=("THE PRIMARY MODEL. Predicts at the first BUY, before any "
               "two-sided behaviour exists. Never retuned here."),
        source_metrics={
            "retrospective250Conditions": {
                "DIRECTIONAL": 0.208, "PROTECT_REBALANCE": 0.584,
                "AGGRESSIVE_OPPOSITE": 0.208,
                "note": "SOURCE distribution. A benchmark, never an expected "
                        "outcome on new data."},
            "forwardExperimentSnapshot": {
                "predictions": 19, "target": 50, "resolved": 0,
                "note": "A source snapshot at one checkpoint. The live "
                        "database is authoritative for current progress."}}))

    registry.register(ModelRecord(
        version="RN1_FROZEN_POST_OPPOSITE_3M_V1",
        name="Post-opposite-buy +3m two-threshold rule",
        status=STATUS_SOURCE_BENCHMARK,
        features=("inventoryRatio", "sharesNeededOppositeToZero"),
        thresholds={"inventoryRatio": RN1_INVENTORY_RATIO_THRESHOLD,
                    "sharesNeededOppositeToZero":
                        RN1_SHARES_NEEDED_THRESHOLD,
                    "horizonMinutes": 3.0},
        label_definition=label_definition,
        notes=("SUPPORTING benchmark, superseded as the primary model by "
               "Strategy Model V1. Answers a different and easier question: "
               "it fires three minutes AFTER the opposite-side buy has "
               "already happened, so it is scored on a two-class population "
               "and cannot be compared with V1's three-class numbers "
               "directly."),
        source_metrics={"holdoutConditions": 235, "holdoutAccuracy": 0.7489,
                        "holdoutBalancedAccuracy": 0.7301,
                        "prospectiveCases": 50, "prospectiveAccuracy": 0.78,
                        "prospectiveBalancedAccuracy": 0.7624}))

    registry.register(ModelRecord(
        version="CANDIDATE_A1_60_79_SINGLE_BUY_3M",
        name="Candidate A — 60-79c single buy, minute-3 clean state",
        status=STATUS_SOURCE_BENCHMARK,
        thresholds={"priceLow": 0.60, "priceHigh": 0.79, "quietMinutes": 3},
        notes=("Historical supporting evidence, NOT the current mission. Its "
               "numbers are forensic/observer evidence and are not equivalent "
               "to executable trading P&L (§23). Never merged into the V1 "
               "training or prospective sample."),
        source_metrics={"qualified": 25, "settled": 9, "record": "7-2",
                        "settledWinRate": 0.7778, "observedReturn": 0.2362,
                        "laterSwitched": 13, "laterSwitchRate": 0.52}))

    registry.register(ModelRecord(
        version="CANDIDATE_B_60_79_PERSIST_30M",
        name="Candidate B — 60-79c, one-sided through minute 30",
        status=STATUS_SOURCE_BENCHMARK,
        thresholds={"priceLow": 0.60, "priceHigh": 0.79,
                    "cleanStateMinute": 3, "persistThroughMinute": 30},
        notes=("The EXECUTION benchmark (§22). Its value here is the lesson "
               "rather than the number: a strong state-selection result "
               "produced much weaker economics once execution was charged, "
               "which is why this package keeps the state signal, the "
               "execution model and the P&L accounting in separate files."),
        source_metrics={"settledExecutions": 53, "record": "40-13",
                        "winRate": 0.7547, "simulatedReturn": 0.0426,
                        "simulatedPnlUsd": 11.29, "fillRate": 0.8967}))

    registry.register(ModelRecord(
        version="RN1_V1_FORWARD_VALIDATOR_CONTAMINATED",
        name="Contaminated forward validator run — EXCLUDED PERMANENTLY",
        status=STATUS_QUARANTINED,
        quarantine_reason=(
            "It replayed OLD historical conditions as though they were fresh "
            "predictions, which is why 42 of 50 'predictions' resolved "
            "immediately. Its ~40.48% is not forward performance and must "
            "never enter model accuracy, benchmark tables, training labels, "
            "validation metrics, dashboards or strategy selection (§52). The "
            "record is kept so the failure mode stays visible; the metrics "
            "are unreachable so they cannot come back."),
        source_metrics={"predictions": 50, "immediatelyResolved": 42,
                        "apparentAccuracy": 0.4048},
        notes="Registered to be excluded, not to be consulted."))
    return registry
