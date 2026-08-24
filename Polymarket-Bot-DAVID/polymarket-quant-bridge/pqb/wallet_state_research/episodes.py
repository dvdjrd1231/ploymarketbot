"""Episodes, snapshots and ground-truth labels — the RN1 event sequence.

One episode is one (wallet, condition) pair. The sequence the brief specifies,
implemented literally:

    first BUY                -> defines ORIGINAL
    first BUY of the other   -> FIRST OPPOSITE BUY, the signal event
    + H minutes              -> the snapshot the classifier reads
    end of lifecycle         -> the label the prediction is graded against

Three decisions in here are load-bearing and are not obvious from the brief.

**Ties inside one second.** The tape's timestamps are whole seconds and a
rebalance can put both legs in the same second. "Which side was bought first"
would then depend on row order. Events arrive already ordered by the store's
monotonic insertion id, and this module never re-sorts by `ts` alone, so the
answer is deterministic and reproducible.

**A snapshot uses only what had happened by the snapshot instant.** Not the
trades near it, not the trades that day — the ones whose timestamp is at or
before `t_opposite + H*60`. That is the entire leakage defence at this layer,
and `Snapshot.available_at` records the newest input timestamp so
`features.leakage_audit` can prove it.

**A truncated episode is not a labelled episode.** The label needs the
lifecycle to be over. Our window ends when the tape ends, so an episode whose
market was still being traded at the edge of the tape has a label that says
"it stopped here" when the truth is "we stopped watching here". Those are
carried with `label_quality = "truncated"` and excluded from headline
accuracy — reported separately, never silently dropped and never silently
counted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable, Optional

from .events import WalletEvent

# The labels. DIRECTIONAL is a third outcome the two-class frozen rule cannot
# predict; it is measured and reported, never folded into either class.
AGGRESSIVE = "AGGRESSIVE_OPPOSITE"
PROTECT = "PROTECT_REBALANCE"
DIRECTIONAL = "DIRECTIONAL"

# Part 3's labelling boundary. A RESEARCH boundary for grading a historical
# prediction — not a claim that any wallet uses 1.40 for anything.
LABEL_RATIO_BOUNDARY = 1.40

# How long after an episode's last observed activity the tape must keep
# running before the episode is treated as finished. Below this the label is
# "we stopped watching", not "the wallet stopped trading".
#
# Two days rather than a week, and the reason is a measured property of this
# store rather than a preference: 87% of the tape's 713k trades fall in its
# final five days, so two days of silence spans several hundred thousand
# observed trades elsewhere. A week would be a stricter-looking rule that
# discards 94% of the switched population and leaves a sample too small to say
# anything — which is not caution, it is a different failure.
#
# The cutoff is a judgement call, so `runner` reports the whole SENSITIVITY
# CURVE across 1/2/3/7 days. If the headline result only exists at one cutoff,
# that is visible rather than hidden.
DEFAULT_QUIET_DAYS = 2.0


@dataclass
class Snapshot:
    """The wallet's reconstructed state at one instant. No future data.

    `available_at` is the newest event timestamp that contributed. The leakage
    audit asserts `available_at <= ts` for every snapshot, which is a fact
    about the arithmetic rather than a promise about the code.
    """

    ts: float
    horizon_minutes: float
    original_shares: float = 0.0
    opposite_shares: float = 0.0
    original_cash: float = 0.0        # net cash out on the original side
    opposite_cash: float = 0.0
    original_buys: int = 0
    opposite_buys: int = 0
    sells: int = 0
    last_original_price: float = 0.0
    last_opposite_price: float = 0.0
    avg_original_price: float = 0.0
    avg_opposite_price: float = 0.0
    events_used: int = 0
    available_at: float = 0.0
    valid: bool = False
    invalid_reason: str = ""

    @property
    def total_cash(self) -> float:
        """Net cash committed across both sides — the cost basis."""
        return self.original_cash + self.opposite_cash

    @property
    def inventory_ratio(self) -> Optional[float]:
        """FEATURE 1. `opposite_shares / original_shares`.

        None rather than 0.0 or infinity when the original side is not
        positive: a wallet that has sold out of its original side has no ratio,
        and substituting a number there would feed the classifier a value that
        means something else entirely.
        """
        if self.original_shares <= 0:
            return None
        return self.opposite_shares / self.original_shares

    def payoff_if_original_wins(self) -> float:
        return self.original_shares - self.total_cash

    def payoff_if_opposite_wins(self) -> float:
        return self.opposite_shares - self.total_cash

    @property
    def weaker_payoff(self) -> float:
        return min(self.payoff_if_original_wins(),
                   self.payoff_if_opposite_wins())

    @property
    def stronger_payoff(self) -> float:
        return max(self.payoff_if_original_wins(),
                   self.payoff_if_opposite_wins())

    def shares_needed_opposite_to_zero(
            self, opposite_price: Optional[float] = None) -> Optional[float]:
        """FEATURE 2. Extra OPPOSITE shares that bring the weaker terminal
        scenario to approximately zero.

        In a binary market, terminal payoff is `shares_on_winning_side - cost`.
        Buying `x` more opposite shares at price `p` moves the opposite
        scenario to `(opposite + x) - (cost + x*p)`, so setting that to zero
        gives::

            x = (cost - opposite_shares) / (1 - p)

        Two cases return None rather than a number, and the distinction is the
        whole point of the feature:

        * The weaker scenario is the ORIGINAL one. Buying more opposite shares
          makes it worse, not better — no quantity neutralises it, and a large
          number here would read as "far from neutral" when the truth is "not
          reachable this way".
        * `p >= 1`, which is not a real quote.

        Already at or above zero returns 0.0: nothing is needed.
        """
        weaker = self.weaker_payoff
        if weaker >= 0:
            return 0.0
        if self.payoff_if_original_wins() < self.payoff_if_opposite_wins():
            return None            # the weak side is the one we cannot fix
        price = (opposite_price if opposite_price is not None
                 else self.last_opposite_price)
        if not price or price <= 0 or price >= 1.0:
            return None
        return (self.total_cash - self.opposite_shares) / (1.0 - price)

    def to_dict(self) -> dict:
        ratio = self.inventory_ratio
        needed = self.shares_needed_opposite_to_zero()
        return {
            "ts": self.ts, "horizonMinutes": self.horizon_minutes,
            "valid": self.valid, "invalidReason": self.invalid_reason,
            "originalShares": round(self.original_shares, 6),
            "oppositeShares": round(self.opposite_shares, 6),
            "originalCash": round(self.original_cash, 6),
            "oppositeCash": round(self.opposite_cash, 6),
            "inventoryRatio": (round(ratio, 6) if ratio is not None else None),
            "sharesNeededOppositeToZero": (round(needed, 6)
                                           if needed is not None else None),
            "payoffOriginalWins": round(self.payoff_if_original_wins(), 6),
            "payoffOppositeWins": round(self.payoff_if_opposite_wins(), 6),
            "originalBuys": self.original_buys,
            "oppositeBuys": self.opposite_buys, "sells": self.sells,
            "eventsUsed": self.events_used, "availableAt": self.available_at,
        }


@dataclass
class Episode:
    """One wallet's whole engagement with one condition."""

    wallet: str
    market_id: str
    question: str = ""
    original_token: str = ""
    opposite_token: str = ""
    first_buy_ts: float = 0.0
    first_buy_price: float = 0.0
    first_opposite_ts: float = 0.0
    first_opposite_price: float = 0.0
    events: list = field(default_factory=list)
    # -- lifecycle outcome, for LABELLING ONLY --------------------------------
    final_original_shares: float = 0.0
    final_opposite_shares: float = 0.0
    final_ratio: Optional[float] = None
    label: str = ""
    label_quality: str = ""      # redeemed | resolved | quiet | truncated
    last_activity_ts: float = 0.0
    # When the wallet redeemed this condition, if it did. A fact ABOUT the
    # episode, never a leg of it — redemptions are excluded from `events`.
    redeemed_ts: float = 0.0

    @property
    def switched(self) -> bool:
        return bool(self.first_opposite_ts)

    @property
    def seconds_to_switch(self) -> float:
        return (self.first_opposite_ts - self.first_buy_ts) if self.switched \
            else 0.0

    @property
    def labelled(self) -> bool:
        """Usable for grading a prediction: a real label from a finished
        lifecycle. `truncated` episodes are excluded here and counted
        separately — an unfinished story is not a wrong answer."""
        return bool(self.label) and self.label_quality != "truncated"

    @property
    def two_class(self) -> bool:
        """In the population the frozen two-class rule can be graded on."""
        return self.label in (AGGRESSIVE, PROTECT)

    def snapshot(self, horizon_minutes: float) -> Snapshot:
        """State at `first_opposite_ts + horizon`, from prior events only."""
        if not self.switched:
            return Snapshot(ts=0.0, horizon_minutes=horizon_minutes,
                            invalid_reason="episode never switched sides")
        cutoff = self.first_opposite_ts + horizon_minutes * 60.0
        snap = Snapshot(ts=cutoff, horizon_minutes=horizon_minutes)
        original_notional = original_size = 0.0
        opposite_notional = opposite_size = 0.0
        for event in self.events:
            if event.ts > cutoff:
                break                       # events are chronological
            snap.events_used += 1
            snap.available_at = max(snap.available_at, event.ts)
            is_original = event.token_id == self.original_token
            if is_original:
                snap.original_shares += event.signed_shares
                snap.original_cash += event.signed_cash
                if event.is_buy:
                    snap.original_buys += 1
                    snap.last_original_price = event.price
                    original_notional += event.price * event.shares
                    original_size += event.shares
            else:
                snap.opposite_shares += event.signed_shares
                snap.opposite_cash += event.signed_cash
                if event.is_buy:
                    snap.opposite_buys += 1
                    snap.last_opposite_price = event.price
                    opposite_notional += event.price * event.shares
                    opposite_size += event.shares
            if not event.is_buy:
                snap.sells += 1
        snap.avg_original_price = (original_notional / original_size
                                   if original_size > 0 else 0.0)
        snap.avg_opposite_price = (opposite_notional / opposite_size
                                   if opposite_size > 0 else 0.0)

        # Validity. A snapshot the classifier cannot read is INVALID and is
        # counted as such; it is never quietly replaced by a default that the
        # rule would happily classify.
        if snap.original_shares <= 0:
            snap.invalid_reason = (
                "original side is not positive at the snapshot — no inventory "
                "ratio exists")
        elif snap.opposite_shares <= 0:
            snap.invalid_reason = (
                "opposite side is not positive at the snapshot — the opposite "
                "buy was already sold out")
        else:
            snap.valid = True
        return snap


def _classify_final(original: float, opposite: float
                    ) -> tuple[str, Optional[float]]:
    """Part 3's ground truth, from FINAL NET shares."""
    if opposite <= 0:
        return DIRECTIONAL, None
    if original <= 0:
        # Finished holding only the opposite side. Two-sided during the
        # episode but not at the end, and the ratio is undefined; treated as
        # its own case rather than forced into a class by dividing by zero.
        return AGGRESSIVE, None
    ratio = opposite / original
    return (AGGRESSIVE if ratio >= LABEL_RATIO_BOUNDARY else PROTECT), ratio


def build_episodes(events: Iterable[WalletEvent],
                   tape_end_ts: float = 0.0,
                   quiet_days: float = DEFAULT_QUIET_DAYS,
                   settled_markets: Optional[set] = None,
                   min_first_buy_shares: float = 0.0,
                   redemptions: Optional[dict] = None) -> list[Episode]:
    """Every (wallet, condition) episode, with labels where the story finished.

    `tape_end_ts` and `quiet_days` decide `label_quality`. Passing
    `tape_end_ts=0` means "do not judge completeness", which is correct for a
    synthetic test and wrong for real research — the runner always passes it.
    """
    settled_markets = settled_markets or set()
    redemptions = redemptions or {}
    grouped: dict[tuple, list] = defaultdict(list)
    for event in events:
        grouped[(event.wallet, event.market_id)].append(event)

    out: list[Episode] = []
    for (wallet, market_id), rows in grouped.items():
        # Chronological, with the incoming order preserved inside a second.
        rows.sort(key=lambda e: e.ts)
        first_buy = next((e for e in rows if e.is_buy), None)
        if first_buy is None:
            continue                     # sells only: no episode to speak of
        if min_first_buy_shares and first_buy.shares < min_first_buy_shares:
            continue
        original = first_buy.token_id
        episode = Episode(
            wallet=wallet, market_id=market_id,
            question=first_buy.question, original_token=original,
            first_buy_ts=first_buy.ts, first_buy_price=first_buy.price,
            events=rows,
            last_activity_ts=max(e.ts for e in rows))

        opposite = next((e for e in rows
                         if e.is_buy and e.token_id != original), None)
        if opposite is not None:
            episode.opposite_token = opposite.token_id
            episode.first_opposite_ts = opposite.ts
            episode.first_opposite_price = opposite.price

        for event in rows:
            if event.token_id == original:
                episode.final_original_shares += event.signed_shares
            else:
                episode.final_opposite_shares += event.signed_shares
        episode.label, episode.final_ratio = _classify_final(
            episode.final_original_shares, episode.final_opposite_shares)

        # A REDEMPTION is the strongest completeness signal there is. The
        # wallet has cashed the condition out: it is over, whatever the tape's
        # end date or a quiet-period heuristic would have guessed. This is
        # what the /activity backfill buys — it converts episodes that were
        # merely "we stopped watching" into episodes that genuinely finished.
        redeemed_ts = redemptions.get((wallet, market_id))
        if redeemed_ts:
            episode.redeemed_ts = float(redeemed_ts)
        if redeemed_ts:
            episode.label_quality = "redeemed"
        elif market_id in settled_markets:
            episode.label_quality = "resolved"
        elif not tape_end_ts:
            episode.label_quality = "unknown"
        elif tape_end_ts - episode.last_activity_ts >= quiet_days * 86_400.0:
            episode.label_quality = "quiet"
        else:
            episode.label_quality = "truncated"
        out.append(episode)

    out.sort(key=lambda e: (e.first_opposite_ts or e.first_buy_ts))
    return out


@dataclass
class EpisodeCensus:
    """The population, stage by stage. Every report opens with this so a
    sample of 90 is never mistaken for a sample of 20,000."""

    episodes: int = 0
    switched: int = 0
    labelled: int = 0
    truncated: int = 0
    directional: int = 0
    protect: int = 0
    aggressive: int = 0
    valid_snapshots: int = 0
    invalid_snapshots: int = 0
    invalid_reasons: dict = field(default_factory=dict)
    wallets: int = 0
    markets: int = 0

    def to_dict(self) -> dict:
        return {
            "episodes": self.episodes, "switched": self.switched,
            "labelled": self.labelled, "truncated": self.truncated,
            "directional": self.directional, "protect": self.protect,
            "aggressive": self.aggressive,
            "validSnapshots": self.valid_snapshots,
            "invalidSnapshots": self.invalid_snapshots,
            "invalidReasons": dict(self.invalid_reasons),
            "wallets": self.wallets, "markets": self.markets,
            "twoClassPopulation": self.protect + self.aggressive,
        }


def census(episodes: Iterable[Episode], horizon_minutes: float) -> EpisodeCensus:
    out = EpisodeCensus()
    wallets, markets = set(), set()
    for episode in episodes:
        out.episodes += 1
        wallets.add(episode.wallet)
        markets.add(episode.market_id)
        if not episode.switched:
            continue
        out.switched += 1
        if episode.label_quality == "truncated":
            out.truncated += 1
        elif episode.label == DIRECTIONAL:
            out.directional += 1
        elif episode.label == PROTECT:
            out.protect += 1
        elif episode.label == AGGRESSIVE:
            out.aggressive += 1
        if episode.labelled:
            out.labelled += 1
        snap = episode.snapshot(horizon_minutes)
        if snap.valid:
            out.valid_snapshots += 1
        else:
            out.invalid_snapshots += 1
            reason = snap.invalid_reason or "unknown"
            out.invalid_reasons[reason] = out.invalid_reasons.get(reason, 0) + 1
    out.wallets, out.markets = len(wallets), len(markets)
    return out
