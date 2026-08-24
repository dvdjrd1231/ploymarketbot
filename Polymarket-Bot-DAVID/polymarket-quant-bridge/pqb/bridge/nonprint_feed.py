"""
The non-print engine, fed from Polymarket — the operator's core architecture.

His prop-firm bridge ran every tick through ``DualNonPrintEngine`` and merged
its structural snapshot into each row BEFORE the LEAN analysis saw it. That
pre-analysis structural layer is, in his words, the core foundation of the
bridge system — and this module restores it for Polymarket. The engine itself
is imported from qc_lean_bridge and not modified; everything here is the
formatting he asked for: shaping this exchange's book stream and trade tape
into the tick contract the engine already expects.

The tick contract (read from the engine's own source): ``ts, price, bid, ask,
bid_size, ask_size``. Two Polymarket-specific facts shape the adapter:

* **Every tick's ``price`` is recorded as a printed level, unconditionally.**
  So a quote-only update must carry the *last trade price* — never zero, and
  never a fabricated print. And idle cycles are not fed at all: re-sending an
  unchanged quote would falsely refresh the print window on a stale level,
  which corrupts exactly the void structure the engine exists to find.
* **Ticks are only fed once a token has traded.** Until the first print there
  is no honest ``price`` to attach, and the engine's own ``quote <= 0`` guard
  covers the rest.

Polymarket markets tick far slower than futures, so the engine's time constants
are exposed through the research config (longer print lookback, longer void
age) rather than hard-coded at futures scale. Tick size comes from each
market's real metadata.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class _Tick:
    """Exactly the fields the engine's ``on_tick`` reads."""

    ts: float
    price: float
    bid: float
    ask: float
    bid_size: float
    ask_size: float


class NonPrintFeed:
    """Per-token DualNonPrintEngine instances behind one feed interface.

    Degrades to inert when the Quant Bridge checkout is missing: ``available``
    is False, every call is a no-op, and the bot runs exactly as before — the
    structural columns simply do not exist, which discovery handles naturally.
    """

    def __init__(self, print_lookback_s: float = 900.0,
                 void_max_age_s: float = 3600.0, log: Any = None):
        self.print_lookback_s = float(print_lookback_s)
        self.void_max_age_s = float(void_max_age_s)
        self.log = log
        self._engines: dict[str, Any] = {}
        self._last_trade: dict[str, tuple[float, float]] = {}   # token -> (ts, price)
        self._last_quote: dict[str, tuple[float, float]] = {}   # token -> (bid, ask)
        self._factory = self._load_factory()

    def _load_factory(self):
        try:
            from ..quant import ensure_on_path
            ensure_on_path()
            from core.nonprint_engine import (DualNonPrintEngine,
                                              NonPrintConfig)
        except Exception as exc:                          # noqa: BLE001
            if self.log is not None:
                self.log.warning(
                    "Non-print engine unavailable; structural columns are "
                    "disabled until qc_lean_bridge is reachable.",
                    error=repr(exc))
            return None

        def make(tick_size: float):
            return DualNonPrintEngine(NonPrintConfig(
                tick_size=max(1e-4, float(tick_size)),
                print_lookback_s=self.print_lookback_s,
                void_max_age_s=self.void_max_age_s))

        return make

    @property
    def available(self) -> bool:
        return self._factory is not None

    def _engine_for(self, token_id: str, tick_size: float):
        engine = self._engines.get(token_id)
        if engine is None:
            engine = self._factory(tick_size)
            self._engines[token_id] = engine
        return engine

    # -- feeding -------------------------------------------------------------

    def feed_trade(self, token_id: str, ts: float, price: float,
                   bid: float, ask: float, bid_size: float, ask_size: float,
                   tick_size: float) -> None:
        """One real print, with the quotes in force around it."""
        if not self.available or price <= 0:
            return
        last = self._last_trade.get(token_id)
        if last is not None and ts <= last[0] and price == last[1]:
            return                        # already fed (tape overlap)
        self._last_trade[token_id] = (float(ts), float(price))
        self._engine_for(token_id, tick_size).on_tick(_Tick(
            ts=float(ts), price=float(price), bid=float(bid or 0.0),
            ask=float(ask or 0.0), bid_size=float(bid_size or 0.0),
            ask_size=float(ask_size or 0.0)))

    def feed_quote(self, token_id: str, ts: float, bid: float, ask: float,
                   bid_size: float, ask_size: float, tick_size: float) -> None:
        """A book update between prints — fed only when the quote moved.

        Carries the last trade price (the engine records ``price`` as printed
        on every tick, so it must be a level that genuinely printed). Before
        the first trade there is nothing honest to send, so nothing is sent.
        """
        if not self.available:
            return
        last_trade = self._last_trade.get(token_id)
        if last_trade is None:
            return
        pair = (float(bid or 0.0), float(ask or 0.0))
        if self._last_quote.get(token_id) == pair:
            return                        # idle: do not refresh stale prints
        self._last_quote[token_id] = pair
        self._engine_for(token_id, tick_size).on_tick(_Tick(
            ts=float(ts), price=last_trade[1], bid=pair[0], ask=pair[1],
            bid_size=float(bid_size or 0.0), ask_size=float(ask_size or 0.0)))

    # -- output --------------------------------------------------------------

    def snapshot(self, token_id: str, ts: float) -> dict[str, float]:
        """The structural columns for this token, ``np_``-prefixed.

        Empty until the token has been fed — a row without the columns is a
        row the engine had nothing to say about, and discovery treats missing
        columns exactly as it should: as absence, not as zero structure.
        """
        engine = self._engines.get(token_id)
        if engine is None:
            return {}
        try:
            raw = engine.snapshot(float(ts))
        except Exception:                                 # noqa: BLE001
            return {}
        return {f"np_{key}": float(value) for key, value in raw.items()
                if isinstance(value, (int, float))}

    def prune(self, now: float, idle_seconds: float = 7_200.0) -> int:
        """Drop engines for tokens that stopped trading hours ago.

        The universe rotates markets continuously; over a long run the
        per-token engines otherwise accumulate forever — the slow-leak shape
        of "it stopped responding after some hours".
        """
        stale = [token for token, (ts, _price) in self._last_trade.items()
                 if now - ts > idle_seconds]
        for token in stale:
            self._engines.pop(token, None)
            self._last_trade.pop(token, None)
            self._last_quote.pop(token, None)
        return len(stale)

    def stats(self) -> dict:
        return {"tokens": len(self._engines),
                "available": self.available}
