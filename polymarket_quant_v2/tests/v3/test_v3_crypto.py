"""Known-answer vectors for the §32 signing primitives.

These are the tests that make hand-written cryptography defensible. Every value
on the right-hand side below is published — keccak-256 digests, well-known
Ethereum test addresses, and the RFC 6979 deterministic-nonce vector — so a
wrong implementation fails here rather than in front of a venue with money
attached.

NO REAL KEY APPEARS IN THIS FILE OR ANYWHERE IN THE PROJECT. The keys used are
either the published test constants (1, 2, and the EIP-155 example key, all of
which are public and hold nothing) or generated per-run from `os.urandom` and
discarded. A private key controlling funds must never enter a source file, a
test, a log or a chat transcript.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from pqv3.execution import crypto as C


# ------------------------------------------------------------- keccak-256
@pytest.mark.parametrize("data,digest", [
    (b"", "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"),
    (b"abc",
     "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"),
    (b"testing",
     "5f16f4c7f149ac4f9510d9cf8cf384038ad348b3bcdc01915f95de12df9d1b02"),
    (b"The quick brown fox jumps over the lazy dog",
     "4d741b6f1eb29cb2a9b9911c82f56fa8d73b04959d3d9d222895df6c0b28aa15"),
])
def test_keccak256_matches_published_vectors(data, digest):
    assert C.keccak256(data).hex() == digest


def test_keccak256_is_not_sha3_256():
    """The single padding byte that separates them.

    Getting this wrong produces a hash that looks fine, signs fine, and is
    rejected by every Ethereum verifier.
    """
    assert C.keccak256(b"") != hashlib.sha3_256(b"").digest()


def test_keccak256_handles_a_multi_block_message():
    """Longer than the 136-byte rate, so the sponge absorbs more than once."""
    assert len(C.keccak256(b"x" * 500)) == 32
    assert C.keccak256(b"x" * 500) != C.keccak256(b"x" * 499)


# ----------------------------------------------------- address derivation
@pytest.mark.parametrize("priv,address", [
    (1, "0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"),
    (2, "0x2B5AD5c4795c026514f8317c7a215E218DcCD6cF"),
    (0x4646464646464646464646464646464646464646464646464646464646464646,
     "0x9d8A62f656a8d1615C1294fd71e9CFb3E4855A4F"),
])
def test_address_from_private_matches_known_vectors(priv, address):
    """Public test keys. They control nothing and are published everywhere."""
    assert C.address_from_private(priv) == address


def test_checksum_casing_is_eip55():
    mixed = C.to_checksum_address(
        "0x5aaeb6053f3e94c9b9a09f33669435e7ef1beaed")
    assert mixed == "0x5aAeb6053F3E94C9b9A09f33669435E7Ef1BeAed"


def test_a_key_outside_the_curve_order_is_rejected():
    for bad in (0, C.N, C.N + 1):
        with pytest.raises(ValueError):
            C.public_key(bad)


# ------------------------------------------------------------- RFC 6979
def test_deterministic_nonce_matches_the_rfc_vector():
    """The number that leaks the key if it ever repeats or biases."""
    k = C._rfc6979_k(1, hashlib.sha256(b"Satoshi Nakamoto").digest())
    assert k == 0x8F8A276C19F4149656B280621E358CCE24F5F52542772691EE69063B74F15D15


def test_different_messages_get_different_nonces(throwaway):
    a = C._rfc6979_k(throwaway, C.keccak256(b"order one"))
    b = C._rfc6979_k(throwaway, C.keccak256(b"order two"))
    assert a != b, "nonce reuse across messages leaks the private key"


def test_the_nonce_is_in_range(throwaway):
    for msg in (b"a", b"bb", b"ccc", b"dddd"):
        assert 1 <= C._rfc6979_k(throwaway, C.keccak256(msg)) < C.N


# -------------------------------------------------------------- signing
@pytest.fixture
def throwaway() -> int:
    """A key generated for this test and discarded when it ends."""
    return int.from_bytes(os.urandom(32), "big") % (C.N - 1) + 1


def test_signing_is_deterministic(throwaway):
    h = C.keccak256(b"the same order twice")
    assert C.sign(throwaway, h) == C.sign(throwaway, h)


def test_signature_verifies_and_rejects(throwaway):
    pub = C.public_key(throwaway)
    h = C.keccak256(b"an order")
    r, s, v = C.sign(throwaway, h)
    assert C.verify(pub, h, r, s)
    assert not C.verify(pub, C.keccak256(b"a different order"), r, s)
    assert not C.verify(pub, h, r, (s + 1) % C.N)


def test_s_is_canonical_low(throwaway):
    """EIP-2. Both s and N-s are valid, and allowing either makes the
    transaction hash malleable."""
    for i in range(12):
        _r, s, _v = C.sign(throwaway, C.keccak256(f"order {i}".encode()))
        assert s * 2 < C.N


def test_recovery_id_is_in_range(throwaway):
    for i in range(8):
        _r, _s, v = C.sign(throwaway, C.keccak256(f"m{i}".encode()))
        assert v in (27, 28)


def test_signature_bytes_are_65_long(throwaway):
    r, s, v = C.sign(throwaway, C.keccak256(b"x"))
    assert len(C.signature_bytes(r, s, v)) == 65


def test_a_hash_of_the_wrong_length_is_refused(throwaway):
    with pytest.raises(ValueError):
        C.sign(throwaway, b"too short")


def test_two_keys_produce_different_signatures():
    a = int.from_bytes(os.urandom(32), "big") % (C.N - 1) + 1
    b = int.from_bytes(os.urandom(32), "big") % (C.N - 1) + 1
    h = C.keccak256(b"same message")
    assert C.sign(a, h)[:2] != C.sign(b, h)[:2]


# -------------------------------------------------------------- backend
def test_the_backend_reports_its_own_limitation():
    b = C.backend()
    assert b["backend"] in ("coincurve", "eth_keys", "pure-python")
    if b["backend"] == "pure-python":
        assert b["constant_time"] is False
        assert "not constant-time" in b["note"].lower() or \
            "NOT constant-time" in b["note"]
        assert "coincurve" in b["note"], "must name the way to remove the risk"
