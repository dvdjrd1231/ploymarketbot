"""
HTTP + WebSocket API.

Two rules govern this layer:

  * **Secrets are write-only.** There is no route that returns the private key.
    ``POST /api/credentials`` accepts one and hands it straight to the OS
    credential store; every read path reports only ``hasPrivateKey: true``.
  * **Reads are free.** The dashboard never polls — it opens one WebSocket and
    receives pushes. The REST routes exist for actions (start/stop/save/close)
    and for the initial snapshot, not for a refresh loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .services.trading import (
    PolymarketError, autodetect_account, clob_available, derive_signer_address,
)
from .state import AppState

router = APIRouter()


def _state(request: Request) -> AppState:
    return request.app.state.app


# --- request bodies ---------------------------------------------------------

class SettingsPatch(BaseModel):
    """Partial settings update. Every field is optional."""

    model_config = {"extra": "ignore"}

    target_wallet: Optional[str] = None
    trading_mode: Optional[str] = None
    signature_type: Optional[int] = None
    funder_address: Optional[str] = None
    poll_interval_seconds: Optional[int] = Field(None, ge=5, le=3600)
    min_order_usdc: Optional[float] = Field(None, gt=0)
    max_copy_fraction_of_target: Optional[float] = Field(None, gt=0, le=1)
    dry_run: Optional[bool] = None
    paper_trading: Optional[bool] = None
    paper_starting_balance: Optional[float] = Field(None, gt=0)
    restore_previous_session: Optional[bool] = None
    max_expiry_days: Optional[int] = Field(None, ge=0, le=3650)
    copy_exits: Optional[bool] = None
    min_seconds_to_expiry: Optional[int] = Field(None, ge=0)
    push_interval_ms: Optional[int] = Field(None, ge=100, le=5000)
    balance_refresh_seconds: Optional[int] = Field(None, ge=2, le=300)
    market_refresh_seconds: Optional[int] = Field(None, ge=5, le=600)
    reconcile_seconds: Optional[int] = Field(None, ge=10, le=600)
    use_price_stream: Optional[bool] = None
    rest_price_fallback_seconds: Optional[int] = Field(None, ge=1, le=60)


class CredentialsBody(BaseModel):
    private_key: str = Field(min_length=1)
    funder_hint: str = ""


class ConnectBody(BaseModel):
    private_key: str = ""
    funder_hint: str = ""


class ClosePositionBody(BaseModel):
    id: int


class CancelOrderBody(BaseModel):
    order_id: str


# --- snapshot / status ------------------------------------------------------

@router.get("/snapshot")
async def snapshot(request: Request) -> dict:
    return _state(request).snapshot()


@router.get("/status")
async def status(request: Request) -> dict:
    app = _state(request)
    ok, err = clob_available()
    return {
        **app.status(),
        "clobAvailable": ok,
        "clobError": err,
        "clients": app.hub.client_count,
        "secureStorage": app.config.secure_storage,
        "storageBackend": app.config.storage_backend,
        "hasPrivateKey": app.config.has_private_key(),
    }


# --- settings ---------------------------------------------------------------

@router.get("/settings")
async def get_settings(request: Request) -> dict:
    return _state(request).config.public_state()


@router.put("/settings")
async def put_settings(body: SettingsPatch, request: Request) -> dict:
    app = _state(request)
    patch: dict[str, Any] = body.model_dump(exclude_none=True)

    if "target_wallet" in patch:
        wallet = patch["target_wallet"].strip()
        if wallet and (not wallet.startswith("0x") or len(wallet) != 42):
            raise HTTPException(
                422, "Target wallet must be a 0x… address of 42 characters.")
        patch["target_wallet"] = wallet
    if "funder_address" in patch:
        patch["funder_address"] = patch["funder_address"].strip()

    settings = app.config.update(patch)
    public = app.config.public_state()
    app.hub.publish("settings", public)
    app.hub.publish("state", app.status())

    # Live-tunable knobs the running engine should adopt without a restart.
    engine = app.engine
    restart_required = False
    if engine is not None and engine.state == "running":
        engine.settings = settings
        structural = {"paper_trading", "target_wallet", "signature_type",
                      "funder_address", "use_price_stream"}
        restart_required = bool(structural & patch.keys())

    return {"ok": True, "settings": public,
            "restartRequired": restart_required,
            "problems": settings.validate()}


# --- credentials (write-only) -----------------------------------------------

@router.get("/credentials")
async def credentials_status(request: Request) -> dict:
    app = _state(request)
    return {
        "hasPrivateKey": app.config.has_private_key(),
        "secureStorage": app.config.secure_storage,
        "storageBackend": app.config.storage_backend,
    }


@router.post("/credentials")
async def set_credentials(body: CredentialsBody, request: Request) -> dict:
    """Store a private key. It is validated, saved, and never echoed back."""
    app = _state(request)
    try:
        address = derive_signer_address(body.private_key)
    except PolymarketError as exc:
        raise HTTPException(422, str(exc)) from exc

    if not app.config.secure_storage:
        raise HTTPException(
            503,
            "No secure credential store is available. Install the 'keyring' "
            "package (pip install keyring) and restart before saving a key.",
        )
    try:
        app.config.set_private_key(body.private_key)
    except Exception as exc:
        raise HTTPException(500, f"Could not store the key: {exc}") from exc

    patch: dict[str, Any] = {"my_wallet_address": address}
    if body.funder_hint.strip():
        patch["funder_address"] = body.funder_hint.strip()
    app.config.update(patch)
    app.log.success(
        f"Wallet key stored securely ({app.config.storage_backend}) for "
        f"{address}."
    )
    app.hub.publish("settings", app.config.public_state())
    return {"ok": True, "address": address,
            "storageBackend": app.config.storage_backend}


@router.delete("/credentials")
async def clear_credentials(request: Request) -> dict:
    app = _state(request)
    app.config.clear_private_key()
    app.log.warning("Stored wallet key removed from this machine.")
    app.hub.publish("settings", app.config.public_state())
    return {"ok": True}


@router.post("/connect")
async def connect(body: ConnectBody, request: Request) -> dict:
    """Paste-key-and-go: connect once, auto-detect the account, show balance.

    Removes the need to understand "signature type" or hunt down a funder
    address — we probe the possible configurations and keep whichever one
    actually holds the USDC.
    """
    app = _state(request)
    key = body.private_key.strip() or (app.config.get_private_key() or "")
    if not key:
        raise HTTPException(422, "No private key provided or stored.")

    settings = app.config.settings
    hint = body.funder_hint.strip() or settings.funder_address
    try:
        client, detected = await autodetect_account(
            settings.clob_host, settings.chain_id, key, hint
        )
    except PolymarketError as exc:
        app.log.error(f"Could not connect: {exc}")
        raise HTTPException(400, str(exc)) from exc
    finally:
        key = ""

    if body.private_key.strip():
        if not app.config.secure_storage:
            raise HTTPException(
                503, "No secure credential store available to save this key.")
        app.config.set_private_key(body.private_key)

    app.config.update({
        "signature_type": detected.signature_type,
        "funder_address": detected.funder_address,
        "my_wallet_address": detected.address,
    })
    app.hub.publish("settings", app.config.public_state())
    app.log.success(
        f"Connected as {detected.address} · {detected.account_label} · "
        f"balance ${detected.balance:,.2f}"
    )
    if detected.balance <= 0:
        app.log.warning(
            "Connected, but no USDC balance was found for this wallet. If your "
            "funds are in a Polymarket email/browser wallet, paste your "
            "Polymarket address in the funder field and connect again."
        )
    with contextlib.suppress(Exception):
        await client.check_connection()
    return {"ok": True, "account": detected.to_dict()}


# --- engine control ---------------------------------------------------------

@router.post("/engine/start")
async def start_engine(request: Request) -> dict:
    result = await _state(request).start_engine()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Could not start."))
    return result


@router.post("/engine/stop")
async def stop_engine(request: Request) -> dict:
    return await _state(request).stop_engine()


@router.post("/engine/restart")
async def restart_engine(request: Request) -> dict:
    result = await _state(request).restart_engine()
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Could not restart."))
    return result


# --- trading actions --------------------------------------------------------

@router.post("/positions/close")
async def close_position(body: ClosePositionBody, request: Request) -> dict:
    engine = _state(request).engine
    if engine is None:
        raise HTTPException(409, "The bot is not running.")
    result = await engine.close_position(body.id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Could not close."))
    return result


@router.post("/orders/cancel")
async def cancel_order(body: CancelOrderBody, request: Request) -> dict:
    engine = _state(request).engine
    if engine is None:
        raise HTTPException(409, "The bot is not running.")
    result = await engine.cancel_order(body.order_id)
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "Could not cancel."))
    return result


# --- data / history ---------------------------------------------------------

@router.get("/history")
async def history(request: Request, limit: int = 500) -> list[dict]:
    app = _state(request)
    return [t.to_dict() for t in app.db.get_closed_trades(min(limit, 2000))]


@router.get("/orders")
async def orders(request: Request, limit: int = 300) -> list[dict]:
    app = _state(request)
    return [o.to_dict() for o in app.db.get_orders(min(limit, 2000))]


@router.get("/logs")
async def logs(request: Request, limit: int = 400) -> list[dict]:
    return _state(request).db.recent_logs(min(limit, 5000))


@router.delete("/logs")
async def clear_logs(request: Request) -> dict:
    app = _state(request)
    app.db.clear_logs()
    app.hub.publish("logs:reset", True)
    return {"ok": True}


@router.post("/session/reset")
async def reset_session(request: Request) -> dict:
    """Wipe trades / orders / history — refused while the bot is running."""
    app = _state(request)
    if app.engine is not None and app.engine.state == "running":
        raise HTTPException(409, "Stop the bot before resetting the session.")
    app.db.reset_session(clear_log=True)
    app.hub.reset_cache("positions", "history", "orders", "stats")
    app.hub.publish("positions", [])
    app.hub.publish("history", [])
    app.hub.publish("orders", [])
    app.hub.publish("logs:reset", True)
    app.log.info("Session reset — all trades, orders and history cleared.")
    return {"ok": True}


@router.get("/target/activity")
async def target_activity(request: Request, limit: int = 40) -> list[dict]:
    """Recent trades on the followed wallet, for the 'Target' panel."""
    app = _state(request)
    engine = app.engine
    wallet = app.config.settings.target_wallet
    if not wallet:
        return []
    if engine is not None:
        data = engine.data
    else:
        from .services.data_api import DataApiClient
        data = DataApiClient(app.config.settings.data_api_host, app.http)
    trades = await data.fetch_activity(wallet, limit=min(limit, 100))
    return [
        {
            "tradeId": t.trade_id, "marketId": t.market_id,
            "tokenId": t.token_id, "outcome": t.outcome, "side": t.side.value,
            "price": t.price, "size": t.size, "usdcSize": t.usdc_size,
            "timestamp": t.timestamp, "question": t.market_question,
            "slug": t.slug,
        }
        for t in trades
    ]


# --- WebSocket --------------------------------------------------------------

@router.websocket("/stream")
async def stream(websocket: WebSocket) -> None:
    """The dashboard's single live connection.

    On connect the client receives a complete snapshot, then a continuous flow
    of coalesced updates. Writes are driven by the hub, so a slow browser tab
    can never back-pressure the trading engine.
    """
    app: AppState = websocket.app.state.app
    await websocket.accept()
    client = app.hub.connect()
    try:
        await websocket.send_json({
            "type": "snapshot", "data": app.snapshot(),
            "ts": asyncio.get_running_loop().time(),
        })
        reader = asyncio.create_task(_reader(websocket))
        waiter: Optional[asyncio.Task] = None
        try:
            while True:
                waiter = asyncio.create_task(client.wait())
                done, _ = await asyncio.wait(
                    {waiter, reader}, return_when=asyncio.FIRST_COMPLETED
                )
                if reader in done:
                    break
                waiter = None
                for message in client.drain():
                    await websocket.send_json(message)
        finally:
            for task in (reader, waiter):
                if task is None:
                    continue
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        pass
    finally:
        app.hub.disconnect(client)
        with contextlib.suppress(Exception):
            await websocket.close()


async def _reader(websocket: WebSocket) -> None:
    """Drain client → server messages; ends when the socket closes.

    The dashboard only sends keepalive pings, but reading is what surfaces a
    disconnect promptly instead of leaving a writer parked on a dead socket.
    Echoing a pong gives the UI a true round-trip latency reading.
    """
    while True:
        message = await websocket.receive_text()
        if message == "ping":
            await websocket.send_json({"type": "pong", "data": None, "ts": 0})
