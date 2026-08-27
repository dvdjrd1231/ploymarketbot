"""EIP-712 against the canonical example published in the specification.

Every value asserted below is from the EIP itself. That matters more here than
anywhere else in the signing path: a wrong signature is rejected and costs
nothing, but a signature over a MISCONSTRUCTED order is accepted and buys
something other than what was intended. The digest is the only thing standing
between those two outcomes.
"""

from __future__ import annotations

import pytest

from pqv3.execution import eip712 as E

# The specification's worked example, verbatim.
TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Person": [
        {"name": "name", "type": "string"},
        {"name": "wallet", "type": "address"},
    ],
    "Mail": [
        {"name": "from", "type": "Person"},
        {"name": "to", "type": "Person"},
        {"name": "contents", "type": "string"},
    ],
}
DOMAIN = {
    "name": "Ether Mail", "version": "1", "chainId": 1,
    "verifyingContract": "0xCcCCccccCCCCcCCCCCCcCcCccCcCCCcCcccccccC",
}
MESSAGE = {
    "from": {"name": "Cow",
             "wallet": "0xCD2a3d9F938E13CD947Ec05AbC7FE734Df8DD826"},
    "to": {"name": "Bob",
           "wallet": "0xbBbBBBBbbBBBbbbBbbBbbbbBBbBbbbbBbBbbBBbB"},
    "contents": "Hello, Bob!",
}


def test_encode_type_matches_the_specification():
    assert E.encode_type("Mail", TYPES) == (
        "Mail(Person from,Person to,string contents)"
        "Person(string name,address wallet)")


def test_referenced_types_are_sorted_alphabetically():
    types = dict(TYPES)
    types["Alpha"] = [{"name": "x", "type": "uint256"}]
    types["Mail"] = TYPES["Mail"] + [{"name": "a", "type": "Alpha"}]
    encoded = E.encode_type("Mail", types)
    assert encoded.startswith("Mail("), "the primary type always comes first"
    assert encoded.index("Alpha(") < encoded.index("Person("), (
        "referenced types must be alphabetical, and Alpha precedes Person")


def test_type_hash_matches_the_specification():
    assert E.type_hash("Mail", TYPES).hex() == (
        "a0cedeb2dc280ba39b857546d74f5549c3a1d7bdc2dd96bf881f76108e23dac2")


def test_domain_separator_matches_the_specification():
    assert E.domain_separator(DOMAIN).hex() == (
        "f2cee375fa42b42143804025fc449deafd50cc031ca257e0b194a650a912090f")


def test_hash_struct_matches_the_specification():
    inner = {k: v for k, v in TYPES.items() if k != "EIP712Domain"}
    assert E.hash_struct("Mail", MESSAGE, inner).hex() == (
        "c52c0ee5d84264471806290a3f2c4cecfc5490626bf912d01f240d7a274b371e")


def test_the_signing_digest_matches_the_specification():
    """The 32 bytes that actually get signed."""
    assert E.digest(DOMAIN, "Mail", MESSAGE, TYPES).hex() == (
        "be609aee343fb3c4b28e1df9e632fca64fcfaede20f02e86244efddf30957bd2")


# ------------------------------------------------------- encoding details
def test_a_different_chain_gives_a_different_digest():
    """Replay protection. The same order on another chain must not verify."""
    other = dict(DOMAIN, chainId=137)
    assert E.digest(other, "Mail", MESSAGE, TYPES) != \
        E.digest(DOMAIN, "Mail", MESSAGE, TYPES)


def test_a_different_verifying_contract_gives_a_different_digest():
    other = dict(DOMAIN,
                 verifyingContract="0x0000000000000000000000000000000000000001")
    assert E.digest(other, "Mail", MESSAGE, TYPES) != \
        E.digest(DOMAIN, "Mail", MESSAGE, TYPES)


def test_absent_domain_fields_are_omitted_not_zeroed():
    """A domain without `salt` must not hash as one with a zero salt."""
    minimal = {"name": "X", "version": "1"}
    with_salt = dict(minimal, salt=b"\x00" * 32)
    assert E.domain_separator(minimal) != E.domain_separator(with_salt)


def test_address_is_left_padded_to_32_bytes():
    enc = E._encode_value(
        "address", "0xCD2a3d9F938E13CD947Ec05AbC7FE734Df8DD826", {})
    assert len(enc) == 32
    assert enc[:12] == b"\x00" * 12, "an address packs to 20 bytes, not here"


def test_dynamic_values_are_hashed_before_encoding():
    assert E._encode_value("string", "Hello, Bob!", {}) == \
        E.keccak256(b"Hello, Bob!") if hasattr(E, "keccak256") else True
    from pqv3.execution.crypto import keccak256
    assert E._encode_value("string", "Hello, Bob!", {}) == \
        keccak256(b"Hello, Bob!")


def test_uint_and_bool_encoding():
    assert E._encode_value("uint256", 1, {}) == (1).to_bytes(32, "big")
    assert E._encode_value("uint256", "0x10", {}) == (16).to_bytes(32, "big")
    assert E._encode_value("bool", True, {}) == (1).to_bytes(32, "big")
    assert E._encode_value("bool", False, {}) == (0).to_bytes(32, "big")


def test_fixed_bytes_are_right_padded():
    enc = E._encode_value("bytes32", "0xab", {})
    assert len(enc) == 32 and enc[0] == 0xAB and enc[1] == 0


def test_arrays_hash_their_concatenated_elements():
    from pqv3.execution.crypto import keccak256
    got = E._encode_value("uint256[]", [1, 2], {})
    want = keccak256((1).to_bytes(32, "big") + (2).to_bytes(32, "big"))
    assert got == want


def test_an_unknown_type_is_refused_not_guessed():
    with pytest.raises(ValueError):
        E._encode_value("float128", 1.5, {})
