"""
Outcome feedback (step 8 of the pipeline) — the loop, closed.

The journal already records ``decision -> entry -> path -> exit -> result`` and
``pqb.cli report`` already summarises it *for a person*. This module is what
makes the same evidence available to the **engine**, so that what has actually
worked changes what gets decided next rather than only what gets read next.

What it produces is a set of tilts: small, bounded adjustments to a candidate's
score, derived from how positions with the same tags have historically resolved.
Three properties keep that honest.

**Shrinkage, again.** Three trades in a category is not evidence about that
category. Every group's mean return is pulled toward the book's overall mean in
proportion to how little sample stands behind it, so a group has to earn its
influence before it gets any.

**Bounded.** The total tilt is capped (``max_tilt``, default 0.10 of score).
A feedback loop that can move a score arbitrarily far will eventually find a
spurious pattern and trade it with total conviction; one that can only nudge
will not.

**Realised only.** Open positions contribute nothing. An unrealised gain is a
price, not a result, and letting it vote would let the loop congratulate itself
for positions that have not yet had the chance to go wrong.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Optional

# Closed trades at which a group's own record carries equal weight with the
# book-wide mean. Higher than the wallet-ranking constant because a position is
# a much scarcer observation than a wallet trade.
SHRINKAGE_K = 12.0

# A group needs at least this many closed positions before it tilts anything.
MIN_GROUP_SAMPLE = 5

# The book needs at least this many closed positions before *any* tilt applies.
# Below it there is no meaningful "overall mean" to shrink toward.
MIN_BOOK_SAMPLE = 20

# The dimensions the journal is grouped on. Each is a column on `lifecycles`.
DIMENSIONS = ("exit_style", "category", "liquidity_bucket", "ttr_bucket")


@dataclass
class GroupStat:
    """What one tag value's closed positions did."""

    dimension: str
    key: str
    n: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    total_return: float = 0.0
    shrunk_return: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def mean_return(self) -> float:
        return self.total_return / self.n if self.n else 0.0

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension, "key": self.key, "n": self.n,
            "winRate": round(self.win_rate, 4),
            "meanReturn": round(self.mean_return, 4),
            "shrunkReturn": round(self.shrunk_return, 4),
            "totalPnl": round(self.total_pnl, 4),
        }


@dataclass
class PerformanceMemory:
    """What the journal says has worked, in a form the engine can apply."""

    groups: dict[str, dict[str, GroupStat]] = field(default_factory=dict)
    book_sample: int = 0
    book_mean_return: float = 0.0
    wallet_followed: Optional[GroupStat] = None
    wallet_overridden: Optional[GroupStat] = None
    max_tilt: float = 0.10

    @property
    def active(self) -> bool:
        """Whether there is enough history for any of this to mean anything."""
        return self.book_sample >= MIN_BOOK_SAMPLE

    def stat(self, dimension: str, key: str) -> Optional[GroupStat]:
        group = self.groups.get(dimension) or {}
        stat = group.get(str(key or ""))
        return stat if stat and stat.n >= MIN_GROUP_SAMPLE else None

    def tilt(self, **tags: Any) -> tuple[float, dict]:
        """A bounded score adjustment for a candidate with these tags.

        Dimensions are averaged rather than summed, so a candidate that happens
        to match on four tags cannot accumulate four times the adjustment of one
        that matches on one — the tags are correlated, and summing would treat
        four views of the same evidence as four pieces of it.
        """
        if not self.active:
            return 0.0, {"applied": False, "reason": "insufficient history"}

        contributions: dict[str, float] = {}
        evidence: dict[str, Any] = {}
        for dimension, key in tags.items():
            stat = self.stat(dimension, key)
            if stat is None:
                continue
            # Relative to the book, not to zero: a category returning +2% in a
            # book averaging +5% is an underperformer, and scoring it as a
            # positive would reward the worst of what we do for being positive.
            excess = stat.shrunk_return - self.book_mean_return
            contributions[dimension] = math.tanh(excess * 4.0)
            evidence[dimension] = stat.to_dict()

        if not contributions:
            return 0.0, {"applied": False, "reason": "no group met the sample floor"}

        mean = sum(contributions.values()) / len(contributions)
        adjustment = max(-self.max_tilt, min(self.max_tilt, mean * self.max_tilt))
        return adjustment, {
            "applied": True,
            "adjustment": round(adjustment, 5),
            "dimensions": {k: round(v, 4) for k, v in contributions.items()},
            "evidence": evidence,
            "bookSample": self.book_sample,
            "bookMeanReturn": round(self.book_mean_return, 4),
        }

    def wallet_exit_bias(self) -> tuple[float, dict]:
        """Has following wallet exits, or overriding them, worked better?

        Returns a signed number: positive means overriding has done better and
        the override threshold should effectively be easier to clear; negative
        means following has. Zero until both arms have enough closed positions
        to be compared — one arm alone says nothing about the other.
        """
        followed, overridden = self.wallet_followed, self.wallet_overridden
        if (followed is None or overridden is None
                or followed.n < MIN_GROUP_SAMPLE
                or overridden.n < MIN_GROUP_SAMPLE):
            return 0.0, {"applied": False, "reason": "not enough of both arms"}
        delta = overridden.shrunk_return - followed.shrunk_return
        return math.tanh(delta * 4.0), {
            "applied": True,
            "followed": followed.to_dict(),
            "overridden": overridden.to_dict(),
            "delta": round(delta, 4),
        }

    def to_dict(self) -> dict:
        return {
            "active": self.active,
            "bookSample": self.book_sample,
            "bookMeanReturn": round(self.book_mean_return, 4),
            "groups": {
                dimension: {k: s.to_dict() for k, s in stats.items()}
                for dimension, stats in self.groups.items()
            },
            "walletExitBias": self.wallet_exit_bias()[1],
        }


def build(journal, max_tilt: float = 0.10,
          limit: int = 5_000) -> PerformanceMemory:
    """Read closed lifecycles and summarise what worked.

    Only ``CLOSED`` rows are read, and only the current mode's — a dry-run
    session's paper results must never tilt live decisions, and a live book's
    results must not be diluted by simulated ones.
    """
    rows = journal.query(
        "SELECT exit_style, category, liquidity_bucket, ttr_bucket, "
        "       wallet_influence, realized_pnl, return_pct "
        "  FROM lifecycles WHERE status = 'CLOSED' AND mode = ? "
        " ORDER BY id DESC LIMIT ?",
        (journal.mode, int(limit)))

    memory = PerformanceMemory(max_tilt=max_tilt)
    if not rows:
        return memory

    returns = [float(r.get("return_pct") or 0.0) for r in rows]
    memory.book_sample = len(rows)
    memory.book_mean_return = sum(returns) / len(returns)

    for dimension in DIMENSIONS:
        memory.groups[dimension] = {}

    for row in rows:
        ret = float(row.get("return_pct") or 0.0)
        pnl = float(row.get("realized_pnl") or 0.0)
        for dimension in DIMENSIONS:
            key = str(row.get(dimension) or "")
            if not key:
                continue
            stat = memory.groups[dimension].setdefault(
                key, GroupStat(dimension=dimension, key=key))
            stat.n += 1
            stat.wins += 1 if pnl > 0 else 0
            stat.total_pnl += pnl
            stat.total_return += ret

    for stats in memory.groups.values():
        for stat in stats.values():
            stat.shrunk_return = (
                (stat.total_return + memory.book_mean_return * SHRINKAGE_K)
                / (stat.n + SHRINKAGE_K))

    memory.wallet_followed, memory.wallet_overridden = _wallet_arms(
        journal, memory.book_mean_return)
    return memory


def _wallet_arms(journal, book_mean: float
                 ) -> tuple[Optional[GroupStat], Optional[GroupStat]]:
    """The two halves of the wallet-exit decision, scored on what they returned.

    These cannot both be read off ``lifecycles.exit_style``. Following a wallet
    closes the position, so it lands there as ``wallet``. **Overriding one is a
    HOLD** — the position stays open and eventually closes for some entirely
    different reason, so it is recorded with whatever style did close it and
    the override leaves no trace on the lifecycle at all.

    Reading only the exit style would therefore compare every followed exit
    against an arm that is permanently empty, and silently conclude that
    following is the only thing that has ever been tried. The override arm has
    to be reached through the decision that made it.
    """
    followed = journal.query(
        "SELECT realized_pnl, return_pct FROM lifecycles "
        " WHERE status='CLOSED' AND mode=? AND exit_style='wallet'",
        (journal.mode,))
    overridden = journal.query(
        "SELECT l.realized_pnl, l.return_pct FROM lifecycles l "
        " WHERE l.status='CLOSED' AND l.mode=? AND EXISTS ("
        "   SELECT 1 FROM decisions d WHERE d.lifecycle_id = l.id "
        "     AND d.exit_style = 'wallet_override')",
        (journal.mode,))

    def arm(name: str, rows: list[dict]) -> Optional[GroupStat]:
        if not rows:
            return None
        stat = GroupStat(dimension="wallet_decision", key=name, n=len(rows))
        for row in rows:
            pnl = float(row.get("realized_pnl") or 0.0)
            stat.wins += 1 if pnl > 0 else 0
            stat.total_pnl += pnl
            stat.total_return += float(row.get("return_pct") or 0.0)
        stat.shrunk_return = ((stat.total_return + book_mean * SHRINKAGE_K)
                              / (stat.n + SHRINKAGE_K))
        return stat

    return arm("followed", followed), arm("overridden", overridden)
