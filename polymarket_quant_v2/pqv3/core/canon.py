"""Canonical objects. The vocabulary every layer downstream agrees on.

`EvidenceState` is the load-bearing one. It is the *only* thing an agent, a
probability estimator, a gate or the scanner is allowed to read when judging an
opportunity. Everything else — a database handle, a live HTTP client, the
current wall clock — is deliberately absent from it.

That restriction is what makes backtesting honest. A component that can only
see an `EvidenceState` cannot reach past its `as_of` timestamp, because there
is no API on it that returns the future. Look-ahead becomes a thing you would
have to work to introduce rather than a thing you have to remember to avoid.

Each sub-state carries its own `available` flag and `as_of`. A layer with no
data reports `available=False` rather than zeros — a zero spread reads as
"measured, and tight", which would justify an execution gate passing on
evidence that does not exist. V2 learned this the expensive way with
`DepthState.UNKNOWN`; V3 makes it the default for every layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Availability(str, Enum):
    OK = "OK"
    STALE = "STALE"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_CONFIGURED = "NOT_CONFIGURED"

    @property
    def usable(self) -> bool:
        return self is Availability.OK


@dataclass
class Layer:
    """One slice of the information environment at a point in time."""

    name: str
    availability: Availability = Availability.UNAVAILABLE
    as_of: int = 0
    age_secs: int = -1
    rows: int = 0
    history_days: float = 0.0
    data: dict = field(default_factory=dict)
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.availability is Availability.OK

    def get(self, key: str, default=None):
        return self.data.get(key, default)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["availability"] = self.availability.value
        return d


@dataclass
class EvidenceState:
    """The canonical point-in-time information environment.

    Constructed only by `pit.get_information_state`. Never mutated after
    construction by a consumer — agents receive the same object and must not be
    able to influence each other by writing to it.
    """

    as_of: int
    market_id: str = ""
    token_id: str = ""

    market: Layer = field(default_factory=lambda: Layer("market"))
    order_book: Layer = field(default_factory=lambda: Layer("order_book"))
    price: Layer = field(default_factory=lambda: Layer("price"))
    volume: Layer = field(default_factory=lambda: Layer("volume"))
    liquidity: Layer = field(default_factory=lambda: Layer("liquidity"))

    wallets: Layer = field(default_factory=lambda: Layer("wallets"))
    top_wallet_signals: Layer = field(default_factory=lambda: Layer("top_wallet_signals"))
    cross_wallet: Layer = field(default_factory=lambda: Layer("cross_wallet"))

    blockchain: Layer = field(default_factory=lambda: Layer("blockchain"))

    news: Layer = field(default_factory=lambda: Layer("news"))
    events: Layer = field(default_factory=lambda: Layer("events"))
    official: Layer = field(default_factory=lambda: Layer("official"))
    public_info: Layer = field(default_factory=lambda: Layer("public_info"))

    related_markets: Layer = field(default_factory=lambda: Layer("related_markets"))
    history: Layer = field(default_factory=lambda: Layer("history"))
    regime: Layer = field(default_factory=lambda: Layer("regime"))

    model_predictions: Layer = field(default_factory=lambda: Layer("model_predictions"))
    agent_predictions: Layer = field(default_factory=lambda: Layer("agent_predictions"))

    execution: Layer = field(default_factory=lambda: Layer("execution"))
    risk: Layer = field(default_factory=lambda: Layer("risk"))

    def layers(self) -> list[Layer]:
        return [v for v in self.__dict__.values() if isinstance(v, Layer)]

    def layer(self, name: str) -> Layer:
        got = getattr(self, name, None)
        return got if isinstance(got, Layer) else Layer(name)

    def available_layers(self) -> list[str]:
        return [l.name for l in self.layers() if l.ok]

    def missing_layers(self) -> list[str]:
        return [l.name for l in self.layers() if not l.ok]

    @property
    def completeness(self) -> float:
        ls = self.layers()
        return sum(1 for l in ls if l.ok) / len(ls) if ls else 0.0

    def to_dict(self) -> dict:
        return {"as_of": self.as_of, "market_id": self.market_id,
                "token_id": self.token_id,
                "completeness": round(self.completeness, 3),
                "available": self.available_layers(),
                "missing": self.missing_layers(),
                "layers": {l.name: l.to_dict() for l in self.layers()}}


# ---------------------------------------------------------------------------
# Signals and opportunities
# ---------------------------------------------------------------------------

class SignalClass(str, Enum):
    """The noise-control ladder. Information is abundant; edge is scarce.

    Promoting between these is the system's actual job, and naming the rungs
    stops "we detected something" from being reported as "we found an edge".
    """

    INFORMATION = "INFORMATION"        # a fact was observed
    SIGNAL = "SIGNAL"                  # it correlates with future price
    EDGE = "EDGE"                      # the correlation survives costs
    EXECUTABLE_EDGE = "EXECUTABLE_EDGE"    # ...and our capital can take it
    VALIDATED_EDGE = "VALIDATED_EDGE"      # ...and it survived out-of-sample


@dataclass
class Signal:
    source: str                        # which agent / detector
    kind: str
    direction: float                   # -1..+1 toward outcome YES
    strength: float                    # 0..1
    classification: SignalClass = SignalClass.INFORMATION
    horizon_secs: int = 0
    note: str = ""
    evidence: list = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["classification"] = self.classification.value
        return d


@dataclass
class Opportunity:
    """A ranked candidate. Not yet a decision — the gates have not run."""

    market_id: str
    token_id: str
    question: str = ""
    side: str = "BUY"
    market_probability: float = 0.0
    fair_probability: float = 0.0
    confidence: float = 0.0

    mispricing_score: float = 0.0
    information_shock_score: float = 0.0
    wallet_signal_score: float = 0.0
    microstructure_score: float = 0.0
    news_score: float = 0.0
    event_score: float = 0.0
    cross_market_score: float = 0.0
    statistical_score: float = 0.0
    execution_score: float = 0.0
    risk_score: float = 0.0

    signals: list = field(default_factory=list)
    evidence_ref: str = ""
    as_of: int = 0

    @property
    def edge(self) -> float:
        return self.fair_probability - self.market_probability

    @property
    def overall_score(self) -> float:
        """Weighted blend, capped by execution feasibility.

        Multiplying by `execution_score` rather than averaging it in is
        deliberate: an unexecutable opportunity should rank at zero, not merely
        lower. Averaging lets a spectacular statistical score drag an
        untradeable market to the top of the list, which is exactly the kind of
        ranking that produces a beautiful dashboard and no fills.
        """
        blend = (0.22 * self.mispricing_score
                 + 0.16 * self.wallet_signal_score
                 + 0.12 * self.statistical_score
                 + 0.12 * self.microstructure_score
                 + 0.10 * self.news_score
                 + 0.10 * self.event_score
                 + 0.08 * self.cross_market_score
                 + 0.10 * self.information_shock_score)
        return round(blend * self.execution_score * (1.0 - self.risk_score), 5)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["signals"] = [s.to_dict() if isinstance(s, Signal) else s
                        for s in self.signals]
        d["edge"] = round(self.edge, 5)
        d["overall_score"] = self.overall_score
        return d


# ---------------------------------------------------------------------------
# Wallet DNA
# ---------------------------------------------------------------------------

@dataclass
class WalletDNA:
    """A behavioural fingerprint. Descriptive, never prescriptive.

    Nothing here authorises a copy. `scanner`/`decision` decide that, from a
    COPY_SCORE that combines this fingerprint with the current evidence state.
    A DNA object on its own is an observation about the past.
    """

    wallet: str
    trades: int = 0
    markets: int = 0
    first_ts: int = 0
    last_ts: int = 0

    # WHAT / WHERE
    category_mix: dict = field(default_factory=dict)
    price_band_mix: dict = field(default_factory=dict)
    preferred_price: float = 0.0
    preferred_ttr_hours: float = 0.0

    # WHEN
    hour_histogram: list = field(default_factory=lambda: [0] * 24)
    dow_histogram: list = field(default_factory=lambda: [0] * 7)
    burstiness: float = 0.0            # CV of inter-trade gaps; 1.0 = Poisson

    # HOW MUCH
    avg_notional: float = 0.0
    median_notional: float = 0.0
    max_notional: float = 0.0
    notional_dispersion: float = 0.0

    # HOW
    scaling_behavior: str = "UNKNOWN"      # SINGLE|SCALE_IN|PYRAMID|AVERAGE_DOWN
    directional_bias: float = 0.0          # -1 fade .. +1 follow price move
    momentum_score: float = 0.0
    mean_reversion_score: float = 0.0
    repeat_market_rate: float = 0.0
    late_market_rate: float = 0.0          # share of entries near resolution
    early_market_rate: float = 0.0

    # PERFORMANCE (settled evidence only)
    win_rate: float = 0.0
    roi: float = 0.0
    expectancy: float = 0.0
    profit_factor: float = 0.0
    max_drawdown: float = 0.0
    sharpe_like: float = 0.0
    sortino_like: float = 0.0
    calibration_error: float = 0.0
    alpha_vs_band: float = 0.0             # THE control: edge net of price band

    evidence_quality: str = "UNRATED"
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    # Mapping-style access. Consumers of a DNA map are split between wanting
    # attributes (`copy_score`, which is typed against this class) and wanting
    # `.get()` (the agents and the ensemble, which also accept DNA-shaped
    # dicts loaded from the store). Supporting both here is one small method;
    # the alternative was a normalisation helper at every call site, which is
    # the kind of thing that gets forgotten at exactly one of them.
    def get(self, key: str, default=None):
        return getattr(self, key, default)

    def __getitem__(self, key: str):
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __contains__(self, key: str) -> bool:
        return hasattr(self, key)


@dataclass
class GateResult:
    gate: str
    passed: bool
    critical: bool = True
    reason: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def jsonable(obj: Any):
    """Best-effort conversion for the API layer."""
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, dict):
        return {k: jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    return obj
