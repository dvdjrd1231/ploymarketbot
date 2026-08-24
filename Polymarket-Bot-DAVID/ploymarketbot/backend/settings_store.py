"""
Non-sensitive configuration.

Every field the desktop app had is preserved verbatim so trading behaviour is
identical; a handful of web-specific tuning knobs are appended at the end.

The private key is *not* stored here — see :mod:`backend.secret_store`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Optional

from .models import MODE_FIXED_50, MODE_PROPORTIONAL
from .paths import app_data_dir
from .secret_store import SecretStore


@dataclass
class Settings:
    """Non-sensitive, user-editable configuration."""

    # -- copy trading --------------------------------------------------------
    target_wallet: str = ""
    my_wallet_address: str = ""
    trading_mode: str = MODE_FIXED_50

    # Polymarket account type / signature scheme:
    #   0 = EOA (MetaMask / Trust Wallet / raw private key)
    #   1 = Polymarket email (Magic) wallet
    #   2 = Polymarket browser proxy wallet
    signature_type: int = 0
    funder_address: str = ""

    # How often (seconds) to poll the target wallet's activity feed.
    poll_interval_seconds: int = 15

    # -- endpoints -----------------------------------------------------------
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    clob_ws_host: str = "wss://ws-subscriptions-clob.polymarket.com/ws"
    chain_id: int = 137  # Polygon mainnet

    # -- safety limits -------------------------------------------------------
    # Rule 2: the copy can never exceed the target's dollar amount.
    max_copy_fraction_of_target: float = 1.0
    min_order_usdc: float = 1.0
    max_price_improvement_ticks: int = 5

    # When True the engine only logs what it *would* do and places no orders.
    dry_run: bool = True

    # Paper trading: fully simulated with a virtual balance, using live public
    # market data. No private key, no funds, no real orders.
    paper_trading: bool = False
    paper_starting_balance: float = 100.0

    # When True, keep prior paper trades/positions across restarts. When False
    # (default), each paper-trading session starts with a clean dashboard.
    restore_previous_session: bool = False

    # Only copy trades whose market settles within this many days (0 = no limit).
    max_expiry_days: int = 0

    # Mirror the target's exits: when the target SELLS a position we copy, close
    # our copy too (instead of only holding to settlement).
    copy_exits: bool = True

    # Never enter a market that has already expired / is settling.
    min_seconds_to_expiry: int = 60

    # -- web build tuning ----------------------------------------------------
    # How often the server pushes a live position/P&L frame to the browser.
    # Mark prices themselves arrive by WebSocket push and are applied instantly;
    # this only controls the render cadence.
    push_interval_ms: int = 400

    # Wallet balance refresh cadence (seconds).
    balance_refresh_seconds: int = 5

    # Market metadata / settlement check cadence (seconds).
    market_refresh_seconds: int = 20

    # Exchange reconciliation cadence (seconds): positions + open orders are
    # compared against what the exchange actually reports.
    reconcile_seconds: int = 30

    # Use the Polymarket CLOB WebSocket for streaming order books. When False,
    # the engine falls back to REST price polling (slower, but a safety valve).
    use_price_stream: bool = True

    # REST safety-net poll for any token the stream has not yet delivered.
    rest_price_fallback_seconds: int = 4

    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty == valid)."""
        problems: list[str] = []
        if not self.target_wallet or not self.target_wallet.startswith("0x"):
            problems.append("Target wallet address is missing or malformed.")
        if len(self.target_wallet) not in (0, 42):
            problems.append("Target wallet address must be 42 characters (0x…).")
        if self.trading_mode not in (MODE_PROPORTIONAL, MODE_FIXED_50):
            problems.append("Trading mode is invalid.")
        if self.poll_interval_seconds < 5:
            problems.append("Poll interval must be at least 5 seconds.")
        # Paper trading is fully simulated — no wallet/funder required.
        if self.paper_trading:
            if self.paper_starting_balance <= 0:
                problems.append("Paper starting balance must be greater than 0.")
            return problems
        if self.signature_type in (1, 2) and not self.funder_address:
            problems.append(
                "Funder address is required for Polymarket email/proxy wallets."
            )
        return problems

    def to_dict(self) -> dict:
        return asdict(self)


class ConfigManager:
    """Loads/saves :class:`Settings` and brokers the secret private key."""

    def __init__(self, path: Optional[Path] = None):
        self.path = path or (app_data_dir() / "config.json")
        self.secrets = SecretStore()
        self.settings = self._load()

    # -- settings persistence ------------------------------------------------

    def _load(self) -> Settings:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return self._coerce(data)
            except Exception:
                # Corrupt config → start fresh rather than crash.
                return Settings()
        # First run: adopt the desktop app's config if it is present, so the
        # user does not have to retype their target wallet and preferences.
        return self._import_desktop_config()

    @staticmethod
    def _coerce(data: dict) -> Settings:
        """Build Settings from a dict, ignoring unknown keys and bad types."""
        known = {f.name: f.type for f in fields(Settings)}
        base = Settings()
        for key, value in (data or {}).items():
            if key not in known or value is None:
                continue
            current = getattr(base, key)
            try:
                if isinstance(current, bool):
                    value = bool(value)
                elif isinstance(current, int):
                    value = int(value)
                elif isinstance(current, float):
                    value = float(value)
                elif isinstance(current, str):
                    value = str(value)
            except (TypeError, ValueError):
                continue
            setattr(base, key, value)
        return base

    def _import_desktop_config(self) -> Settings:
        import os

        legacy = Path(os.environ.get("APPDATA") or Path.home()) / \
            "PolymarketCopyBot" / "config.json"
        if legacy.exists():
            try:
                return self._coerce(json.loads(legacy.read_text(encoding="utf-8")))
            except Exception:
                pass
        return Settings()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.settings), indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def update(self, patch: dict[str, Any]) -> Settings:
        """Apply a partial update from the API and persist it."""
        merged = asdict(self.settings)
        merged.update({k: v for k, v in (patch or {}).items() if v is not None})
        self.settings = self._coerce(merged)
        self.save()
        return self.settings

    # -- secret private key --------------------------------------------------

    @property
    def secure_storage(self) -> bool:
        return self.secrets.secure

    @property
    def storage_backend(self) -> str:
        return self.secrets.backend

    def set_private_key(self, private_key: str) -> None:
        self.secrets.set(private_key)

    def get_private_key(self) -> Optional[str]:
        return self.secrets.get()

    def clear_private_key(self) -> None:
        self.secrets.clear()

    def has_private_key(self) -> bool:
        return self.secrets.exists()

    # -- what the browser is allowed to see ----------------------------------

    def public_state(self) -> dict:
        """Settings as sent to the frontend.

        The private key is never included — only a boolean saying one is stored
        and which backend holds it.
        """
        data = asdict(self.settings)
        data["hasPrivateKey"] = self.has_private_key()
        data["secureStorage"] = self.secure_storage
        data["storageBackend"] = self.storage_backend
        return data
