"""Hidden-structure discovery (§26), with a null model that can win.

    theta* = argmin_theta [ L(data | theta) + lambda * L(theta) ]

Minimum description length. A candidate structure is kept only if describing
the data THROUGH it — plus the cost of describing the structure itself — is
cheaper than describing the data directly. That second term is what stops the
search: a transformation with enough parameters can always fit, and MDL
charges for exactly that.

The discipline that makes this honest is one line long: **the null must be
able to win.** So every candidate is also scored against shuffled data, the
verdict is `NO_ROBUST_STRUCTURE_FOUND` unless the real data beats both the
direct encoding AND the shuffled control, and the number of candidates
examined is reported next to the winner — because the best of forty candidates
is a maximum of forty draws before it is a discovery.

Everything here is descriptive. Nothing in this module can register a model,
raise a research priority, or reach an execution path.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

# How many bits a single real-valued parameter costs to describe. The standard
# MDL convention of (1/2)log2(n) per parameter, applied per candidate.
# Deliberately not tuned: a lambda chosen to make a favourite candidate win is
# the same error as a threshold chosen the same way.
SHUFFLE_TRIALS = 25
MIN_OBSERVATIONS = 100

FOUND = "STRUCTURE_FOUND"
NOT_FOUND = "NO_ROBUST_STRUCTURE_FOUND"


def _entropy_bits(counts: Iterable[int]) -> float:
    """Shannon entropy of a count distribution, in bits."""
    values = [c for c in counts if c > 0]
    total = sum(values)
    if total <= 0:
        return 0.0
    return -sum((c / total) * math.log2(c / total) for c in values)


def _code_length(labels: list) -> float:
    """Bits to encode the labels with no structure — the direct encoding."""
    return len(labels) * _entropy_bits(Counter(labels).values())


def _conditional_code_length(keys: list, labels: list) -> float:
    """Bits to encode the labels GIVEN a partition of the data by `keys`."""
    buckets: dict[Any, list] = defaultdict(list)
    for key, label in zip(keys, labels):
        buckets[key].append(label)
    total = 0.0
    for group in buckets.values():
        total += len(group) * _entropy_bits(Counter(group).values())
    return total


def _model_cost(keys: list, labels: list) -> float:
    """Bits to describe the STRUCTURE itself.

    One distribution per partition cell, each over (classes - 1) free
    parameters, at the usual (1/2)log2(n) bits per parameter. This is the term
    that makes an over-fine partition lose: split the data into singletons and
    the conditional entropy hits zero while this cost explodes.
    """
    cells = len(set(keys))
    classes = max(2, len(set(labels)))
    n = max(2, len(labels))
    return cells * (classes - 1) * 0.5 * math.log2(n)


@dataclass
class Candidate:
    """One structural hypothesis and how it scored."""

    name: str
    description: str
    cells: int = 0
    observations: int = 0
    direct_bits: float = 0.0
    structured_bits: float = 0.0
    model_bits: float = 0.0
    shuffled_gain_mean: float = 0.0
    shuffled_gain_max: float = 0.0
    survived: bool = False
    reason: str = ""

    @property
    def gain(self) -> float:
        """Bits saved. Positive means the structure paid for itself."""
        return self.direct_bits - (self.structured_bits + self.model_bits)

    @property
    def gain_per_observation(self) -> float:
        return (self.gain / self.observations) if self.observations else 0.0

    def to_dict(self) -> dict:
        return {
            "name": self.name, "description": self.description,
            "cells": self.cells, "observations": self.observations,
            "directBits": round(self.direct_bits, 1),
            "structuredBits": round(self.structured_bits, 1),
            "modelBits": round(self.model_bits, 1),
            "gainBits": round(self.gain, 1),
            "gainBitsPerObservation": round(self.gain_per_observation, 5),
            "shuffledGainMean": round(self.shuffled_gain_mean, 1),
            "shuffledGainMax": round(self.shuffled_gain_max, 1),
            "survived": self.survived, "reason": self.reason,
        }


def score_candidate(name: str, description: str, keys: list, labels: list,
                    trials: int = SHUFFLE_TRIALS,
                    seed: int = 20260823) -> Candidate:
    """MDL-score one candidate against the direct encoding and a null.

    The shuffled control is the important half. A partition into many cells
    reduces conditional entropy even on random labels, and without measuring
    how much it does so on noise there is no way to know whether a positive
    gain means anything.
    """
    out = Candidate(name=name, description=description,
                    observations=len(labels), cells=len(set(keys)))
    if len(labels) < MIN_OBSERVATIONS:
        out.reason = (f"{len(labels)} observation(s), below the "
                      f"{MIN_OBSERVATIONS} floor")
        return out
    if out.cells < 2:
        out.reason = "the candidate produced a single cell — no partition"
        return out
    if len(set(labels)) < 2:
        out.reason = "a single label class — nothing to explain"
        return out

    out.direct_bits = _code_length(labels)
    out.structured_bits = _conditional_code_length(keys, labels)
    out.model_bits = _model_cost(keys, labels)

    rng = random.Random(seed)
    gains = []
    for _ in range(trials):
        shuffled = list(labels)
        rng.shuffle(shuffled)
        gains.append(out.direct_bits
                     - (_conditional_code_length(keys, shuffled)
                        + _model_cost(keys, shuffled)))
    out.shuffled_gain_mean = sum(gains) / len(gains)
    out.shuffled_gain_max = max(gains)

    if out.gain <= 0:
        out.reason = ("the structure does not pay for itself: describing the "
                      "data through it costs more than describing it directly")
    elif out.gain <= out.shuffled_gain_max:
        out.reason = ("beaten by the null: at least one shuffle of the same "
                      "labels through the same partition saved as many bits, "
                      "so the gain is what this partition does to noise")
    else:
        out.survived = True
        out.reason = (f"saves {out.gain:.0f} bits over the direct encoding "
                      f"and beats every one of {trials} shuffled controls "
                      f"(best null {out.shuffled_gain_max:.0f})")
    return out


# ---------------------------------------------------------------------------
# The candidate transformations
# ---------------------------------------------------------------------------


def _price_band(value: float) -> str:
    for edge in (0.05, 0.1, 0.2, 0.35, 0.5, 0.65, 0.8, 0.9, 0.95):
        if value <= edge:
            return f"<={edge}"
    return ">0.95"


def _log_band(value: float) -> str:
    if value <= 0:
        return "<=0"
    return f"1e{int(math.floor(math.log10(value)))}"


def _time_band(seconds: float) -> str:
    for edge, label in ((60, "<1m"), (900, "<15m"), (3_600, "<1h"),
                        (86_400, "<1d")):
        if seconds <= edge:
            return label
    return ">1d"


def candidates_for(rows: list) -> list:
    """Every candidate transformation this data can actually support.

    `rows` are dicts with the observable fields. The list is fixed and
    reported in full — including the ones that lost — so the search's scale is
    part of the result rather than something the reader has to take on trust.
    """
    def _keys(fn):
        return [fn(r) for r in rows]

    out = [
        ("initial_price_band", "initial price, banded",
         _keys(lambda r: _price_band(r["initial_price"]))),
        ("initial_capital_magnitude", "initial capital, order of magnitude",
         _keys(lambda r: _log_band(r["initial_capital"]))),
        ("price_x_capital", "price band x capital magnitude (interaction)",
         _keys(lambda r: f"{_price_band(r['initial_price'])}/"
                         f"{_log_band(r['initial_capital'])}")),
        ("hour_of_day_utc", "periodicity: hour of day",
         _keys(lambda r: f"h{int((r['first_buy_ts'] // 3600) % 24)}")),
        ("day_of_week_utc", "periodicity: day of week",
         _keys(lambda r: f"d{int((r['first_buy_ts'] // 86400) % 7)}")),
        ("price_rank_decile", "rank transform: price decile within the pass",
         _rank_deciles([r["initial_price"] for r in rows])),
        ("capital_rank_decile", "rank transform: capital decile",
         _rank_deciles([r["initial_capital"] for r in rows])),
        ("category", "market category (keyword heuristic)",
         _keys(lambda r: r.get("category", "other"))),
        ("wallet_identity", "wallet identity itself",
         _keys(lambda r: r.get("wallet", "")[:12])),
        ("time_to_first_add", "timing: seconds to the first same-side add",
         _keys(lambda r: _time_band(r.get("seconds_to_add", 0.0)))),
        ("same_side_buy_count", "recurrence: number of same-side buys",
         _keys(lambda r: f"n{min(int(r.get('same_side_buys', 0)), 6)}")),
        ("price_modulo_cent", "modular: initial price mod 5 cents",
         _keys(lambda r: f"m{int(round(r['initial_price'] * 100)) % 5}")),
    ]
    return out


def _rank_deciles(values: list) -> list:
    """Decile membership by rank — a scale-free view of the same variable."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    decile = [0] * len(values)
    for position, index in enumerate(order):
        decile[index] = min(9, int(10 * position / max(1, len(values))))
    return [f"q{d}" for d in decile]


@dataclass
class StructureReport:
    """§26's verdict, with the whole search visible."""

    examined: int = 0
    survived: list = field(default_factory=list)
    all_candidates: list = field(default_factory=list)
    observations: int = 0
    verdict: str = NOT_FOUND

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "candidatesExamined": self.examined,
            "candidatesSurvived": len(self.survived),
            "observations": self.observations,
            "survived": [c.to_dict() for c in self.survived],
            "allCandidates": [c.to_dict() for c in self.all_candidates],
            "note": ("MDL: a structure is kept only if describing the labels "
                     "through it, PLUS describing the structure itself, costs "
                     "fewer bits than describing them directly — and only if "
                     "it also beats every shuffled control. "
                     f"{self.examined} candidates were examined; the best of "
                     "N is a maximum of N draws before it is a discovery, "
                     "which is why the full list is reported."),
        }


def discover(rows: list, labels: list, trials: int = SHUFFLE_TRIALS,
             seed: int = 20260823) -> StructureReport:
    """Run the whole search. The null is allowed to win, and usually should."""
    out = StructureReport(observations=len(labels))
    if len(labels) < MIN_OBSERVATIONS:
        out.verdict = NOT_FOUND
        return out
    for name, description, keys in candidates_for(rows):
        out.examined += 1
        candidate = score_candidate(name, description, keys, labels,
                                    trials=trials, seed=seed)
        out.all_candidates.append(candidate)
        if candidate.survived:
            out.survived.append(candidate)
    out.survived.sort(key=lambda c: -c.gain)
    out.all_candidates.sort(key=lambda c: -c.gain)
    out.verdict = FOUND if out.survived else NOT_FOUND
    return out
