"""
Backfilling settled history, so wallets can be studied today rather than in a month.

Everything the analytical layer wants to know — was this wallet right, what
exit rule would have worked, does its edge hold up — needs **markets that have
already resolved**. Watching live markets produces almost none of that: they
are live precisely because they have not settled yet. After a full day of
running, the store held 26 settled outcomes and not one wallet had enough
resolved positions to test an exit rule against.

The fix is to stop waiting. Polymarket publishes closed markets with their
settlement prices, and the Data API serves their full trade history. So we can
ask directly for markets that have *already* finished and pull the whole story
at once: who traded, at what price, in what order, and how it ended.

One market backfilled this way yields around 500 trades from ~400 distinct
wallets **with a complete price path** — more usable evidence than a day of
live watching. It is the difference between a tool that will be powerful
eventually and one that is powerful now.

Nothing here trades or decides. It only fills in history the live loop cannot
reach.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..logs import Log
from ..models import WalletTrade
from .store import IntelStore

# The Data API serves a market's tape newest-first, and a settled market's
# newest trades are the ones placed after everyone already knew the answer.
# Asking for the first N therefore returns the epilogue: of the research series
# built that way, 91% moved less than five cents end to end and every one
# finished pinned at 0.999 or 0.001. Discovery over them produced rules with a
# 97.9% win rate that had learned only that a token at 0.998 reaches 0.999.
#
# So walk back down the tape to where the outcome was still in doubt, and
# collect from there instead.
PROBE_LIMIT = 5            # trades per probe; median of them resists one outlier
MAX_PROBES = 16            # gallop + bisect budget, per market
PAGE_LIMIT = 500           # trades per collection page
REQUEST_PAUSE = 0.12       # seconds between calls - a public API, used politely


def _as_list(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:                                # noqa: BLE001
            return []
    return []


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _settlement_moment(record: dict) -> tuple[float, str]:
    """When the market actually settled, and which field said so.

    Preference order is closest-to-the-event first: `closedTime` is when
    Polymarket closed it, `endDate`/`umaEndDate` are the scheduled end. Zero
    with an empty source when the record carries none — the caller then knows
    it does not have a settlement clock, rather than being handed one that is
    really our insert time.
    """
    for field_name, label in (("closedTime", "gamma_closed"),
                              ("endDate", "gamma_end"),
                              ("umaEndDate", "gamma_end")):
        stamp = _timestamp(record.get(field_name))
        if stamp > 0:
            return stamp, label
    return 0.0, ""


def _timestamp(value) -> float:
    """Epoch seconds from Polymarket's several date spellings, or 0.0."""
    if value in (None, "", 0):
        return 0.0
    number = _f(value, 0.0)
    if number > 0:
        # Some fields arrive as epoch milliseconds; a 2001+ date in seconds
        # is under 1e11, so anything above that is milliseconds.
        return number / 1000.0 if number > 1e11 else number
    import datetime as _dt

    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return parsed.timestamp()


async def _trade_page(http, data_host: str, market_id: str,
                      limit: int, offset: int) -> list[dict]:
    """One page of a market's tape, newest first. Empty list on any failure."""
    try:
        response = await http.get(
            f"{data_host.rstrip('/')}/trades",
            params={"market": market_id, "limit": limit, "offset": offset})
        response.raise_for_status()
        payload = response.json()
    except Exception:                                    # noqa: BLE001
        return []
    items = payload if isinstance(payload, list) else (payload.get("data") or [])
    return [row for row in items if isinstance(row, dict)]


async def _probe_price(http, data_host: str, market_id: str,
                       offset: int) -> Optional[float]:
    """Typical trade price at a depth in the tape. None past the end of it."""
    await asyncio.sleep(REQUEST_PAUSE)
    rows = await _trade_page(http, data_host, market_id, PROBE_LIMIT, offset)
    prices = sorted(_f(r.get("price")) for r in rows if _f(r.get("price")) > 0)
    if not prices:
        return None
    return prices[len(prices) // 2]


async def _uncertain_offset(http, data_host: str, market_id: str,
                            lo: float, hi: float) -> Optional[int]:
    """Depth of the newest trade still priced inside the uncertainty band.

    Gallops outward to bracket the boundary, then bisects it, so a tape of any
    length costs about a dozen requests rather than a full scan. Price is not
    perfectly monotonic, so the bisection lands near the transition rather than
    exactly on it - near is all the collection step needs.

    None means the band was never reached: the market was already decided for
    the whole reachable tape, and there is nothing here worth studying.
    """
    in_band = lambda p: p is not None and lo <= p <= hi   # noqa: E731

    first = await _probe_price(http, data_host, market_id, 0)
    if first is None:
        return None
    if in_band(first):
        return 0                       # still in play at the end of the tape

    probes = 1
    low, high = 0, 0                   # low: known decided, high: known in-band
    step = 250
    ceiling: Optional[int] = None      # shallowest offset known to be past the end
    while probes < MAX_PROBES:
        price = await _probe_price(http, data_host, market_id, step)
        probes += 1
        if price is None:
            # Past the end of the tape, not proof the market stayed decided:
            # a low-volume market can have fewer trades than the first stride.
            # Halve back toward the last readable depth instead of giving up -
            # skipping those was skipping exactly the quiet markets whose whole
            # life sits inside the band.
            ceiling = step
            nearer = (low + step) // 2
            if nearer <= low + PROBE_LIMIT:
                break                  # tape ends here; nothing in band below
            step = nearer
            continue
        if in_band(price):
            high = step
            break
        low = step
        step = step * 2 if ceiling is None else (low + ceiling) // 2
        if ceiling is not None and step <= low + PROBE_LIMIT:
            break
    if not high:
        return None

    while probes < MAX_PROBES and high - low > PAGE_LIMIT:
        middle = (low + high) // 2
        price = await _probe_price(http, data_host, market_id, middle)
        probes += 1
        if price is None:
            high = middle              # ran off the end; the boundary is nearer
        elif in_band(price):
            high = middle
        else:
            low = middle
    return high


@dataclass
class BackfillResult:
    markets_seen: int = 0
    markets_used: int = 0
    trades_written: int = 0
    resolutions_written: int = 0
    wallets: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def note(self, why: str) -> None:
        self.skipped[why] = self.skipped.get(why, 0) + 1

    def to_dict(self) -> dict:
        return {
            "marketsSeen": self.markets_seen, "marketsUsed": self.markets_used,
            "tradesWritten": self.trades_written,
            "resolutionsWritten": self.resolutions_written,
            "wallets": self.wallets, "skipped": self.skipped,
        }


async def run(http, store: IntelStore, log: Optional[Log] = None,
              gamma_host: str = "https://gamma-api.polymarket.com",
              data_host: str = "https://data-api.polymarket.com",
              markets: int = 60, trades_per_market: int = 500,
              progress: Optional[Callable[[str], None]] = None,
              uncertain_lo: float = 0.10, uncertain_hi: float = 0.90,
              max_volume: float = 25_000_000.0,
              min_volume: float = 50_000.0
              ) -> BackfillResult:
    """Pull recently-closed markets and the part of their tape worth studying.

    `uncertain_lo`/`uncertain_hi` bound the prices at which a market's outcome
    was still genuinely in doubt. Collection starts where the tape was last
    inside that band, because the trades after it are settlement arithmetic
    rather than price discovery.

    `max_volume` skips markets too heavily traded to page back into, and
    `min_volume` skips ones too quiet to have a tape at all - see the comment
    on the listing loop. 0 disables either bound.
    """
    say = progress or (lambda _m: None)
    result = BackfillResult()

    rows: list[dict] = []
    page = 0
    # Pages scale with the ask: a years-deep pull needs to walk far beyond the
    # first few hundred closed markets. Volume ordering is kept deliberately -
    # the highest-volume markets in Polymarket's history span its whole life
    # and carry the densest tapes, which is exactly what research wants.
    #
    # ...except that the Data API refuses an offset past 10,000, so on the very
    # biggest markets the reachable tape is only the last ten thousand trades -
    # all of it after the outcome was obvious. Measured across volume tiers,
    # the share of markets whose uncertain period is still reachable runs:
    #
    #     top 15 by volume ($200M+)   0%
    #     rank ~400      (~$21M)     33%
    #     rank ~1500     (~$6M)      42%
    #
    # So the densest tapes are the least studiable, and taking them first was
    # loading the pull with markets that can only contribute settlement
    # arithmetic. `max_volume` skips that head of the list; the tapes below it
    # are thinner but they still contain the part worth studying. `min_volume`
    # trims the other end, where markets have no tape at all.
    #
    # Both bounds go to the API rather than being applied to what comes back:
    # the whole top of the volume ranking is above the ceiling, so filtering
    # locally just returned an empty list however many pages were walked.
    bounds: dict[str, float] = {}
    if max_volume > 0:
        bounds["volume_num_max"] = max_volume
    if min_volume > 0:
        bounds["volume_num_min"] = min_volume
    while len(rows) < markets and page < max(6, (markets * 2) // 100 + 2):
        try:
            response = await http.get(
                f"{gamma_host.rstrip('/')}/markets",
                params={"closed": "true", "limit": min(100, markets * 2),
                        "offset": page * 100, "order": "volumeNum",
                        "ascending": "false", **bounds})
            response.raise_for_status()
            batch = response.json()
        except Exception as exc:                         # noqa: BLE001
            say(f"  could not list closed markets: {exc}")
            break
        if not isinstance(batch, list) or not batch:
            break
        rows.extend(r for r in batch if isinstance(r, dict))
        page += 1

    result.markets_seen = len(rows)
    say(f"  found {len(rows)} closed markets")
    if bounds:
        span = " to ".join(filter(None, [
            f"${min_volume:,.0f}" if min_volume > 0 else "",
            f"${max_volume:,.0f}" if max_volume > 0 else ""]))
        say(f"  volume {span}: above that the tape is longer than the API will "
            "page, so the only reachable part is after the outcome was decided")

    wallets: set[str] = set()
    for record in rows[:markets]:
        market_id = str(record.get("conditionId") or "")
        if not market_id:
            result.note("no condition id")
            continue

        tokens = [str(t) for t in _as_list(record.get("clobTokenIds"))]
        prices = [_f(p) for p in _as_list(record.get("outcomePrices"))]
        if len(tokens) != len(prices) or not tokens:
            result.note("no outcome prices")
            continue
        # Only decisive settlements are usable as ground truth. A market that
        # closed without one outcome taking essentially everything cannot say
        # whether a wallet was right, and recording a mid-range price as the
        # settled value would score every trade in it against a number the
        # market never paid.
        if not any(p >= 0.99 for p in prices):
            result.note("closed but not decisively resolved")
            continue

        # Collect from where the outcome was still in doubt, not from the tail.
        start = await _uncertain_offset(http, data_host, market_id,
                                        uncertain_lo, uncertain_hi)
        if start is None:
            result.note("decided for its whole reachable tape")
            continue

        items: list[dict] = []
        offset = start
        while len(items) < trades_per_market:
            await asyncio.sleep(REQUEST_PAUSE)
            page = await _trade_page(http, data_host, market_id,
                                     min(PAGE_LIMIT, trades_per_market - len(items)),
                                     offset)
            if not page:
                break
            items.extend(page)
            offset += len(page)
        if not items:
            result.note("trade history unavailable")
            continue

        observed: list[WalletTrade] = []
        for row in items:
            if not isinstance(row, dict):
                continue
            wallet = str(row.get("proxyWallet") or row.get("maker") or "").lower()
            if not wallet:
                continue
            price, size = _f(row.get("price")), _f(row.get("size"))
            side = str(row.get("side") or "BUY").upper()
            observed.append(WalletTrade(
                wallet=wallet, ts=int(_f(row.get("timestamp"))),
                market_id=market_id, token_id=str(row.get("asset") or ""),
                outcome=str(row.get("outcome") or ""),
                side="SELL" if side.startswith("S") else "BUY",
                price=price, size=size, usdc=price * size,
                question=str(row.get("title") or record.get("question") or ""),
                tx=str(row.get("transactionHash") or ""), source="backfill"))
            wallets.add(wallet)

        if not observed:
            result.note("no trades returned")
            continue

        result.trades_written += store.record_trades(observed)
        # §6: the market's OWN settlement moment, from the source record —
        # never the moment this database happened to write the row. A backfill
        # run today would otherwise stamp a market that closed last year with
        # today's date, and every time-aware comparison downstream inherits
        # the error.
        settled_ts, settled_source = _settlement_moment(record)
        for token_id, price in zip(tokens, prices):
            store.record_resolution(token_id, market_id,
                                    1.0 if price >= 0.99 else 0.0,
                                    settled_ts=settled_ts,
                                    settled_source=settled_source)
            result.resolutions_written += 1
        result.markets_used += 1
        if result.markets_used % 10 == 0:
            say(f"  {result.markets_used} markets, "
                f"{result.trades_written:,} new trades, "
                f"{len(wallets):,} wallets")

    result.wallets = len(wallets)
    if log is not None:
        log.event("intel.backfill", **{k: v for k, v in result.to_dict().items()
                                       if k != "skipped"})
    return result
