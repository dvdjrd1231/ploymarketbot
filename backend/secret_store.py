"""
Secure local storage for the wallet private key.

Storage backends, in order of preference:

  1. **OS keyring** (``keyring``) — on Windows this is the Credential Manager.
     The secret is protected by the logged-in user's credentials and is never
     written into any file this project owns.
  2. **Windows DPAPI** — ``CryptProtectData`` with ``CRYPTPROTECT_LOCAL_MACHINE``
     off, i.e. the ciphertext can only be decrypted by *this* Windows user on
     *this* machine. Written to ``secret.bin`` in the app data directory.
     Used only if keyring is unavailable.
  3. **Passphrase-encrypted file** (``secret.enc``, AES-256-GCM, scrypt KDF) —
     for headless servers, where no OS keyring daemon is running and DPAPI does
     not exist. Enabled only when ``POLYBOT_SECRET_PASSPHRASE`` is set in the
     environment; the passphrase itself is never written to disk by this module.
  4. Refuse. We do not fall back to weak obfuscation — a reversible XOR in a
     JSON file gives a false sense of safety for a key that controls funds.

The plaintext key never leaves this module's callers: it is read once when the
engine connects, and the HTTP/WebSocket layers only ever report whether a key
*exists*. There is no API route that returns it.
"""

from __future__ import annotations

import ctypes
import os
import sys
from typing import Optional

from .paths import app_data_dir

_IS_WINDOWS = sys.platform == "win32"

# ctypes.wintypes raises on import off Windows, so it cannot sit at module top
# level — importing this module must not depend on the platform.
if _IS_WINDOWS:  # pragma: no cover - platform dependent
    from ctypes import wintypes
else:
    wintypes = None  # type: ignore[assignment]

try:
    import keyring
    _KEYRING_IMPORTED = True
except Exception:  # pragma: no cover - environment dependent
    _KEYRING_IMPORTED = False

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    _CRYPTO_AVAILABLE = True
except Exception:  # pragma: no cover - environment dependent
    _CRYPTO_AVAILABLE = False

# Deliberately the same service name the desktop app used, so an already-stored
# key is found without the user re-entering it.
_SERVICE = "PolymarketCopyBot"
_USERNAME = "trading_private_key"
_DPAPI_FILE = "secret.bin"
_ENC_FILE = "secret.enc"
_PASSPHRASE_ENV = "POLYBOT_SECRET_PASSPHRASE"

# scrypt cost. n=2**15 keeps derivation around a tenth of a second, which is
# irrelevant at our call rate (once per connect) and expensive to brute-force.
_SCRYPT_N = 2 ** 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_LEN = 16
_NONCE_LEN = 12
_MAGIC = b"PMB1"


# --- OS keyring -------------------------------------------------------------

def _keyring_available() -> bool:
    """True only if ``keyring`` has a backend that can actually store secrets.

    On a headless Linux box the import succeeds but resolves to ``fail.Keyring``
    (no Secret Service / D-Bus), whose ``set_password`` raises. Treating "import
    worked" as "storage works" would send us down a path that throws at the
    moment the user saves a key.
    """
    if not _KEYRING_IMPORTED:
        return False
    try:
        from keyring.backends.fail import Keyring as _FailKeyring
        backend = keyring.get_keyring()
    except Exception:
        return False
    if isinstance(backend, _FailKeyring):
        return False
    # A chainer with nothing viable behind it fails the same way.
    candidates = getattr(backend, "backends", None)
    if candidates is not None and not list(candidates):
        return False
    try:
        return backend.priority > 0  # type: ignore[attr-defined]
    except Exception:
        return True


# --- Windows DPAPI ----------------------------------------------------------

class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD if _IS_WINDOWS else ctypes.c_ulong),
                ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    """Build a DATA_BLOB plus the buffer backing it.

    The buffer must be returned alongside the struct and kept alive by the
    caller for the duration of the Win32 call — the struct only holds a raw
    pointer, so letting the buffer be collected would corrupt the input.
    """
    buf = ctypes.create_string_buffer(data, len(data))
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _blob_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _dpapi_available() -> bool:
    return _IS_WINDOWS


def _dpapi_call(func, data: bytes) -> bytes:
    in_blob, in_buf = _blob(data)
    ent_blob, ent_buf = _blob(_SERVICE.encode("utf-8"))
    out = _DataBlob()
    ok = func(ctypes.byref(in_blob), None, ctypes.byref(ent_blob),
              None, None, 0, ctypes.byref(out))
    # Touch the buffers so they are provably alive across the call.
    del in_buf, ent_buf
    if not ok:
        raise ctypes.WinError()
    try:
        return _blob_bytes(out)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _dpapi_encrypt(plaintext: str) -> bytes:
    return _dpapi_call(ctypes.windll.crypt32.CryptProtectData,
                       plaintext.encode("utf-8"))


def _dpapi_decrypt(ciphertext: bytes) -> str:
    return _dpapi_call(ctypes.windll.crypt32.CryptUnprotectData,
                       ciphertext).decode("utf-8")


# --- passphrase-encrypted file (headless servers) ---------------------------

def _passphrase() -> Optional[str]:
    value = (os.environ.get(_PASSPHRASE_ENV) or "").strip()
    return value or None


def _encfile_available() -> bool:
    return _CRYPTO_AVAILABLE and _passphrase() is not None


def _derive(passphrase: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    return kdf.derive(passphrase.encode("utf-8"))


def _encfile_encrypt(plaintext: str, passphrase: str) -> bytes:
    salt = os.urandom(_SALT_LEN)
    nonce = os.urandom(_NONCE_LEN)
    box = AESGCM(_derive(passphrase, salt))
    # The magic is authenticated too, so a truncated or foreign file fails the
    # tag check rather than decrypting to garbage.
    body = box.encrypt(nonce, plaintext.encode("utf-8"), _MAGIC)
    return _MAGIC + salt + nonce + body


def _encfile_decrypt(blob: bytes, passphrase: str) -> str:
    header = len(_MAGIC) + _SALT_LEN + _NONCE_LEN
    if len(blob) <= header or not blob.startswith(_MAGIC):
        raise ValueError("secret.enc is not in the expected format")
    salt = blob[len(_MAGIC):len(_MAGIC) + _SALT_LEN]
    nonce = blob[len(_MAGIC) + _SALT_LEN:header]
    box = AESGCM(_derive(passphrase, salt))
    return box.decrypt(nonce, blob[header:], _MAGIC).decode("utf-8")


# --- public API -------------------------------------------------------------

class SecretStore:
    """Stores exactly one secret: the trading private key."""

    def __init__(self) -> None:
        base = app_data_dir()
        self._path = base / _DPAPI_FILE
        self._enc_path = base / _ENC_FILE

    @property
    def backend(self) -> str:
        if _keyring_available():
            return "os-keyring"
        if _dpapi_available():
            return "windows-dpapi"
        if _encfile_available():
            return "encrypted-file"
        return "none"

    @property
    def secure(self) -> bool:
        return self.backend != "none"

    def _write_private(self, path, data: bytes) -> None:
        path.write_bytes(data)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    def set(self, private_key: str) -> None:
        private_key = (private_key or "").strip()
        if not private_key:
            return
        if _keyring_available():
            keyring.set_password(_SERVICE, _USERNAME, private_key)
            return
        if _dpapi_available():
            self._write_private(self._path, _dpapi_encrypt(private_key))
            return
        if _encfile_available():
            passphrase = _passphrase()
            assert passphrase is not None  # guaranteed by _encfile_available
            self._write_private(
                self._enc_path, _encfile_encrypt(private_key, passphrase))
            return
        if _CRYPTO_AVAILABLE:
            raise RuntimeError(
                f"No secure credential store is available. On a headless server, "
                f"set {_PASSPHRASE_ENV} in the environment to enable the "
                f"encrypted keyfile, then restart the app."
            )
        raise RuntimeError(
            "No secure credential store is available on this system. Install a "
            "keyring backend (pip install keyring), or on a headless server "
            "install 'cryptography' and set " + _PASSPHRASE_ENV + "."
        )

    def get(self) -> Optional[str]:
        if _keyring_available():
            try:
                value = keyring.get_password(_SERVICE, _USERNAME)
                if value:
                    return value
            except Exception:
                pass
        if _dpapi_available() and self._path.exists():
            try:
                return _dpapi_decrypt(self._path.read_bytes())
            except Exception:
                return None
        if _encfile_available() and self._enc_path.exists():
            passphrase = _passphrase()
            try:
                return _encfile_decrypt(self._enc_path.read_bytes(), passphrase)
            except Exception:
                # Wrong passphrase or a corrupt file — indistinguishable here by
                # design, and either way there is no key we can hand back.
                return None
        return None

    def clear(self) -> None:
        if _keyring_available():
            try:
                keyring.delete_password(_SERVICE, _USERNAME)
            except Exception:
                pass
        for path in (self._path, self._enc_path):
            try:
                path.unlink()
            except OSError:
                pass

    def exists(self) -> bool:
        return bool(self.get())
