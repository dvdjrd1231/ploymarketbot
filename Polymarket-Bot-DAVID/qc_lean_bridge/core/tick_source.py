"""Module 2 (input half) - IBKR Time & Sales acquisition.

Three interchangeable sources behind ONE async interface, so every downstream
engine is identical whether the ticks come from the wire, from disk, or from the
simulator:

    IBKRTickSource       ib_insync -> live or historical Time & Sales
    CsvTickSource        replay a previously captured T&S file (offline research)
    SyntheticTickSource  generated microstructure (no IBKR / no data needed)

    async for tick in source.stream():
        ...

`ib_insync` is an OPTIONAL import. The platform must remain fully runnable (and
buildable into the exe) on a machine with no IBKR install, so we degrade to the
CSV/synthetic sources instead of failing at import time.

IBKR notes that shape this code:
  * reqHistoricalTicks returns at most 1000 ticks per call, so history is paged
    backwards/forwards by timestamp until the window is covered.
  * TRADES ticks carry price/size; the bid/ask in force is taken from the
    parallel BID_ASK tick stream and merged onto each trade by timestamp, which
    is what the Non-Print engines need (a trade plus the quote it crossed).
  * Live: reqTickByTickData(..., 'AllLast') + ('BidAsk') gives the same pair.
"""
from __future__ import annotations

import asyncio
import csv
import math
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator

from .events import Tick
from .logging_setup import get_logger

log = get_logger("ticks")


def ib_insync_available() -> bool:
    try:
        import ib_insync  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def tws_reachable(host: str, port: int, timeout: float = 0.6) -> bool:
    """Is TWS / IB Gateway actually listening?

    ib_insync being installed does NOT mean IBKR is running. Probing the socket
    before we commit to the IBKR source is what lets `source: auto` degrade to
    replay/synthetic instead of dying on a ConnectionRefusedError mid-run.
    """
    import socket
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class TickSource(ABC):
    name = "base"

    @abstractmethod
    def stream(self) -> AsyncIterator[Tick]:
        """Yield Ticks in ascending timestamp order."""
        raise NotImplementedError

    async def close(self) -> None:
        return None


# --------------------------------------------------------------------- IBKR
class IBKRTickSource(TickSource):
    """Historical and live Time & Sales via ib_insync.

    cfg keys (ibkr section):
        host, port, client_id, symbol, sec_type, exchange, currency,
        expiry (futures), what_to_show, use_rth, lookback_days, live
    """

    name = "ibkr"

    def __init__(self, cfg: dict, max_ticks: int = 0):
        self.cfg = cfg
        self.max_ticks = int(max_ticks or 0)
        self.ib = None
        self._quote = (0.0, 0, 0.0, 0)   # bid, bid_size, ask, ask_size

    # -- connection -------------------------------------------------------
    async def connect(self):
        from ib_insync import IB  # imported lazily: optional dependency

        self.ib = IB()
        host = self.cfg.get("host", "127.0.0.1")
        port = int(self.cfg.get("port", 7497))
        cid = int(self.cfg.get("client_id", 17))
        log.info("connecting to IBKR", extra={"host": host, "port": port, "client_id": cid})
        try:
            await self.ib.connectAsync(host, port, clientId=cid, timeout=15)
        except (OSError, asyncio.TimeoutError) as exc:
            raise RuntimeError(
                f"Cannot reach IBKR at {host}:{port} ({exc}). Start TWS or IB "
                f"Gateway and enable 'ActiveX and Socket Clients' in "
                f"API > Settings, or set ingest.source to 'csv'/'synthetic' to "
                f"research without a live IBKR connection."
            ) from exc
        log.info("IBKR connected", extra={"server_version": self.ib.client.serverVersion()})
        return self.ib

    def _contract(self):
        from ib_insync import Future, Stock, Contract

        c = self.cfg
        sec = str(c.get("sec_type", "FUT")).upper()
        symbol = c.get("symbol", "MES")
        exchange = c.get("exchange", "CME")
        currency = c.get("currency", "USD")
        if sec == "FUT":
            expiry = str(c.get("expiry", "") or "")
            contract = Future(symbol=symbol, exchange=exchange, currency=currency,
                              lastTradeDateOrContractMonth=expiry)
        elif sec == "STK":
            contract = Stock(symbol, exchange if exchange != "CME" else "SMART", currency)
        else:
            contract = Contract(secType=sec, symbol=symbol, exchange=exchange,
                                currency=currency)
        return contract

    async def _qualified(self):
        contract = self._contract()
        details = await self.ib.reqContractDetailsAsync(contract)
        if not details:
            raise RuntimeError(
                f"IBKR could not qualify contract {contract}. Check symbol/expiry/exchange.")
        # Front month = nearest expiry when no explicit expiry was configured.
        chosen = sorted(details, key=lambda d: d.contract.lastTradeDateOrContractMonth or "")[0]
        log.info("contract qualified", extra={
            "symbol": chosen.contract.localSymbol,
            "expiry": chosen.contract.lastTradeDateOrContractMonth})
        return chosen.contract

    # -- streaming --------------------------------------------------------
    async def stream(self) -> AsyncIterator[Tick]:
        if self.ib is None:
            await self.connect()
        contract = await self._qualified()

        if bool(self.cfg.get("live", False)):
            async for t in self._stream_live(contract):
                yield t
        else:
            async for t in self._stream_history(contract):
                yield t

    async def _stream_history(self, contract) -> AsyncIterator[Tick]:
        """Page reqHistoricalTicks forward until the window is covered.

        IBKR returns <=1000 ticks per request, so we walk `start` forward using
        the last timestamp we received. Quotes (BID_ASK) are pulled for the same
        window and merged onto trades by nearest-preceding timestamp.
        """
        days = int(self.cfg.get("lookback_days", 1))
        use_rth = bool(self.cfg.get("use_rth", True))
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)
        emitted = 0
        cursor = start

        quotes = await self._fetch_quotes(contract, start, end, use_rth)
        qi = 0

        while cursor < end:
            batch = await self.ib.reqHistoricalTicksAsync(
                contract, cursor, None, 1000, "TRADES", use_rth, False)
            if not batch:
                break
            for ht in batch:
                ts = ht.time.timestamp()
                # advance the quote cursor to the last quote at//before this trade
                while qi + 1 < len(quotes) and quotes[qi + 1][0] <= ts:
                    qi += 1
                bid, bsz, ask, asz = (quotes[qi][1:] if quotes else (0.0, 0, 0.0, 0))
                yield Tick(ts=ts, price=float(ht.price), size=int(ht.size),
                           bid=bid, bid_size=bsz, ask=ask, ask_size=asz)
                emitted += 1
                if self.max_ticks and emitted >= self.max_ticks:
                    log.info("history complete (max_ticks)", extra={"ticks": emitted})
                    return
            last_ts = batch[-1].time
            if last_ts <= cursor:          # no forward progress -> done
                break
            cursor = last_ts + timedelta(milliseconds=1)
            await asyncio.sleep(0)          # let the event loop breathe
        log.info("history complete", extra={"ticks": emitted})

    async def _fetch_quotes(self, contract, start, end, use_rth) -> list[tuple]:
        """BID_ASK ticks for the window, as a time-sorted list."""
        out: list[tuple] = []
        cursor = start
        while cursor < end:
            batch = await self.ib.reqHistoricalTicksAsync(
                contract, cursor, None, 1000, "BID_ASK", use_rth, False)
            if not batch:
                break
            for q in batch:
                out.append((q.time.timestamp(), float(q.priceBid), int(q.sizeBid),
                            float(q.priceAsk), int(q.sizeAsk)))
            last_ts = batch[-1].time
            if last_ts <= cursor:
                break
            cursor = last_ts + timedelta(milliseconds=1)
            if len(out) > 2_000_000:        # hard RAM guard
                break
            await asyncio.sleep(0)
        out.sort(key=lambda r: r[0])
        log.info("quotes fetched", extra={"quotes": len(out)})
        return out

    async def _stream_live(self, contract) -> AsyncIterator[Tick]:
        """Live tick-by-tick: AllLast (trades) + BidAsk (quotes)."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=10_000)

        trades = self.ib.reqTickByTickData(contract, "AllLast")
        quotes = self.ib.reqTickByTickData(contract, "BidAsk")

        def on_quote(tickers):
            for tk in tickers if isinstance(tickers, list) else [tickers]:
                for q in getattr(tk, "tickByTicks", []) or []:
                    if hasattr(q, "bidPrice"):
                        self._quote = (float(q.bidPrice), int(q.bidSize),
                                       float(q.askPrice), int(q.askSize))

        def on_trade(tickers):
            for tk in tickers if isinstance(tickers, list) else [tickers]:
                for tr in getattr(tk, "tickByTicks", []) or []:
                    if not hasattr(tr, "price"):
                        continue
                    b, bs, a, as_ = self._quote
                    tick = Tick(ts=tr.time.timestamp(), price=float(tr.price),
                                size=int(tr.size), bid=b, bid_size=bs,
                                ask=a, ask_size=as_)
                    try:
                        queue.put_nowait(tick)
                    except asyncio.QueueFull:
                        pass    # live feed outruns research: drop, never stall IB

        quotes.updateEvent += on_quote
        trades.updateEvent += on_trade
        log.info("live tick stream started", extra={"symbol": contract.localSymbol})

        emitted = 0
        try:
            while True:
                tick = await queue.get()
                yield tick
                emitted += 1
                if self.max_ticks and emitted >= self.max_ticks:
                    return
        finally:
            trades.updateEvent -= on_trade
            quotes.updateEvent -= on_quote
            self.ib.cancelTickByTickData(contract, "AllLast")
            self.ib.cancelTickByTickData(contract, "BidAsk")

    async def close(self) -> None:
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()
            log.info("IBKR disconnected")


# ---------------------------------------------------------------------- CSV
class CsvTickSource(TickSource):
    """Replay a captured Time & Sales CSV.

    Accepts the file this platform writes (see EventStore.export_ticks) and the
    common IBKR/TWS export shapes; column names are matched case-insensitively
    with a few aliases so a hand-exported file usually just works.
    """

    name = "csv"
    ALIASES = {
        "ts": ("ts", "timestamp", "time", "datetime", "date_time"),
        "price": ("price", "last", "last_price", "trade_price", "close"),
        "size": ("size", "last_size", "trade_size", "volume", "qty"),
        "bid": ("bid", "bid_price", "bidprice"),
        "bid_size": ("bid_size", "bidsize", "bid_qty"),
        "ask": ("ask", "ask_price", "askprice"),
        "ask_size": ("ask_size", "asksize", "ask_qty"),
    }

    def __init__(self, path: str | Path, max_ticks: int = 0):
        self.path = Path(path)
        self.max_ticks = int(max_ticks or 0)

    @staticmethod
    def _to_epoch(v: str) -> float:
        try:
            return float(v)                      # already epoch seconds
        except ValueError:
            pass
        s = v.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(s).timestamp()
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                        "%Y%m%d %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
                try:
                    return datetime.strptime(s, fmt).timestamp()
                except ValueError:
                    continue
        raise ValueError(f"unparseable timestamp: {v!r}")

    def _resolve(self, header: list[str]) -> dict[str, str]:
        lower = {h.lower().strip(): h for h in header}
        out: dict[str, str] = {}
        for field_name, names in self.ALIASES.items():
            for n in names:
                if n in lower:
                    out[field_name] = lower[n]
                    break
        missing = {"ts", "price"} - set(out)
        if missing:
            raise ValueError(
                f"{self.path.name}: missing required column(s) {sorted(missing)}; "
                f"found {header}")
        return out

    async def stream(self) -> AsyncIterator[Tick]:
        if not self.path.exists():
            raise FileNotFoundError(f"Tick CSV not found: {self.path}")
        emitted = 0
        with open(self.path, "r", newline="", encoding="utf-8") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if header is None:
                return
            cols = self._resolve(header)
            idx = {k: header.index(v) for k, v in cols.items()}

            def num(row, key, cast, default=0):
                i = idx.get(key)
                if i is None or i >= len(row) or row[i] == "":
                    return default
                try:
                    return cast(float(row[i]))
                except ValueError:
                    return default

            for row in reader:
                if not row:
                    continue
                try:
                    ts = self._to_epoch(row[idx["ts"]])
                except ValueError:
                    continue
                yield Tick(
                    ts=ts,
                    price=num(row, "price", float),
                    size=num(row, "size", int),
                    bid=num(row, "bid", float),
                    bid_size=num(row, "bid_size", int),
                    ask=num(row, "ask", float),
                    ask_size=num(row, "ask_size", int),
                )
                emitted += 1
                if self.max_ticks and emitted >= self.max_ticks:
                    break
                if emitted % 20_000 == 0:
                    await asyncio.sleep(0)      # keep the loop responsive
        log.info("csv replay complete", extra={"ticks": emitted, "file": self.path.name})


# ---------------------------------------------------------------- synthetic
class SyntheticTickSource(TickSource):
    """Generated Time & Sales with realistic microstructure.

    Deliberately produces the phenomena the Non-Print engines exist to detect:
    quotes that jump several ticks with no trade at the intervening levels
    (non-prints / liquidity voids), bursts, and quiet compression phases. Used
    for the self-test, the demo dataset, and development without market data.
    """

    name = "synthetic"

    def __init__(self, n_ticks: int = 200_000, tick_size: float = 0.25,
                 start_price: float = 5000.0, seed: int = 42,
                 start_ts: float | None = None):
        self.n = int(n_ticks)
        self.tick_size = float(tick_size)
        self.start_price = float(start_price)
        self.seed = int(seed)
        self.start_ts = start_ts

    async def stream(self) -> AsyncIterator[Tick]:
        import numpy as np

        rng = np.random.RandomState(self.seed)
        ts = self.start_ts
        if ts is None:
            ts = datetime(2026, 1, 5, 14, 30, 0, tzinfo=timezone.utc).timestamp()
        tick = self.tick_size
        price = self.start_price
        regime = 1
        vol = 1.0

        for i in range(self.n):
            if i % 5000 == 0:
                regime = int(rng.choice([-1, 1]))
            if i % 1200 == 0:
                vol = float(rng.uniform(0.15, 0.8))    # compression / expansion

            # Real index-future ticks move 0 or 1 tick the overwhelming majority
            # of the time. Keeping the base step sub-tick is what makes a
            # multi-tick jump an EVENT rather than the norm - otherwise every
            # tick is a "non-print" and the signal means nothing.
            drift = 0.012 * regime * tick
            step = rng.normal(drift, vol * tick)
            # Liquidity vacuum: the book occasionally jumps several ticks with
            # nothing trading in between - the non-print signature we detect.
            if rng.random() < 0.006:
                step += float(rng.choice([-1, 1])) * tick * rng.randint(2, 7)
            price = max(tick, price + step)
            price = round(price / tick) * tick

            spread = tick * (1 if rng.random() < 0.85 else rng.randint(2, 5))
            bid = round((price - spread / 2) / tick) * tick
            ask = round(bid + spread, 10)
            aggressive_buy = rng.random() < 0.5
            trade_price = ask if aggressive_buy else bid

            ts += float(max(0.0005, rng.exponential(0.05)))   # irregular arrivals
            yield Tick(
                ts=ts,
                price=round(trade_price, 10),
                size=int(rng.randint(1, 20)),
                bid=round(bid, 10), bid_size=int(rng.randint(1, 300)),
                ask=round(ask, 10), ask_size=int(rng.randint(1, 300)),
            )
            if i % 20_000 == 0:
                await asyncio.sleep(0)


# ------------------------------------------------------------------ factory
def build_source(cfg, max_ticks: int = 0) -> TickSource:
    """Pick the tick source from config, degrading gracefully.

    ingest.source: "ibkr" | "csv" | "synthetic" | "auto"
    "auto" = IBKR if ib_insync is installed, else the captured CSV if present,
    else synthetic. This is what lets the app run end-to-end on a fresh machine.
    """
    section = cfg.section("ingest") if hasattr(cfg, "section") else dict(cfg)
    kind = str(section.get("source", "auto")).lower()
    csv_path = section.get("csv_path", "") or ""
    if csv_path and not Path(csv_path).is_absolute():
        csv_path = str(cfg.resolve_path("ingest.csv_path"))

    ibkr_cfg = dict(section.get("ibkr", {}) or {})
    host = str(ibkr_cfg.get("host", "127.0.0.1"))
    port = int(ibkr_cfg.get("port", 7497))

    if kind == "auto":
        # Installed != running. Probe the socket, then fall back cleanly.
        if (ibkr_cfg.get("enabled", True) and ib_insync_available()
                and tws_reachable(host, port)):
            kind = "ibkr"
        elif csv_path and Path(csv_path).exists():
            kind = "csv"
            log.info("IBKR not reachable - replaying captured ticks",
                     extra={"host": host, "port": port, "csv": csv_path})
        else:
            kind = "synthetic"
            log.info("IBKR not reachable and no tick CSV - using synthetic ticks",
                     extra={"host": host, "port": port})
        log.info("tick source auto-selected", extra={"source": kind})

    if kind == "ibkr":
        if not ib_insync_available():
            raise RuntimeError(
                "ingest.source is 'ibkr' but ib_insync is not installed. "
                "Run: pip install ib_insync   (or set ingest.source: auto)")
        ibkr_cfg.setdefault("symbol", cfg.get("instrument.symbol", "MES"))
        return IBKRTickSource(ibkr_cfg, max_ticks=max_ticks)

    if kind == "csv":
        return CsvTickSource(csv_path, max_ticks=max_ticks)

    return SyntheticTickSource(
        n_ticks=int(max_ticks or section.get("synthetic_ticks", 200_000)),
        tick_size=float(cfg.get("instrument.tick_size", 0.25)),
        seed=int(section.get("seed", 42)),
    )
