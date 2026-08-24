"""CAPITAL PRESERVATION — bounded, auditable, and deliberately not clever.

The account has fallen from $100 to $41.86 while exactly one strategy is
validated. Two things must be true at once while that is being diagnosed:

* the validated strategy must keep being allowed to express its edge, because
  a strategy that is prevented from trading cannot be measured; and
* a bad sequence must not be able to end the account before the diagnosis is
  finished.

This module is the second of those and nothing else. It is a set of CAPS. It
has no view on any market, never sizes a position up, never reads a strategy,
and cannot open or close anything. Given the account state and the open book,
it answers two questions:

    may a new entry be opened at all?      -> entry_block()
    by how much should stakes be shrunk?   -> size_scale()

Everything it can do is bounded below by a configured floor, so the worst a
bug here can do is trade smaller. Four behaviours are absent by construction
rather than by discipline, because they are the ways an account like this
usually dies:

* **no martingale** — `size_scale` is monotonically non-increasing in
  drawdown, so a loss can never raise the next stake;
* **no revenge trading** — there is no term for "recent losses", only for
  current exposure and current drawdown;
* **no leverage escalation** — the multiplier is clamped to `<= 1.0`; it can
  only ever shrink what the engine already decided;
* **no exit blocking** — `entry_block` names its reason and the runner applies
  it to BUYs only. Getting out of a position is how risk is reduced, and a
  control that trapped capital in a loser would defeat its own purpose.

Correlated exposure is treated as the thing it is: four positions in one
category are one bet held four times. The cap is computed on the *effective*
exposure, not the position count.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional


@dataclass
class ExposureView:
    """One open position, as the policy needs to see it. Deliberately plain
    numbers so this is testable without an engine, an exchange or a config."""

    token_id: str
    market_id: str = ""
    category: str = ""
    wallet_thesis: str = ""
    value: float = 0.0            # mark-to-market
    cost: float = 0.0             # what it was opened for


@dataclass
class GuardState:
    """Latched state for the session guards. Persisted by the runner.

    The existing caps are stateless: they read the account and decide. The two
    guards added by the surgical risk patch cannot be, for opposite reasons.

    * The equity-peak guard needs HYSTERESIS. A bare threshold at 5% flips to
      blocked at 5.01% and back to clear at 4.99%, which on a live equity curve
      means it chatters every cycle and logs a transition each time. So the
      guard latches: it arms at `peak_guard_threshold` and only releases at
      `peak_guard_recovery`, and between the two it holds whatever it already
      was.

    * The loss-cascade guard needs a DEADLINE, because "pause for 30 minutes"
      is not a function of the current account state at all.

    Persisted rather than recomputed for the same reason peak equity is: a
    restart must not silently release a guard, and a process bounce is exactly
    when that would be most damaging.
    """

    equity_guard_active: bool = False
    cascade_pause_until: float = 0.0

    def to_dict(self) -> dict:
        return {
            "equityGuardActive": bool(self.equity_guard_active),
            "cascadePauseUntil": float(self.cascade_pause_until),
        }

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "GuardState":
        data = data or {}
        return cls(
            equity_guard_active=bool(data.get("equityGuardActive", False)),
            cascade_pause_until=float(data.get("cascadePauseUntil", 0.0) or 0.0),
        )


@dataclass
class PolicyVerdict:
    """What the policy decided, and why — in the operator's words.

    `reasons` is a list rather than a string because more than one cap can
    bind at once, and knowing that both the drawdown floor and the category
    cap are active is a different situation from either alone.
    """

    block_reason: str = ""
    scale: float = 1.0
    reasons: list = field(default_factory=list)
    exposure: float = 0.0
    effective_exposure: float = 0.0
    largest_cluster: str = ""
    largest_cluster_value: float = 0.0
    drawdown_pct: float = 0.0

    # --- surgical risk patch -------------------------------------------------
    # `shadow_block_reason` carries what the NEW guards WOULD have blocked when
    # running in shadow mode. It is deliberately a separate field: the whole
    # point of shadow mode is that `blocked` stays false, so writing the reason
    # into `block_reason` would defeat it. Kept populated in enforce mode too,
    # so the two modes log identically and can be compared directly.
    shadow_block_reason: str = ""
    equity_guard_active: bool = False
    cascade_active: bool = False
    peak_equity: float = 0.0
    max_new_position_usdc: float = 0.0   # 0 = no ceiling from this policy

    @property
    def blocked(self) -> bool:
        return bool(self.block_reason)

    @property
    def would_block(self) -> bool:
        """True when a guard fired, whether or not it was enforced."""
        return bool(self.block_reason or self.shadow_block_reason)

    def to_dict(self) -> dict:
        return {
            "blocked": self.blocked,
            "blockReason": self.block_reason,
            "sizeScale": round(self.scale, 4),
            "reasons": list(self.reasons),
            "exposure": round(self.exposure, 4),
            "effectiveExposure": round(self.effective_exposure, 4),
            "largestCluster": self.largest_cluster,
            "largestClusterValue": round(self.largest_cluster_value, 4),
            "drawdownPct": round(self.drawdown_pct, 4),
            "shadowBlockReason": self.shadow_block_reason,
            "equityGuardActive": bool(self.equity_guard_active),
            "cascadeActive": bool(self.cascade_active),
            "peakEquity": round(self.peak_equity, 4),
            "maxNewPositionUsdc": round(self.max_new_position_usdc, 4),
        }


def cluster_key(position: ExposureView) -> str:
    """The economic bet a position belongs to.

    Coarsest first: same market is the same outcome, same wallet thesis is the
    same reason for being there, same category is the same environment. A
    position with none of these is its own cluster, which is the honest answer
    for a genuinely independent bet.
    """
    if position.market_id:
        return f"market:{position.market_id}"
    if position.wallet_thesis:
        return f"wallet:{position.wallet_thesis}"
    if position.category:
        return f"category:{position.category}"
    return f"token:{position.token_id}"


def effective_exposure(positions: Iterable[ExposureView]) -> tuple[float, dict]:
    """Exposure counted by CLUSTER, not by position (§13).

    Returns ``(effective, per_cluster)``. 'Effective' sums the clusters, so
    four positions in one market count once at their combined value — which is
    what a correlated-exposure cap has to bind on if it is to mean anything.
    """
    per_cluster: dict[str, float] = {}
    for position in positions:
        key = cluster_key(position)
        per_cluster[key] = per_cluster.get(key, 0.0) + max(0.0, position.value)
    return sum(per_cluster.values()), per_cluster


def category_exposure(positions: Iterable[ExposureView]) -> dict[str, float]:
    out: dict[str, float] = {}
    for position in positions:
        key = (position.category or "uncategorised").lower()
        out[key] = out.get(key, 0.0) + max(0.0, position.value)
    return out


def equity_guard(cfg, drawdown: float, was_active: bool) -> tuple[bool, str]:
    """The latching equity-peak guard (patches 1-3). Pure; caller holds state.

    Returns ``(active_now, transition)`` where transition is ``""``, ``"armed"``
    or ``"recovered"`` — the caller logs it, this does not.

    The two thresholds are deliberately different values. Arming at 5% and
    releasing at 5% is one threshold wearing two names, and it produces a guard
    that toggles on every tick that straddles it. Arming at 5% and releasing at
    2% means the account has to actually recover something before entries
    resume, and the 3-point band in between is where nothing changes.
    """
    if not getattr(cfg, "peak_guard_enabled", False):
        return False, ""

    arm_at = float(getattr(cfg, "peak_guard_threshold", 0.0) or 0.0)
    release_at = float(getattr(cfg, "peak_guard_recovery", 0.0) or 0.0)
    if arm_at <= 0:
        return False, ""
    # A recovery threshold at or above the arm threshold would release the
    # guard in the same breath that armed it. Clamp rather than trust config.
    release_at = min(release_at, arm_at * 0.5)

    if was_active:
        if drawdown <= release_at:
            return False, "recovered"
        return True, ""
    if drawdown >= arm_at:
        return True, "armed"
    return False, ""


def cascade_guard(cfg, realised_losses: int, now_ts: float,
                  pause_until: float) -> tuple[bool, float, str]:
    """The loss-cascade pause (patch 8). Pure; caller holds state and counts.

    Returns ``(active_now, new_pause_until, transition)``.

    Note what this is NOT, because this module's contract says there is "no
    term for recent losses" and this adds one. The distinction that keeps that
    promise intact: this can only ever PAUSE new entries for a fixed period. It
    never changes a stake, never sizes up after a loss, and never touches an
    exit — so it cannot become a martingale or a revenge trade. Pausing after
    losses is the opposite of chasing them.

    Only REALISED losses count. Counting unrealised ones would make the guard
    fire on the same mark-to-market wobble the stop already governs, and would
    pause entries every time an open position was briefly underwater.
    """
    if not getattr(cfg, "cascade_guard_enabled", False):
        return False, 0.0, ""

    if pause_until > 0 and now_ts < pause_until:
        return True, pause_until, ""
    if pause_until > 0 and now_ts >= pause_until:
        return False, 0.0, "released"

    count = int(getattr(cfg, "cascade_loss_count", 0) or 0)
    minutes = float(getattr(cfg, "cascade_pause_minutes", 0.0) or 0.0)
    if count <= 0 or minutes <= 0:
        return False, 0.0, ""
    if realised_losses >= count:
        return True, now_ts + minutes * 60.0, "armed"
    return False, 0.0, ""


def max_new_position(cfg, equity: float, stop_loss_pct: float) -> float:
    """Ceiling on a NEW position's stake, from the single-position loss cap.

    Patch 4. This does not close anything and does not replace the stop; it
    only bounds how much a *new* position may risk, expressed in the money the
    engine is about to spend.

        worst-case loss = stake x stop_loss_pct
        stake_ceiling   = equity x max_single_loss_pct / stop_loss_pct

    Measured against this build: `max_position_fraction` 0.25 and
    `stop_loss_pct` 0.25 give a worst case of 6.25% of equity. A cap set below
    that WILL bind on ordinary trades and shrink them — at 0.05 it holds the
    stake to 20% of equity instead of 25%. That is a real change to sizing, so
    it is configurable, reported in the A/B, and 0 disables it entirely.
    """
    if not getattr(cfg, "single_loss_cap_enabled", False):
        return 0.0
    cap = float(getattr(cfg, "max_single_position_loss_pct", 0.0) or 0.0)
    if cap <= 0 or equity <= 0 or stop_loss_pct <= 0:
        return 0.0
    return equity * cap / float(stop_loss_pct)


def evaluate(cfg, equity: float, cash: float,
             positions: Iterable[ExposureView],
             peak_equity: float = 0.0,
             state: Optional[GuardState] = None,
             realised_losses: int = 0,
             now_ts: float = 0.0,
             stop_loss_pct: float = 0.0) -> PolicyVerdict:
    """The whole policy, as one pure function.

    `cfg` is a :class:`CapitalPreservationConfig`. Disabled returns a verdict
    that blocks nothing and scales nothing, so an install that has not opted
    in behaves exactly as it did before.

    The `state`, `realised_losses`, `now_ts` and `stop_loss_pct` arguments were
    added by the surgical risk patch and all default to values that make the
    new guards inert. An existing caller that does not pass them gets exactly
    the verdict it got before the patch — which is the property the A/B
    comparison rests on.
    """
    verdict = PolicyVerdict()
    if not getattr(cfg, "enabled", False):
        return verdict

    positions = list(positions)
    exposure = sum(max(0.0, p.value) for p in positions)
    effective, per_cluster = effective_exposure(positions)
    verdict.exposure = exposure
    verdict.effective_exposure = effective
    if per_cluster:
        largest = max(per_cluster.items(), key=lambda kv: kv[1])
        verdict.largest_cluster, verdict.largest_cluster_value = largest

    equity = max(0.0, float(equity))
    peak = max(float(peak_equity or 0.0), equity)
    drawdown = ((peak - equity) / peak) if peak > 0 else 0.0
    verdict.drawdown_pct = drawdown
    verdict.peak_equity = peak

    # -- 1. the cash reserve. A floor under the account, not a target -------
    reserve = equity * max(0.0, float(cfg.min_cash_reserve_fraction))
    if reserve > 0 and cash <= reserve:
        verdict.block_reason = (
            f"cash ${cash:,.2f} is at or below the ${reserve:,.2f} reserve "
            f"({cfg.min_cash_reserve_fraction:.0%} of equity) — no new "
            "entries until an exit releases capital. Exits are unaffected.")
        return verdict

    # -- 2. total open exposure --------------------------------------------
    if cfg.max_total_exposure_fraction > 0 and equity > 0:
        ceiling = equity * float(cfg.max_total_exposure_fraction)
        if exposure >= ceiling:
            verdict.block_reason = (
                f"open exposure ${exposure:,.2f} has reached the "
                f"${ceiling:,.2f} ceiling "
                f"({cfg.max_total_exposure_fraction:.0%} of equity)")
            return verdict

    # -- 3. correlated exposure. Four positions, one bet -------------------
    if cfg.max_cluster_fraction > 0 and equity > 0 and per_cluster:
        ceiling = equity * float(cfg.max_cluster_fraction)
        if verdict.largest_cluster_value >= ceiling:
            verdict.block_reason = (
                f"correlated exposure in {verdict.largest_cluster} is "
                f"${verdict.largest_cluster_value:,.2f}, at the "
                f"${ceiling:,.2f} cap "
                f"({cfg.max_cluster_fraction:.0%} of equity). Positions "
                "sharing a market, wallet thesis or category are one bet, "
                "not several.")
            return verdict

    # -- 4. the drawdown halt. The last line, and it is a HALT on ENTRIES ---
    if cfg.halt_entries_drawdown_pct > 0 and \
            drawdown >= float(cfg.halt_entries_drawdown_pct):
        verdict.block_reason = (
            f"account is {drawdown:.0%} below its peak, at or beyond the "
            f"{cfg.halt_entries_drawdown_pct:.0%} entry halt. Existing "
            "positions are still managed and exits still run; only NEW "
            "entries stop.")
        return verdict

    # -- 5. sizing. Shrink only, never grow --------------------------------
    verdict.scale = size_scale(cfg, drawdown, verdict.reasons)

    # -- 6. the surgical risk patch ----------------------------------------
    # Deliberately placed AFTER everything above. Every existing cap keeps its
    # position in the order and its exact behaviour; these guards can only add
    # a block that was not already there, never remove one. If a cap above
    # returned early, none of this runs and the verdict is byte-identical to
    # what the previous build produced.
    _apply_patch_guards(cfg, verdict, drawdown, state, realised_losses,
                        now_ts, equity, stop_loss_pct)
    return verdict


def _apply_patch_guards(cfg, verdict: PolicyVerdict, drawdown: float,
                        state: Optional[GuardState], realised_losses: int,
                        now_ts: float, equity: float,
                        stop_loss_pct: float) -> None:
    """Equity-peak guard, loss-cascade guard and the new-position ceiling.

    Mutates `verdict` in place. Honours shadow mode: in `"shadow"` the reason
    is recorded but `block_reason` is left empty, so the engine behaves exactly
    as the unpatched build while the guard's opinion is still logged.
    """
    state = state or GuardState()
    enforce = str(getattr(cfg, "guard_mode", "enforce")).lower() != "shadow"
    reasons: list[str] = []

    active, transition = equity_guard(cfg, drawdown, state.equity_guard_active)
    state.equity_guard_active = active
    verdict.equity_guard_active = active
    if transition:
        verdict.reasons.append(f"equity guard {transition}")
    if active:
        reasons.append(
            f"equity guard: {drawdown:.2%} below the session peak of "
            f"${verdict.peak_equity:,.2f}, at or beyond the "
            f"{float(getattr(cfg, 'peak_guard_threshold', 0)):.0%} pause. "
            "New entries only; existing positions are managed and exited by "
            "the unchanged strategy.")

    cascade_on, until, cascade_transition = cascade_guard(
        cfg, realised_losses, now_ts, state.cascade_pause_until)
    state.cascade_pause_until = until
    verdict.cascade_active = cascade_on
    if cascade_transition:
        verdict.reasons.append(f"loss cascade {cascade_transition}")
    if cascade_on:
        left = max(0.0, (until - now_ts) / 60.0)
        reasons.append(
            f"loss cascade: {realised_losses} realised losses inside the "
            f"window; new entries paused for another {left:.0f} min. "
            "Exits are unaffected.")

    verdict.max_new_position_usdc = max_new_position(cfg, equity, stop_loss_pct)

    if not reasons:
        return
    joined = " | ".join(reasons)
    verdict.shadow_block_reason = joined
    if enforce:
        verdict.block_reason = joined


def size_scale(cfg, drawdown: float, reasons: Optional[list] = None) -> float:
    """How much to shrink stakes, 1.0 = not at all. Never above 1.0.

    A single linear ramp between two configured drawdown levels, floored. Not
    a curve, not adaptive, not fitted to anything: this number moves real money
    and it must be possible to state in one sentence what it will be at any
    account level. Monotonically non-increasing in drawdown, which is what
    makes martingale behaviour structurally impossible rather than merely
    absent.
    """
    reasons = reasons if reasons is not None else []
    start = float(getattr(cfg, "shrink_from_drawdown_pct", 0.0))
    full = float(getattr(cfg, "shrink_to_drawdown_pct", 0.0))
    floor = max(0.0, min(1.0, float(getattr(cfg, "min_size_scale", 1.0))))
    if start <= 0 or full <= start or drawdown <= start:
        return 1.0
    if drawdown >= full:
        reasons.append(
            f"stakes at the {floor:.0%} floor: {drawdown:.0%} below peak, at "
            f"or past the {full:.0%} mark")
        return floor
    span = (drawdown - start) / (full - start)
    scale = 1.0 - span * (1.0 - floor)
    reasons.append(
        f"stakes scaled to {scale:.0%}: {drawdown:.0%} below peak, between "
        f"the {start:.0%} and {full:.0%} marks")
    return round(max(floor, min(1.0, scale)), 4)


def positions_from(views: Iterable[Any],
                   markets: Optional[dict] = None,
                   mark_for=None) -> list[ExposureView]:
    """Adapter from the runner's `PositionView` objects.

    Kept here rather than in the runner so the policy's input shape is defined
    next to the policy, and so a test can build the input without importing
    the trading loop.
    """
    markets = markets or {}
    out: list[ExposureView] = []
    for view in views:
        token = str(getattr(view, "token_id", "") or "")
        market_id = str(getattr(view, "market_id", "") or "")
        market = markets.get(market_id)
        mark = None
        if mark_for is not None:
            mark = mark_for(token)
        price = float(mark or getattr(view, "avg_price", 0.0) or 0.0)
        size = float(getattr(view, "size", 0.0) or 0.0)
        out.append(ExposureView(
            token_id=token, market_id=market_id,
            category=str(getattr(market, "category", "") or ""),
            wallet_thesis=str(getattr(view, "wallet_influence", "") or ""),
            value=size * price,
            cost=float(getattr(view, "cost_basis", 0.0) or size * price)))
    return out
