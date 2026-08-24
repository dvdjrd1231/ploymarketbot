"""Drain the settlement backlog, standalone.

The bot already learns how markets settled — 60 markets every 300 seconds,
inside the trading loop, whenever the bot happens to be running. That cadence
is right for a live loop and wrong for a backlog: with 16k traded markets on
record it needs roughly a day of continuous uptime to catch up, and until it
does, almost every research question that depends on an outcome is answered on
a few hundred markets instead of thousands.

That is not a cosmetic problem. Settlement is what turns a wallet-behaviour
prediction into a graded prediction and a simulated entry into a realised P&L,
so an unresolved backlog silently caps two entirely separate research results
at the same small number.

So this is the same sweep, off the loop, batched hard, resumable, and
rate-limit aware. It writes only to `resolutions` and it writes only decisive
outcomes — the upstream `settlements()` call refuses a market that closed
without resolving, because recording a half-priced outcome as a settled value
would score every wallet that traded it against a number the market never paid.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class DrainResult:
    """What one drain achieved. Reported per batch so a long run is legible."""

    checked: int = 0
    settled_tokens: int = 0
    settled_markets: int = 0
    batches: int = 0
    errors: int = 0
    exhausted: bool = False
    error_samples: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"checked": self.checked, "settledTokens": self.settled_tokens,
                "settledMarkets": self.settled_markets,
                "batches": self.batches, "errors": self.errors,
                "exhausted": self.exhausted,
                "errorSamples": self.error_samples[:5]}


async def drain(store, settlements: Callable, batch: int = 60,
                max_batches: int = 0, pause_seconds: float = 0.4,
                patience: int = 10,
                progress: Optional[Callable] = None) -> DrainResult:
    """Resolve as much of the backlog as the source will give us.

    `settlements` is the adapter coroutine `market_ids -> {token: (market,
    price)}`. Injected rather than constructed here so this is testable
    without a network, and so the one place that knows how to ask Gamma stays
    the adapter.

    Stops when the backlog is empty, when `max_batches` is reached, or when a
    batch returns nothing new — the last of which matters: `markets_without_
    resolution` returns oldest-first, so a batch that resolves none of its
    sixty means those sixty are genuinely unresolvable (still open, or void),
    and hammering the same sixty forever would look like progress while making
    none.
    """
    out = DrainResult()
    seen_unresolvable: set = set()
    barren = 0
    while True:
        if max_batches and out.batches >= max_batches:
            break
        # Ask for the batch PLUS everything already parked, because the store
        # cannot know what this run has given up on. Without the extra room
        # the parked markets eat the limit — batch two sees 19 fresh markets
        # instead of 60, batch three sees none, and the drain reports itself
        # finished after touching 1% of the backlog.
        window = batch + len(seen_unresolvable)
        pending = [m for m in store.markets_without_resolution(limit=window)
                   if m not in seen_unresolvable][:batch]
        if not pending:
            out.exhausted = True
            break
        out.batches += 1
        out.checked += len(pending)
        try:
            found = await settlements(pending)
        except Exception as exc:                          # noqa: BLE001
            out.errors += 1
            out.error_samples.append(repr(exc)[:160])
            if out.errors >= 5:
                break
            await asyncio.sleep(max(pause_seconds, 2.0))
            continue

        markets = set()
        for token_id, (market_id, price) in found.items():
            store.record_resolution(token_id, market_id, price)
            out.settled_tokens += 1
            markets.add(market_id)
        out.settled_markets += len(markets)

        # Anything in this batch we did NOT resolve is parked for this run, so
        # the next batch advances instead of asking the same question again.
        seen_unresolvable |= {m for m in pending if m not in markets}

        if progress:
            progress(f"  batch {out.batches}: checked {len(pending)}, "
                     f"settled {len(markets)} market(s) / "
                     f"{len(found)} outcome(s) — running total "
                     f"{out.settled_markets:,} markets")
        if not markets:
            barren += 1
            # Oldest-first means a barren batch is weak evidence that the rest
            # is barren too — but only weak. Unresolved markets cluster (a
            # whole event's worth can be open at once), so one empty batch is
            # a cluster and several in a row is a backlog that has caught up
            # with the present.
            if barren >= max(1, patience):
                break
        else:
            barren = 0
        await asyncio.sleep(pause_seconds)
    return out
