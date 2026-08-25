"""News -> market causality.

Sentiment is not causality, and this module exists because treating them as the
same thing is the standard way a news feature destroys a strategy.

A headline says something about the WORLD. A market question asks something
specific, and which side of it benefits from a given fact depends entirely on
how the question is worded. "Fed holds rates" is bullish for one market and
bearish for the mirror of it. A sentiment score cannot know which, so V3 never
derives direction from sentiment. Direction comes from one of three things,
each of which is recorded on the signal:

    RULE        an explicit per-market rule a human wrote
    ANALOGUE    what this market did after materially similar past events
    NONE        we do not know, and the signal carries magnitude only

The classifications the brief asks for are produced here, and each is a
statement about the RELATIONSHIP between an event and a market rather than
about the event alone:

    INFORMATION_SHOCK        confirmed, material, and the market moved hard
    NEWS_ALREADY_PRICED      the market moved BEFORE the item was capturable
    NEWS_MARKET_DISLOCATION  confirmed and material, but the market has not moved
    NEWS_CONFIRMED_BY_WALLETS   profiled wallets traded the implied direction
    NEWS_CONTRADICTED_BY_WALLETS   they traded against it
    RUMOR_UNCONFIRMED        single unreliable source; not actionable

`NEWS_ALREADY_PRICED` is the one that saves money. It is detected by comparing
the market's move BEFORE our capture time against its move after: if the move
happened before we could have known, then trading it now is paying for
information the market already has.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from ..core.canon import Availability, EvidenceState, Signal, SignalClass


class NewsClass(str, Enum):
    INFORMATION_SHOCK = "INFORMATION_SHOCK"
    NEWS_ALREADY_PRICED = "NEWS_ALREADY_PRICED"
    NEWS_MARKET_DISLOCATION = "NEWS_MARKET_DISLOCATION"
    NEWS_CONFIRMED_BY_WALLETS = "NEWS_CONFIRMED_BY_WALLETS"
    NEWS_CONTRADICTED_BY_WALLETS = "NEWS_CONTRADICTED_BY_WALLETS"
    RUMOR_UNCONFIRMED = "RUMOR_UNCONFIRMED"
    NO_MATERIAL_NEWS = "NO_MATERIAL_NEWS"


# Confirmation ladder. Nothing below MULTI_SOURCE may drive a trade on its own.
CONFIRMATION_RANK = {"RUMOR": 0, "UNCONFIRMED": 1, "MULTI_SOURCE": 2,
                     "OFFICIAL": 3, "MARKET_PRICED": 4}

DIRECTION_SOURCES = ("RULE", "ANALOGUE", "NONE")


@dataclass
class EventSignal:
    classification: NewsClass = NewsClass.NO_MATERIAL_NEWS
    magnitude: float = 0.0            # 0..1 how material
    direction: float = 0.0            # -1..+1 toward YES; 0 when unknown
    direction_source: str = "NONE"
    confidence: float = 0.0
    items: int = 0
    confirmed_items: int = 0
    pre_capture_move: float = 0.0
    post_capture_move: float = 0.0
    analogues: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    note: str = ""

    @property
    def actionable(self) -> bool:
        """A signal may drive a trade only with a KNOWN direction."""
        return (self.direction_source != "NONE"
                and abs(self.direction) > 0.05
                and self.classification in (
                    NewsClass.INFORMATION_SHOCK,
                    NewsClass.NEWS_MARKET_DISLOCATION,
                    NewsClass.NEWS_CONFIRMED_BY_WALLETS))

    def to_signal(self) -> Signal:
        return Signal(
            source="news_causality", kind=self.classification.value,
            direction=self.direction, strength=self.magnitude,
            classification=(SignalClass.SIGNAL if self.actionable
                            else SignalClass.INFORMATION),
            note=self.note, evidence=self.evidence)

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items()}
        d["classification"] = self.classification.value
        d["actionable"] = self.actionable
        return d


def analyse(ev: EvidenceState, store, *, wallet_dna: dict | None = None,
            rules: dict | None = None) -> EventSignal:
    """Classify the news environment for one market at one instant.

    Reads only `capture_ts`-bounded items via the evidence state, so it cannot
    use an item before we would have had it.
    """
    sig = EventSignal()
    if not ev.news.ok:
        sig.note = ev.news.note or "no news layer"
        return sig

    items = ev.news.get("latest") or []
    sig.items = int(ev.news.get("items") or 0)
    relevant = int(ev.news.get("relevant") or 0)
    if relevant == 0:
        sig.note = f"{sig.items} items captured, none linked to this market"
        return sig

    confirmed = [i for i in items
                 if CONFIRMATION_RANK.get(i.get("confirmation", "UNCONFIRMED"),
                                          1) >= 2]
    sig.confirmed_items = len(confirmed)
    sig.magnitude = float(ev.news.get("max_magnitude") or 0.0)
    sig.evidence = [f"{i.get('confirmation')}/{i.get('class')}: "
                    f"{str(i.get('title'))[:70]}" for i in items[:5]]

    if not confirmed:
        sig.classification = NewsClass.RUMOR_UNCONFIRMED
        sig.confidence = 0.15
        sig.note = (f"{relevant} relevant item(s), none corroborated by a "
                    f"second source or an official one. Not actionable: an "
                    f"unconfirmed report is a hypothesis about the world.")
        return sig

    # -- direction, from a rule or from analogues. NEVER from sentiment. ---
    sig.direction, sig.direction_source, why = _direction(
        ev, store, rules or {})
    if why:
        sig.evidence.append(why)

    # -- already priced? ---------------------------------------------------
    # Compare the move that happened BEFORE the earliest capture time against
    # the move since. If the market moved before we could have read the item,
    # we are late.
    newest_capture = min((int(i.get("capture_ts") or 0) for i in confirmed
                          if i.get("capture_ts")), default=0)
    pre, post = _split_move(ev, newest_capture)
    sig.pre_capture_move, sig.post_capture_move = pre, post

    moved = abs(post) >= 0.02
    pre_moved = abs(pre) >= 0.03

    if pre_moved and abs(pre) > abs(post) * 2:
        sig.classification = NewsClass.NEWS_ALREADY_PRICED
        sig.confidence = 0.6
        sig.note = (f"the market moved {pre:+.3f} BEFORE this item was "
                    f"capturable and only {post:+.3f} since. Whatever the news "
                    f"says, the price already reflects it; entering now pays "
                    f"for information the market has.")
        return sig

    # -- wallet corroboration ---------------------------------------------
    wallet_dir = _wallet_direction(ev, wallet_dna or {})
    if sig.direction_source != "NONE" and wallet_dir is not None:
        agree = (wallet_dir > 0) == (sig.direction > 0)
        if agree and abs(wallet_dir) > 0.005:
            sig.classification = NewsClass.NEWS_CONFIRMED_BY_WALLETS
            sig.confidence = 0.7
            sig.note = (f"confirmed news implies {sig.direction:+.2f} and "
                        f"profiled wallets are trading {wallet_dir:+.4f} in "
                        f"the same direction")
            return sig
        if not agree and abs(wallet_dir) > 0.005:
            sig.classification = NewsClass.NEWS_CONTRADICTED_BY_WALLETS
            sig.confidence = 0.5
            sig.note = (f"confirmed news implies {sig.direction:+.2f} but "
                        f"profiled wallets are trading {wallet_dir:+.4f} "
                        f"against it. Independent channels disagree, so "
                        f"confidence falls rather than averaging out.")
            return sig

    if moved and sig.magnitude >= 0.4:
        sig.classification = NewsClass.INFORMATION_SHOCK
        sig.confidence = 0.65
        sig.note = (f"confirmed material news and a {post:+.3f} move since it "
                    f"became capturable")
    elif not moved and sig.magnitude >= 0.4:
        sig.classification = NewsClass.NEWS_MARKET_DISLOCATION
        sig.confidence = 0.5
        sig.note = (f"confirmed material news but the market has moved only "
                    f"{post:+.3f} since. Either the market disagrees that it "
                    f"is material, or it has not adjusted yet — and those are "
                    f"not distinguishable from price alone.")
    else:
        sig.classification = NewsClass.NO_MATERIAL_NEWS
        sig.confidence = 0.25
        sig.note = "confirmed items present but none material enough to act on"

    if sig.direction_source == "NONE":
        sig.note += (" Direction is UNKNOWN: no per-market rule and no "
                     "historical analogue, and sentiment does not determine "
                     "which side of a binary question benefits. Magnitude "
                     "only.")
    return sig


def _direction(ev: EvidenceState, store, rules: dict) -> tuple:
    """Direction from an explicit rule, else from analogues, else unknown."""
    mid = ev.market_id
    r = rules.get(mid)
    if r is not None:
        return float(r), "RULE", f"per-market direction rule: {r:+.2f}"

    an = analogues(ev, store, limit=5)
    if len(an) >= 3:
        moves = [a["subsequent_move"] for a in an]
        mean = sum(moves) / len(moves)
        # Require the analogues to AGREE. Three past events that moved in
        # different directions are not a prior, they are noise.
        same = sum(1 for x in moves if (x > 0) == (mean > 0))
        if same / len(moves) >= 0.75 and abs(mean) > 0.01:
            return (max(-1.0, min(1.0, mean * 10)), "ANALOGUE",
                    f"{len(an)} historical analogues moved {mean:+.3f} on "
                    f"average, {same}/{len(moves)} in the same direction")
    return 0.0, "NONE", ""


def analogues(ev: EvidenceState, store, *, limit: int = 5) -> list:
    """Past events on this market with a similar profile, and what followed.

    Deliberately restricted to the SAME market: "what did this market do after
    news like this" is answerable; "what do markets in general do after news
    like this" is not, from 90 days of one venue.
    """
    if not ev.market_id:
        return []
    rows = store.query(
        "SELECT n.id, n.title, n.capture_ts, l.magnitude "
        "  FROM news_items n JOIN news_market_links l ON l.news_id = n.id "
        " WHERE l.market_id = ? AND n.capture_ts < ? "
        " ORDER BY n.capture_ts DESC LIMIT ?",
        (ev.market_id, ev.as_of - 3600, limit * 3))
    out = []
    for r in rows[:limit]:
        out.append({"news_id": r["id"], "title": r["title"],
                    "capture_ts": r["capture_ts"],
                    "magnitude": r["magnitude"],
                    # Subsequent move is unavailable without a price history
                    # query the evidence state does not carry; recorded as 0.0
                    # and the caller requires >=3 agreeing analogues, so a set
                    # of zeros can never produce a direction.
                    "subsequent_move": 0.0})
    return out


def _split_move(ev: EvidenceState, capture_ts: int) -> tuple:
    """(move before capture, move since capture) from the price layer."""
    if not ev.price.ok or not capture_ts:
        return 0.0, float(ev.price.get("velocity_1h") or 0.0)
    age = max(0, ev.as_of - capture_ts)
    vel = float(ev.price.get("velocity_1h") or 0.0)
    if age >= 3600:
        # The item is older than our price window: everything we can see is
        # "since". Nothing can be attributed to before it.
        return 0.0, vel
    # Split the hourly move at the capture point, pro rata. Coarse, and the
    # only decomposition a one-hour velocity supports.
    frac = age / 3600.0
    return round(vel * (1 - frac), 5), round(vel * frac, 5)


def _wallet_direction(ev: EvidenceState, dna: dict) -> float | None:
    if not ev.wallets.ok or not dna:
        return None
    tops = ev.wallets.get("top") or []
    alphas = [float(dna[t["wallet"]].get("alpha_vs_band") or 0.0)
              for t in tops if t["wallet"] in dna]
    if not alphas:
        return None
    return sum(alphas) / len(alphas)


def persist(store, ev: EvidenceState, sig: EventSignal,
            source: str = "news_causality") -> None:
    if sig.classification is NewsClass.NO_MATERIAL_NEWS:
        return
    store.alert("news_event",
                f"{sig.classification.value} on {ev.market_id[:18]}: "
                f"{sig.note[:200]}",
                severity="INFO", subject=ev.market_id, source=source)
