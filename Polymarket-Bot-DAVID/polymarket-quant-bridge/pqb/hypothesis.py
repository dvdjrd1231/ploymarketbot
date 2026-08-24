"""The multi-source hypothesis layer: what are the engines agreeing about?

This sits ABOVE discovery and BELOW the existing validation architecture, and
it is strictly additive. It creates no second validation system, no second
evidence system, no second candidate identity. It may propose, rank, connect
and prioritise. It may not declare anything validated or tradable — the only
route to either remains the existing OOS ladder, and the only way anything from
here reaches that ladder is by becoming an ordinary `CandidateSpec` and
registering through the one registry like everything else.

A HYPOTHESIS is not a strategy. It is a proposed market relationship — "sharp
probability decline tends to be followed by recovery" — and its job is to
survive being attacked. A strategy is what you get if it does.

The distinction that makes the layer worth having is between *same-source
repetition* and *independent-source convergence*. Three threshold rules that
all fire on the same column of the same series are one observation wearing
three hats; a quant rule, a wallet behaviour and a sequence chain that
independently describe the same relationship are three. Treating the first as
the second is how a research system talks itself into a phantom edge, so
correlation control is not an afterthought here — it is most of the file.

Everything is persisted and versioned. Hypothesis states are never
overwritten; a materially changed hypothesis becomes a new version with its
own record, exactly as a rethresholded rule becomes a new candidate version.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .sources import ALL_SOURCES, rule_complexity, source_of

# -- statuses -----------------------------------------------------------------

NEW = "NEW"
OBSERVING = "OBSERVING"
CONVERGING = "CONVERGING"
ADVERSARIAL_TEST = "ADVERSARIAL_TEST"
OOS_PENDING = "OOS_PENDING"
SUPPORTED = "SUPPORTED"
WEAKENING = "WEAKENING"
REJECTED = "REJECTED"
PROMOTED_TO_CANDIDATE = "PROMOTED_TO_CANDIDATE"

STATUSES = (NEW, OBSERVING, CONVERGING, ADVERSARIAL_TEST, OOS_PENDING,
            SUPPORTED, WEAKENING, REJECTED, PROMOTED_TO_CANDIDATE)

# SUPPORTED is the strongest thing this layer can say, and it means "survived
# what we could throw at it", not "validated". The rename is deliberate: there
# is no status here that could be mistaken for a verdict from the ladder.

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hypotheses (
    id             TEXT PRIMARY KEY,      -- pattern-signature#vN
    signature      TEXT NOT NULL,         -- the normalised relationship
    version        INTEGER NOT NULL,
    status         TEXT NOT NULL DEFAULT 'NEW',
    relationship   TEXT NOT NULL,         -- human-readable
    pattern        TEXT NOT NULL,         -- normalised Observation, JSON
    direction      TEXT DEFAULT '',
    inverse        TEXT DEFAULT '',
    supporting     INTEGER DEFAULT 0,
    contradicting  INTEGER DEFAULT 0,
    markets        TEXT DEFAULT '[]',
    periods        TEXT DEFAULT '[]',
    regimes        TEXT DEFAULT '[]',
    candidates     TEXT DEFAULT '[]',     -- related candidate IDs
    families       TEXT DEFAULT '[]',
    sources        TEXT DEFAULT '[]',     -- contributing discovery sources
    adversarial    TEXT DEFAULT '{}',     -- test name -> result
    failure_states TEXT DEFAULT '[]',
    priority       REAL DEFAULT 0,
    created_ts     REAL NOT NULL,
    updated_ts     REAL NOT NULL,
    note           TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hyp_sig ON hypotheses(signature);
CREATE INDEX IF NOT EXISTS idx_hyp_status ON hypotheses(status);

-- The append-only state log. §20: do not overwrite historical hypothesis
-- states. Every transition keeps its reason and its moment.
CREATE TABLE IF NOT EXISTS hypothesis_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    hypothesis_id TEXT NOT NULL,
    ts            REAL NOT NULL,
    from_status   TEXT DEFAULT '',
    to_status     TEXT NOT NULL,
    reason        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_hyplog ON hypothesis_log(hypothesis_id, ts);

-- Which hypotheses describe the same underlying phenomenon. A relationship,
-- not a merge: the members keep their separate identities and evidence.
CREATE TABLE IF NOT EXISTS convergence_groups (
    id            TEXT PRIMARY KEY,
    pattern       TEXT NOT NULL,
    members       TEXT DEFAULT '[]',
    sources       TEXT DEFAULT '[]',
    independent   INTEGER DEFAULT 0,     -- genuinely independent source count
    priority      REAL DEFAULT 0,
    created_ts    REAL NOT NULL,
    updated_ts    REAL NOT NULL
);
"""


# -- the normalised pattern ---------------------------------------------------

# The vocabulary a discovered behaviour is translated into so two rules from
# different engines, written in different terms, can be compared at all
# (§5 of the second directive). Every field is a coarse bucket on purpose:
# the question is "are these describing the same thing?", and precision here
# would answer "no" for two descriptions of one phenomenon.
_PATTERN_FIELDS = ("direction", "entry_state", "probability_region",
                   "price_movement", "volatility_state", "liquidity_state",
                   "volume_state", "wallet_behavior", "time_to_resolution",
                   "sequence_order", "holding_behavior", "category", "regime")


@dataclass(frozen=True)
class Observation:
    """One discovered behaviour, in the shared vocabulary."""

    direction: str = ""
    entry_state: str = ""
    probability_region: str = ""
    price_movement: str = ""
    volatility_state: str = ""
    liquidity_state: str = ""
    volume_state: str = ""
    wallet_behavior: str = ""
    time_to_resolution: str = ""
    sequence_order: str = ""
    holding_behavior: str = ""
    category: str = ""
    regime: str = ""

    def signature(self) -> str:
        """Identity of the RELATIONSHIP. Two engines describing one
        phenomenon must land on one signature, or convergence can never be
        detected; two different phenomena must not, or it is detected
        everywhere."""
        return "|".join(f"{k}={getattr(self, k)}" for k in _PATTERN_FIELDS
                        if getattr(self, k))

    def similarity(self, other: "Observation") -> float:
        """Share of populated fields the two agree on, in 0..1.

        Fields neither one populates are not evidence of agreement and are
        excluded from both sides of the ratio — otherwise two nearly-empty
        observations would read as a perfect match.
        """
        considered = [k for k in _PATTERN_FIELDS
                      if getattr(self, k) or getattr(other, k)]
        if not considered:
            return 0.0
        agree = sum(1 for k in considered
                    if getattr(self, k) == getattr(other, k))
        return agree / len(considered)


def _band(price: Optional[float]) -> str:
    if price is None:
        return ""
    # Rounded before comparison. A band midpoint of (0.55 + 0.65) / 2 is
    # 0.6000000000000001 in binary floating point, which falls past a `<=
    # 0.60` boundary and lands the market one band too high — and since the
    # band is part of the pattern signature, that silently prevents two
    # engines describing the same mid-band relationship from ever converging.
    price = round(float(price), 6)
    if price < 0.20:
        return "longshot"
    if price < 0.45:
        return "low"
    if price <= 0.60:
        return "mid"
    if price <= 0.80:
        return "favourite"
    return "near_certain"


def normalize(rule: dict) -> Observation:
    """Translate any engine's rule into the shared vocabulary.

    Lossy by design. What survives is the claim about the market; what is
    dropped is how a particular engine happens to spell it.
    """
    rule = rule or {}
    kind = str(rule.get("type") or "threshold")
    direction = str(rule.get("direction") or "")
    if direction in ("up", "long"):
        direction = "long"
    elif direction in ("down", "short"):
        direction = "short"

    hold = ""
    if str(rule.get("hold") or "") == "resolution":
        hold = "resolution"
    elif rule.get("hold_seconds") is not None:
        seconds = float(rule.get("hold_seconds") or 0)
        hold = ("immediate" if seconds <= 900 else
                "short" if seconds <= 7200 else
                "medium" if seconds <= 86_400 else "long")
    elif rule.get("hold_bars") is not None:
        bars = int(rule.get("hold_bars") or 0)
        hold = ("immediate" if bars <= 5 else
                "short" if bars <= 20 else "medium")

    chain = [str(c) for c in (rule.get("chain") or [])]
    entry_state = ""
    price_movement = ""
    if kind == "sequence" and chain:
        entry_state = chain[-1]
        price_movement = chain[0]
    elif kind == "sharp_move":
        entry_state = str(rule.get("price_region") or "")
        price_movement = str(rule.get("move_direction") or "")
    elif kind == "wallet_behavior":
        entry_state = str(rule.get("trigger") or "")
    elif kind in ("threshold", ""):
        feature = str(rule.get("entry_feature") or "")
        op = str(rule.get("entry_op") or "")
        entry_state = f"{feature}{op}" if feature else ""
        price_movement = _feature_movement(feature, op)

    region = ""
    for lo_key, hi_key in (("prob_lo", "prob_hi"), ("price_lo", "price_hi")):
        if rule.get(lo_key) is not None:
            lo = float(rule.get(lo_key) or 0.0)
            hi = float(rule.get(hi_key) or lo)
            region = _band((lo + hi) / 2.0)
            break
    if not region and rule.get("band"):
        region = str(rule.get("band"))
    if not region and kind == "longshot":
        region = "longshot" if rule.get("side") == "low" else "favourite"

    return Observation(
        direction=direction,
        entry_state=entry_state,
        probability_region=region,
        price_movement=price_movement,
        volatility_state=str(rule.get("volatility") or ""),
        liquidity_state=str(rule.get("liquidity") or ""),
        volume_state=str(rule.get("volume") or ""),
        wallet_behavior=str(rule.get("origin") or "")
        if kind in ("wallet_behavior", "wallet_state") else "",
        time_to_resolution=str(rule.get("ttr_bucket") or ""),
        sequence_order=">".join(chain) if chain else "",
        holding_behavior=hold,
        category=str(rule.get("category") or ""),
        regime=str(rule.get("regime") or ""))


def _feature_movement(feature: str, op: str) -> str:
    """A threshold rule's implied claim about price behaviour."""
    if not feature or not op:
        return ""
    reverting = feature.startswith(("price", "mid", "ms_reversal"))
    if op == "<":
        return "decline" if reverting else "contraction"
    if op == ">":
        return "advance" if reverting else "expansion"
    return ""


# -- the hypothesis record ----------------------------------------------------

@dataclass
class Hypothesis:
    """A proposed market relationship. Not a strategy, and never validated."""

    id: str = ""
    signature: str = ""
    version: int = 1
    status: str = NEW
    relationship: str = ""
    pattern: Observation = field(default_factory=Observation)
    direction: str = ""
    inverse: str = ""
    supporting: int = 0
    contradicting: int = 0
    markets: set[str] = field(default_factory=set)
    periods: set[str] = field(default_factory=set)
    regimes: set[str] = field(default_factory=set)
    candidates: list[str] = field(default_factory=list)
    families: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    adversarial: dict[str, Any] = field(default_factory=dict)
    failure_states: list[str] = field(default_factory=list)
    priority: float = 0.0
    created_ts: float = 0.0
    updated_ts: float = 0.0
    note: str = ""

    @property
    def independent_markets(self) -> int:
        return len(self.markets)

    @property
    def complexity(self) -> int:
        return max(1, len(self.pattern.signature().split("|")))

    def is_supported(self) -> bool:
        """The strongest claim this layer can make, and it is not validation.

        Deliberately spelled out where it is easiest to misread: a SUPPORTED
        hypothesis has survived inversion and adversarial testing and has
        breadth behind it. It has not passed a single OOS gate. Only the
        existing ladder can say that, about a registered candidate, on
        independent market evidence.
        """
        return self.status == SUPPORTED


# -- correlation control ------------------------------------------------------

def independent_sources(contributions: Iterable[dict],
                        max_market_overlap: float = 0.5) -> int:
    """How many GENUINELY independent sources support this relationship (§16).

    Two things disqualify a contribution from counting as independent
    confirmation: it comes from an engine already counted, or its supporting
    markets are largely the same markets as one already counted. Three quant
    rules over one series are one observation, not three, and a wallet
    behaviour mined from the same five markets a sequence chain was found in
    is not a second opinion about those markets.

    Returns a count, and only a count. Convergence raises research priority
    and can never be added to statistical evidence (§4, §12 of the second
    directive).
    """
    accepted: list[tuple[str, set[str]]] = []
    for entry in contributions:
        source = str(entry.get("source") or "")
        markets = {str(m) for m in (entry.get("markets") or [])}
        if any(source == seen_source for seen_source, _ in accepted):
            continue
        overlapping = False
        for _seen_source, seen_markets in accepted:
            if not markets or not seen_markets:
                continue
            overlap = len(markets & seen_markets) / len(markets | seen_markets)
            if overlap > max_market_overlap:
                overlapping = True
                break
        if overlapping:
            continue
        accepted.append((source, markets))
    return len(accepted)


def convergence_priority(hypothesis: Hypothesis, independent: int) -> float:
    """§12: what to investigate harder. Never what is true.

    Multiplicative in the same spirit as the evidence score: a zero anywhere
    zeroes it, so a hypothesis cannot be pushed up the queue by one flattering
    dimension. Simplicity is a positive term (§9's complexity penalty) and
    adversarial survival is a gate rather than a bonus — a hypothesis that has
    not been attacked yet is not thereby promising.
    """
    if hypothesis.status == REJECTED:
        return 0.0
    breadth = min(1.0, hypothesis.independent_markets / 15.0)
    sources = min(1.0, max(0, independent) / 3.0)
    periods = min(1.0, len(hypothesis.periods) / 4.0)
    regimes = min(1.0, len(hypothesis.regimes) / 3.0)
    observations = min(1.0, hypothesis.supporting / 20.0)
    contradiction = hypothesis.supporting + hypothesis.contradicting
    agreement = (hypothesis.supporting / contradiction) if contradiction else 0.0
    simplicity = 1.0 / (1.0 + 0.15 * max(0, hypothesis.complexity - 1))
    # Inversion tested and survived is worth more than untested; inversion
    # that WON means the relationship is real but backwards, which is a
    # finding, not a disqualification — it earns its own hypothesis instead.
    inverse_result = str(hypothesis.adversarial.get("inverse") or "")
    inverse_term = {"survived": 1.0, "inconclusive": 0.6,
                    "inverse_won": 0.3}.get(inverse_result, 0.5)
    failure_clarity = 1.0 if hypothesis.failure_states else 0.7
    return round(
        breadth * sources * max(0.15, periods) * max(0.15, regimes)
        * observations * agreement * simplicity * inverse_term
        * failure_clarity, 6)


# -- persistence --------------------------------------------------------------

class HypothesisStore:
    """Append-only, versioned storage for the layer. Holds no OOS evidence."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- writes ----------------------------------------------------------

    def upsert(self, hypothesis: Hypothesis) -> str:
        """Register or update. A materially changed pattern is a NEW VERSION
        with its own record — §20 — so a hypothesis can never be quietly
        redefined into one that fits the evidence it has already collected.
        """
        now = time.time()
        signature = hypothesis.pattern.signature() or hypothesis.signature
        with self._lock:
            row = self._conn.execute(
                "SELECT id, version, status FROM hypotheses WHERE "
                "signature=? ORDER BY version DESC LIMIT 1",
                (signature,)).fetchone()
            if row and row["id"] == hypothesis.id:
                self._conn.execute(
                    "UPDATE hypotheses SET status=?, supporting=?, "
                    "contradicting=?, markets=?, periods=?, regimes=?, "
                    "candidates=?, families=?, sources=?, adversarial=?, "
                    "failure_states=?, priority=?, updated_ts=?, note=? "
                    "WHERE id=?",
                    (hypothesis.status, hypothesis.supporting,
                     hypothesis.contradicting,
                     json.dumps(sorted(hypothesis.markets)),
                     json.dumps(sorted(hypothesis.periods)),
                     json.dumps(sorted(hypothesis.regimes)),
                     json.dumps(hypothesis.candidates),
                     json.dumps(hypothesis.families),
                     json.dumps(hypothesis.sources),
                     json.dumps(hypothesis.adversarial),
                     json.dumps(hypothesis.failure_states),
                     float(hypothesis.priority), now, hypothesis.note,
                     hypothesis.id))
                self._conn.commit()
                return hypothesis.id

            version = (int(row["version"]) + 1) if row else 1
            new_id = f"{signature}#v{version}" if signature else \
                f"hyp{int(now)}#v{version}"
            self._conn.execute(
                "INSERT OR REPLACE INTO hypotheses(id, signature, version, "
                "status, relationship, pattern, direction, inverse, "
                "supporting, contradicting, markets, periods, regimes, "
                "candidates, families, sources, adversarial, failure_states, "
                "priority, created_ts, updated_ts, note) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id, signature, version, hypothesis.status,
                 hypothesis.relationship,
                 json.dumps(asdict(hypothesis.pattern)),
                 hypothesis.direction, hypothesis.inverse,
                 hypothesis.supporting, hypothesis.contradicting,
                 json.dumps(sorted(hypothesis.markets)),
                 json.dumps(sorted(hypothesis.periods)),
                 json.dumps(sorted(hypothesis.regimes)),
                 json.dumps(hypothesis.candidates),
                 json.dumps(hypothesis.families),
                 json.dumps(hypothesis.sources),
                 json.dumps(hypothesis.adversarial),
                 json.dumps(hypothesis.failure_states),
                 float(hypothesis.priority), now, now, hypothesis.note))
            self._conn.execute(
                "INSERT INTO hypothesis_log(hypothesis_id, ts, from_status, "
                "to_status, reason) VALUES(?,?,?,?,?)",
                (new_id, now, "", hypothesis.status, "registered"))
            self._conn.commit()
        hypothesis.id = new_id
        hypothesis.signature = signature
        hypothesis.version = version
        return new_id

    def set_status(self, hypothesis_id: str, status: str,
                   reason: str = "") -> None:
        """Transition, logged. History is appended, never rewritten."""
        if status not in STATUSES:
            raise ValueError(f"unknown hypothesis status: {status}")
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM hypotheses WHERE id=?",
                (hypothesis_id,)).fetchone()
            previous = row["status"] if row else ""
            self._conn.execute(
                "UPDATE hypotheses SET status=?, updated_ts=?, note=? "
                "WHERE id=?", (status, now, reason, hypothesis_id))
            self._conn.execute(
                "INSERT INTO hypothesis_log(hypothesis_id, ts, from_status, "
                "to_status, reason) VALUES(?,?,?,?,?)",
                (hypothesis_id, now, previous, status, reason))
            self._conn.commit()

    def record_adversarial(self, hypothesis_id: str, test: str,
                           result: str, detail: str = "") -> None:
        """§13: record failures instead of deleting them."""
        with self._lock:
            row = self._conn.execute(
                "SELECT adversarial FROM hypotheses WHERE id=?",
                (hypothesis_id,)).fetchone()
            try:
                current = json.loads(row["adversarial"]) if row else {}
            except (TypeError, ValueError):
                current = {}
            current[test] = {"result": result, "detail": detail,
                             "ts": time.time()} if detail else result
            self._conn.execute(
                "UPDATE hypotheses SET adversarial=?, updated_ts=? WHERE id=?",
                (json.dumps(current), time.time(), hypothesis_id))
            self._conn.commit()

    def upsert_group(self, group_id: str, pattern: str, members: list[str],
                     sources: list[str], independent: int,
                     priority: float) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO convergence_groups(id, pattern, members, "
                "sources, independent, priority, created_ts, updated_ts) "
                "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET "
                "members=excluded.members, sources=excluded.sources, "
                "independent=excluded.independent, "
                "priority=excluded.priority, updated_ts=excluded.updated_ts",
                (group_id, pattern, json.dumps(sorted(members)),
                 json.dumps(sorted(sources)), int(independent),
                 float(priority), now, now))
            self._conn.commit()

    # -- reads -----------------------------------------------------------

    def all(self) -> list[Hypothesis]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM hypotheses ORDER BY priority DESC").fetchall()
        return [_row_to_hypothesis(r) for r in rows]

    def get(self, hypothesis_id: str) -> Optional[Hypothesis]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM hypotheses WHERE id=?",
                (hypothesis_id,)).fetchone()
        return _row_to_hypothesis(row) if row else None

    def history(self, hypothesis_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, from_status, to_status, reason FROM "
                "hypothesis_log WHERE hypothesis_id=? ORDER BY ts",
                (hypothesis_id,)).fetchall()
        return [dict(r) for r in rows]

    def groups(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM convergence_groups ORDER BY priority DESC"
            ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            for key in ("members", "sources"):
                try:
                    entry[key] = json.loads(entry[key] or "[]")
                except (TypeError, ValueError):
                    entry[key] = []
            out.append(entry)
        return out

    def summary(self) -> dict:
        """§21's dashboard block. Deliberately separate from validation
        metrics — mixing them is how a research signal starts reading as a
        result."""
        rows = self.all()
        by_status = {name: 0 for name in STATUSES}
        for hypothesis in rows:
            by_status[hypothesis.status] = by_status.get(hypothesis.status,
                                                         0) + 1
        groups = self.groups()
        adversarial_tested = sum(1 for h in rows if h.adversarial)
        return {
            "hypothesesTotal": len(rows),
            "hypothesesByStatus": by_status,
            "convergenceGroups": len(groups),
            "convergenceIndependentMax": max(
                (int(g["independent"]) for g in groups), default=0),
            "convergenceAvgSources": round(
                sum(len(g["sources"]) for g in groups) / len(groups), 2)
            if groups else 0.0,
            "adversarialTested": adversarial_tested,
            "adversarialRejected": by_status.get(REJECTED, 0),
            "adversarialWeakened": by_status.get(WEAKENING, 0),
            "inverseWinners": sum(
                1 for h in rows
                if str(h.adversarial.get("inverse") or "") == "inverse_won"),
            "failureStatesFound": sum(len(h.failure_states) for h in rows),
            "promotedToCandidate": by_status.get(PROMOTED_TO_CANDIDATE, 0),
        }


def _row_to_hypothesis(row) -> Hypothesis:
    def _json(key, default):
        try:
            return json.loads(row[key] or default)
        except (TypeError, ValueError, IndexError):
            return json.loads(default)

    pattern_fields = _json("pattern", "{}")
    return Hypothesis(
        id=row["id"], signature=row["signature"], version=int(row["version"]),
        status=row["status"], relationship=row["relationship"],
        pattern=Observation(**{k: v for k, v in pattern_fields.items()
                               if k in _PATTERN_FIELDS}),
        direction=row["direction"] or "", inverse=row["inverse"] or "",
        supporting=int(row["supporting"] or 0),
        contradicting=int(row["contradicting"] or 0),
        markets=set(_json("markets", "[]")),
        periods=set(_json("periods", "[]")),
        regimes=set(_json("regimes", "[]")),
        candidates=list(_json("candidates", "[]")),
        families=list(_json("families", "[]")),
        sources=list(_json("sources", "[]")),
        adversarial=dict(_json("adversarial", "{}")),
        failure_states=list(_json("failure_states", "[]")),
        priority=float(row["priority"] or 0.0),
        created_ts=float(row["created_ts"] or 0.0),
        updated_ts=float(row["updated_ts"] or 0.0),
        note=row["note"] or "")
