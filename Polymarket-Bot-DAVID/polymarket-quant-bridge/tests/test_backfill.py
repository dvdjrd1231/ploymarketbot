"""Where in a settled market's tape the backfill collects from.

The Data API serves a market's trades newest-first, so asking for the first N
of a *settled* market returns the epilogue - the trades placed after everyone
already knew the answer. Of the research series built that way, 91% moved less
than five cents end to end and every one finished pinned at 0.999 or 0.001;
discovery over them produced rules with a 97.9% win rate that had learned only
that a token at 0.998 reaches 0.999.

These lock in that collection starts where the outcome was still in doubt.
"""
from __future__ import annotations

import asyncio

from pqb.analytics.backfill import _uncertain_offset


class FakeTape:
    """A market's tape, newest-first, served like the Data API serves it.

    `prices` is oldest-first (how the market actually lived); the fake reverses
    it, because that ordering is the whole reason the bug existed.
    """

    def __init__(self, prices: list[float]):
        self.newest_first = list(reversed(prices))
        self.calls: list[int] = []

    async def get(self, _url, params=None):
        params = params or {}
        offset = int(params.get("offset", 0))
        limit = int(params.get("limit", 100))
        self.calls.append(offset)
        window = self.newest_first[offset:offset + limit]
        return _Response([{"price": p, "size": 1, "proxyWallet": "0xw",
                           "timestamp": 1_700_000_000 + i}
                          for i, p in enumerate(window)])


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def offset_for(prices, lo=0.10, hi=0.90):
    """The project drives async code from sync tests with asyncio.run."""
    tape = FakeTape(prices)
    found = asyncio.run(_uncertain_offset(tape, "http://x", "M1", lo, hi))
    return found, tape


def in_play(n):
    return [0.45] * n


def decided(n):
    return [0.995] * n


# ---------------------------------------------------------------------------

def test_collection_starts_past_the_settled_tail():
    """The bug this exists to prevent: offset 0 is the epilogue."""
    found, tape = offset_for(in_play(4000) + decided(3000))
    assert found is not None
    assert found > 0, "started at the tail, which is all post-decision"
    # Landing anywhere inside the decided run would still collect settlement
    # arithmetic; it has to be at or past the boundary.
    assert found >= 2500


def test_a_tape_still_in_play_collects_from_the_front():
    found, _ = offset_for(in_play(2000))
    assert found == 0


def test_a_market_decided_throughout_is_skipped_not_collected():
    """Nothing here is worth studying, and saying so beats collecting it."""
    found, _ = offset_for(decided(5000))
    assert found is None


def test_an_empty_tape_is_skipped():
    found, _ = offset_for([])
    assert found is None


def test_the_search_costs_a_handful_of_requests_not_a_full_scan():
    """A tape of any length has to stay affordable: this runs over hundreds of
    markets against a public API."""
    found, tape = offset_for(in_play(50_000) + decided(40_000))
    assert found is not None
    assert len(tape.calls) <= 16


def test_the_band_is_honoured():
    """Widening the band moves the boundary, so the caller's config decides
    what counts as decided rather than a constant in here."""
    prices = in_play(2000) + [0.93] * 2000 + decided(1000)
    narrow, _ = offset_for(prices, lo=0.10, hi=0.90)
    wide, _ = offset_for(prices, lo=0.10, hi=0.95)
    assert narrow is not None and wide is not None
    assert wide < narrow, "a wider band should reach the in-band region sooner"


def test_one_odd_print_does_not_move_the_boundary():
    """Probes take the median of several trades, so a single stray price in the
    settled tail cannot be mistaken for the market still being in play."""
    prices = in_play(3000) + decided(3000)
    prices[-1500] = 0.50                      # one outlier deep in the tail
    found, _ = offset_for(prices)
    assert found is not None
    assert found >= 2500


def test_a_short_tape_is_not_mistaken_for_a_decided_one():
    """The bug this exists to prevent.

    The first stride was 250 trades. A low-volume market with fewer trades than
    that read as "past the end of the tape", which the search treated as proof
    the market stayed decided - so it skipped 91 of 100 markets, and the ones it
    skipped were exactly the quiet markets whose whole life sits inside the band.
    """
    for total_in_play in (40, 90, 200):
        found, _ = offset_for(in_play(total_in_play) + decided(10))
        assert found is not None, f"{total_in_play}-trade tape wrongly skipped"
        assert found > 0


def test_short_tapes_still_cost_only_a_few_requests():
    _, tape = offset_for(in_play(90) + decided(10))
    assert len(tape.calls) <= 8


def test_a_short_tape_that_really_is_decided_is_still_skipped():
    found, _ = offset_for(decided(60))
    assert found is None
