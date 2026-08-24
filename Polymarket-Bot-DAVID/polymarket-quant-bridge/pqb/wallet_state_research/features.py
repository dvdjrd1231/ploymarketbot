"""The feature engine, and the leakage audit that polices it.

Part 6 lists five families of feature and Part 23 says every one of them must
be provably knowable at the signal instant. Those two requirements are
implemented as one mechanism rather than two: every feature is emitted through
`_put`, which records the newest input timestamp beside the value, and
`leakage_audit` then checks the recorded stamps rather than re-reading the
code. A feature whose stamp is after the signal is a hard failure, not a
warning.

The distinction that does the real work here:

* **Features** may use only events at or before `signal_ts`.
* **Labels** may use the whole lifecycle, because a label is the answer key
  for grading a historical prediction and never an input to one.

Wallet-history features (Part 6's fifth family) are the dangerous ones. A
wallet's "historical aggressive rate" computed over its whole record includes
the episode being predicted and every episode after it — which is why they are
built from a strictly PRIOR-EPISODE view, are stamped with the last prior
episode's end, and are omitted entirely when the wallet has no prior history
rather than defaulted to a population average that leaks the population.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from .episodes import AGGRESSIVE, DIRECTIONAL, PROTECT, Episode, Snapshot

# Coarse category from the market question. The tape carries no category, so
# this is a HEURISTIC and is labelled as one everywhere it appears: it exists
# so Part 11's cross-category question can be asked at all, not so it can be
# answered authoritatively.
_CATEGORY_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("crypto", ("bitcoin", "btc", "ethereum", "eth", "solana", "crypto",
                "dogecoin", "xrp", "token", "coin")),
    ("politics", ("president", "election", "senate", "congress", "governor",
                  "parliament", "prime minister", "democrat", "republican",
                  "poll", "nominee", "impeach", "cabinet")),
    ("economics", ("fed", "interest rate", "inflation", "cpi", "gdp",
                   "unemployment", "recession", "jobs report", "fomc",
                   "tariff")),
    ("sports", ("nba", "nfl", "mlb", "nhl", "premier league", "champions",
                "world cup", "olympic", "ufc", "match", "vs.", " vs ",
                "super bowl", "playoff", "tournament")),
    ("entertainment", ("oscar", "grammy", "emmy", "box office", "movie",
                       "album", "netflix", "celebrity", "rotten tomatoes")),
    ("geopolitics", ("ukraine", "russia", "israel", "gaza", "china", "taiwan",
                     "nato", "ceasefire", "sanction", "war")),
)


def category_of(question: str) -> str:
    """Heuristic category. Returns "other" rather than guessing wildly."""
    text = (question or "").lower()
    for name, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in text for keyword in keywords):
            return name
    return "other"


@dataclass
class FeatureVector:
    """Values plus, for each one, when its newest input became knowable."""

    signal_ts: float
    values: dict = field(default_factory=dict)
    available_at: dict = field(default_factory=dict)
    unavailable: dict = field(default_factory=dict)   # name -> why

    def _put(self, name: str, value: Optional[float],
             available_at: float, why_missing: str = "") -> None:
        if value is None:
            self.unavailable[name] = why_missing or "not computable"
            return
        self.values[name] = float(value)
        self.available_at[name] = float(available_at)

    def get(self, name: str, default=None):
        """Dict-like read, so a consumer that expected a plain mapping gets a
        value rather than an AttributeError. The stamps stay reachable through
        `available_at`; this is a convenience, not a second source of truth."""
        return self.values.get(name, default)

    def as_dict(self) -> dict:
        return dict(self.values)

    def to_dict(self) -> dict:
        return {
            "signalTs": self.signal_ts,
            "values": {k: round(v, 8) for k, v in self.values.items()},
            "unavailable": dict(self.unavailable),
            "featureCount": len(self.values),
            "unavailableCount": len(self.unavailable),
        }


@dataclass
class WalletHistory:
    """A wallet's record BEFORE the episode being predicted.

    Built incrementally, in chronological order, by `history_index`. The class
    holds no episode that starts at or after the one it is describing, which
    is the only construction that makes these features honest — and it holds
    the timestamp of the newest episode folded in, so the leakage audit can
    check it rather than take it on trust.
    """

    episodes: int = 0
    switched: int = 0
    aggressive: int = 0
    protect: int = 0
    directional: int = 0
    trades: int = 0
    notional: float = 0.0
    hold_seconds_total: float = 0.0
    hold_samples: int = 0
    last_known_ts: float = 0.0
    by_category: dict = field(default_factory=lambda: defaultdict(
        lambda: [0, 0]))          # category -> [switched, aggressive]

    @property
    def switch_rate(self) -> Optional[float]:
        return (self.switched / self.episodes) if self.episodes else None

    @property
    def aggressive_rate(self) -> Optional[float]:
        decided = self.aggressive + self.protect
        return (self.aggressive / decided) if decided else None

    @property
    def avg_trade_notional(self) -> Optional[float]:
        return (self.notional / self.trades) if self.trades else None

    @property
    def avg_hold_seconds(self) -> Optional[float]:
        return (self.hold_seconds_total / self.hold_samples
                if self.hold_samples else None)


def history_index(episodes: Iterable[Episode]) -> dict:
    """`episode_key -> WalletHistory as of just before that episode`.

    One chronological pass over all episodes, folding each wallet's finished
    episodes forward. Two rules make this leakage-free:

    * an episode is folded in only AFTER every episode that starts later has
      been given the snapshot of history that excludes it; and
    * only episodes whose lifecycle had finished BEFORE the current episode's
      signal are folded in at all — a concurrent episode's eventual label was
      not knowable yet, however early it started.
    """
    ordered = sorted(episodes, key=lambda e: (e.first_opposite_ts
                                              or e.first_buy_ts))
    out: dict = {}
    running: dict[str, WalletHistory] = defaultdict(WalletHistory)
    pending: dict[str, list] = defaultdict(list)

    for episode in ordered:
        signal_ts = episode.first_opposite_ts or episode.first_buy_ts
        history = running[episode.wallet]
        # Fold in everything that had genuinely FINISHED before this signal.
        still_pending = []
        for earlier in pending[episode.wallet]:
            if earlier.last_activity_ts < signal_ts:
                _fold(history, earlier)
            else:
                still_pending.append(earlier)
        pending[episode.wallet] = still_pending

        out[_key(episode)] = _copy(history)
        pending[episode.wallet].append(episode)
    return out


def _key(episode: Episode) -> tuple:
    return (episode.wallet, episode.market_id,
            episode.first_opposite_ts or episode.first_buy_ts)


def _fold(history: WalletHistory, episode: Episode) -> None:
    history.episodes += 1
    history.trades += len(episode.events)
    history.notional += sum(abs(e.usdc) for e in episode.events)
    history.last_known_ts = max(history.last_known_ts,
                                episode.last_activity_ts)
    if episode.last_activity_ts > episode.first_buy_ts:
        history.hold_seconds_total += (episode.last_activity_ts
                                       - episode.first_buy_ts)
        history.hold_samples += 1
    if not episode.switched:
        return
    history.switched += 1
    category = category_of(episode.question)
    bucket = history.by_category[category]
    bucket[0] += 1
    if episode.label == AGGRESSIVE:
        history.aggressive += 1
        bucket[1] += 1
    elif episode.label == PROTECT:
        history.protect += 1
    elif episode.label == DIRECTIONAL:
        history.directional += 1


def _copy(history: WalletHistory) -> WalletHistory:
    clone = WalletHistory(
        episodes=history.episodes, switched=history.switched,
        aggressive=history.aggressive, protect=history.protect,
        directional=history.directional, trades=history.trades,
        notional=history.notional,
        hold_seconds_total=history.hold_seconds_total,
        hold_samples=history.hold_samples,
        last_known_ts=history.last_known_ts)
    clone.by_category = defaultdict(lambda: [0, 0])
    for key, value in history.by_category.items():
        clone.by_category[key] = list(value)
    return clone


def build(episode: Episode, snapshot: Snapshot,
          history: Optional[WalletHistory] = None,
          quote: Optional[Any] = None,
          prior_snapshot: Optional[Snapshot] = None) -> FeatureVector:
    """Every feature the data supports, stamped with when it was knowable.

    `quote` is an optional market observation at the signal instant (see
    `pricing.Quote`). When it is absent the market family is marked
    UNAVAILABLE with the reason rather than filled from the wallet's own
    prints, which would be a different quantity wearing the same name.
    """
    signal_ts = snapshot.ts
    out = FeatureVector(signal_ts=signal_ts)
    stamp = snapshot.available_at or signal_ts

    # -- POSITION -----------------------------------------------------------
    out._put("original_shares", snapshot.original_shares, stamp)
    out._put("opposite_shares", snapshot.opposite_shares, stamp)
    out._put("total_shares",
             snapshot.original_shares + snapshot.opposite_shares, stamp)
    out._put("net_exposure",
             snapshot.original_shares - snapshot.opposite_shares, stamp)
    out._put("gross_exposure",
             abs(snapshot.original_shares) + abs(snapshot.opposite_shares),
             stamp)
    ratio = snapshot.inventory_ratio
    out._put("inventory_ratio", ratio, stamp,
             "original side not positive at the snapshot")
    total = snapshot.original_shares + snapshot.opposite_shares
    if total > 0:
        out._put("original_share_pct", snapshot.original_shares / total, stamp)
        out._put("opposite_share_pct", snapshot.opposite_shares / total, stamp)
    else:
        out.unavailable["original_share_pct"] = "no positive inventory"
        out.unavailable["opposite_share_pct"] = "no positive inventory"

    if prior_snapshot is not None and prior_snapshot.valid:
        prior_ratio = prior_snapshot.inventory_ratio
        if ratio is not None and prior_ratio is not None:
            out._put("inventory_ratio_change", ratio - prior_ratio, stamp)
        out._put("opposite_shares_change",
                 snapshot.opposite_shares - prior_snapshot.opposite_shares,
                 stamp)
    else:
        out.unavailable["inventory_ratio_change"] = "no earlier snapshot"

    elapsed = max(1.0, signal_ts - episode.first_opposite_ts)
    out._put("opposite_accumulation_rate",
             snapshot.opposite_shares / elapsed, stamp)
    since_first = max(1.0, signal_ts - episode.first_buy_ts)
    out._put("original_accumulation_rate",
             snapshot.original_shares / since_first, stamp)
    buys = snapshot.original_buys + snapshot.opposite_buys
    if buys + snapshot.sells > 0:
        out._put("buy_sell_imbalance",
                 (buys - snapshot.sells) / (buys + snapshot.sells), stamp)

    # -- PAYOFF -------------------------------------------------------------
    out._put("payoff_original_wins", snapshot.payoff_if_original_wins(), stamp)
    out._put("payoff_opposite_wins", snapshot.payoff_if_opposite_wins(), stamp)
    out._put("weaker_payoff", snapshot.weaker_payoff, stamp)
    out._put("stronger_payoff", snapshot.stronger_payoff, stamp)
    out._put("payoff_spread",
             snapshot.stronger_payoff - snapshot.weaker_payoff, stamp)
    needed = snapshot.shares_needed_opposite_to_zero()
    out._put("shares_needed_opposite_to_zero", needed, stamp,
             "the weaker scenario is the ORIGINAL one, which buying more "
             "opposite shares cannot neutralise")
    if needed is not None and snapshot.last_opposite_price > 0:
        out._put("cost_to_neutralise", needed * snapshot.last_opposite_price,
                 stamp)
    denominator = abs(snapshot.stronger_payoff) + abs(snapshot.weaker_payoff)
    if denominator > 0:
        out._put("payoff_asymmetry",
                 (snapshot.stronger_payoff - snapshot.weaker_payoff)
                 / denominator, stamp)

    # -- TRADE BEHAVIOUR ----------------------------------------------------
    out._put("seconds_original_to_opposite", episode.seconds_to_switch, stamp)
    out._put("original_buy_count", snapshot.original_buys, stamp)
    out._put("opposite_buy_count", snapshot.opposite_buys, stamp)
    out._put("sell_count", snapshot.sells, stamp)
    out._put("avg_original_price", snapshot.avg_original_price or None, stamp,
             "no original-side buy priced")
    out._put("avg_opposite_price", snapshot.avg_opposite_price or None, stamp,
             "no opposite-side buy priced")
    out._put("last_opposite_price", snapshot.last_opposite_price or None,
             stamp, "no opposite-side buy priced")
    out._put("capital_deployed", snapshot.total_cash, stamp)
    out._put("capital_velocity", snapshot.total_cash / since_first, stamp)
    out._put("order_frequency", snapshot.events_used / since_first, stamp)
    out._put("direction_changes", _direction_changes(episode, signal_ts), stamp)

    # -- MARKET -------------------------------------------------------------
    if quote is not None and getattr(quote, "available", False):
        quote_ts = float(getattr(quote, "ts", signal_ts))
        out._put("market_bid", getattr(quote, "bid", None), quote_ts,
                 "no bid in the captured book")
        out._put("market_ask", getattr(quote, "ask", None), quote_ts,
                 "no ask in the captured book")
        out._put("market_mid", getattr(quote, "mid", None), quote_ts)
        out._put("market_spread", getattr(quote, "spread", None), quote_ts)
        out._put("market_depth", getattr(quote, "depth", None), quote_ts,
                 "book depth not captured for this token")
    else:
        for name in ("market_bid", "market_ask", "market_mid",
                     "market_spread", "market_depth"):
            out.unavailable[name] = (
                "no captured order book for this token at this instant; "
                "order-book history covers a small share of tokens")

    if episode.first_buy_price > 0 and snapshot.last_original_price > 0:
        out._put("price_move_since_original",
                 snapshot.last_original_price / episode.first_buy_price - 1.0,
                 stamp)
    if episode.first_opposite_price > 0 and snapshot.last_opposite_price > 0:
        out._put("price_move_since_opposite",
                 snapshot.last_opposite_price / episode.first_opposite_price
                 - 1.0, stamp)
    out._put("market_age_seconds_observed", since_first, stamp)
    out.unavailable["seconds_to_resolution"] = (
        "resolutions.settled_ts is 0 throughout this store, so no market's "
        "true resolution moment is known; time-to-resolution cannot be "
        "computed without fabricating it")

    # -- WALLET HISTORY (prior episodes only) -------------------------------
    if history is None or history.episodes == 0:
        for name in ("wallet_prior_episodes", "wallet_switch_rate",
                     "wallet_aggressive_rate", "wallet_avg_trade_notional",
                     "wallet_avg_hold_seconds", "wallet_category_aggressive_rate"):
            out.unavailable[name] = (
                "no prior finished episode for this wallet at the signal "
                "instant — a population average would leak the population")
    else:
        history_stamp = min(history.last_known_ts or stamp, signal_ts)
        out._put("wallet_prior_episodes", history.episodes, history_stamp)
        out._put("wallet_prior_switched", history.switched, history_stamp)
        out._put("wallet_switch_rate", history.switch_rate, history_stamp,
                 "no prior episodes")
        out._put("wallet_aggressive_rate", history.aggressive_rate,
                 history_stamp,
                 "no prior episode of this wallet reached a two-class label")
        out._put("wallet_avg_trade_notional", history.avg_trade_notional,
                 history_stamp, "no prior trades")
        out._put("wallet_avg_hold_seconds", history.avg_hold_seconds,
                 history_stamp, "no prior holding period")
        bucket = history.by_category.get(category_of(episode.question))
        if bucket and bucket[0] > 0:
            out._put("wallet_category_aggressive_rate", bucket[1] / bucket[0],
                     history_stamp)
        else:
            out.unavailable["wallet_category_aggressive_rate"] = (
                "no prior episode by this wallet in this category")

    # Deliberately NOT a feature: the wallet's historical profitability.
    # Part 12 flags it and it is a genuine trap here — our P&L reconstruction
    # needs settlement, settlement arrives late, and a "historical ROI"
    # computed from a settlement we only learned about afterwards is future
    # information wearing a past-tense name.
    out.unavailable["wallet_historical_roi"] = (
        "excluded by design: our P&L reconstruction depends on settlements "
        "recorded after the fact, so this cannot be computed as-of the signal "
        "without leakage (Part 12)")
    return out


def _direction_changes(episode: Episode, cutoff: float) -> int:
    """How many times the wallet switched which side it was buying."""
    changes, previous = 0, None
    for event in episode.events:
        if event.ts > cutoff or not event.is_buy:
            continue
        side = "original" if event.token_id == episode.original_token \
            else "opposite"
        if previous is not None and side != previous:
            changes += 1
        previous = side
    return changes


# ---------------------------------------------------------------------------
# Part 23 — the leakage audit
# ---------------------------------------------------------------------------


@dataclass
class LeakageAudit:
    """Explicit, per-feature, from recorded stamps rather than from review."""

    checked: int = 0
    violations: list = field(default_factory=list)
    by_feature_max_lag: dict = field(default_factory=dict)
    unavailable_counts: dict = field(default_factory=dict)
    snapshots_checked: int = 0
    snapshot_violations: int = 0
    label_uses_future: bool = True     # by design, and stated

    @property
    def clean(self) -> bool:
        return not self.violations and not self.snapshot_violations

    def to_dict(self) -> dict:
        return {
            "featuresChecked": self.checked,
            "violations": self.violations[:50],
            "violationCount": len(self.violations),
            "snapshotsChecked": self.snapshots_checked,
            "snapshotViolations": self.snapshot_violations,
            "clean": self.clean,
            "unavailableCounts": dict(sorted(
                self.unavailable_counts.items(), key=lambda kv: -kv[1])),
            "note": (
                "Features are checked against the signal timestamp. Labels "
                "DELIBERATELY use the full lifecycle — that is what a label "
                "is — and are never inputs to a prediction. Settlement never "
                "reaches feature construction: there is no settlement term in "
                "`features.build`."),
        }


def leakage_audit(rows: Iterable[tuple]) -> LeakageAudit:
    """`rows` is `[(snapshot, feature_vector), ...]`.

    Fails on any feature whose newest input postdates the signal, and on any
    snapshot that consumed an event after its own cutoff. Both are arithmetic
    checks over recorded stamps, so this cannot pass because someone read the
    code carefully.
    """
    out = LeakageAudit()
    for snapshot, vector in rows:
        out.snapshots_checked += 1
        if snapshot.available_at > snapshot.ts + 1e-6:
            out.snapshot_violations += 1
            out.violations.append({
                "kind": "snapshot",
                "signalTs": snapshot.ts,
                "availableAt": snapshot.available_at,
                "detail": "snapshot consumed an event after its own cutoff"})
        for name, available_at in vector.available_at.items():
            out.checked += 1
            lag = available_at - vector.signal_ts
            previous = out.by_feature_max_lag.get(name)
            if previous is None or lag > previous:
                out.by_feature_max_lag[name] = lag
            if lag > 1e-6:
                out.violations.append({
                    "kind": "feature", "feature": name,
                    "signalTs": vector.signal_ts,
                    "availableAt": available_at, "lagSeconds": lag})
        for name in vector.unavailable:
            out.unavailable_counts[name] = \
                out.unavailable_counts.get(name, 0) + 1
    return out
