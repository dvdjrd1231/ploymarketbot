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

    @property
    def blocked(self) -> bool:
        return bool(self.block_reason)

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


def evaluate(cfg, equity: float, cash: float,
             positions: Iterable[ExposureView],
             peak_equity: float = 0.0) -> PolicyVerdict:
    """The whole policy, as one pure function.

    `cfg` is a :class:`CapitalPreservationConfig`. Disabled returns a verdict
    that blocks nothing and scales nothing, so an install that has not opted
    in behaves exactly as it did before.
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
    return verdict


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
