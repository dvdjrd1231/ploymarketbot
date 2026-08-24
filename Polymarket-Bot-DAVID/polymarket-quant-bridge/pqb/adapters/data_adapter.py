"""
Polymarket -> bridge, inbound (prompt section 3).

This is one of only two files that know Polymarket exists. It turns the
exchange's four surfaces into the bridge's own structures:

  * **CLOB WebSocket + REST** — order books, via the upstream ``PriceService``.
    Streamed where possible, REST-topped-up where not; the provenance of every
    quote is carried through so the engine can tell a live book from a poll.
  * **Gamma** — market metadata: question, category, outcomes, liquidity,
    volume, expiration, tick size, min order size, resolution state.
  * **Data API** — my positions, and each target wallet's activity/positions.
  * **CLOB authenticated REST** — my balance and open orders.

Everything is best-effort per source: one endpoint failing degrades that one
field and logs it, rather than aborting the cycle. A cycle that produces a
partial world view is still worth a decision; a cycle that raises produces
nothing.

Target wallets deliberately arrive as :class:`WalletSignal` objects with a
weight and no imperative — the engine decides what, if anything, to do with
them. Nothing in this file can cause an order.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Iterable, Optional

import httpx

from ..config import Config
from ..logs import Log
from ..models import (
    AccountState, MarketFeatures, MarketStatus, OutcomeQuote, PositionView,
    WalletPosition, WalletSignal, WalletTrade,
)
from ..upstream import (
    DataApiClient, GammaClient, MarketInfo, PriceService, Side, TradingClient,
    iso_to_ts,
)


# Markets discovered by watching wallets, held for the next universe refresh.
# Bounded because the global sweep sees the whole exchange (see _note_market).
_MAX_DISCOVERED_MARKETS = 500


def _f(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fee_bps(record: dict, keys: tuple[str, ...]) -> Optional[float]:
    """First present fee field among ``keys``, normalised to basis points.

    Polymarket has published fees under several names and units over time, and
    this document's field names must be verified against the live API — so this
    tries the likely candidates and interprets the unit defensively: a value
    below 1 is read as a fraction (0.02 -> 200 bps), a larger value as bps
    already. Returns ``None`` when the exchange publishes nothing, which the EV
    layer treats as "use the flat fallback fee", never as "fees are zero".
    """
    for key in keys:
        if key not in record or record[key] in (None, ""):
            continue
        try:
            value = float(record[key])
        except (TypeError, ValueError):
            continue
        if value <= 0:
            return 0.0
        return value * 10_000.0 if value < 1 else value
    return None


def _as_list(value) -> list:
    """Gamma returns several fields as JSON-encoded strings; normalise."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        import json
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def _category(record: dict) -> str:
    """The market's category, or "" — never the event title.

    The journal groups P&L by category, so the value has to be shared across
    many markets to say anything. Falling back to the parent event's *title*
    ("LoL: Hanwha Life Esports vs Gen.G - Game 2") gives almost every market its
    own category and turns that whole report cut into noise. An honest blank
    groups correctly as "uncategorised".
    """
    direct = record.get("category")
    if direct:
        return str(direct)
    events = record.get("events")
    if isinstance(events, list) and events and isinstance(events[0], dict):
        return str(events[0].get("category") or "")
    return ""


class PolymarketDataAdapter:
    """Assembles the inbound feature set. Read-only by construction."""

    def __init__(self, config: Config, log: Log,
                 http: Optional[httpx.AsyncClient] = None,
                 client: Optional[TradingClient] = None):
        self.config = config
        self.log = log
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(20.0, connect=12.0),
            limits=httpx.Limits(max_connections=32,
                                max_keepalive_connections=16,
                                keepalive_expiry=30.0),
            headers={"User-Agent": "PolymarketQuantBridge/0.1"},
        )
        pm = config.polymarket
        self.gamma = GammaClient(pm.gamma_host, self.http)
        self.data = DataApiClient(pm.data_api_host, self.http)
        self.prices = PriceService(
            clob_host=pm.clob_host,
            ws_host=pm.clob_ws_host,
            http=self.http,
            use_stream=pm.use_price_stream,
            rest_fallback_seconds=pm.rest_price_fallback_seconds,
        )
        self.client = client       # authenticated; None in dry-run

        # One DataApiClient per tracked wallet: it keeps the "last seen"
        # watermark internally, so sharing one across wallets would let the
        # busiest wallet's timestamps hide every other wallet's activity.
        self._wallet_feeds: dict[str, DataApiClient] = {}
        self._wallet_meta: dict[str, tuple[str, float]] = {}
        for wallet in config.wallets:
            address = wallet.address.strip()
            if not address:
                continue
            key = address.lower()
            self._wallet_feeds[key] = DataApiClient(pm.data_api_host, self.http)
            self._wallet_meta[key] = (wallet.label or address[:10],
                                      float(wallet.weight))

        # Broad ingestion. Observations accumulate here as market features are
        # built and are drained once per cycle by ``observe_trades()``.
        self._observed: list[WalletTrade] = []
        self._trade_page = max(25, config.intel.trades_per_market)
        self._global_page = max(0, config.intel.global_sweep_limit)

        self._records: dict[str, dict] = {}      # condition id -> raw Gamma row
        self._records_ts: float = 0.0
        self._universe: list[str] = []
        self._extra_markets: set[str] = set()    # discovered via wallets
        # Markets holding open positions. Tracked unconditionally — see pin().
        self._pinned: set[str] = set()
        self._pinned_tokens: set[str] = set()
        self._primed = False

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        await self.prices.start()
        await self.refresh_universe()
        await self.prime_wallets()

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.prices.stop()
        if self._owns_http:
            with contextlib.suppress(Exception):
                await self.http.aclose()

    async def prime_wallets(self) -> None:
        """Baseline each wallet's feed so history is not replayed as new signals.

        Without this, the first cycle would emit every trade the wallet has ever
        made as a fresh signal — a burst of stale context arriving as if it were
        happening now.
        """
        if self._primed:
            return
        for key, feed in self._wallet_feeds.items():
            try:
                seen = await feed.prime(key)
                label, _ = self._wallet_meta[key]
                self.log.event("wallet.primed", wallet=label, historical=seen)
            except Exception as exc:
                self.log.warning("Could not prime wallet feed",
                                 wallet=key[:10], error=repr(exc))
        self._primed = True

    # -- market universe -----------------------------------------------------

    @property
    def universe(self) -> list[str]:
        return list(self._universe)

    def pin(self, market_ids: Iterable[str],
            token_ids: Iterable[str] = ()) -> bool:
        """Track these markets unconditionally — they hold open positions.

        The discovered universe is a ranked slice (top N by volume/liquidity),
        so a market can legitimately fall out of it while we are still holding
        a position in it — and on a restart the slice is re-resolved from
        scratch. Without pinning, such a position goes blind: no book, so its
        mark falls back to its own entry price. P&L freezes at zero, the stop
        and trailing exits can never fire because the mark never moves, and an
        exit is priced at cost rather than at the bid — which in live trading
        would rest above the market and never fill.

        Returns True when something new was pinned, so the caller can force an
        immediate universe refresh rather than waiting out the normal interval.
        """
        markets = {str(m) for m in market_ids if m}
        tokens = {str(t) for t in token_ids if t}
        if tokens:
            # Track the books immediately; waiting for the next universe
            # refresh would leave the position unpriced until then.
            self.prices.track(tokens)
        added = markets - self._pinned
        self._pinned = markets
        self._pinned_tokens = tokens
        missing = {m for m in markets if m not in self._records}
        return bool(added or missing)

    def _note_market(self, market_id: str) -> None:
        """Remember a market some wallet touched, without growing without bound.

        The global sweep sees the whole exchange, so this set would otherwise
        accumulate every market Polymarket has ever listed over a long session
        — and each one is a metadata lookup the next universe refresh would try
        to make. Oldest entries are dropped once the cap is reached; a market
        that still matters will be observed again on the next sweep.
        """
        if not market_id or market_id in self._extra_markets:
            return
        if len(self._extra_markets) >= _MAX_DISCOVERED_MARKETS:
            self._extra_markets.pop()
        self._extra_markets.add(market_id)

    @property
    def pinned(self) -> set[str]:
        return set(self._pinned)

    @property
    def pinned_tokens(self) -> set[str]:
        return set(self._pinned_tokens)

    async def refresh_universe(self) -> list[str]:
        """Resolve which markets to track: explicit ids, filters, or both."""
        cfg = self.config.markets
        explicit = [m.strip() for m in cfg.track if m and m.strip()]
        records: dict[str, dict] = {}

        if explicit:
            records.update(await self._fetch_by_ids(explicit))
        if not explicit or cfg.filters.max_markets > len(records):
            records.update(await self._discover(cfg.filters))
        if cfg.follow_wallet_markets and self._extra_markets:
            missing = [m for m in self._extra_markets if m not in records]
            if missing:
                records.update(await self._fetch_by_ids(missing[:40]))
        # Markets we hold, fetched last so nothing above can leave one out.
        held = [m for m in self._pinned if m not in records]
        if held:
            records.update(await self._fetch_by_ids(held))

        keep = self._apply_filters(records, explicit)
        self._records = {mid: records[mid] for mid in keep if mid in records}
        self._records_ts = time.time()
        self._universe = list(self._records)

        tokens: set[str] = set()
        for record in self._records.values():
            tokens.update(str(t) for t in _as_list(record.get("clobTokenIds")))
        # Retain rather than accumulate: the universe rotates as volume moves,
        # and `track()` alone would grow the WebSocket subscription and the REST
        # sweep without bound over a long session. Held tokens are unioned back
        # in so releasing a market never un-prices a position in it.
        self.prices.retain_only(tokens | self._pinned_tokens)

        self.log.event("universe.refreshed", markets=len(self._universe),
                       tokens=len(tokens), pinned=len(self._pinned),
                       explicit=len(explicit))
        return self._universe

    def _apply_filters(self, records: dict[str, dict],
                       explicit: Iterable[str]) -> list[str]:
        """Filters gate *discovered* markets only.

        A market named explicitly in the config is an instruction, so it is kept
        even if it is thin or far from resolution. Silently dropping something
        the operator listed would be the adapter overruling the config.

        Markets holding open positions are kept for a stronger reason: a filter
        exists to decide what is worth *entering*, and it has no business
        deciding what we can still see once we are already in. A position whose
        market has become thin or is close to resolution is precisely the one
        that most needs a live price.
        """
        f = self.config.markets.filters
        forced = set(explicit) | set(self._pinned)
        wanted_categories = {c.lower() for c in f.categories}
        now = time.time()
        scored: list[tuple[float, str]] = []
        kept: list[str] = []

        for mid, record in records.items():
            if mid in forced:
                kept.append(mid)
                continue
            if bool(record.get("closed")) or record.get("active") is False:
                continue
            category = _category(record).lower()
            if wanted_categories and category not in wanted_categories:
                continue
            liquidity = _f(record.get("liquidityNum") or record.get("liquidity"))
            if liquidity < f.min_liquidity:
                continue
            volume_24h = _f(record.get("volume24hr"))
            if volume_24h < f.min_volume_24h:
                continue
            end_ts = iso_to_ts(record.get("endDate") or record.get("end_date_iso"))
            if end_ts is None:
                continue
            remaining = end_ts - now
            if remaining < f.min_seconds_to_resolution:
                continue
            if f.max_days_to_resolution and \
                    remaining > f.max_days_to_resolution * 86400:
                continue
            scored.append((volume_24h + liquidity, mid))

        scored.sort(reverse=True)
        budget = max(0, f.max_markets - len(kept))
        kept.extend(mid for _, mid in scored[:budget])
        return kept

    async def _fetch_by_ids(self, condition_ids: list[str]) -> dict[str, dict]:
        """Batch metadata lookup. Gamma accepts repeated ``condition_ids``."""
        if not condition_ids:
            return {}
        out: dict[str, dict] = {}
        for chunk in _chunks(condition_ids, 20):
            rows = await self._query({"condition_ids": chunk, "limit": len(chunk)})
            if not rows:
                # A batch query can fail where a single lookup succeeds (and the
                # upstream client also retries with closed markets included, so
                # a settled market is still found).
                for cid in chunk:
                    info = await self.gamma.get_market_info(cid)
                    if info is not None:
                        out[cid] = _record_from_info(info)
                continue
            for row in rows:
                cid = str(row.get("conditionId") or row.get("condition_id") or "")
                if cid:
                    out[cid] = row
        return out

    async def _discover(self, filters) -> dict[str, dict]:
        params = {
            "active": "true", "closed": "false",
            "limit": max(filters.max_markets * 6, 60),
            "order": "volume24hr", "ascending": "false",
        }
        rows = await self._query(params)
        out: dict[str, dict] = {}
        for row in rows:
            cid = str(row.get("conditionId") or row.get("condition_id") or "")
            if cid:
                out[cid] = row
        return out

    async def _query(self, params: dict, attempts: int = 3) -> list[dict]:
        """Ask Gamma for markets, retrying briefly before giving up.

        This one call decides whether the bridge can see anything at all: it
        resolves the tradable universe, and an empty result means no markets,
        no quotes, no captured features and no decisions — a bot that runs
        perfectly while being completely blind.

        It was originally a single attempt, and a client on a slower connection
        hit ``ConnectTimeout`` every cycle: the endpoint answers in ~1.5s here
        but the connect budget was 5s, which is not a generous margin on a
        home connection. Retrying with a short backoff costs nothing on the
        overwhelmingly common path where the first attempt succeeds.
        """
        data = None
        last: Optional[Exception] = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                resp = await self.http.get(
                    f"{self.config.polymarket.gamma_host.rstrip('/')}/markets",
                    params=params)
                resp.raise_for_status()
                data = resp.json()
                if attempt > 1:
                    self.log.event("gamma.recovered", attempt=attempt)
                break
            except Exception as exc:
                last = exc
                if attempt < attempts:
                    await asyncio.sleep(0.8 * attempt)
        if data is None:
            self.log.error(
                "Cannot reach Polymarket market data (Gamma). The bridge is "
                "blind until this recovers: no markets, no decisions. Check "
                "the internet connection and any firewall or VPN.",
                error=repr(last), host=self.config.polymarket.gamma_host,
                attempts=attempts)
            return []
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)]
        if isinstance(data, dict):
            rows = data.get("data") or []
            return [r for r in rows if isinstance(r, dict)]
        return []

    # -- market features -----------------------------------------------------

    async def market_features(self,
                              with_trades: bool = False
                              ) -> dict[str, MarketFeatures]:
        """Build the per-market feature set from metadata plus live books."""
        features: dict[str, MarketFeatures] = {}
        for mid, record in self._records.items():
            features[mid] = self._features_from(mid, record)

        # Resolution state is the one field worth a dedicated check: it decides
        # realized P&L, and the upstream client already polls it aggressively
        # near a market's end time and shares in-flight requests.
        for mid, market in features.items():
            info = self.gamma.cached(mid)
            if info is None:
                continue
            if info.resolved:
                market.status = MarketStatus.RESOLVED
            elif info.closed:
                market.status = MarketStatus.CLOSED
            if info.end_ts:
                market.end_ts = info.end_ts
            if info.outcome_prices:
                market.outcome_prices = list(info.outcome_prices)

        if with_trades and features:
            await self._attach_trades(features)
        return features

    async def prices_history(self, token_id: str, interval: str = "max",
                             fidelity: int = 0) -> list[tuple[int, float]]:
        """Price (implied-probability) history for one outcome token.

        The CLOB ``/prices-history`` endpoint, which neither reference project
        used — the interpretation layer (§4) wants probability *history*, not
        just the current touch, and a streamed book only ever shows now. Returns
        chronological ``(unix_ts, price)`` pairs, or an empty list on any error
        rather than raising, so a missing history never stalls a caller.
        """
        params: dict = {"market": str(token_id)}
        if interval:
            params["interval"] = interval
        if fidelity:
            params["fidelity"] = int(fidelity)
        try:
            resp = await self.http.get(
                f"{self.config.polymarket.clob_host.rstrip('/')}/prices-history",
                params=params)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:                          # noqa: BLE001
            self.log.warning("prices-history failed",
                             token=str(token_id)[:12], error=repr(exc))
            return []
        history = payload.get("history") if isinstance(payload, dict) else payload
        out: list[tuple[int, float]] = []
        for row in history or []:
            try:
                out.append((int(row["t"]), float(row["p"])))
            except (KeyError, TypeError, ValueError):
                continue
        out.sort()
        return out

    def _features_from(self, market_id: str, record: dict) -> MarketFeatures:
        token_ids = [str(t) for t in _as_list(record.get("clobTokenIds"))]
        outcomes = [str(o) for o in _as_list(record.get("outcomes"))]
        prices = [_f(p) for p in _as_list(record.get("outcomePrices"))]
        tick = _f(record.get("orderPriceMinTickSize"), 0.01) or 0.01
        min_size = _f(record.get("orderMinSize"), 0.0)

        status = MarketStatus.ACTIVE
        if bool(record.get("umaResolutionStatus") == "resolved") or \
                bool(record.get("resolved")):
            status = MarketStatus.RESOLVED
        elif bool(record.get("closed")) or record.get("active") is False:
            status = MarketStatus.CLOSED

        quotes: dict[str, OutcomeQuote] = {}
        for index, token_id in enumerate(token_ids):
            quotes[token_id] = self._quote_for(
                token_id,
                outcome=outcomes[index] if index < len(outcomes) else "",
                fallback=prices[index] if index < len(prices) else None,
                tick=tick,
            )

        return MarketFeatures(
            market_id=market_id,
            question=str(record.get("question") or record.get("title") or ""),
            slug=str(record.get("slug") or ""),
            category=_category(record),
            status=status,
            end_ts=iso_to_ts(record.get("endDate") or record.get("end_date_iso")),
            quotes=quotes,
            liquidity=_f(record.get("liquidityNum") or record.get("liquidity")),
            volume_total=_f(record.get("volumeNum") or record.get("volume")),
            volume_24h=_f(record.get("volume24hr")),
            outcome_prices=prices,
            tick_size=tick,
            min_order_size=min_size,
            taker_fee_bps=_fee_bps(record, (
                "takerBaseFee", "taker_base_fee", "takerFeeBps",
                "taker_fee_bps", "takerFee", "feeRateBps", "fee_rate_bps")),
            maker_fee_bps=_fee_bps(record, (
                "makerBaseFee", "maker_base_fee", "makerFeeBps",
                "maker_fee_bps", "makerFee")),
            created_ts=iso_to_ts(record.get("startDate")
                                 or record.get("createdAt")
                                 or record.get("creationDate")),
        )

    def _quote_for(self, token_id: str, outcome: str,
                   fallback: Optional[float], tick: float) -> OutcomeQuote:
        book = self.prices.book(token_id)
        if book is None:
            # No book yet (the stream has not delivered and REST has not run).
            # Gamma's own last price is a weak but honest stand-in, tagged as
            # such so nothing downstream mistakes it for a tradable quote.
            return OutcomeQuote(token_id=token_id, outcome=outcome,
                                last=fallback, mid=fallback, tick_size=tick,
                                source="none")
        bid, ask = book.best_bid, book.best_ask
        return OutcomeQuote(
            token_id=token_id,
            outcome=outcome,
            bid=bid,
            ask=ask,
            mid=book.midpoint,
            last=book.last_trade if book.last_trade else fallback,
            spread=book.spread,
            # Sizes are real only on a streamed book; the REST top-up knows the
            # touch but not the depth behind it and stores a placeholder.
            bid_depth=(book.bids.get(bid, 0.0)
                       if bid is not None and book.source == "stream" else 0.0),
            ask_depth=(book.asks.get(ask, 0.0)
                       if ask is not None and book.source == "stream" else 0.0),
            tick_size=book.tick_size or tick,
            source=book.source,
            updated_ts=book.updated,
        )

    async def _attach_trades(self, features: dict[str, MarketFeatures]) -> None:
        """Recent prints per market, and every wallet behind them.

        One fetch serves two purposes: the anonymised print tape the engine sees
        on :attr:`MarketFeatures.recent_trades`, and the identity-preserving
        observations the analytical layer ingests. Fetching twice for the same
        rows would double the request budget for no extra information.

        Best-effort per market: a failure costs that market's tape for a cycle
        and is never fatal.
        """
        observed: list[WalletTrade] = []

        async def one(market: MarketFeatures) -> None:
            rows = await self._trade_rows({"market": market.market_id,
                                           "limit": self._trade_page})
            if not rows:
                return
            market.recent_trades = [
                {
                    "tokenId": str(r.get("asset") or ""),
                    "side": str(r.get("side") or ""),
                    "price": _f(r.get("price")),
                    "size": _f(r.get("size")),
                    "ts": int(_f(r.get("timestamp"))),
                }
                for r in rows
            ][:25]
            observed.extend(_wallet_trade(r, source="market") for r in rows)

        await asyncio.gather(*(one(m) for m in features.values()),
                            return_exceptions=True)
        self._observed.extend(t for t in observed if t.wallet)

    async def _trade_rows(self, params: dict) -> list[dict]:
        try:
            resp = await self.http.get(
                f"{self.config.polymarket.data_api_host.rstrip('/')}/trades",
                params=params)
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            return []
        items = (payload if isinstance(payload, list)
                 else (payload.get("data") or []))
        return [r for r in items if isinstance(r, dict)]

    # -- broad ingestion (step 1) --------------------------------------------

    async def observe_trades(self) -> list[WalletTrade]:
        """Every wallet trade seen this cycle, from every source available.

        Two sweeps, because neither alone is the broad dataset the brief asks
        for. The per-market sweep is deep but blind to anything outside the
        tracked universe; the global sweep is shallow but sees the whole
        exchange, which is how a market nobody is tracking yet — and the wallets
        moving into it early — becomes visible at all.

        Both are already de-duplicated downstream by the store's natural key, so
        the overlap between them costs nothing but is what makes coverage of the
        busy markets complete.
        """
        if not self.config.intel.enabled:
            return []
        if self.config.intel.global_sweep:
            rows = await self._trade_rows({"limit": self._global_page,
                                           "takerOnly": "false"})
            self._observed.extend(
                t for t in (_wallet_trade(r, source="global") for r in rows)
                if t.wallet)
            # A market that only the global sweep has seen is a candidate for
            # the tracked universe: capital arriving somewhere we are not
            # watching is precisely what should pull it into view.
            if self.config.markets.follow_wallet_markets:
                for row in rows:
                    self._note_market(str(row.get("conditionId") or ""))

        drained, self._observed = self._observed, []
        # Bounded so a burst on the exchange cannot turn one cycle's ingestion
        # into an unbounded write. Newest first: if anything has to be dropped,
        # drop the stalest.
        limit = self.config.intel.max_trades_per_cycle
        if len(drained) > limit:
            drained.sort(key=lambda t: t.ts, reverse=True)
            self.log.warning("Trade ingestion truncated",
                             observed=len(drained), kept=limit)
            drained = drained[:limit]
        return drained

    async def settlements(self, market_ids: list[str]
                          ) -> dict[str, tuple[str, float]]:
        """How these markets settled: ``token_id -> (market_id, price)``.

        Batched through the same Gamma lookup the universe uses, so learning
        the fate of sixty markets is a handful of requests rather than sixty.

        Only *decisive* settlements are returned. A market whose outcome prices
        are still mid-range has closed without resolving, and recording a
        half-priced outcome as its settled value would score every wallet that
        traded it against a number the market never paid.
        """
        if not market_ids:
            return {}
        records = await self._fetch_by_ids([m for m in market_ids if m])
        out: dict[str, tuple[str, float]] = {}
        for market_id, record in records.items():
            resolved = (bool(record.get("resolved"))
                        or record.get("umaResolutionStatus") == "resolved"
                        or bool(record.get("closed")))
            if not resolved:
                continue
            tokens = [str(t) for t in _as_list(record.get("clobTokenIds"))]
            prices = [_f(p) for p in _as_list(record.get("outcomePrices"))]
            if len(tokens) != len(prices) or not tokens:
                continue
            # Decisive means one outcome took essentially everything.
            if not any(p >= 0.99 for p in prices):
                continue
            for token_id, price in zip(tokens, prices):
                out[token_id] = (market_id, 1.0 if price >= 0.99 else 0.0)
        return out

    async def refresh_prices(self, tokens: Iterable[str]) -> None:
        """Force a REST top-up for specific tokens (used before sizing an order)."""
        wanted = [str(t) for t in tokens if t]
        if not wanted:
            return
        await asyncio.gather(
            *(self.prices.price_now(t, Side.BUY) for t in wanted),
            return_exceptions=True,
        )

    # -- my account ----------------------------------------------------------

    @property
    def wallet_address(self) -> str:
        if self.client is not None:
            return self.client.funder_address or self.client.address
        return (self.config.polymarket.funder_address or "").strip()

    async def account(self) -> AccountState:
        """Free balance + position value from the exchange (live mode only)."""
        state = AccountState(address=self.wallet_address)
        if self.client is None:
            return state
        try:
            state.balance = await self.client.get_usdc_balance()
        except Exception as exc:
            self.log.warning("Balance read failed", error=repr(exc))
        positions = await self.exchange_positions()
        state.position_value = sum(p.market_value for p in positions)
        state.unrealized_pnl = sum(p.unrealized_pnl for p in positions)
        state.updated_ts = time.time()
        return state

    async def exchange_positions(self) -> list[PositionView]:
        """What the exchange says I hold, marked against the live book."""
        wallet = self.wallet_address
        if not wallet:
            return []
        try:
            raw = await self.data.get_positions(wallet)
        except Exception as exc:
            self.log.warning("Position read failed", error=repr(exc))
            return []
        out: list[PositionView] = []
        for token_id, row in raw.items():
            mark, _ = self.prices.mark(token_id, _f(row.get("curPrice")))
            avg = _f(row.get("avgPrice"))
            info = self.gamma.cached(str(row.get("marketId") or ""))
            out.append(PositionView(
                token_id=token_id,
                market_id=str(row.get("marketId") or ""),
                outcome=str(row.get("outcome") or ""),
                question=str(row.get("question") or ""),
                size=_f(row.get("size")),
                avg_price=avg,
                cur_price=mark or avg,
                end_ts=info.end_ts if info else None,
                source="exchange",
            ))
        return out

    async def open_orders(self) -> list[dict]:
        if self.client is None:
            return []
        try:
            return await self.client.open_orders()
        except Exception as exc:
            self.log.warning("Open-order read failed", error=repr(exc))
            return []

    # -- target wallets (signal, not command) --------------------------------

    async def wallet_signals(self) -> list[WalletSignal]:
        """New entries/exits observed on the tracked wallets since last poll."""
        if not self._wallet_feeds:
            return []

        async def one(key: str) -> list[WalletSignal]:
            label, weight = self._wallet_meta[key]
            try:
                trades = await self._wallet_feeds[key].poll_new_trades(key)
            except Exception as exc:
                self.log.warning("Wallet activity poll failed", wallet=label,
                                 error=repr(exc))
                return []
            signals = []
            for t in trades:
                signals.append(WalletSignal(
                    wallet=key, label=label, weight=weight,
                    action="ENTRY" if t.side == Side.BUY else "EXIT",
                    token_id=t.token_id, market_id=t.market_id,
                    outcome=t.outcome, price=t.price, size=t.size,
                    usdc=t.usdc_size, timestamp=t.timestamp,
                ))
                self._note_market(t.market_id)
            return signals

        results = await asyncio.gather(*(one(k) for k in self._wallet_feeds),
                                       return_exceptions=True)
        out: list[WalletSignal] = []
        for result in results:
            if isinstance(result, list):
                out.extend(result)
        out.sort(key=lambda s: s.timestamp)
        for signal in out:
            self.log.event("wallet.signal", wallet=signal.label,
                           action=signal.action, outcome=signal.outcome,
                           price=signal.price, usdc=signal.usdc,
                           market=signal.market_id[:12])
        return out

    async def wallet_positions(self) -> list[WalletPosition]:
        """Current holdings of every tracked wallet."""
        if not self._wallet_feeds:
            return []

        async def one(key: str) -> list[WalletPosition]:
            label, weight = self._wallet_meta[key]
            try:
                raw = await self._wallet_feeds[key].get_positions(key)
            except Exception:
                return []
            out = []
            for token_id, row in raw.items():
                self._note_market(str(row.get("marketId") or ""))
                out.append(WalletPosition(
                    wallet=key, label=label, weight=weight, token_id=token_id,
                    market_id=str(row.get("marketId") or ""),
                    outcome=str(row.get("outcome") or ""),
                    size=_f(row.get("size")),
                    avg_price=_f(row.get("avgPrice")),
                    value=_f(row.get("size")) * _f(row.get("curPrice")),
                ))
            return out

        results = await asyncio.gather(*(one(k) for k in self._wallet_feeds),
                                       return_exceptions=True)
        out: list[WalletPosition] = []
        for result in results:
            if isinstance(result, list):
                out.extend(result)
        return out

    # -- diagnostics ---------------------------------------------------------

    def feed_health(self) -> dict:
        return {
            "prices": self.prices.health(),
            "markets": len(self._universe),
            "wallets": len(self._wallet_feeds),
            "universeAgeSeconds": (time.time() - self._records_ts
                                   if self._records_ts else None),
        }


def _wallet_trade(row: dict, source: str) -> WalletTrade:
    """Normalise one Data API trade row (step 2 of the pipeline).

    Every downstream feature reads this shape and never the raw row, so a
    field the API renames — or returns under a different key on a different
    endpoint — is handled here once instead of in each detector.

    USDC is computed rather than read: the endpoint publishes shares and a
    price per share, and a notional derived from those two always agrees with
    the price the wallet actually paid.
    """
    wallet = str(row.get("proxyWallet") or row.get("proxy_wallet")
                 or row.get("wallet") or row.get("maker") or "").lower()
    price = _f(row.get("price"))
    size = _f(row.get("size"))
    side = str(row.get("side") or "BUY").upper()
    return WalletTrade(
        wallet=wallet,
        ts=int(_f(row.get("timestamp"))),
        market_id=str(row.get("conditionId") or row.get("condition_id") or ""),
        token_id=str(row.get("asset") or row.get("tokenId") or ""),
        outcome=str(row.get("outcome") or ""),
        side="SELL" if side.startswith("S") else "BUY",
        price=price,
        size=size,
        usdc=price * size,
        question=str(row.get("title") or ""),
        tx=str(row.get("transactionHash") or ""),
        source=source,
    )


def _record_from_info(info: MarketInfo) -> dict:
    """Shape an upstream ``MarketInfo`` like a raw Gamma row.

    Used only on the single-lookup fallback path, so the rest of this adapter
    has one record shape to parse instead of two. ``MarketInfo`` carries no
    liquidity or volume, so a market that reaches us only this way will read as
    zero on both and be filtered out unless it was listed explicitly — which is
    the safe direction: it means the batch metadata query failed, and sizing
    against unknown liquidity is worse than skipping the market.
    """
    import json
    from datetime import datetime, timezone
    end_iso = ""
    if info.end_ts:
        end_iso = datetime.fromtimestamp(
            info.end_ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "conditionId": info.market_id,
        "question": info.question,
        "slug": info.slug,
        "closed": info.closed,
        "resolved": info.resolved,
        "end_date_iso": end_iso,
        "clobTokenIds": json.dumps(list(info.token_ids)),
        "outcomePrices": json.dumps(list(info.outcome_prices)),
    }


def _chunks(items: list, size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]
