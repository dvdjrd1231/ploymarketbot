"""
Application state container.

Owns the singletons (config, database, hub, logger, HTTP client) and brokers the
engine's lifecycle. Everything the API layer needs hangs off here, so routes stay
thin and there is exactly one place that knows how to start and stop trading.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from typing import Optional

import httpx

from .applog import AppLogger
from .db import Database
from .hub import Hub
from .models import MODE_FIXED_50
from .services.engine import CopyEngine
from .services.trading import PolymarketError, clob_available
from .settings_store import ConfigManager


class AppState:
    def __init__(self) -> None:
        self.config = ConfigManager()
        self.db = Database()
        self.hub = Hub()
        self.log = AppLogger(self.db, self.hub)
        self.http: Optional[httpx.AsyncClient] = None
        self.engine: Optional[CopyEngine] = None
        self._lock = asyncio.Lock()
        self.started_at = time.time()

    # -- lifecycle -----------------------------------------------------------

    async def startup(self) -> None:
        limits = httpx.Limits(max_connections=64, max_keepalive_connections=32,
                              keepalive_expiry=30.0)
        self.http = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            limits=limits,
            headers={"User-Agent": "PolymarketCopyBot-Web/1.0"},
            http2=False,
        )
        self.hub.publish("settings", self.config.public_state())
        self.hub.publish("state", self.status())
        ok, err = clob_available()
        if not ok:
            self.log.warning(
                f"Trading library not loaded ({err}). Paper trading still works; "
                "install requirements.txt to place real orders."
            )
        if not self.config.secure_storage:
            self.log.warning(
                "No secure credential store found on this system. Install the "
                "'keyring' package before saving a private key."
            )

    async def shutdown(self) -> None:
        await self.stop_engine()
        if self.http is not None:
            with contextlib.suppress(Exception):
                await self.http.aclose()
        with contextlib.suppress(Exception):
            self.db.close()

    # -- engine control ------------------------------------------------------

    async def start_engine(self) -> dict:
        async with self._lock:
            if self.engine is not None and self.engine.state in (
                    "running", "connecting"):
                return {"ok": False, "error": "The bot is already running."}

            settings = self.config.settings
            problems = settings.validate()
            if problems:
                return {"ok": False, "error": " ".join(problems),
                        "problems": problems}

            private_key = ""
            if not settings.paper_trading:
                private_key = self.config.get_private_key() or ""
                if not private_key:
                    return {
                        "ok": False,
                        "error": "No private key is stored. Add one in Settings "
                                 "first, or switch on Paper trading.",
                    }

            # Fresh paper-trading session: clear old simulated trades /
            # positions / P&L / history unless the user opted to restore.
            if settings.paper_trading and not settings.restore_previous_session:
                self.db.reset_session(clear_log=True)
                self.hub.reset_cache("positions", "history", "orders", "stats")
                self.hub.publish("positions", [])
                self.hub.publish("history", [])
                self.hub.publish("orders", [])
                self.hub.publish("logs:reset", True)
                self.log.info(
                    "New paper session — dashboard reset to a clean slate."
                )

            engine = CopyEngine(
                settings=settings, db=self.db, hub=self.hub, logger=self.log,
                http=self.http, private_key=private_key,
            )
            self.engine = engine
            try:
                await engine.start()
            except PolymarketError as exc:
                self.engine = None
                self.hub.publish("state", self.status())
                return {"ok": False, "error": str(exc)}
            except Exception as exc:
                self.engine = None
                self.hub.publish("state", self.status())
                return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            finally:
                # Never leave a plaintext key sitting in a local for longer than
                # the connect call needs it.
                private_key = ""

            return {"ok": True, "state": engine.status()}

    async def stop_engine(self) -> dict:
        async with self._lock:
            engine = self.engine
            if engine is None:
                return {"ok": True}
            await engine.stop()
            self.engine = None
            self.hub.publish("state", self.status())
            self.hub.publish("positions", engine._positions_payload())
            return {"ok": True}

    async def restart_engine(self) -> dict:
        await self.stop_engine()
        return await self.start_engine()

    # -- read models ---------------------------------------------------------

    def status(self) -> dict:
        if self.engine is not None:
            return self.engine.status()
        settings = self.config.settings
        return {
            "state": "idle",
            "detail": "Idle — press Start to begin copying.",
            "connected": False,
            "running": False,
            "mode": settings.trading_mode,
            "modeLabel": ("Fixed 50%"
                          if settings.trading_mode == MODE_FIXED_50
                          else "Proportional"),
            "paper": settings.paper_trading,
            "dryRun": settings.dry_run,
            "targetWallet": settings.target_wallet,
            "address": settings.my_wallet_address,
            "startedTs": 0,
            "uptime": 0,
        }

    def snapshot(self) -> dict:
        """Everything a freshly loaded page needs, in one payload."""
        engine = self.engine
        stats = engine._stats_payload() if engine else self._idle_stats()
        return {
            "state": self.status(),
            "settings": self.config.public_state(),
            "account": (engine._snapshot.to_dict() if engine
                        else {"address": "", "balance": 0.0,
                              "positionValue": 0.0, "buyingPower": 0.0,
                              "equity": 0.0, "connected": False,
                              "paper": self.config.settings.paper_trading,
                              "accountLabel": "", "updatedTs": time.time()}),
            "positions": (engine._positions_payload() if engine
                          else [t.to_dict() for t in self.db.get_open_trades()]),
            "history": [t.to_dict() for t in self.db.get_closed_trades(500)],
            "orders": [o.to_dict() for o in self.db.get_orders(300)],
            "stats": stats,
            "equity": engine.equity_payload() if engine else [],
            "logs": self.db.recent_logs(400),
            "feeds": (engine._feeds_payload() if engine else
                      {"prices": {"state": "idle"}, "user": {"state": "off"},
                       "books": []}),
            "serverTime": time.time(),
        }

    def _idle_stats(self) -> dict:
        from .services.engine import _day_start_ts
        stats = self.db.daily_stats(_day_start_ts())
        open_trades = self.db.get_open_trades()
        cost = sum(t.my_usdc for t in open_trades)
        stats.update({
            "openPositions": len(open_trades),
            "unrealizedPnl": 0.0,
            "openCost": cost,
            "openValue": cost,
            "openReturnPct": 0.0,
            "portfolioPnl": stats["realizedPnlToday"],
            "targetAccountValue": None,
        })
        return stats
