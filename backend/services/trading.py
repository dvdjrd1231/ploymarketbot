"""
Authenticated exchange access — async facade over ``py-clob-client``.

``py-clob-client`` is synchronous and signs orders with ``eth-account``, which is
CPU-bound. Calling it directly from the event loop would stall every WebSocket
push in the app — precisely the class of freeze this rebuild has to remove. So
**every** call here is dispatched to a dedicated thread pool via
``asyncio.to_thread``, and the event loop is never blocked.

The connection/auth/order semantics are ported unchanged from the desktop app's
``core/polymarket_client.py`` so live trading behaves identically.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from ..models import Side

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import (
        ApiCreds, AssetType, BalanceAllowanceParams, OrderArgs, OrderType,
    )
    from py_clob_client.order_builder.constants import BUY, SELL
    _CLOB_AVAILABLE = True
    _CLOB_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - import-time environment issue
    _CLOB_AVAILABLE = False
    _CLOB_IMPORT_ERROR = str(exc)


class PolymarketError(Exception):
    pass


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    filled_size: float = 0.0
    avg_price: float = 0.0
    error: str = ""
    status: str = ""


def clob_available() -> tuple[bool, str]:
    """Report whether the trading library imported successfully."""
    return _CLOB_AVAILABLE, _CLOB_IMPORT_ERROR


class TradingClient:
    """Thin, defensive, *async* facade over :class:`ClobClient`."""

    def __init__(self, host: str, chain_id: int, private_key: str,
                 signature_type: int = 0, funder_address: str = ""):
        if not _CLOB_AVAILABLE:
            raise PolymarketError(
                "Polymarket trading library could not be loaded "
                f"(import error: {_CLOB_IMPORT_ERROR}). "
                "Fix: pip install --upgrade py-clob-client py-order-utils "
                "poly_eip712_structs — make sure you install into the SAME "
                "Python that runs the app."
            )
        self.host = host
        self.chain_id = chain_id
        self._signature_type = signature_type
        self._funder = _validate_funder(funder_address)
        self._client: Optional[ClobClient] = None
        self._creds: Optional[ApiCreds] = None
        self.address = ""
        self._key = normalize_private_key(private_key)
        # Serialises order signing: eth-account is not documented as reentrant
        # for a single client instance, and orders must keep a stable nonce.
        self._order_lock = asyncio.Lock()

    # -- connection / auth ---------------------------------------------------

    async def connect(self) -> "TradingClient":
        await asyncio.to_thread(self._connect_blocking)
        return self

    def _connect_blocking(self) -> None:
        kwargs = dict(host=self.host, key=self._key, chain_id=self.chain_id)
        # signature_type 0 = EOA; 1 = email/magic; 2 = browser proxy.
        if self._signature_type in (1, 2):
            kwargs["signature_type"] = self._signature_type
            if self._funder:
                kwargs["funder"] = self._funder
        try:
            self._client = ClobClient(**kwargs)
            self.address = self._client.get_address() or ""
            # Derive (or fetch) the L2 API credentials required for trading.
            creds = self._client.create_or_derive_api_creds()
            self._client.set_api_creds(creds)
            self._creds = creds
        except Exception as exc:
            raise PolymarketError(f"Failed to connect to Polymarket: {exc}") from exc

    async def check_connection(self) -> bool:
        """True if the authenticated session is healthy."""
        def _check() -> bool:
            try:
                self._client.get_ok()
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_check)

    @property
    def signature_type(self) -> int:
        return self._signature_type

    @property
    def funder_address(self) -> str:
        return self._funder or self.address

    def ws_auth(self) -> Optional[dict]:
        """L2 credentials for the CLOB *user* WebSocket channel."""
        if self._creds is None:
            return None
        return {
            "apiKey": getattr(self._creds, "api_key", ""),
            "secret": getattr(self._creds, "api_secret", ""),
            "passphrase": getattr(self._creds, "api_passphrase", ""),
        }

    # -- balances ------------------------------------------------------------

    async def get_usdc_balance(self) -> float:
        """Free (collateral) USDC balance in dollars."""
        def _read() -> float:
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=self._signature_type,
            )
            res = self._client.get_balance_allowance(params)
            # Balances come back as integer strings in 6-decimal USDC units.
            raw = res.get("balance") if isinstance(res, dict) else None
            if raw is None:
                return 0.0
            return float(raw) / 1_000_000.0
        try:
            return await asyncio.to_thread(_read)
        except Exception as exc:
            raise PolymarketError(f"Could not read balance: {exc}") from exc

    # -- prices (used only as a fallback; PriceService is the fast path) -----

    async def get_best_price(self, token_id: str, side: Side) -> Optional[float]:
        def _read() -> Optional[float]:
            try:
                book_side = "SELL" if side == Side.BUY else "BUY"
                res = self._client.get_price(token_id=token_id, side=book_side)
                price = res.get("price") if isinstance(res, dict) else res
                return float(price) if price is not None else None
            except Exception:
                return None
        return await asyncio.to_thread(_read)

    # -- orders --------------------------------------------------------------

    async def place_limit_order(self, token_id: str, side: Side, price: float,
                                size: float,
                                order_type: str = "FAK") -> OrderResult:
        """Place a limit order.

        ``price`` is the worst price you will accept. Because the entry-price
        rule forbids paying above the target's entry, callers pass the target
        entry (or better) as the limit price. A Fill-And-Kill order fills
        whatever it can at ``price`` or better and cancels the rest — exactly
        the "same or better, otherwise skip" behaviour we want.
        """
        def _place() -> OrderResult:
            try:
                clob_side = BUY if side == Side.BUY else SELL
                # Round to Polymarket tick sizes: price to 3dp, size to shares.
                px = round(float(price), 3)
                sz = round(float(size), 2)
                order_args = OrderArgs(
                    price=px, size=sz, side=clob_side, token_id=token_id
                )
                signed = self._client.create_order(order_args)
                resp = self._client.post_order(signed, _order_type(order_type))
                return _parse_order_response(resp, px)
            except Exception as exc:
                return OrderResult(success=False, error=str(exc))

        async with self._order_lock:
            return await asyncio.to_thread(_place)

    async def cancel_order(self, order_id: str) -> bool:
        def _cancel() -> bool:
            try:
                self._client.cancel(order_id=order_id)
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_cancel)

    async def cancel_all(self) -> bool:
        def _cancel() -> bool:
            try:
                self._client.cancel_all()
                return True
            except Exception:
                return False
        return await asyncio.to_thread(_cancel)

    async def open_orders(self) -> list[dict]:
        """Orders currently resting on the exchange, for reconciliation."""
        def _read() -> list[dict]:
            try:
                res = self._client.get_orders()
                if isinstance(res, list):
                    return res
                if isinstance(res, dict):
                    return res.get("data") or []
            except Exception:
                pass
            return []
        return await asyncio.to_thread(_read)


# --- helpers ----------------------------------------------------------------

def _order_type(name: str):
    mapping = {
        "FOK": getattr(OrderType, "FOK", None),
        "FAK": getattr(OrderType, "FAK", None),
        "GTC": getattr(OrderType, "GTC", None),
    }
    return mapping.get((name or "").upper()) or OrderType.GTC


def _parse_order_response(resp: dict, price: float) -> OrderResult:
    if not isinstance(resp, dict):
        return OrderResult(success=False, error=f"Unexpected response: {resp}")
    success = bool(resp.get("success", False)) or resp.get("status") in (
        "matched", "live", "delayed",
    )
    order_id = resp.get("orderID") or resp.get("orderId") or resp.get("id") or ""
    filled = 0.0
    for key in ("size_matched", "sizeMatched", "makingAmount"):
        if resp.get(key) is not None:
            try:
                filled = float(resp[key])
                break
            except (TypeError, ValueError):
                pass
    return OrderResult(
        success=success,
        order_id=str(order_id),
        filled_size=filled,
        avg_price=price,
        status=str(resp.get("status", "")),
        error="" if success else str(
            resp.get("errorMsg") or resp.get("error") or resp),
    )


def normalize_private_key(key: str) -> str:
    """Clean and validate a private key, with friendly errors.

    Accepts a 64-character hex string with or without the ``0x`` prefix, and
    rejects the common mistakes (seed phrase / wrong field) up front so the user
    gets an actionable message instead of "Non-hexadecimal digit found".
    """
    cleaned = (key or "").strip().replace(" ", "").replace("\n", "")
    body = cleaned[2:] if cleaned.lower().startswith("0x") else cleaned

    if " " in (key or "").strip() or len((key or "").split()) > 1 or _looks_like_seed(key):
        raise PolymarketError(
            "That looks like a SEED / RECOVERY PHRASE (words), not a private "
            "key. Paste the 64-character hex PRIVATE KEY instead (export it "
            "from Trust Wallet, or from Polymarket → Export Private Key)."
        )
    if len(body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise PolymarketError(
            "The private key is not valid. It must be a 64-character "
            "hexadecimal string (optionally starting with 0x) — no spaces, no "
            "words, no email address."
        )
    return "0x" + body


def _validate_funder(funder: str) -> Optional[str]:
    funder = (funder or "").strip()
    if not funder:
        return None
    if "@" in funder or not funder.lower().startswith("0x") or len(funder) != 42:
        raise PolymarketError(
            "Funder address is invalid. It must be a 0x… wallet address (42 "
            "characters) — NOT an email address. For a Polymarket email wallet, "
            "paste your Polymarket deposit/wallet address here."
        )
    return funder


def _looks_like_seed(key: str) -> bool:
    """Heuristic: a seed phrase is multiple space-separated alphabetic words."""
    parts = (key or "").strip().split()
    return len(parts) >= 6 and all(p.isalpha() for p in parts)


def derive_signer_address(private_key: str) -> str:
    """Locally derive the 0x address for a private key (no network call)."""
    key = normalize_private_key(private_key)
    try:
        from eth_account import Account
        return Account.from_key(key).address
    except Exception as exc:  # pragma: no cover - environment dependent
        raise PolymarketError(f"Could not read the private key: {exc}") from exc


@dataclass
class DetectedAccount:
    """Result of auto-detecting how a private key is connected to Polymarket."""

    signature_type: int
    funder_address: str
    address: str            # the signer (EOA) address derived from the key
    balance: float          # USDC found for this configuration
    account_label: str      # human-readable, e.g. "EOA / Trust Wallet"

    def to_dict(self) -> dict:
        return {
            "signatureType": self.signature_type,
            "funderAddress": self.funder_address,
            "address": self.address,
            "balance": self.balance,
            "accountLabel": self.account_label,
        }


_LABELS = {
    0: "EOA / self-custody (Trust Wallet, MetaMask, …)",
    1: "Polymarket email wallet",
    2: "Polymarket browser proxy",
}


async def autodetect_account(host: str, chain_id: int, private_key: str,
                             funder_hint: str = "",
                             ) -> tuple[TradingClient, DetectedAccount]:
    """Connect with just a private key and figure out the account setup.

    The user should not have to know whether their Polymarket funds sit in a
    plain EOA (Trust Wallet / MetaMask) or in a Polymarket proxy wallet, nor
    what "signature type" means. We try the possible configurations, keep the
    one that actually reports a USDC balance, and return it.

    ``funder_hint`` is optional: if the user pasted the address shown in their
    Polymarket app, we use it to check the proxy-wallet configurations too. For
    a plain Trust Wallet EOA it is not needed — the signer address is the funder.
    """
    if not _CLOB_AVAILABLE:
        raise PolymarketError(
            "Polymarket trading library could not be loaded "
            f"(import error: {_CLOB_IMPORT_ERROR})."
        )

    signer = derive_signer_address(private_key)
    hint = (funder_hint or "").strip()

    # Candidate (signature_type, funder) configurations, most common first.
    candidates: list[tuple[int, str]] = [(0, signer)]  # EOA: signer holds USDC
    if hint and hint.lower() != signer.lower():
        candidates += [(2, hint), (1, hint)]           # proxy funded by `hint`
    candidates += [(2, signer), (1, signer)]           # last-resort proxies

    first_ok: tuple[TradingClient, DetectedAccount] | None = None
    last_error = ""
    seen: set[tuple[int, str]] = set()

    for sig_type, funder in candidates:
        combo = (sig_type, funder.lower())
        if combo in seen:
            continue
        seen.add(combo)
        try:
            client = await TradingClient(
                host=host, chain_id=chain_id, private_key=private_key,
                signature_type=sig_type,
                funder_address="" if sig_type == 0 else funder,
            ).connect()
        except PolymarketError as exc:
            last_error = str(exc)
            continue

        try:
            balance = await client.get_usdc_balance()
        except PolymarketError:
            balance = 0.0

        detected = DetectedAccount(
            signature_type=sig_type,
            funder_address="" if sig_type == 0 else funder,
            address=client.address or signer,
            balance=balance,
            account_label=_LABELS.get(sig_type, str(sig_type)),
        )

        if balance > 0:
            # Definitive match — this configuration holds the funds.
            return client, detected
        if first_ok is None:
            # Authenticated but no balance seen; remember as a fallback.
            first_ok = (client, detected)

    if first_ok is not None:
        return first_ok

    raise PolymarketError(
        last_error
        or "Could not connect with this key. Check that it is correct and that "
        "your USDC is on Polygon. If your funds are in a Polymarket email/"
        "browser wallet, also paste your Polymarket address."
    )
