"""CONSISTENCY / LOSS-MINIMISATION — Layer 2, and nothing else.

The strategy in ``bridge/baseline_engine.py`` is Layer 1 and is not touched by
this file. Every existing exit — stop, trailing, edge-gone, take-profit,
wallet, reduce, stagnation, time decay, resolution — keeps its exact place in
the ordering and its exact behaviour. This module can only ever look at a
position the strategy has already decided to HOLD, and say one of two things:

    "the reason you are in this trade has failed"   -> an exit candidate
    (anything else)                                 -> stay out of the way

That asymmetry is structural, not a convention. :func:`evaluate` is called by
the engine *after* Layer 1 has returned, and only when Layer 1 returned HOLD,
so there is no code path by which this can displace a take-profit, widen a
stop, or keep a position the strategy wanted closed. The observation that
motivated the whole layer — take-profit exits at +30.8% average against stop
exits at -46.3% — means the winner engine is the asset and the loss tail is
the liability, and a patch that could reach the winners would be trading the
asset for the liability.

**The distinction this file exists to make.** A position that is red is not
the same as a position that is wrong. The strategy entered because a set of
conditions held; the only question worth asking mid-trade is whether those
same conditions still hold. So THESIS_HEALTH is computed from the ORIGINAL
ENTRY THESIS (:class:`EntryThesis`, captured at entry and hydrated back from
the journal), never from the P&L, and never from a new view of the market
this module invented. Unrealised loss is not an input to it. That is the
point: temporary adverse movement inside a winner's normal path and genuine
thesis failure look identical on the P&L line and completely different here.

**Why nothing here fires by default.** ``mode`` is ``"shadow"``, the loss-tail
guard is off, the profit floor is off, and ``min_adverse_room_pct`` is 0.
A shadow verdict is recorded, journalled and measured; it changes no decision.
Promotion to ``"enforce"`` is earned in :mod:`pqb.analytics.consistency_research`
against walk-forward evidence, not chosen here — none of the thresholds in
:class:`~pqb.config.ConsistencyConfig` were derived from the 16-trade sample,
and the two that would need a distribution to set honestly (the room a winner
needs, the level at which profit is worth protecting) default to disabled
rather than to a guess.

Everything in here is a pure function over plain values. No I/O, no clock
beyond what is passed in, no engine, no config loading — so the whole layer is
testable without a trading loop, and a bug in it cannot do anything worse than
name an exit that shadow mode then discards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ---------------------------------------------------------------------------
# Thesis health states (Module 3)
# ---------------------------------------------------------------------------

HEALTHY = "HEALTHY"
WEAKENING = "WEAKENING"
INVALIDATED = "INVALIDATED"
UNKNOWN = "UNKNOWN"

STATES = (HEALTHY, WEAKENING, INVALIDATED, UNKNOWN)

# UNKNOWN is a first-class answer, not a failure mode, and it never triggers
# anything. A position whose entry decision predates this patch has no stored
# thesis, and "we cannot tell" must not be allowed to read as "it has failed" —
# that would make the layer most aggressive exactly where it knows least.


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _f(value: Any, default: float = 0.0) -> Optional[float]:
    """Coerce to a float, or None. None means MISSING, which is not zero.

    A spread of 0.0 and an unrecorded spread lead to opposite conclusions about
    whether the book has deteriorated, so they must not collapse into the same
    value on the way in.
    """
    if value is None:
        return default if default is not None else None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


# ---------------------------------------------------------------------------
# Module 1 — the trade state record
# ---------------------------------------------------------------------------


@dataclass
class EntryThesis:
    """What was true when the position was opened, as the entry recorded it.

    Reconstructed by the runner from the entry decision's own rationale and
    feature vector, both of which the journal already stores for every entry.
    Nothing here is recomputed from today's market: the whole value of the
    record is that it is the past, unrevised.

    `available` is a list of the conditions this entry can actually be
    re-checked against. An entry that never recorded its liquidity cannot have
    a liquidity-collapse test run on it, and running one anyway against a
    default of 0.0 would report every such trade as collapsed.
    """

    score: Optional[float] = None            # the blended entry score
    market_score: Optional[float] = None
    wallet_score: Optional[float] = None
    wallet_influence: str = ""               # labels of the wallets that drove it
    price: Optional[float] = None
    liquidity: Optional[float] = None
    spread: Optional[float] = None
    depth: Optional[float] = None
    market_state: str = ""                   # ms_state at entry, if captured
    seconds_to_resolution: Optional[float] = None
    ts: float = 0.0

    @property
    def available(self) -> list:
        out = []
        if self.score is not None:
            out.append("score")
        if self.wallet_score and self.wallet_influence:
            out.append("wallet")
        if self.liquidity is not None and self.liquidity > 0:
            out.append("liquidity")
        if self.spread is not None:
            out.append("spread")
        if self.price is not None and self.price > 0:
            out.append("price_band")
        if self.market_state:
            out.append("market_state")
        return out

    def to_dict(self) -> dict:
        return {
            "score": self.score, "marketScore": self.market_score,
            "walletScore": self.wallet_score,
            "walletInfluence": self.wallet_influence,
            "price": self.price, "liquidity": self.liquidity,
            "spread": self.spread, "depth": self.depth,
            "marketState": self.market_state,
            "secondsToResolution": self.seconds_to_resolution,
            "available": self.available,
        }

    @classmethod
    def from_journal(cls, rationale: Optional[dict],
                     features: Optional[dict],
                     entry_ts: float = 0.0,
                     wallet_influence: str = "") -> "EntryThesis":
        """Rebuild from the stored entry decision. Missing stays missing.

        `rationale` is ``decisions.rationale`` for the entry (components, the
        quote, the market's liquidity); `features` is ``decisions.features``,
        the full captured vector, which is where depth and the Market-State
        classification live. Either may be absent on a position opened before
        a given column existed, and the result is simply a thesis with fewer
        `available` conditions — which the health detector then reports as
        UNKNOWN rather than guessing.
        """
        rationale = rationale or {}
        features = features or {}
        parts = rationale.get("components") or {}
        quote = rationale.get("quote") or {}

        state = features.get("ms_state")
        # ms_state is captured as a number (the enum's ordinal) and is only
        # meaningful as an identity — it is compared for change, never ordered.
        state = "" if state in (None, "") else str(state)

        return cls(
            score=_f(parts.get("final"), None),
            market_score=_f(parts.get("marketScore"), None),
            wallet_score=_f(parts.get("walletScore"), None),
            wallet_influence=str(wallet_influence or ""),
            price=_f(quote.get("ask") or quote.get("mid")
                     or features.get("price"), None),
            liquidity=_f(rationale.get("liquidity")
                         or features.get("liquidity"), None),
            spread=_f(quote.get("spread") if quote.get("spread") is not None
                      else features.get("spread"), None),
            depth=_f(features.get("depth_total"), None),
            market_state=state,
            seconds_to_resolution=_f(rationale.get("secondsToResolution"),
                                     None),
            ts=float(entry_ts or 0.0),
        )


@dataclass
class TradeState:
    """The compact state record for one open position (Module 1).

    Deliberately a COLLECTION, not a signal. Most of these fields are not read
    by any rule in this file; they are here to be journalled so the research
    layer can find out which of them actually separate winners from losers, and
    the ones that do not are expected to stay unused. Adding a variable to a
    rule because it is available is how a system ends up with twelve parameters
    fitted to sixteen trades.

    `unavailable` names what this build could not measure for this position, so
    an aggregate never averages a missing number as a zero.
    """

    token_id: str = ""
    market_id: str = ""
    lifecycle_id: Optional[int] = None

    # -- the path ---------------------------------------------------------
    entry_price: float = 0.0
    entry_ts: float = 0.0
    price: float = 0.0
    peak_price: float = 0.0
    trough_price: float = 0.0
    unrealized_pct: float = 0.0
    unrealized_usdc: float = 0.0
    mfe: float = 0.0                 # maximum favourable excursion, on entry
    mae: float = 0.0                 # maximum adverse excursion, on entry (<=0)
    held_seconds: float = 0.0
    velocity: Optional[float] = None      # return per hour since entry
    acceleration: Optional[float] = None  # recent velocity vs whole-trade

    # -- the evidence, then and now ---------------------------------------
    entry_score: Optional[float] = None
    current_score: float = 0.0            # Layer 1's live hold conviction
    wallet_score: Optional[float] = None
    wallet_exited: bool = False
    market_state_at_entry: str = ""
    market_state_now: str = ""
    spread: Optional[float] = None
    entry_spread: Optional[float] = None
    depth: Optional[float] = None
    liquidity: Optional[float] = None
    entry_liquidity: Optional[float] = None
    correlated_positions: int = 0

    # -- distances (Module 1's "distance from entry / from thesis") --------
    distance_from_entry: float = 0.0      # |price - entry| / entry
    thesis_failures: list = field(default_factory=list)
    thesis_available: list = field(default_factory=list)

    unavailable: list = field(default_factory=list)

    @property
    def is_losing(self) -> bool:
        return self.unrealized_pct < 0.0

    def to_dict(self) -> dict:
        """Compact enough to journal every cycle without growing the DB fast."""
        return {
            "lifecycleId": self.lifecycle_id,
            "entryPrice": round(self.entry_price, 6),
            "price": round(self.price, 6),
            "returnPct": round(self.unrealized_pct, 6),
            "unrealizedUsdc": round(self.unrealized_usdc, 6),
            "mfe": round(self.mfe, 6), "mae": round(self.mae, 6),
            "heldSeconds": round(self.held_seconds, 1),
            "velocity": (round(self.velocity, 6)
                         if self.velocity is not None else None),
            "acceleration": (round(self.acceleration, 6)
                             if self.acceleration is not None else None),
            "entryScore": self.entry_score,
            "currentScore": round(self.current_score, 4),
            "walletExited": self.wallet_exited,
            "marketStateAtEntry": self.market_state_at_entry,
            "marketStateNow": self.market_state_now,
            "spread": self.spread, "entrySpread": self.entry_spread,
            "liquidity": self.liquidity, "entryLiquidity": self.entry_liquidity,
            "correlatedPositions": self.correlated_positions,
            "thesisFailures": list(self.thesis_failures),
            "thesisAvailable": list(self.thesis_available),
            "unavailable": list(self.unavailable),
        }


def build_state(position: Any, thesis: EntryThesis, conviction: float,
                now: float, market: Any = None, quote: Any = None,
                wallet_exited: bool = False,
                correlated_positions: int = 0,
                market_state_now: str = "") -> TradeState:
    """Assemble the state record from what the engine already has in hand.

    Everything here is read off objects the engine holds anyway — no lookup, no
    query, no allocation beyond the record itself — because this runs once per
    open position per cycle and the cycle budget belongs to the strategy.
    """
    entry = float(getattr(position, "avg_price", 0.0) or 0.0)
    price = float(getattr(position, "cur_price", 0.0) or entry)
    peak = float(getattr(position, "peak_price", 0.0) or 0.0)
    trough = float(getattr(position, "trough_price", 0.0) or 0.0)
    opened = float(getattr(position, "opened_ts", 0) or 0.0)
    held = max(0.0, now - opened) if opened else 0.0

    state = TradeState(
        token_id=str(getattr(position, "token_id", "") or ""),
        market_id=str(getattr(position, "market_id", "") or ""),
        lifecycle_id=getattr(position, "lifecycle_id", None),
        entry_price=entry, entry_ts=opened, price=price,
        peak_price=peak, trough_price=trough,
        unrealized_pct=float(getattr(position, "return_pct", 0.0) or 0.0),
        unrealized_usdc=float(getattr(position, "unrealized_pnl", 0.0) or 0.0),
        held_seconds=held,
        entry_score=thesis.score,
        current_score=float(conviction or 0.0),
        wallet_score=thesis.wallet_score,
        wallet_exited=bool(wallet_exited),
        market_state_at_entry=thesis.market_state,
        market_state_now=str(market_state_now or ""),
        entry_spread=thesis.spread,
        entry_liquidity=thesis.liquidity,
        correlated_positions=int(correlated_positions or 0),
        thesis_available=thesis.available,
    )

    if entry > 0 and peak > 0:
        state.mfe = peak / entry - 1.0
    else:
        state.unavailable.append("mfe")
    if entry > 0 and trough > 0:
        state.mae = trough / entry - 1.0
    else:
        state.unavailable.append("mae")
    if entry > 0:
        state.distance_from_entry = abs(price - entry) / entry

    if held > 0 and entry > 0:
        state.velocity = (price / entry - 1.0) / (held / 3600.0)
    else:
        state.unavailable.append("velocity")

    # Acceleration without a tick store: how the move since the peak/trough
    # compares with the move over the whole trade. It is a coarse second
    # difference and it is labelled as one — the alternative is a per-position
    # price buffer in the hot loop, which is a new architecture for a variable
    # no rule here reads yet.
    if state.velocity is not None and peak > 0 and trough > 0 and entry > 0:
        recent = (price - (peak if price < peak else trough)) / entry
        state.acceleration = recent
    else:
        state.unavailable.append("acceleration")

    if quote is not None:
        state.spread = _f(getattr(quote, "spread", None), None)
        bid_depth = _f(getattr(quote, "bid_depth", None), None) or 0.0
        ask_depth = _f(getattr(quote, "ask_depth", None), None) or 0.0
        state.depth = bid_depth + ask_depth
    else:
        state.unavailable.append("book")
    if market is not None:
        state.liquidity = _f(getattr(market, "liquidity", None), None)
    else:
        state.unavailable.append("market")
    return state


# ---------------------------------------------------------------------------
# Module 3 — the early-invalidation detector
# ---------------------------------------------------------------------------


@dataclass
class ThesisVerdict:
    """The health reading, and the evidence for it.

    `failures` and `checked` are both reported because a verdict of WEAKENING
    on one failed condition out of five and one out of two are different
    statements, and the ratio is what the research layer needs to know whether
    the detector is worth anything.
    """

    state: str = UNKNOWN
    failures: list = field(default_factory=list)
    checked: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)

    @property
    def failed(self) -> int:
        return len(self.failures)

    def to_dict(self) -> dict:
        return {"state": self.state, "failures": list(self.failures),
                "checked": list(self.checked), "detail": dict(self.detail)}


def thesis_health(cfg, state: TradeState, thesis: EntryThesis,
                  band: Optional[tuple] = None) -> ThesisVerdict:
    """Is what the entry depended on still true? (Module 3.)

    `band` is the entry gate's ``(min_price, max_price)``, passed in rather
    than duplicated into this module's config: it is the *entry* config's
    number, and a second copy here would silently stop matching the gate the
    day the gate is retuned. ``None`` simply drops that one condition.

    Each condition below is one of the things the entry actually used, checked
    against its own recorded value at entry. Note what is absent: the position's
    P&L. A losing position with every entry condition intact is HEALTHY here,
    and a winning position whose conditions have all failed is INVALIDATED —
    which is the only way the layer can tell temporary adverse movement from
    genuine failure, and the reason Module 4 can say "do not exit merely
    because it is red" and have that mean something mechanical.

    A condition that cannot be checked is not a failed condition. Conditions
    that could not be checked never enter the count in either direction, and
    if too few remain the answer is UNKNOWN.
    """
    verdict = ThesisVerdict()
    checked: list[str] = []
    failures: list[str] = []
    detail: dict = {}

    available = set(thesis.available)

    # 1. The score the entry cleared. Conviction is Layer 1's own live view of
    #    whether holding is still supported, so a large decay in it is the
    #    single most direct statement that the reason for entering is gone.
    if "score" in available and thesis.score:
        checked.append("score")
        floor = float(thesis.score) * float(
            getattr(cfg, "score_floor_fraction", 0.6) or 0.6)
        detail["scoreFloor"] = round(floor, 4)
        if state.current_score < floor:
            failures.append("score")

    # 2. The wallet evidence. Only checked when a wallet is WHY we are here —
    #    an entry that scored on market quality alone cannot fail a wallet test,
    #    and counting one against it would penalise the entries with the fewest
    #    moving parts.
    if "wallet" in available:
        checked.append("wallet")
        if state.wallet_exited:
            failures.append("wallet")

    # 3. Liquidity. Not "is it thin" — is it materially thinner than the book
    #    the entry was scored against. An entry into a thin market knew it was
    #    thin; a market that has lost half its depth since is a different one.
    if "liquidity" in available and state.liquidity is not None:
        checked.append("liquidity")
        ratio = state.liquidity / max(1e-9, float(thesis.liquidity or 0.0))
        detail["liquidityRatio"] = round(ratio, 4)
        if ratio < float(getattr(cfg, "liquidity_collapse_fraction", 0.5)
                         or 0.5):
            failures.append("liquidity")

    # 4. Spread. Same logic: a blown-out spread means the exit will not pay
    #    what the mark says, which is a deterioration in the trade regardless
    #    of where the mid sits.
    if "spread" in available and state.spread is not None:
        checked.append("spread")
        base = float(thesis.spread or 0.0)
        multiple = float(getattr(cfg, "spread_blowout_multiple", 2.0) or 2.0)
        if base > 0:
            detail["spreadMultiple"] = round(state.spread / base, 4)
            if state.spread > base * multiple:
                failures.append("spread")

    # 5. The price band. The entry gate refuses prices outside
    #    [min_price, max_price]; a position that has drifted out of that band
    #    is one the strategy would no longer open, which is as clean a
    #    statement of "this is not the trade any more" as the config contains.
    low, high = (band or (None, None))
    if low is not None and high is not None and state.price > 0:
        checked.append("price_band")
        detail["band"] = [low, high]
        if not (low <= state.price <= high):
            failures.append("price_band")

    # 6. Market state. Compared as an identity, never ordered: the classifier's
    #    output is a label (DORMANT..REVERSAL) and the numeric form it is
    #    captured in is an ordinal, so "greater than" would be meaningless.
    if "market_state" in available and state.market_state_now:
        checked.append("market_state")
        if state.market_state_now != state.market_state_at_entry:
            failures.append("market_state")

    verdict.checked = checked
    verdict.failures = failures
    verdict.detail = detail

    min_evidence = int(getattr(cfg, "min_evidence", 2) or 2)
    if len(checked) < min_evidence:
        verdict.state = UNKNOWN
        return verdict

    invalidate_at = int(getattr(cfg, "invalidate_at", 2) or 2)
    weakening_at = int(getattr(cfg, "weakening_at", 1) or 1)
    if len(failures) >= invalidate_at:
        verdict.state = INVALIDATED
    elif len(failures) >= weakening_at:
        verdict.state = WEAKENING
    else:
        verdict.state = HEALTHY
    return verdict


# ---------------------------------------------------------------------------
# Modules 4, 7, 8, 19 — the safety exit
# ---------------------------------------------------------------------------


@dataclass
class SafetyVerdict:
    """What Layer 2 would do, and whether it is allowed to do it.

    `triggered` and `enforced` are separate on purpose, for the same reason the
    risk patch separated `block_reason` from `shadow_block_reason`: shadow mode
    is worthless if the thing it is shadowing has to be suppressed at the
    source to keep it quiet. Both modes compute the identical verdict and log
    it identically; only `enforced` differs, so the two can be compared
    directly on the same journal.
    """

    triggered: bool = False
    enforced: bool = False
    style: str = ""              # thesis_invalidated | loss_tail | profit_floor
    reason: str = ""
    health: str = UNKNOWN
    streak: int = 0
    state: Optional[TradeState] = None
    thesis: Optional[ThesisVerdict] = None
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "triggered": self.triggered, "enforced": self.enforced,
            "style": self.style, "reason": self.reason,
            "health": self.health, "streak": self.streak,
            "notes": list(self.notes),
            "state": self.state.to_dict() if self.state else None,
            "thesis": self.thesis.to_dict() if self.thesis else None,
        }


def evaluate(cfg, state: TradeState, thesis: EntryThesis,
             prior_state: str = "", prior_streak: int = 0,
             equity: float = 0.0, band: Optional[tuple] = None) -> SafetyVerdict:
    """The whole of Layer 2, as one pure function (Module 19).

    Called only on a position Layer 1 has already decided to HOLD. Returns a
    verdict whose `enforced` flag is the only thing the engine acts on, and
    which is false in shadow mode however severe the finding.

    Order inside the layer is worst-consequence-first, and each of the three
    is a different kind of statement:

      1. LOSS_TAIL_GUARD (Module 7) — account protection. Not a view on the
         trade at all: a single position has become large enough relative to
         equity that its remaining downside is a portfolio problem. Kept
         independent of the strategy stop, and off unless configured.
      2. Confirmed thesis invalidation (Modules 3, 4) — the reason for the
         trade has failed, for `confirm_cycles` consecutive readings.
      3. Profit floor (Module 8) — a position that ran far enough to be worth
         protecting has given most of it back. Off unless configured, and
         deliberately last: it is the only one of the three that can touch a
         trade that was winning.
    """
    verdict = SafetyVerdict(state=state)
    if not getattr(cfg, "enabled", False):
        return verdict

    mode = str(getattr(cfg, "mode", "shadow")).lower()
    if mode == "off":
        return verdict
    enforce = mode == "enforce"

    health = thesis_health(cfg, state, thesis, band)
    verdict.thesis = health
    verdict.health = health.state

    # The streak is the confirmation requirement (Module 4: an early-exit
    # CANDIDATE, not an exit). One cycle of INVALIDATED is a reading; several
    # consecutive ones are a finding. Anything other than INVALIDATED resets
    # it, so a flickering condition can never accumulate its way to an exit.
    streak = (int(prior_streak or 0) + 1) if health.state == INVALIDATED \
        and prior_state == INVALIDATED else (1 if health.state == INVALIDATED
                                             else 0)
    verdict.streak = streak

    grace = float(getattr(cfg, "grace_seconds", 0.0) or 0.0)
    if state.held_seconds < grace:
        verdict.notes.append(
            f"within the {grace / 60:.0f} min grace window; no opinion yet")
        return verdict

    def fire(style: str, reason: str) -> SafetyVerdict:
        verdict.triggered = True
        verdict.enforced = enforce
        verdict.style = style
        verdict.reason = reason
        return verdict

    # -- 1. loss tail guard (Module 7) -------------------------------------
    tail = _loss_tail(cfg, state, equity)
    if tail:
        return fire("loss_tail", tail)

    # -- 2. confirmed thesis invalidation (Modules 3, 4) -------------------
    confirm = int(getattr(cfg, "confirm_cycles", 3) or 3)
    if health.state == INVALIDATED and streak >= confirm:
        # The winner-room floor is the promise made to Module 6, enforced in
        # code rather than in a document: below the distance a historical
        # winner normally travels against us, this layer does not act at all,
        # however certain the thesis reading is. 0 means the distribution has
        # not been measured yet, in which case nothing may fire on it.
        room = float(getattr(cfg, "min_adverse_room_pct", 0.0) or 0.0)
        if room <= 0:
            verdict.notes.append(
                "thesis invalidated and confirmed, but no validated winner-room "
                "distance is configured — research has not yet established how "
                "much room a real winner needs, so nothing acts on this")
            return verdict
        if state.unrealized_pct > -room:
            verdict.notes.append(
                f"thesis invalidated and confirmed, but {state.unrealized_pct:+.1%} "
                f"is still inside the {room:.1%} of adverse movement a winner "
                "normally needs; holding")
            return verdict
        return fire("thesis_invalidated", (
            f"Thesis has failed: {', '.join(health.failures)} "
            f"({health.failed} of {len(health.checked)} entry conditions) for "
            f"{streak} consecutive readings, and the position is "
            f"{state.unrealized_pct:+.1%} — past the {room:.1%} a winner "
            "normally needs. The reason for this trade no longer exists."))

    if health.state == WEAKENING:
        # Module 4: weakening raises monitoring sensitivity and does nothing
        # else. There is no action here by design — the note exists so the
        # research layer can measure how often WEAKENING preceded a loss.
        verdict.notes.append(
            f"weakening: {', '.join(health.failures)} of "
            f"{len(health.checked)} entry conditions; monitoring only")

    # -- 3. profit floor (Module 8) ----------------------------------------
    floor = _profit_floor(cfg, state)
    if floor:
        return fire("profit_floor", floor)

    return verdict


def _loss_tail(cfg, state: TradeState, equity: float) -> str:
    """Module 7. Account protection, independent of the strategy stop.

    Deliberately NOT a percentage of the position: the strategy stop already
    governs that, and duplicating it here would be a second stop wearing a
    different name. This binds on the loss as a share of the ACCOUNT, which is
    the quantity the strategy stop cannot see and the only one that makes a
    single trade a portfolio event.
    """
    if not getattr(cfg, "loss_tail_enabled", False):
        return ""
    cap = float(getattr(cfg, "max_single_trade_loss_pct", 0.0) or 0.0)
    if cap <= 0 or equity <= 0:
        return ""
    loss = -min(0.0, state.unrealized_usdc)
    if loss <= 0:
        return ""
    share = loss / equity
    if share < cap:
        return ""
    return (f"Loss-tail guard: this position is down ${loss:,.2f}, "
            f"{share:.1%} of the ${equity:,.2f} account, at or past the "
            f"{cap:.1%} single-trade ceiling. Account protection, not a view "
            "on the market.")


def _profit_floor(cfg, state: TradeState) -> str:
    """Module 8. Conservative by construction, and off until measured.

    Arms only after a position has shown `profit_floor_arm_pct` of favourable
    excursion, then protects `profit_floor_keep_fraction` of it. The explicit
    requirement is that a winner which normally travels +30% must not be
    repeatedly stopped at +5%, so the arm level is a research output and the
    default is 0 — disabled — rather than a number chosen here.
    """
    arm = float(getattr(cfg, "profit_floor_arm_pct", 0.0) or 0.0)
    if arm <= 0 or state.mfe < arm:
        return ""
    keep = _clamp(float(getattr(cfg, "profit_floor_keep_fraction", 0.5) or 0.5))
    floor = state.mfe * keep
    if state.unrealized_pct > floor:
        return ""
    return (f"Profit floor: ran to {state.mfe:+.1%} and has given back to "
            f"{state.unrealized_pct:+.1%}, below the {floor:+.1%} floor "
            f"({keep:.0%} of the best excursion). Banking what is left.")


# ---------------------------------------------------------------------------
# Helpers the engine needs, kept here so the policy's inputs are defined next
# to the policy
# ---------------------------------------------------------------------------


def correlated_count(position: Any, positions: Iterable[Any]) -> int:
    """How many OTHER open positions are the same economic bet.

    Same cluster rule as the risk patch uses (:func:`pqb.riskpolicy.cluster_key`)
    — same market, else same wallet thesis — so the two layers agree on what
    "correlated" means rather than each having a private definition.
    """
    market = str(getattr(position, "market_id", "") or "")
    token = str(getattr(position, "token_id", "") or "")
    if not market:
        return 0
    return sum(1 for p in positions
               if str(getattr(p, "token_id", "") or "") != token
               and str(getattr(p, "market_id", "") or "") == market)
