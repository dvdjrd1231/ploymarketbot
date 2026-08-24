"""EXPERIMENT MEMORY: failures kept as information, not as absence.

The library already refuses to delete anything — a rejected rule keeps its row,
its reason and its whole ledger. What it does not keep is the *shape* of the
failure in a form the search can read back. `retired_reason` is prose written
for a person; `status` is one word covering a dozen different ways of being
wrong. Neither can answer the question this module exists for:

    We are about to register this idea. Have we already learned something that
    says it will fail, and what did we learn?

So every meaningful outcome is classified against a fixed taxonomy and written
to a memory that hypothesis generation consults BEFORE registering. Three
properties keep that from becoming a second validation system:

* **It classifies, it never judges.** A failure reason is derived from
  evidence the ladder already produced. Nothing here sets a status, and the
  taxonomy has no code path that could make a candidate validated.
* **It suppresses REGISTRATION, never EVIDENCE.** A dead end stops the pass
  spending a slot re-registering the fifth spelling of a refuted idea. It
  cannot stop an existing candidate from being tested, and it cannot remove
  or discount a single validation row.
* **A dead end is a fact with a count, and it can be revived.** One failure
  is an observation; the suppression only bites after `DEAD_END_STRIKES`
  independent candidates in a family failed the same way. Any success in the
  family clears it — the memory is there to stop repetition, not to close off
  the search, and §18's "maximise exploration freedom" is the constraint that
  rules here.

The other half of the module is the useful half: a failure reason implies a
NEXT question. `EXCESSIVE_COSTS` says try a longer hold, not try harder;
`SINGLE_MARKET_DEPENDENCE` says the idea is untested, not refuted, and needs
breadth rather than a new variant. Those directives are what turn a failure
into the next hypothesis instead of into a gap in the record.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# -- the failure taxonomy -----------------------------------------------------

# Verbatim from the directive, plus the three the running system actually
# produces and the directive's list does not name. RULE_NEVER_FIRED in
# particular is not a failure of the rule at all — it is a non-observation,
# and conflating it with NEGATIVE_EXPECTANCY is how a rule nobody has tested
# reads as a rule that was tried and lost.
NEGATIVE_EXPECTANCY = "NEGATIVE_EXPECTANCY"
INSUFFICIENT_MARKET_BREADTH = "INSUFFICIENT_MARKET_BREADTH"
SINGLE_MARKET_DEPENDENCE = "SINGLE_MARKET_DEPENDENCE"
SINGLE_REGIME_DEPENDENCE = "SINGLE_REGIME_DEPENDENCE"
EXCESSIVE_COSTS = "EXCESSIVE_COSTS"
EXCESSIVE_DRAWDOWN = "EXCESSIVE_DRAWDOWN"
PARAMETER_SENSITIVITY = "PARAMETER_SENSITIVITY"
NO_REPLICATION = "NO_REPLICATION"
INSUFFICIENT_SAMPLE = "INSUFFICIENT_SAMPLE"
DATA_QUALITY_FAILURE = "DATA_QUALITY_FAILURE"
TEMPORAL_FAILURE = "TEMPORAL_FAILURE"
ADVERSARIAL_FAILURE = "ADVERSARIAL_FAILURE"
RULE_NEVER_FIRED = "RULE_NEVER_FIRED"
FEATURE_UNAVAILABLE = "FEATURE_UNAVAILABLE"
NOT_DIRECTIONAL = "NOT_DIRECTIONAL"
# The signal was indistinguishable from firing at random on the same markets
# for the same duration. Distinct from NEGATIVE_EXPECTANCY: the record can be
# positive and still earn this, because what pays is the hold and the
# direction rather than the condition the candidate claims to detect.
INDISTINGUISHABLE_FROM_RANDOM = "INDISTINGUISHABLE_FROM_RANDOM"
# The edge exists only where the book is too thin to take the position. Not a
# cost failure — costs were already charged — but a tradability failure, and
# the one kind of edge that looks best exactly where it is least real.
LIQUIDITY_DEPENDENCE = "LIQUIDITY_DEPENDENCE"

FAILURE_REASONS = (
    NEGATIVE_EXPECTANCY, INSUFFICIENT_MARKET_BREADTH, SINGLE_MARKET_DEPENDENCE,
    SINGLE_REGIME_DEPENDENCE, EXCESSIVE_COSTS, EXCESSIVE_DRAWDOWN,
    PARAMETER_SENSITIVITY, NO_REPLICATION, INSUFFICIENT_SAMPLE,
    DATA_QUALITY_FAILURE, TEMPORAL_FAILURE, ADVERSARIAL_FAILURE,
    RULE_NEVER_FIRED, FEATURE_UNAVAILABLE, NOT_DIRECTIONAL)

# Outcomes an experiment can have. Deliberately NOT the ladder's statuses:
# these describe what the experiment TAUGHT, which is a different question
# from what the candidate is currently allowed to do.
R_VALIDATED = "VALIDATED"
R_PROMISING = "PROMISING"
R_INCONCLUSIVE = "INCONCLUSIVE"
R_FAILED = "FAILED"

# Reasons that are statements about the DATA or the SAMPLE rather than about
# the idea. These never accumulate toward a dead end: suppressing a family
# because it has not been given enough markets yet is the circular starvation
# `allocation.py` was written to break, arriving by a different route.
_NOT_THE_IDEAS_FAULT = frozenset({
    INSUFFICIENT_MARKET_BREADTH, INSUFFICIENT_SAMPLE, DATA_QUALITY_FAILURE,
    RULE_NEVER_FIRED, SINGLE_MARKET_DEPENDENCE, FEATURE_UNAVAILABLE})

# Independent candidates in one family that must fail the SAME way before the
# family's registrations are throttled.
DEAD_END_STRIKES = 3

# What to try next, per reason. This table is the module's real output — the
# classification exists to reach it.
NEXT_QUESTION: dict[str, tuple[str, str]] = {
    NEGATIVE_EXPECTANCY: (
        "INVERT",
        "a decisively losing rule may be a winning rule pointed backwards; "
        "the inverse earns its own record from zero"),
    NOT_DIRECTIONAL: (
        "RECAST_AS_NON_DIRECTIONAL",
        "both sides pay, so the signal is not about direction - ask it as "
        "volatility, spread or timing instead"),
    EXCESSIVE_COSTS: (
        "LENGTHEN_HOLD",
        "the move is real but smaller than the round trip; a longer hold "
        "spreads one cost over a larger move"),
    PARAMETER_SENSITIVITY: (
        "WIDEN_CONDITION",
        "the effect lives at one threshold, which is a property of the "
        "threshold; a coarser condition either finds the relationship or "
        "shows there was not one"),
    TEMPORAL_FAILURE: (
        "ADD_REGIME_CONDITION",
        "it worked and then stopped, so a regime changed; find the regime "
        "rather than re-fitting the rule"),
    SINGLE_REGIME_DEPENDENCE: (
        "ADD_REGIME_CONDITION",
        "the edge is conditional; state the condition explicitly and test "
        "the conditional version as its own candidate"),
    EXCESSIVE_DRAWDOWN: (
        "SHORTEN_HOLD",
        "the direction may be right and the path unsurvivable; a shorter "
        "hold tests whether the entry or the duration carries the edge"),
    SINGLE_MARKET_DEPENDENCE: (
        "NEEDS_BREADTH",
        "not refuted - untested. Allocate independent markets before "
        "concluding anything"),
    INSUFFICIENT_MARKET_BREADTH: (
        "NEEDS_BREADTH",
        "the blocker is market supply, not the idea"),
    INSUFFICIENT_SAMPLE: (
        "NEEDS_EVENTS",
        "the conditions are too rare here; a looser entry or a wider pool "
        "is the question, not a different direction"),
    RULE_NEVER_FIRED: (
        "LOOSEN_ENTRY",
        "the rule's conditions never occurred in the markets tried; this "
        "says nothing about the rule and everything about its entry"),
    ADVERSARIAL_FAILURE: (
        "ABANDON_OR_CONDITION",
        "it survived ordinary backtesting and not deliberate attack; either "
        "find the condition under which it holds, or let it go"),
    NO_REPLICATION: (
        "ABANDON",
        "it did not reproduce anywhere else - this is what a local anomaly "
        "looks like"),
    DATA_QUALITY_FAILURE: (
        "FIX_DATA",
        "the replay failed rather than the rule; nothing has been learned "
        "about the idea"),
    FEATURE_UNAVAILABLE: (
        "FIX_DATA",
        "the columns the rule needs do not exist in validation data"),
    INDISTINGUISHABLE_FROM_RANDOM: (
        "TEST_THE_HOLD_ALONE",
        "random entries of the same duration paid the same, so the hold and "
        "the direction are carrying this, not the signal; register the "
        "always-on version and see whether the condition adds anything"),
    LIQUIDITY_DEPENDENCE: (
        "REQUIRE_DEPTH",
        "the edge is concentrated where the book is thinnest, which is where "
        "it is least takeable; re-ask it with a depth floor in the entry "
        "condition, and treat the thin-market record as untradable"),
}


# -- classification -----------------------------------------------------------

def classify(status: str, cumulative: dict, cfg,
             adversarial: Optional[Any] = None,
             attempts: Optional[dict] = None) -> tuple[str, str]:
    """What kind of outcome is this, and why. Returns (result, reason).

    Order is the whole design. The most specific and most actionable
    explanation must win, because a reason that is technically true and
    useless — "negative expectancy" for a rule that only ever traded in one
    market — sends the search off in the wrong direction. So:

    1. Things that are not about the idea at all (no data, never fired).
    2. Things a deliberate attack found (adversarial).
    3. Things the record itself shows (concentration, drawdown, expectancy).
    4. Things that are merely not yet known (breadth, sample).

    A candidate that is doing well is classified too. An experiment memory
    holding only failures cannot answer "what kinds of thing have worked",
    which is half of what a research organisation learns from.
    """
    from .adversarial import FAILED, INVERSE_WON

    trades = int(cumulative.get("trades") or 0)
    markets = int(cumulative.get("markets") or 0)
    expectancy = float(cumulative.get("expectancy") or 0.0)
    top_share = float(cumulative.get("top_share") or 0.0)

    if status == "quarantined":
        return R_FAILED, FEATURE_UNAVAILABLE

    tried = attempts or {}
    if trades == 0:
        if int(tried.get("errors") or 0):
            return R_FAILED, DATA_QUALITY_FAILURE
        if int(tried.get("zeroTrades") or 0):
            return R_INCONCLUSIVE, RULE_NEVER_FIRED
        return R_INCONCLUSIVE, INSUFFICIENT_SAMPLE

    # An attack that landed outranks the summary statistics, because the
    # summary statistics are exactly what it was attacking.
    if adversarial is not None:
        results = getattr(adversarial, "results", {}) or {}
        # Concentration first. A record whose P&L is one market's story
        # cannot support ANY other split's verdict — the temporal halves and
        # the market subsets both fail because whichever side holds that one
        # market is the only side that is positive. Diagnosing those as
        # "decaying" or "does not replicate" describes a consequence and
        # sends the search after a regime that was never there; the actual
        # finding is that the idea has not been tested yet.
        if results.get("leave_one_market_out") == FAILED:
            return R_INCONCLUSIVE, SINGLE_MARKET_DEPENDENCE
        # Before any statement about WHERE or WHEN the edge holds: does the
        # condition do anything at all? If random entries of the same
        # duration pay the same, every downstream question ("which regime",
        # "which category") is being asked about a signal that was never
        # there, and answering them would invent a story for noise.
        if results.get("placebo") == FAILED:
            return R_FAILED, INDISTINGUISHABLE_FROM_RANDOM
        if results.get("inverse") == FAILED:
            return R_FAILED, NOT_DIRECTIONAL
        if results.get("liquidity_stress") == FAILED:
            return R_FAILED, LIQUIDITY_DEPENDENCE
        if results.get("temporal_split") == FAILED:
            return R_FAILED, TEMPORAL_FAILURE
        if results.get("neighbour_thresholds") == FAILED:
            return R_FAILED, PARAMETER_SENSITIVITY
        if results.get("market_subsets") == FAILED:
            return R_FAILED, NO_REPLICATION
        if results.get("cost_stress") == FAILED:
            return R_FAILED, EXCESSIVE_COSTS
        if results.get("edge_vs_dispersion") == FAILED:
            # Not a cost failure, which is what this used to be filed as. The
            # edge is inside the spread between markets, so what is missing is
            # replication rather than margin — and the follow-up that implies
            # is "does it reproduce anywhere", not "hold it for longer".
            return R_FAILED, NO_REPLICATION
        if results.get("drawdown_stress") == FAILED:
            return R_FAILED, EXCESSIVE_DRAWDOWN
        if getattr(adversarial, "failed_tests", None):
            return R_FAILED, ADVERSARIAL_FAILURE
        if results.get("inverse") == INVERSE_WON:
            # Not a failure of the research — a finding. The inverse is
            # already its own candidate; this row records why.
            return R_INCONCLUSIVE, NOT_DIRECTIONAL

    if status in ("validated", "high_confidence"):
        return R_VALIDATED, ""

    if markets >= 2 and expectancy < 0 and trades >= cfg.oos_min_trades:
        return R_FAILED, NEGATIVE_EXPECTANCY
    if top_share > float(getattr(cfg, "oos_max_concentration", 0.7)):
        return R_INCONCLUSIVE, SINGLE_MARKET_DEPENDENCE
    if markets < cfg.oos_min_markets:
        return R_INCONCLUSIVE, INSUFFICIENT_MARKET_BREADTH
    if trades < cfg.oos_min_trades:
        return R_INCONCLUSIVE, INSUFFICIENT_SAMPLE
    if expectancy > 0:
        return R_PROMISING, ""
    return R_INCONCLUSIVE, NEGATIVE_EXPECTANCY


def next_question(reason: str) -> tuple[str, str]:
    """The directive implied by a failure reason, and the reasoning behind it.

    Returns ("", "") for reasons with no useful follow-up, so a caller can
    tell "nothing to try" from "try nothing".
    """
    return NEXT_QUESTION.get(reason, ("", ""))


# -- persistence --------------------------------------------------------------

_SCHEMA = """
-- One row per meaningful CHANGE in what an experiment has taught. Not one
-- per pass: an hourly pass over two thousand candidates would write two
-- thousand identical rows an hour and bury the moments something actually
-- moved. The dedupe key is what was learned, not when it was looked at.
CREATE TABLE IF NOT EXISTS experiments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    candidate_id  TEXT NOT NULL,
    signature     TEXT DEFAULT '',
    family        TEXT DEFAULT '',
    source        TEXT DEFAULT '',
    parent_id     TEXT DEFAULT '',
    hypothesis    TEXT DEFAULT '',     -- normalised pattern signature
    rule          TEXT DEFAULT '{}',
    features      TEXT DEFAULT '[]',
    status        TEXT DEFAULT '',
    maturity      TEXT DEFAULT '',
    markets       INTEGER DEFAULT 0,
    forward_markets INTEGER DEFAULT 0,
    trades        INTEGER DEFAULT 0,
    expectancy    REAL DEFAULT 0,
    drawdown      REAL DEFAULT 0,
    top_share     REAL DEFAULT 0,
    result        TEXT NOT NULL,
    failure_reason TEXT DEFAULT '',
    robustness    REAL DEFAULT 0,
    coverage      REAL DEFAULT 0,
    adversarial   TEXT DEFAULT '{}',
    directive     TEXT DEFAULT '',
    lesson        TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_exp_cand ON experiments(candidate_id, ts);
CREATE INDEX IF NOT EXISTS idx_exp_family ON experiments(family, result);
CREATE INDEX IF NOT EXISTS idx_exp_reason ON experiments(failure_reason);

-- The suppression index: (family, reason) -> how many INDEPENDENT candidates
-- died that way. `candidates` is a JSON list rather than a bare counter
-- because the same candidate re-classified twice is one strike, not two, and
-- a counter cannot tell the difference.
CREATE TABLE IF NOT EXISTS dead_ends (
    family        TEXT NOT NULL,
    reason        TEXT NOT NULL,
    candidates    TEXT DEFAULT '[]',
    strikes       INTEGER DEFAULT 0,
    first_ts      REAL NOT NULL,
    last_ts       REAL NOT NULL,
    cleared_ts    REAL DEFAULT 0,
    PRIMARY KEY (family, reason)
);
"""


@dataclass
class Experiment:
    """One thing the system tried, and what it learned."""

    candidate_id: str = ""
    signature: str = ""
    family: str = ""
    source: str = ""
    parent_id: str = ""
    hypothesis: str = ""
    rule: dict = field(default_factory=dict)
    features: list[str] = field(default_factory=list)
    status: str = ""
    maturity: str = ""
    markets: int = 0
    forward_markets: int = 0
    trades: int = 0
    expectancy: float = 0.0
    drawdown: float = 0.0
    top_share: float = 0.0
    result: str = R_INCONCLUSIVE
    failure_reason: str = ""
    robustness: float = 0.0
    coverage: float = 0.0
    adversarial: dict = field(default_factory=dict)
    directive: str = ""
    lesson: str = ""

    @property
    def fingerprint(self) -> str:
        """What must change before this is worth writing down again.

        Trade count is bucketed, not exact. Without the bucket a candidate
        picking up one trade a pass would write a fresh row every hour with
        nothing new in it; with it, the row appears when the evidence has
        moved enough to possibly mean something different.
        """
        bucket = 0 if self.trades < 10 else (self.trades // 10)
        return (f"{self.result}|{self.failure_reason}|{self.status}|"
                f"{bucket}|{self.markets}")


class ExperimentStore:
    """Append-only research memory. Holds no evidence and no verdicts."""

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

    def record(self, experiment: Experiment) -> bool:
        """Write it if it says something new. Returns whether it was written."""
        with self._lock:
            row = self._conn.execute(
                "SELECT result, failure_reason, status, trades, markets "
                "FROM experiments WHERE candidate_id=? ORDER BY ts DESC "
                "LIMIT 1", (experiment.candidate_id,)).fetchone()
            if row is not None:
                bucket = 0 if int(row["trades"]) < 10 else int(row["trades"]) // 10
                previous = (f"{row['result']}|{row['failure_reason']}|"
                            f"{row['status']}|{bucket}|{int(row['markets'])}")
                if previous == experiment.fingerprint:
                    return False
            self._conn.execute(
                "INSERT INTO experiments(ts, candidate_id, signature, family, "
                "source, parent_id, hypothesis, rule, features, status, "
                "maturity, markets, forward_markets, trades, expectancy, "
                "drawdown, top_share, result, failure_reason, robustness, "
                "coverage, adversarial, directive, lesson) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), experiment.candidate_id, experiment.signature,
                 experiment.family, experiment.source, experiment.parent_id,
                 experiment.hypothesis,
                 json.dumps(experiment.rule, default=str),
                 json.dumps(sorted(experiment.features)), experiment.status,
                 experiment.maturity, int(experiment.markets),
                 int(experiment.forward_markets), int(experiment.trades),
                 float(experiment.expectancy), float(experiment.drawdown),
                 float(experiment.top_share), experiment.result,
                 experiment.failure_reason, float(experiment.robustness),
                 float(experiment.coverage),
                 json.dumps(experiment.adversarial, default=str),
                 experiment.directive, experiment.lesson))
            self._conn.commit()
        if experiment.result == R_FAILED and experiment.failure_reason:
            self._strike(experiment.family, experiment.failure_reason,
                         experiment.candidate_id)
        elif experiment.result in (R_VALIDATED, R_PROMISING):
            # A success anywhere in the family reopens it entirely. The
            # memory exists to stop repetition, not to close off a search
            # that has just shown it was worth running.
            self._clear(experiment.family)
        return True

    def _strike(self, family: str, reason: str, candidate_id: str) -> None:
        if not family or reason in _NOT_THE_IDEAS_FAULT:
            return
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT candidates FROM dead_ends WHERE family=? AND "
                "reason=?", (family, reason)).fetchone()
            try:
                known = set(json.loads(row["candidates"])) if row else set()
            except (TypeError, ValueError):
                known = set()
            if candidate_id in known:
                return                       # one candidate, one strike
            known.add(candidate_id)
            self._conn.execute(
                "INSERT INTO dead_ends(family, reason, candidates, strikes, "
                "first_ts, last_ts, cleared_ts) VALUES(?,?,?,?,?,?,0) "
                "ON CONFLICT(family, reason) DO UPDATE SET "
                "candidates=excluded.candidates, strikes=excluded.strikes, "
                "last_ts=excluded.last_ts, cleared_ts=0",
                (family, reason, json.dumps(sorted(known)), len(known),
                 now, now))
            self._conn.commit()

    def _clear(self, family: str) -> None:
        if not family:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE dead_ends SET cleared_ts=? WHERE family=? AND "
                "cleared_ts=0", (time.time(), family))
            self._conn.commit()

    # -- reads -----------------------------------------------------------

    def dead_ends(self) -> dict[str, list[str]]:
        """family -> reasons it has repeatedly died of, still in force."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT family, reason FROM dead_ends WHERE strikes >= ? "
                "AND cleared_ts = 0", (DEAD_END_STRIKES,)).fetchall()
        out: dict[str, list[str]] = {}
        for row in rows:
            out.setdefault(str(row["family"]), []).append(str(row["reason"]))
        return out

    def is_dead_end(self, family: str) -> tuple[bool, str]:
        """Whether this family's registrations should be throttled, and why.

        Throttled, never blocked. The caller reduces the family's slots; it
        does not refuse the idea, because a family that has failed three times
        for one reason may still be right under a condition nobody has stated
        yet, and refusing it outright is how the search closes.
        """
        reasons = self.dead_ends().get(str(family) or "", [])
        if not reasons:
            return False, ""
        return True, ", ".join(sorted(reasons))

    def history(self, candidate_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM experiments WHERE candidate_id=? ORDER BY ts",
                (candidate_id,)).fetchall()
        return [dict(r) for r in rows]

    def latest_directives(self) -> dict[str, str]:
        """candidate id -> its most recent follow-up directive, in one query.

        Read at the start of a pass so the pass can act on what the LAST one
        concluded. That lag is the design, not a compromise: a directive is
        the product of a completed classification, and classifying mid-pass
        from half-updated evidence would be acting on a conclusion the pass
        has not finished reaching.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT e.candidate_id, e.directive FROM experiments e "
                "JOIN (SELECT candidate_id, MAX(ts) ts FROM experiments "
                "GROUP BY candidate_id) latest "
                "ON latest.candidate_id = e.candidate_id "
                "AND latest.ts = e.ts WHERE e.directive != ''").fetchall()
        return {str(r["candidate_id"]): str(r["directive"]) for r in rows}

    def by_reason(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT failure_reason, COUNT(DISTINCT candidate_id) n FROM "
                "experiments WHERE failure_reason != '' GROUP BY "
                "failure_reason").fetchall()
        return {str(r["failure_reason"]): int(r["n"]) for r in rows}

    def directives(self, limit: int = 40) -> list[dict]:
        """Open follow-up questions, newest first — the research to-do list.

        One per candidate: the latest classification supersedes its
        predecessors, and showing a candidate's whole history here would
        present resolved questions as open ones.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT candidate_id, family, failure_reason, directive, "
                "lesson, MAX(ts) ts FROM experiments WHERE directive != '' "
                "GROUP BY candidate_id ORDER BY ts DESC LIMIT ?",
                (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def summary(self) -> dict:
        """§13's research-health block for the memory itself."""
        with self._lock:
            totals = self._conn.execute(
                "SELECT COUNT(*) rows, COUNT(DISTINCT candidate_id) subjects "
                "FROM experiments").fetchone()
            by_result = self._conn.execute(
                "SELECT result, COUNT(DISTINCT candidate_id) n FROM "
                "experiments GROUP BY result").fetchall()
            dead = self._conn.execute(
                "SELECT COUNT(*) n FROM dead_ends WHERE strikes >= ? AND "
                "cleared_ts = 0", (DEAD_END_STRIKES,)).fetchone()
            open_dirs = self._conn.execute(
                "SELECT COUNT(DISTINCT candidate_id) n FROM experiments "
                "WHERE directive != ''").fetchone()
        return {
            "experimentsRecorded": int(totals["rows"] or 0),
            "experimentSubjects": int(totals["subjects"] or 0),
            "experimentsByResult": {str(r["result"]): int(r["n"])
                                    for r in by_result},
            "experimentFailureReasons": self.by_reason(),
            "deadEndFamilies": int(dead["n"] or 0),
            "openResearchDirectives": int(open_dirs["n"] or 0),
        }


def from_candidate(entry: dict, cumulative: dict, cfg,
                   adversarial: Optional[Any] = None,
                   attempts: Optional[dict] = None,
                   maturity: str = "",
                   hypothesis: str = "") -> Experiment:
    """Build the record for one library row. Pure; the store does the writing."""
    from .feature_domain import features_of

    result, reason = classify(entry.get("status") or "", cumulative, cfg,
                              adversarial, attempts)
    directive, lesson = next_question(reason)
    rule = entry.get("rule") or {}
    return Experiment(
        candidate_id=str(entry.get("id") or ""),
        signature=str(entry.get("signature") or ""),
        family=str(entry.get("family") or ""),
        source=str(entry.get("source") or ""),
        parent_id=str(entry.get("parent_id") or ""),
        hypothesis=hypothesis,
        rule=rule,
        features=sorted(features_of(rule)),
        status=str(entry.get("status") or ""),
        maturity=maturity,
        markets=int(cumulative.get("markets") or 0),
        forward_markets=int(cumulative.get("forward_markets") or 0),
        trades=int(cumulative.get("trades") or 0),
        expectancy=float(cumulative.get("expectancy") or 0.0),
        drawdown=float(cumulative.get("drawdown") or 0.0),
        top_share=float(cumulative.get("top_share") or 0.0),
        result=result, failure_reason=reason,
        robustness=float(getattr(adversarial, "robustness", 0.0) or 0.0),
        coverage=float(getattr(adversarial, "coverage", 0.0) or 0.0),
        adversarial=(adversarial.to_dict()
                     if adversarial is not None else {}),
        directive=directive, lesson=lesson)
