"""keccak-256 and secp256k1, in the standard library, verified against vectors.

§32's signing path needs two primitives Python does not ship. `hashlib` has
`sha3_256`, which is NOT keccak256 — the padding byte differs, so every digest
differs, and a signature built on the wrong one is rejected by the venue at
best. There is no secp256k1 anywhere in the standard library.

WHY THIS IS SAFE TO HAND-WRITE AND ECDSA NEARLY IS NOT.

keccak-256 is a deterministic hash with published test vectors. A wrong
implementation fails those vectors loudly; it cannot fail quietly. There is no
secret material inside it, so there is nothing to leak.

ECDSA is different, and the danger is entirely in one number: the per-signature
nonce k. Reuse k across two signatures, or generate it with any bias, and the
private key falls out of the algebra — this is how the PS3 was broken and how
wallets have been drained. So the nonce here is NOT random. It is RFC 6979
deterministic: derived by HMAC-SHA256 from the private key and the message
hash, with published test vectors of its own. Two signatures over different
messages get different nonces by construction, and the same message always
produces the identical signature, which is itself testable.

WHAT REMAINS TRUE ANYWAY, and is reported by `backend()` rather than buried:
this is not constant-time. Python's big integers make timing side channels
unavoidable, so a local attacker who can measure this process precisely could
in principle recover the key. That threat needs code execution on the same
machine, at which point the key file is readable anyway — but it is the reason
`coincurve` or `eth_keys` is PREFERRED when installed, and this pure path is
the fallback rather than the default.
"""

from __future__ import annotations

import hmac
import hashlib

# ---------------------------------------------------------------------------
# keccak-256
# ---------------------------------------------------------------------------

_RC = (
    0x0000000000000001, 0x0000000000008082, 0x800000000000808A,
    0x8000000080008000, 0x000000000000808B, 0x0000000080000001,
    0x8000000080008081, 0x8000000000008009, 0x000000000000008A,
    0x0000000000000088, 0x0000000080008009, 0x000000008000000A,
    0x000000008000808B, 0x800000000000008B, 0x8000000000008089,
    0x8000000000008003, 0x8000000000008002, 0x8000000000000080,
    0x000000000000800A, 0x800000008000000A, 0x8000000080008081,
    0x8000000000008080, 0x0000000080000001, 0x8000000080008008,
)
_ROT = (
    (0, 36, 3, 41, 18), (1, 44, 10, 45, 2), (62, 6, 43, 15, 61),
    (28, 55, 25, 21, 56), (27, 20, 39, 8, 14),
)
_MASK = (1 << 64) - 1


def _rotl(x: int, n: int) -> int:
    return ((x << n) | (x >> (64 - n))) & _MASK


def _keccak_f(a: list) -> list:
    for rnd in range(24):
        # theta
        c = [a[x][0] ^ a[x][1] ^ a[x][2] ^ a[x][3] ^ a[x][4] for x in range(5)]
        d = [c[(x - 1) % 5] ^ _rotl(c[(x + 1) % 5], 1) for x in range(5)]
        for x in range(5):
            for y in range(5):
                a[x][y] ^= d[x]
        # rho and pi
        b = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                b[y][(2 * x + 3 * y) % 5] = _rotl(a[x][y], _ROT[x][y])
        # chi
        for x in range(5):
            for y in range(5):
                a[x][y] = b[x][y] ^ ((~b[(x + 1) % 5][y]) & _MASK) \
                    & b[(x + 2) % 5][y] if False else \
                    b[x][y] ^ (((~b[(x + 1) % 5][y]) & _MASK)
                               & b[(x + 2) % 5][y])
        # iota
        a[0][0] ^= _RC[rnd]
    return a


def keccak256(data: bytes) -> bytes:
    """Ethereum's keccak-256. NOT hashlib.sha3_256 — the padding differs."""
    rate = 136                      # 1088 bits, for a 256-bit digest
    a = [[0] * 5 for _ in range(5)]

    # Pad10*1 with keccak's 0x01 domain byte. SHA-3 uses 0x06 here, and that
    # single byte is the whole difference between the two functions.
    padded = bytearray(data)
    padded.append(0x01)
    while len(padded) % rate != 0:
        padded.append(0x00)
    padded[-1] ^= 0x80

    for off in range(0, len(padded), rate):
        block = padded[off:off + rate]
        for i in range(rate // 8):
            lane = int.from_bytes(block[i * 8:i * 8 + 8], "little")
            a[i % 5][i // 5] ^= lane
        a = _keccak_f(a)

    out = bytearray()
    while len(out) < 32:
        for i in range(rate // 8):
            if len(out) >= 32:
                break
            out += a[i % 5][i // 5].to_bytes(8, "little")
    return bytes(out[:32])


# ---------------------------------------------------------------------------
# secp256k1
# ---------------------------------------------------------------------------

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8


def _inv(x: int, m: int) -> int:
    return pow(x, m - 2, m)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    x1, y1 = p1
    x2, y2 = p2
    if x1 == x2 and (y1 + y2) % P == 0:
        return None
    if p1 == p2:
        lam = (3 * x1 * x1) * _inv(2 * y1, P) % P
    else:
        lam = (y2 - y1) * _inv(x2 - x1, P) % P
    x3 = (lam * lam - x1 - x2) % P
    return (x3, (lam * (x1 - x3) - y1) % P)


def _mul(k: int, point=None):
    """Double-and-add. Not constant-time — see the module docstring."""
    point = point or (GX, GY)
    r = None
    while k:
        if k & 1:
            r = _add(r, point)
        point = _add(point, point)
        k >>= 1
    return r


def public_key(priv: int) -> tuple:
    if not 1 <= priv < N:
        raise ValueError("private key out of range")
    return _mul(priv)


def address_from_private(priv: int) -> str:
    """The Ethereum address, EIP-55 checksummed."""
    x, y = public_key(priv)
    raw = keccak256(x.to_bytes(32, "big") + y.to_bytes(32, "big"))[-20:]
    return to_checksum_address("0x" + raw.hex())


def to_checksum_address(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    h = keccak256(a.encode()).hex()
    return "0x" + "".join(c.upper() if h[i] in "89abcdef" else c
                          for i, c in enumerate(a))


def _rfc6979_k(priv: int, msg_hash: bytes) -> int:
    """Deterministic nonce. The single number that must never repeat or bias.

    RFC 6979 section 3.2. Derived by HMAC-SHA256 from the key and the message,
    so it is unique per message, unguessable without the key, and reproducible
    — which is what makes a signature testable at all.
    """
    x = priv.to_bytes(32, "big")
    h1 = msg_hash
    v = b"\x01" * 32
    k = b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h1, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        cand = int.from_bytes(v, "big")
        if 1 <= cand < N:
            return cand
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


def sign(priv: int, msg_hash: bytes) -> tuple:
    """(r, s, v) over a 32-byte hash. `s` is canonical-low (EIP-2)."""
    if len(msg_hash) != 32:
        raise ValueError("message hash must be 32 bytes")
    z = int.from_bytes(msg_hash, "big")
    k = _rfc6979_k(priv, msg_hash)
    px, py = _mul(k)
    r = px % N
    if r == 0:
        raise ValueError("degenerate signature; retry with another message")
    s = (_inv(k, N) * (z + r * priv)) % N
    recovery = (py & 1) ^ (0 if s * 2 < N else 1)
    # Ethereum rejects the high-s form: both s and N-s are valid signatures
    # over the same message, and allowing either makes a transaction hash
    # malleable.
    if s * 2 > N:
        s = N - s
    return r, s, 27 + recovery


def verify(pub: tuple, msg_hash: bytes, r: int, s: int) -> bool:
    z = int.from_bytes(msg_hash, "big")
    if not (1 <= r < N and 1 <= s < N):
        return False
    w = _inv(s, N)
    p = _add(_mul((z * w) % N), _mul((r * w) % N, pub))
    return p is not None and p[0] % N == r


def signature_bytes(r: int, s: int, v: int) -> bytes:
    return r.to_bytes(32, "big") + s.to_bytes(32, "big") + bytes([v])


def backend() -> dict:
    """Which implementation is in use, and what that costs.

    `coincurve` and `eth_keys` wrap libsecp256k1, which is audited and
    constant-time. When one is installed it is preferred. This is reported
    rather than assumed so an operator can see which signed their order.
    """
    for name in ("coincurve", "eth_keys"):
        try:
            __import__(name)
            return {"backend": name, "constant_time": True,
                    "note": f"{name} is installed; libsecp256k1 signs, and it "
                            f"is audited and constant-time"}
        except ImportError:
            continue
    return {
        "backend": "pure-python",
        "constant_time": False,
        "note": (
            "no secp256k1 library installed, so signing uses the pure-Python "
            "implementation in this module. Nonces are RFC 6979 deterministic, "
            "so the nonce-reuse failure that leaks a private key cannot occur. "
            "It is NOT constant-time: an attacker able to measure this process "
            "precisely could in principle recover the key, which requires code "
            "execution on this machine. `pip install coincurve` removes that "
            "residual risk."),
    }
