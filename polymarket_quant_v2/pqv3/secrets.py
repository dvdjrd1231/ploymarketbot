"""The signing boundary. The only module in V3 permitted to read a credential.

Design rule, enforced by shape rather than by care: **this module never returns
a secret to a caller inside the process.** It answers presence questions
(`status()`), and it performs signing on request (`sign()`), and that is the
whole public surface. Research agents, the feature store, the scanner, the
dashboard and the JSON API can all import this module safely, because there is
nothing here for them to leak.

Where secrets may live, in priority order:

    1. OS credential store       Windows Credential Manager via `keyring`
    2. Environment variable      POLYMARKET_PRIVATE_KEY
    3. Nowhere                   -> WALLET NOT CONFIGURED, live trading blocked

Where they may NEVER live: Rust source, Python source, HTML, JavaScript, JSON,
logs, the database, the git repository, an agent prompt, or a dashboard
payload. `tests/test_secret_isolation.py` greps the rendered dashboard, the API
payloads and the store for any value that looks like a key, and asserts the
redaction helper catches a planted one.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

# The names we look for. Nothing else in V3 mentions these strings.
ENV_PRIVATE_KEY = "POLYMARKET_PRIVATE_KEY"
ENV_API_KEY = "POLYMARKET_API_KEY"
ENV_API_SECRET = "POLYMARKET_API_SECRET"
ENV_API_PASSPHRASE = "POLYMARKET_API_PASSPHRASE"

_KEYRING_SERVICE = "polymarket-quant-bridge-v3"

_ALL = (ENV_PRIVATE_KEY, ENV_API_KEY, ENV_API_SECRET, ENV_API_PASSPHRASE)

# Shapes that must never appear in any output. Broad on purpose: a false
# positive costs one redacted string in a log, a false negative costs the
# wallet.
_SECRET_SHAPES = (
    re.compile(r"0x[a-fA-F0-9]{64}"),                 # raw 32-byte private key
    re.compile(r"\b[a-fA-F0-9]{64}\b"),               # same, unprefixed
    re.compile(r"\b(?:[a-z]{3,8}\s+){11}[a-z]{3,8}\b"),   # 12-word seed phrase
    re.compile(r"\b(?:[a-z]{3,8}\s+){23}[a-z]{3,8}\b"),   # 24-word seed phrase
)

REDACTED = "[REDACTED]"


def _keyring_get(name: str) -> str | None:
    try:
        import keyring                                        # type: ignore
    except Exception:                                         # noqa: BLE001
        return None
    try:
        return keyring.get_password(_KEYRING_SERVICE, name) or None
    except Exception:                                         # noqa: BLE001
        return None


def _resolve(name: str) -> tuple[str | None, str]:
    """Return (value, source). Private to this module — never re-exported."""
    v = _keyring_get(name)
    if v:
        return v, "os_credential_store"
    v = os.environ.get(name) or None
    if v:
        return v, "environment"
    return None, "unset"


@dataclass(frozen=True)
class SecretStatus:
    """What the rest of the system is allowed to know: whether, and from where.

    Deliberately carries no length, no prefix, no fingerprint and no last-four.
    Each of those has been used to narrow a key search space, and none of them
    helps a user who only needs to know if the wallet is configured.
    """

    name: str
    present: bool
    source: str


def status() -> list[SecretStatus]:
    return [SecretStatus(n, _resolve(n)[0] is not None, _resolve(n)[1])
            for n in _ALL]


def wallet_configured() -> bool:
    """True when enough credentials exist to sign. Used by the live gate."""
    return _resolve(ENV_PRIVATE_KEY)[0] is not None


def wallet_banner() -> str:
    """The only wallet string the dashboard is allowed to render."""
    return "WALLET CONNECTED" if wallet_configured() else "WALLET NOT CONFIGURED"


def redact(text: str) -> str:
    """Scrub anything key-shaped out of arbitrary text.

    Applied to every log line, every agent prompt and every rendered report.
    Cheap enough to apply unconditionally, which is the only way it stays
    applied.
    """
    if not text:
        return text
    out = text
    for pat in _SECRET_SHAPES:
        out = pat.sub(REDACTED, out)
    # Belt and braces: if a live secret is somehow present in the string,
    # remove it by value too.
    for n in _ALL:
        v, _ = _resolve(n)
        if v and len(v) >= 8 and v in out:
            out = out.replace(v, REDACTED)
    return out


def scrub(obj):
    """Recursively redact a JSON-serialisable structure.

    Every API response and every persisted agent record passes through this on
    the way out. Keys whose NAME looks secret are dropped entirely rather than
    redacted, because a redacted field still tells an attacker it exists.
    """
    _bad_key = re.compile(r"(secret|private|passphrase|seed|mnemonic|api_key|"
                          r"password|token)", re.I)
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if not _bad_key.search(str(k))}
    if isinstance(obj, (list, tuple)):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


class SigningBoundary:
    """Signs payloads without disclosing the key.

    V3 does not implement Polymarket order signing here — that belongs with the
    execution adapter and requires the venue's exact EIP-712 domain. What this
    class fixes in place is the *shape*: the key is read inside `sign`, used,
    and never returned, so no future execution code has a legitimate reason to
    call a `get_private_key()` that does not exist.
    """

    def available(self) -> bool:
        return wallet_configured()

    def _key(self) -> int:
        """Read the key, use it, never return it upward.

        Private and named so that the one place it is resolved is greppable.
        Nothing outside this class calls it, and nothing returns its value to a
        caller — the whole reason this class exists.
        """
        raw, _src = _resolve(ENV_PRIVATE_KEY)
        if not raw:
            raise NotImplementedError(
                f"no wallet credential is present. Set {ENV_PRIVATE_KEY} in "
                f"the environment or the OS keyring. It is never read into "
                f"the config tree and never written to a log, a report or a "
                f"prompt.")
        return int(raw.strip().replace("0x", ""), 16)

    def sign(self, payload: bytes) -> bytes:
        """Sign a 32-byte digest. Returns 65 bytes: r || s || v."""
        from .execution.crypto import sign as _sign, signature_bytes
        if len(payload) != 32:
            raise ValueError(
                "sign() takes a 32-byte digest, not a message. Hash the "
                "structure with execution.eip712.digest first — signing raw "
                "bytes would skip the domain binding that stops a signature "
                "being replayed against another chain or contract.")
        r, s, v = _sign(self._key(), payload)
        return signature_bytes(r, s, v)

    def address(self) -> str:
        """The address this boundary signs for. Derived, never stored."""
        from .execution.crypto import address_from_private
        return address_from_private(self._key())
