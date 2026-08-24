"""
Wallet behavioral discovery: from "which wallets look good" to "WHAT do
they repeatedly do, and does that behavior work WITHOUT them".

The existing analyzer ranks wallets; the RN1 wallet-state family follows
one wallet's checkpoint-frozen commitment. This layer asks the third
question, per the operator's spec: reconstruct what ranked wallets
actually did, extract the REPEATING behavior as an explicit market rule
(price band, preceding move, holding class), and hand that rule —
wallet-free — to the same library / frozen-OOS ladder every other
candidate walks. The wallet inspires the hypothesis; the historical
market universe is the laboratory; the wallet is never the proof.

Discipline, in order of importance:

* **No future information.** Every entry feature is computed from tape
  rows strictly BEFORE the wallet's entry timestamp. Time-to-resolution
  is knowable only after the fact on our tapes, so it is recorded as a
  LABEL for the per-wallet report and never becomes a rule condition;
  rules condition on market age (time since first trade), which an
  entrant genuinely knows.
* **A pattern, not a trade.** An observation is one wallet's FIRST buy
  of an engagement with one token (adds are sizing behavior, not new
  evidence). A cell must repeat — at least one wallet showing the
  behavior ``min_repeat`` times — before it may become a hypothesis.
* **Canonical identity.** A cell's identity is (direction, price-band
  bucket, trigger, hold class); the discovered numeric edges are
  thresholds, excluded from the signature exactly like every other
  family's. A hundred wallets sharing one behavior therefore merge into
  ONE candidate whose ``source_wallets`` metadata lists them all.
* **Independent replay.** ``frozen_replay`` needs only a market's price
  series (and its payout, for resolution holds) — no wallet present.
  The markets that produced the pattern ride in
  ``source_markets_list`` and join the candidate's permanent discovery
  exclusions at registration, so source markets can never testify.

Nothing here trades: ``wallet_behavior`` rules are excluded from the
engine's voting set even once validated, like every other event-shaped
family, until an execution path is deliberately built.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

# Price-band buckets: the cell's coarse identity. The rule's precise
# lo/hi edges are DISCOVERED from the member observations (and stay
# inside the bucket); the bucket name is what two wallets must share for
# their behavior to be recognized as the same idea.
BAND_BUCKETS = (
    ("longshot", 0.03, 0.20),
    ("low", 0.20, 0.40),
    ("mid", 0.40, 0.60),
    ("high", 0.60, 0.80),
    ("favorite", 0.80, 0.97),
)

# Hold classes from the wallet's OWN observed exits — data-supported, not
# an invented grid. "resolution" means the wallet never sold.
HOLD_CLASSES = (
    ("quick", 0.0, 3600.0),
    ("short", 3600.0, 6 * 3600.0),
    ("medium", 6 * 3600.0, 48 * 3600.0),
    ("long", 48 * 3600.0, float("inf")),
)

# How far back the preceding-move trigger looks on the tape.
LOOKBACK_SECONDS = 3600.0

# Floor for the discovered move threshold — below this the "move" is
# spread noise, and "calm" vs "after_drop" would be a coin flip.
MIN_MOVE_THRESHOLD = 0.03


def band_bucket(price: float) -> str:
    for name, lo, hi in BAND_BUCKETS:
        if lo <= price < hi:
            return name
    return ""


def hold_class(seconds: Optional[float]) -> str:
    if seconds is None:
        return "resolution"
    for name, lo, hi in HOLD_CLASSES:
        if lo <= seconds < hi:
            return name
    return "long"


@dataclass
class Observation:
    """One wallet's engagement entry with one token, entry-time features
    only. ``None`` marks a field the data could not provide — never
    invented.

    ``origin`` separates the two research populations the operator's spec
    demands stay distinct: a plain ``entry`` (first buy of a side with no
    prior opposite position) and a ``side_switch`` (first buy of a side
    the wallet took AFTER holding the opposite side). Switches carry the
    condition that preceded them — most importantly whether the prior
    position was winning or losing at the moment of the switch, computed
    from the opposite token's tape strictly before the switch."""
    wallet: str
    market: str
    token: str
    entry_ts: float
    entry_price: float
    move_before: Optional[float] = None    # signed, over LOOKBACK_SECONDS
    market_age: Optional[float] = None     # seconds since tape start
    exit_ts: Optional[float] = None        # wallet's own first sell
    exit_price: Optional[float] = None
    payout: Optional[float] = None         # settlement of the token
    adds: int = 0                          # sizing behavior, not evidence
    switched_sides: bool = False           # LABEL: bought opposite later
    time_to_resolution: Optional[float] = None   # LABEL only, never a rule
    origin: str = "entry"                  # "entry" | "side_switch"
    prior_result: Optional[str] = None     # switch: was the old side
    #                                        "winning"/"losing"/"flat"?
    switch_gap_seconds: Optional[float] = None   # since last opposite buy
    entry_usd: float = 0.0                 # notional of the first buy

    @property
    def hold_seconds(self) -> Optional[float]:
        if self.exit_ts is None:
            return None                    # held to resolution
        return max(0.0, self.exit_ts - self.entry_ts)

    def realized_return(self) -> Optional[float]:
        """Per-share result of the ENTRY, before costs."""
        if self.exit_price is not None:
            return self.exit_price - self.entry_price
        if self.payout is not None:
            return self.payout - self.entry_price
        return None


def observations_from_token(wallet_rows: list[dict], tape: list[dict],
                            payout: Optional[float],
                            opposite_rows: Optional[list[dict]] = None,
                            opposite_tape: Optional[list[dict]] = None
                            ) -> list[Observation]:
    """Reconstruct one wallet's engagements with one token.

    ``wallet_rows``: THIS wallet's trades on this token (ts, price, side).
    ``tape``: the token's full trade tape (all wallets), for entry-time
    market features. ``opposite_rows``: the wallet's trades on the
    market's OTHER token — an opposite BUY before this entry makes the
    entry a SIDE SWITCH; one after it sets the switching label.
    ``opposite_tape``: the other token's tape, for the prior position's
    mark at the switch moment (strictly before it).
    """
    buys = [r for r in sorted(wallet_rows, key=lambda r: float(r.get("ts") or 0))
            if not str(r.get("side") or "BUY").upper().startswith("S")]
    sells = [r for r in sorted(wallet_rows, key=lambda r: float(r.get("ts") or 0))
             if str(r.get("side") or "BUY").upper().startswith("S")]
    if not buys:
        return []
    tape_sorted = sorted(tape, key=lambda r: float(r.get("ts") or 0))
    tape_start = float(tape_sorted[0].get("ts") or 0) if tape_sorted else None
    tape_end = float(tape_sorted[-1].get("ts") or 0) if tape_sorted else None

    first = buys[0]
    entry_ts = float(first.get("ts") or 0.0)
    obs = Observation(
        wallet=str(first.get("wallet") or "").lower(),
        market=str(first.get("market_id") or ""),
        token=str(first.get("token_id") or ""),
        entry_ts=entry_ts,
        entry_price=float(first.get("price") or 0.0),
        payout=payout,
        adds=len(buys) - 1,
        entry_usd=float(first.get("usdc") or 0.0),
    )
    # Entry-time market features: STRICTLY before the entry. The last
    # tape price before the lookback window opens vs the last before
    # entry — both in the past at decision time.
    before = [r for r in tape_sorted
              if float(r.get("ts") or 0) < entry_ts]
    if before and tape_start is not None:
        obs.market_age = entry_ts - tape_start
        window_open = entry_ts - LOOKBACK_SECONDS
        older = [r for r in before if float(r.get("ts") or 0) <= window_open]
        if older:
            obs.move_before = (float(before[-1].get("price") or 0.0)
                               - float(older[-1].get("price") or 0.0))
    # The wallet's own exit: first SELL after the entry.
    for row in sells:
        ts = float(row.get("ts") or 0.0)
        if ts > entry_ts:
            obs.exit_ts = ts
            obs.exit_price = float(row.get("price") or 0.0)
            break
    if opposite_rows:
        opp_buys = sorted(
            (r for r in opposite_rows
             if not str(r.get("side") or "BUY").upper().startswith("S")),
            key=lambda r: float(r.get("ts") or 0))
        obs.switched_sides = any(
            float(r.get("ts") or 0) > entry_ts for r in opp_buys)
        prior = [r for r in opp_buys if float(r.get("ts") or 0) < entry_ts]
        if prior:
            # This entry is a SIDE SWITCH: the wallet held the opposite
            # side first. The condition that matters most (the operator's
            # §4): was the old position winning or losing AT the switch —
            # marked from the opposite tape strictly before this moment.
            obs.origin = "side_switch"
            obs.switch_gap_seconds = entry_ts - float(
                prior[-1].get("ts") or 0.0)
            if opposite_tape:
                marks = [r for r in sorted(
                    opposite_tape, key=lambda r: float(r.get("ts") or 0))
                    if float(r.get("ts") or 0) < entry_ts]
                if marks:
                    old_entry = float(prior[0].get("price") or 0.0)
                    mark = float(marks[-1].get("price") or 0.0)
                    if mark > old_entry + 0.01:
                        obs.prior_result = "winning"
                    elif mark < old_entry - 0.01:
                        obs.prior_result = "losing"
                    else:
                        obs.prior_result = "flat"
    if obs.exit_ts is None and tape_end is not None:
        obs.time_to_resolution = max(0.0, tape_end - entry_ts)  # LABEL
    return [obs]


@dataclass
class Engagement:
    """One wallet's whole involvement with ONE market — the unit at which
    "plays both sides" is actually decidable.

    A market is the independent unit of evidence, so the taxonomy is
    decided here rather than per trade. The distinction the operator
    insists on: entering the opposite side while STILL HOLDING the first
    is hedge-like (a book), whereas exiting first and then entering the
    opposite is reversal-like (a changed opinion). They are different
    behaviors and must never be summed into one "plays both sides"
    number."""
    wallet: str
    market: str
    legs: list                             # Observation, entry order
    kind: str = "one_sided"                # see classify_engagement
    gap_seconds: Optional[float] = None    # first entry -> opposite entry

    @property
    def entry_price(self) -> float:
        return self.legs[0].entry_price if self.legs else 0.0

    @property
    def hold_seconds(self) -> Optional[float]:
        return self.legs[0].hold_seconds if self.legs else None

    def book_return(self) -> Optional[float]:
        """Notional-weighted return of the whole book, per share of cost.

        Weighted because a $5 hedge leg against a $500 position is not
        half the story. Legs the data cannot value are excluded; a book
        with nothing valuable returns None rather than a fabricated 0."""
        pairs = [(o.realized_return(), max(0.0, o.entry_usd))
                 for o in self.legs if o.realized_return() is not None]
        if not pairs:
            return None
        weight = sum(w for _r, w in pairs)
        if weight <= 0:                    # no notional recorded: plain mean
            return sum(r for r, _w in pairs) / len(pairs)
        return sum(r * w for r, w in pairs) / weight


def classify_engagement(legs: list) -> tuple[str, Optional[float]]:
    """The two-sided taxonomy, from entry-time facts only.

    * ``one_sided`` — the wallet only ever took one outcome.
    * ``simultaneous_two_sided`` — the opposite side was entered while
      the first was still held (hedge-like: a book, not an opinion).
    * ``sequential_two_sided`` — the first side was exited BEFORE the
      opposite was entered (reversal-like: a changed opinion).
    * ``two_sided_unknown_order`` — both sides present but the exit data
      cannot say which; recorded honestly rather than guessed.
    """
    if len(legs) < 2:
        return "one_sided", None
    ordered = sorted(legs, key=lambda o: o.entry_ts)
    first, second = ordered[0], ordered[1]
    gap = second.entry_ts - first.entry_ts
    if first.exit_ts is None:
        # Never sold the first side: still held when the opposite arrived.
        return "simultaneous_two_sided", gap
    if first.exit_ts <= second.entry_ts:
        return "sequential_two_sided", gap
    return "simultaneous_two_sided", gap


def engagements_of(observations: list[Observation]) -> list[Engagement]:
    """Group per-token observations into per-(wallet, market) books."""
    grouped: dict[tuple[str, str], list[Observation]] = {}
    for o in observations:
        grouped.setdefault((o.wallet, o.market), []).append(o)
    out: list[Engagement] = []
    for (wallet, market), legs in grouped.items():
        legs = sorted(legs, key=lambda o: o.entry_ts)
        kind, gap = classify_engagement(legs)
        out.append(Engagement(wallet=wallet, market=market, legs=legs,
                              kind=kind, gap_seconds=gap))
    return out


def two_sided_study(engagements: list[Engagement], *,
                    min_side: int = 8) -> dict:
    """The operator's central test: does two-sided participation actually
    do better than one-sided, once conditions are controlled?

    Reported as a CONDITIONAL comparison, never as "N% two-sided = N%
    edge". Markets are the unit (a market counts once), the comparison is
    matched on entry-price bucket so a two-sided book in the 80-97c band
    is not credited for beating one-sided longshots, and the verdict
    refuses to say "edge" below ``min_side`` markets a side."""
    one = [e for e in engagements
           if e.kind == "one_sided" and e.book_return() is not None]
    two = [e for e in engagements
           if e.kind != "one_sided" and e.book_return() is not None]

    def _summary(rows: list[Engagement]) -> dict:
        returns = [e.book_return() for e in rows]
        holds = [e.hold_seconds for e in rows if e.hold_seconds is not None]
        return {
            "markets": len(rows),
            "expectancy": round(sum(returns) / len(returns), 4) if returns
            else 0.0,
            "median": round(_quantile(returns, 0.5), 4) if returns else 0.0,
            "winRate": (round(sum(1 for r in returns if r > 0)
                              / len(returns), 4) if returns else 0.0),
            "medianHold": (round(_quantile(holds, 0.5), 1) if holds
                           else None),
        }

    # Matched comparison: only price buckets where BOTH populations are
    # present can speak to the difference; the rest are noted as unmatched.
    buckets: dict[str, dict] = {}
    for name, lo, hi in BAND_BUCKETS:
        in_one = [e for e in one if lo <= e.entry_price < hi]
        in_two = [e for e in two if lo <= e.entry_price < hi]
        if not in_one or not in_two:
            continue
        a, b = _summary(in_one), _summary(in_two)
        buckets[name] = {
            "oneSided": a, "twoSided": b,
            "incremental": round(b["expectancy"] - a["expectancy"], 4),
        }
    matched_markets = sum(v["oneSided"]["markets"] + v["twoSided"]["markets"]
                          for v in buckets.values())
    matched_incremental = (
        round(sum(v["incremental"]
                  * (v["oneSided"]["markets"] + v["twoSided"]["markets"])
                  for v in buckets.values()) / matched_markets, 4)
        if matched_markets else None)

    overall_one, overall_two = _summary(one), _summary(two)
    out = {
        "oneSided": overall_one, "twoSided": overall_two,
        "twoSidedMarketShare": (
            round(len(two) / (len(one) + len(two)), 4)
            if (one or two) else 0.0),
        "kinds": {k: sum(1 for e in engagements if e.kind == k)
                  for k in ("one_sided", "simultaneous_two_sided",
                            "sequential_two_sided",
                            "two_sided_unknown_order")},
        "byEntryPrice": buckets,
        "matchedIncremental": matched_incremental,
    }
    out["verdict"] = two_sided_verdict(out, min_side=min_side)
    return out


def two_sided_verdict(study: dict, *, min_side: int = 8,
                      material: float = 0.02) -> str:
    """Plain-language conclusion that refuses to overclaim.

    The operator's rule, enforced here: a share of two-sided activity is
    an OBSERVATION. It becomes a claimed difference only with enough
    markets on both sides, and even then it is called a measured
    difference — never a validated edge, which only the OOS ladder can
    confer."""
    one = study["oneSided"]["markets"]
    two = study["twoSided"]["markets"]
    share = float(study.get("twoSidedMarketShare") or 0.0)
    if two == 0:
        return (f"No two-sided markets observed ({one} one-sided). "
                "Nothing to compare.")
    if one < min_side or two < min_side:
        return (f"Two-sided activity in {share:.0%} of markets "
                f"({two} two-sided vs {one} one-sided) - too few markets "
                f"on one side to compare (need {min_side} each). This is "
                "an observation, not an edge.")
    incremental = study.get("matchedIncremental")
    basis = "at comparable entry prices"
    if incremental is None:
        incremental = round(study["twoSided"]["expectancy"]
                            - study["oneSided"]["expectancy"], 4)
        basis = "unmatched (no price bucket held both kinds)"
    if abs(incremental) < material:
        return (f"Two-sided activity in {share:.0%} of markets; conditional "
                f"expectancy {study['twoSided']['expectancy']:+.3f} vs "
                f"{study['oneSided']['expectancy']:+.3f} one-sided "
                f"{basis} - no material difference.")
    better = "better" if incremental > 0 else "WORSE"
    return (f"Two-sided activity in {share:.0%} of markets; conditional "
            f"expectancy {study['twoSided']['expectancy']:+.3f} vs "
            f"{study['oneSided']['expectancy']:+.3f} one-sided {basis} - "
            f"two-sided measured {incremental:+.3f}/share {better} "
            f"({two} vs {one} markets). A measured difference, not a "
            "validated edge: only independent OOS testing can confer that.")


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def _snap(value: float, *, up: bool) -> float:
    """Snap a discovered band edge outward to the 0.05 grid."""
    import math
    grid = 20.0
    snapped = (math.ceil(value * grid) if up
               else math.floor(value * grid)) / grid
    return round(min(0.99, max(0.01, snapped)), 2)


def trigger_of(move: Optional[float], threshold: float) -> str:
    if move is None:
        return ""                          # unavailable is not "calm"
    if move <= -threshold:
        return "after_drop"
    if move >= threshold:
        return "after_rise"
    return "calm"


def mine(observations: list[Observation], *, cost: float,
         min_trades: int = 10, min_markets: int = 4,
         min_repeat: int = 3, top_n: int = 3) -> dict:
    """Aggregate observations into canonical behavior cells and keep the
    ones that repeat, span independent markets, and beat costs on the
    SOURCE record. Survivors are hypotheses — never validations."""
    funnel: dict[str, Any] = {
        "observations": len(observations), "settledObservations": 0,
        "cellsFormed": 0, "duplicatesMerged": 0, "kept": 0,
    }
    reject: dict[str, int] = {}
    usable = [o for o in observations
              if o.realized_return() is not None
              and band_bucket(o.entry_price)]
    funnel["settledObservations"] = len(usable)

    # The move threshold is discovered from the population of observed
    # preceding moves — the data's own scale, floored above spread noise.
    moves = [abs(o.move_before) for o in usable if o.move_before is not None]
    move_threshold = round(max(MIN_MOVE_THRESHOLD, _quantile(moves, 0.6)), 4)
    funnel["moveThreshold"] = move_threshold

    funnel["switchObservations"] = sum(
        1 for o in usable if o.origin == "side_switch")

    cells: dict[tuple, list[Observation]] = {}
    for o in usable:
        trig = trigger_of(o.move_before, move_threshold)
        if not trig:
            reject["no tape before entry (move unavailable)"] = \
                reject.get("no tape before entry (move unavailable)", 0) + 1
            continue
        # Origin is part of the cell identity: a side switch into a band
        # after a move and a fresh entry into the same band after the
        # same move are DIFFERENT hypotheses (the operator's §5 — the
        # switching and non-switching versions compete, never merge).
        key = (o.origin, "long", band_bucket(o.entry_price), trig,
               hold_class(o.hold_seconds))
        cells.setdefault(key, []).append(o)
    funnel["cellsFormed"] = len(cells)
    funnel["switchCells"] = sum(1 for k in cells if k[0] == "side_switch")

    candidates: list[dict] = []
    for (origin, direction, bucket, trig, hold), members in cells.items():
        per_wallet: dict[str, int] = {}
        for o in members:
            per_wallet[o.wallet] = per_wallet.get(o.wallet, 0) + 1
        supporters = sorted(w for w, n in per_wallet.items()
                            if n >= min_repeat)
        markets = sorted({o.market for o in members})
        trades = len(members)
        if not supporters:
            reject["behavior never repeats within any single wallet"] = \
                reject.get("behavior never repeats within any single wallet",
                           0) + 1
            continue
        if trades < min_trades:
            reject["insufficient source trades"] = \
                reject.get("insufficient source trades", 0) + 1
            continue
        if len(markets) < min_markets:
            reject["insufficient independent source markets"] = \
                reject.get("insufficient independent source markets", 0) + 1
            continue
        returns = [o.realized_return() for o in members]
        net = sum(r - cost for r in returns) / trades
        wins = sum(1 for r in returns if r - cost > 0)
        if net <= 0:
            reject["source record cannot clear costs"] = \
                reject.get("source record cannot clear costs", 0) + 1
            continue
        # Discovered numeric conditions: the members' own distribution,
        # snapped outward to the grid, kept inside the identity bucket.
        bucket_lo, bucket_hi = next(
            (lo, hi) for name, lo, hi in BAND_BUCKETS if name == bucket)
        prices = [o.entry_price for o in members]
        prob_lo = max(bucket_lo, _snap(_quantile(prices, 0.10), up=False))
        prob_hi = min(bucket_hi, _snap(_quantile(prices, 0.90), up=True))
        if prob_hi <= prob_lo:
            prob_lo, prob_hi = bucket_lo, bucket_hi
        holds = [o.hold_seconds for o in members
                 if o.hold_seconds is not None]
        rule: dict[str, Any] = {
            "type": "wallet_behavior",
            "origin": origin,
            "direction": direction,
            "band": bucket, "trigger": trig, "hold": hold,
            "prob_lo": prob_lo, "prob_hi": prob_hi,
            "move_min": move_threshold,
            "lookback_seconds": LOOKBACK_SECONDS,
            "source_wallets": supporters,
            "supporting_wallets": len(supporters),
            "source_trades": trades,
            "source_markets": len(markets),
            "source_markets_list": markets,
            "source_win_rate": round(wins / trades, 4),
            "source_net": round(net, 6),
            "bothSidedShare": round(
                sum(1 for o in members if o.switched_sides) / trades, 4),
        }
        if origin == "side_switch":
            # The condition that preceded the switches — recorded from
            # the data, never assumed. "switch_after" names the dominant
            # prior-position state; the full split stays for audit.
            states = [o.prior_result for o in members if o.prior_result]
            split = {s: states.count(s) for s in
                     ("losing", "winning", "flat") if s in states}
            rule["switch_prior_split"] = split
            rule["switch_after"] = (max(split, key=split.get)
                                    if split else "unknown")
            gaps = [o.switch_gap_seconds for o in members
                    if o.switch_gap_seconds is not None]
            if gaps:
                rule["switch_gap_median_s"] = float(_quantile(gaps, 0.5))
        if hold != "resolution":
            rule["hold_seconds"] = max(600.0, float(_quantile(holds, 0.5))
                                       if holds else 3600.0)
        candidates.append(rule)

    # One canonical candidate per identity is structural (the cell key IS
    # the identity), so "duplicates merged" is every extra wallet that
    # fed an emitted cell rather than spawning its own strategy.
    funnel["duplicatesMerged"] = sum(
        max(0, int(c["supporting_wallets"]) - 1) for c in candidates)
    candidates.sort(key=lambda c: (-int(c["supporting_wallets"]),
                                   -float(c["source_net"])))
    kept = candidates[:top_n]
    funnel["hypotheses"] = len(candidates)
    funnel["kept"] = len(kept)
    funnel["keptSwitch"] = sum(
        1 for c in kept if c.get("origin") == "side_switch")
    funnel["multiWallet"] = sum(
        1 for c in kept if int(c["supporting_wallets"]) >= 2)
    funnel["rejectReasons"] = dict(sorted(reject.items(),
                                          key=lambda kv: -kv[1]))
    return {"candidates": kept, "funnel": funnel}


def behavioral_profile(observations: list[Observation]) -> dict:
    """One wallet's behavioral fingerprint, for the Wallets research view.

    Every field is an OBSERVATION about how the wallet behaves, not a
    claim that the behavior works. ``sampleQuality`` uses the SAME
    evidence weighting as the authoritative ranking (ranking.SHRINKAGE_K)
    rather than inventing a second confidence scale, and multiplies it by
    market breadth — because 50 trades in 2 markets is thin evidence
    however confident the trade count looks."""
    from .ranking import SHRINKAGE_K

    settled = [o for o in observations if o.realized_return() is not None]
    engagements = engagements_of(settled)
    markets = len({e.market for e in engagements})
    profile: dict[str, Any] = {
        "observations": len(observations),
        "settled": len(settled),
        "independentMarkets": markets,
        "trades": len(settled) + sum(o.adds for o in settled),
    }
    if not settled:
        profile.update({"sampleQuality": 0.0, "researchPriority": 0.0})
        return profile

    holds = [o.hold_seconds for o in settled if o.hold_seconds is not None]
    winners = [o.hold_seconds for o in settled
               if o.hold_seconds is not None and (o.realized_return() or 0) > 0]
    losers = [o.hold_seconds for o in settled
              if o.hold_seconds is not None and (o.realized_return() or 0) <= 0]
    to_resolution = sum(1 for o in settled if o.hold_seconds is None)
    buckets = [band_bucket(o.entry_price) for o in settled
               if band_bucket(o.entry_price)]
    switches = [o for o in settled if o.origin == "side_switch"]
    two_sided = two_sided_study(engagements)

    post = [o.realized_return() or 0.0 for o in switches]
    hold_classes = [hold_class(o.hold_seconds) for o in settled]
    profile.update({
        # ``markets`` is the long-standing key for independent markets;
        # both names carry the same number so nothing reads it twice.
        "markets": markets,
        "holdClasses": {h: hold_classes.count(h) for h in set(hold_classes)},
        "avgAdds": round(sum(o.adds for o in settled) / len(settled), 2),
        "switchRate": (round(len(switches) / len(settled), 4)
                       if switches else 0.0),
        "switchMarkets": len({o.market for o in switches}),
        "postSwitchWinRate": (round(sum(1 for r in post if r > 0)
                                    / len(post), 4) if post else 0.0),
        "postSwitchAvgReturn": (round(sum(post) / len(post), 4)
                                if post else 0.0),
        "switchedSides": sum(1 for o in settled if o.switched_sides),
        "medianHold": round(_quantile(holds, 0.5), 1) if holds else None,
        "winningHold": round(_quantile(winners, 0.5), 1) if winners else None,
        "losingHold": round(_quantile(losers, 0.5), 1) if losers else None,
        "heldToResolution": to_resolution,
        "entryPriceBias": (max(set(buckets), key=buckets.count)
                           if buckets else ""),
        "entryPriceSpread": {b: buckets.count(b) for b in set(buckets)},
        "winRate": round(sum(1 for o in settled
                             if (o.realized_return() or 0) > 0)
                         / len(settled), 4),
        "avgReturn": round(sum(o.realized_return() or 0.0 for o in settled)
                           / len(settled), 4),
        "switches": len(switches),
        "switchAfterLosing": sum(1 for o in switches
                                 if o.prior_result == "losing"),
        "switchAfterWinning": sum(1 for o in switches
                                  if o.prior_result == "winning"),
        "twoSided": two_sided,
        "sequencePattern": _dominant_sequence(engagements),
    })
    # Evidence weight (same K as the ranking) x market breadth.
    n = len(settled)
    confidence = n / (n + SHRINKAGE_K)
    breadth = min(1.0, markets / 20.0)
    quality = round(confidence * breadth, 4)
    profile["sampleQuality"] = quality
    # Research priority: quality, lifted when the wallet actually shows a
    # behavior worth extracting (repeated switching or a distinct
    # two-sided population), because a wallet that only ever does one
    # thing has nothing for the behavioral layer to test.
    texture = 0.0
    if len(switches) >= 3:
        texture += 0.25
    kinds = two_sided.get("kinds") or {}
    if kinds.get("simultaneous_two_sided", 0) >= 3:
        texture += 0.15
    if kinds.get("sequential_two_sided", 0) >= 3:
        texture += 0.15
    profile["researchPriority"] = round(min(1.0, quality + texture), 4)
    return profile


def _dominant_sequence(engagements: list[Engagement]) -> str:
    """The shape this wallet repeats most: how it enters, and whether the
    market ends one-sided, hedged, or reversed."""
    if not engagements:
        return ""
    counts: dict[str, int] = {}
    for e in engagements:
        if e.kind == "one_sided":
            leg = e.legs[0]
            label = ("ENTRY-HOLD-RESOLUTION" if leg.hold_seconds is None
                     else "ENTRY-HOLD-EXIT")
        elif e.kind == "sequential_two_sided":
            label = "ENTRY-EXIT-OPPOSITE"
        elif e.kind == "simultaneous_two_sided":
            label = "ENTRY-ADD-OPPOSITE (book)"
        else:
            label = "TWO-SIDED (order unknown)"
        counts[label] = counts.get(label, 0) + 1
    top = max(counts, key=counts.get)
    return f"{top} ({counts[top]}/{len(engagements)})"


def study(store, *, pinned: list[str], cost: float,
          min_trades: int = 10, min_markets: int = 4,
          min_repeat: int = 3, min_wallet_trades: int = 8,
          max_wallets: int = 12, top_n: int = 3,
          max_tokens: int = 600) -> dict:
    """Reconstruct ranked + pinned wallets' behavior from the settled
    tapes and mine the repeating patterns. Returns candidates, a funnel,
    and a per-wallet behavioral report for the explainability surface."""
    targets = [str(a).lower() for a in pinned if a]
    try:
        ranked = store.query(
            "SELECT wallet FROM wallet_scores WHERE rank > 0 "
            "ORDER BY rank LIMIT ?", (max_wallets,))
        targets += [str(r["wallet"]).lower() for r in ranked]
    except Exception:                                    # noqa: BLE001
        pass
    targets = list(dict.fromkeys(targets))[:max_wallets]
    funnel: dict[str, Any] = {"walletsConsidered": len(targets),
                              "walletsEligible": 0}
    if not targets:
        funnel.update({"observations": 0, "kept": 0,
                       "rejectReasons": {"no ranked or pinned wallets yet": 1}})
        return {"candidates": [], "funnel": funnel, "perWallet": {}}

    resolutions = store.resolutions()
    payouts = {t: (1.0 if float(p) >= 0.99 else 0.0)
               for t, p in resolutions.items()}

    placeholders = ",".join("?" for _ in targets)
    rows = store.query(
        "SELECT wallet, market_id, token_id, ts, price, usdc, side "
        "FROM wallet_trades WHERE lower(wallet) IN (" + placeholders + ") "
        "ORDER BY ts", tuple(targets))
    by_wallet_token: dict[tuple[str, str], list[dict]] = {}
    tokens_of_market: dict[str, set[str]] = {}
    for row in rows:
        wallet = str(row["wallet"]).lower()
        token = str(row["token_id"])
        by_wallet_token.setdefault((wallet, token), []).append(row)
        tokens_of_market.setdefault(str(row["market_id"]), set()).add(token)

    # Settled tokens only — an unresolved engagement has no honest label.
    settled_keys = [k for k in by_wallet_token if k[1] in payouts]
    skipped_tokens = max(0, len({k[1] for k in settled_keys}) - max_tokens)
    if skipped_tokens:
        funnel["tokensSkippedByCap"] = skipped_tokens
    allowed_tokens = set(sorted({k[1] for k in settled_keys})[:max_tokens])

    tape_cache: dict[str, list[dict]] = {}

    def _tape(token: str) -> list[dict]:
        if token not in tape_cache:
            tape_cache[token] = store.query(
                "SELECT ts, price FROM wallet_trades WHERE token_id = ? "
                "ORDER BY ts", (token,))
        return tape_cache[token]

    observations: list[Observation] = []
    per_wallet: dict[str, dict] = {}
    for wallet in targets:
        mine_keys = [k for k in settled_keys
                     if k[0] == wallet and k[1] in allowed_tokens]
        wallet_obs: list[Observation] = []
        for _, token in mine_keys:
            wallet_rows = by_wallet_token[(wallet, token)]
            market = str(wallet_rows[0].get("market_id") or "")
            siblings = tokens_of_market.get(market, set()) - {token}
            opposite: list[dict] = []
            for sibling in siblings:
                opposite += by_wallet_token.get((wallet, sibling), [])
            # The prior position's mark must come from the tape of the
            # token the wallet actually held first. Binary markets have
            # one sibling, but a multi-outcome market has several — the
            # earliest opposite BUY before this entry is the position
            # the reconstruction prices, so its token is the right tape.
            entry_ts = min((float(r.get("ts") or 0.0) for r in wallet_rows
                            if not str(r.get("side") or "BUY").upper()
                            .startswith("S")), default=None)
            opposite_tape = None
            if opposite and entry_ts is not None:
                prior = sorted(
                    (r for r in opposite
                     if float(r.get("ts") or 0.0) < entry_ts
                     and not str(r.get("side") or "BUY").upper()
                     .startswith("S")),
                    key=lambda r: float(r.get("ts") or 0.0))
                if prior:
                    opposite_tape = _tape(
                        str(prior[0].get("token_id") or ""))
            wallet_obs += observations_from_token(
                wallet_rows, _tape(token), payouts.get(token), opposite,
                opposite_tape)
        # ONE implementation of the behavioral fingerprint — the Wallets
        # research view, the CLI report and the hypothesis layer all read
        # this same function rather than each computing their own.
        per_wallet[wallet] = behavioral_profile(wallet_obs)
        if len([o for o in wallet_obs if o.realized_return() is not None]) \
                >= min_wallet_trades:
            funnel["walletsEligible"] += 1
            observations += wallet_obs

    mined = mine(observations, cost=cost, min_trades=min_trades,
                 min_markets=min_markets, min_repeat=min_repeat,
                 top_n=top_n)
    mined["funnel"].update(funnel)
    # The population-level answer to "is playing both sides an edge?" —
    # pooled across the eligible wallets, matched on entry price, and
    # phrased so a share can never be read as an edge.
    mined["funnel"]["twoSided"] = two_sided_study(
        engagements_of([o for o in observations
                        if o.realized_return() is not None]))
    return {"candidates": mined["candidates"], "funnel": mined["funnel"],
            "perWallet": per_wallet}


def frozen_replay(rows: list[dict], rule: dict,
                  payout: Optional[float], cost: float) -> dict:
    """One FROZEN wallet-behavior rule against one unseen market's
    exported series — no wallet involved; the rule must stand alone.

    ``rows`` are research-series bars (``_ts``, ``price``). ``cost`` is
    the full round trip. Resolution-hold rules take ONE observation per
    market (multiple entries would all ride the same payout, which is one
    piece of evidence pretending to be several) and skip markets whose
    payout is unknown."""
    if len(rows) < 10:
        return {"trades": 0}
    prob_lo = float(rule.get("prob_lo") or 0.0)
    prob_hi = float(rule.get("prob_hi") or 1.0)
    move_min = float(rule.get("move_min") or MIN_MOVE_THRESHOLD)
    lookback = float(rule.get("lookback_seconds") or LOOKBACK_SECONDS)
    trigger = str(rule.get("trigger") or "calm")
    hold = str(rule.get("hold") or "resolution")
    hold_seconds = float(rule.get("hold_seconds") or 3600.0)
    direction = str(rule.get("direction") or "long")
    sign = 1.0 if direction == "long" else -1.0
    if hold == "resolution" and payout is None:
        return {"trades": 0}

    pnl: list[float] = []
    blocked_until = -float("inf")
    anchor = -1                       # newest row at least ``lookback`` old
    for i, row in enumerate(rows):
        ts = float(row.get("_ts") or 0.0)
        price = float(row.get("price") or 0.0)
        while anchor + 1 < i and \
                float(rows[anchor + 1].get("_ts") or 0.0) <= ts - lookback:
            anchor += 1
        if len(pnl) >= 50:
            break                     # one market is one market
        if ts <= blocked_until or not prob_lo <= price < prob_hi:
            continue
        if anchor < 0:
            continue
        move = price - float(rows[anchor].get("price") or 0.0)
        if trigger_of(move, move_min) != trigger:
            continue
        if hold == "resolution":
            pnl.append(sign * (float(payout) - price) - cost)
            break                          # one observation per market
        exit_price = None
        for later in rows[i + 1:]:
            if float(later.get("_ts") or 0.0) >= ts + hold_seconds:
                exit_price = float(later.get("price") or 0.0)
                blocked_until = float(later.get("_ts") or 0.0) + lookback
                break
        if exit_price is None:
            break                          # series ends inside the hold
        pnl.append(sign * (exit_price - price) - cost)

    if not pnl:
        return {"trades": 0}
    total = sum(pnl)
    equity = peak = draw = 0.0
    for value in pnl:
        equity += value
        peak = max(peak, equity)
        draw = max(draw, peak - equity)
    return {"trades": len(pnl), "wins": sum(1 for v in pnl if v > 0),
            "pnl": total, "expectancy": total / len(pnl),
            "drawdown": draw, "sharpe": 0.0}


def convergences(rule: dict, existing: list[dict]) -> list[str]:
    """Signatures of NON-wallet library strategies that independently
    landed on the same idea — the operator's convergent-discovery
    signal. Prioritization metadata only; it validates nothing."""
    trig = str(rule.get("trigger") or "")
    direction = str(rule.get("direction") or "long")
    lo = float(rule.get("prob_lo") or 0.0)
    hi = float(rule.get("prob_hi") or 1.0)
    # after_drop + long is buying weakness (mean reversion); after_rise +
    # long is buying strength (momentum). Shorts mirror.
    wanted_family = ""
    if (trig, direction) in (("after_drop", "long"), ("after_rise", "short")):
        wanted_family = "mean-reversion"
    elif (trig, direction) in (("after_rise", "long"),
                               ("after_drop", "short")):
        wanted_family = "momentum"
    out: list[str] = []
    for row in existing:
        other = row.get("rule") or {}
        if str(other.get("type") or "") == "wallet_behavior":
            continue
        signature = str(row.get("signature") or "")
        if other.get("type") == "longshot":
            o_lo = float(other.get("prob_lo") or 0.0)
            o_hi = float(other.get("prob_hi") or 1.0)
            same_sense = ((str(other.get("side") or "low") == "low")
                          == (direction == "long"))
            if same_sense and o_lo < hi and lo < o_hi:
                out.append(signature)
        elif wanted_family:
            from ..research import family_of
            if family_of(other) == wanted_family:
                out.append(signature)
    return sorted(set(out))


def describe(rule: dict) -> str:
    hold = str(rule.get("hold") or "resolution")
    hold_text = ("hold to resolution" if hold == "resolution"
                 else f"hold ~{float(rule.get('hold_seconds') or 3600)/3600.0:g}h"
                 f" ({hold})")
    trig = {"after_drop": f"after drop >={float(rule.get('move_min') or 0):.0%}",
            "after_rise": f"after rise >={float(rule.get('move_min') or 0):.0%}",
            "calm": "in calm tape"}.get(str(rule.get("trigger") or ""), "?")
    side = "BUY" if str(rule.get("direction") or "long") == "long" else "FADE"
    switch = ""
    if str(rule.get("origin") or "entry") == "side_switch":
        after = str(rule.get("switch_after") or "unknown")
        switch = (" as a SIDE SWITCH"
                  + (f" (old side {after})" if after != "unknown" else ""))
    return (f"WALLET-PATTERN ({int(rule.get('supporting_wallets') or 0)} "
            f"wallet(s)): {side} {float(rule.get('prob_lo') or 0):.0%}-"
            f"{float(rule.get('prob_hi') or 1):.0%} {trig}{switch}, "
            f"{hold_text}")
