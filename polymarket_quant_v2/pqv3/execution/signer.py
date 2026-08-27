"""§32 — the signing boundary, implemented. And the one thing still missing.

`secrets.SigningBoundary` has always fixed the SHAPE of this: the key is read
inside `sign`, used, and never returned, so no calling code has a legitimate
reason to reach for a `get_private_key()` that does not exist. This module
fills it in.

WHAT IS NOW REAL AND VERIFIED, against published known-answer vectors in
`tests/v3/test_v3_crypto.py` and `tests/v3/test_v3_eip712.py`:

    keccak-256          four published digests, and proof it differs from
                        hashlib.sha3_256
    address derivation  three well-known Ethereum test addresses
    RFC 6979 nonce      the standard secp256k1/SHA-256 vector
    ECDSA               deterministic, canonical-low-s, verify accepts and
                        rejects
    EIP-712             encodeType, typeHash, domainSeparator, hashStruct and
                        the final signing digest, all against the example
                        published in the EIP

WHAT IS STILL MISSING, and it is not cryptography. To sign a POLYMARKET order
rather than an arbitrary struct, four constants and one schema must be exactly
right:

    the EIP-712 domain  name, version, chainId, verifyingContract
    the Order struct    field names, types, and their ORDER

These are facts about a live venue. I do not know them to the certainty this
requires, and a plausible guess is the worst possible outcome here — not
because a wrong signature is dangerous (the venue rejects it, loudly, costing
nothing) but because a RIGHT domain with a TRANSPOSED struct produces a valid
signature over a different order than the one intended. That is bought, not
rejected.

So `VenueProfile` is data, it is empty by default, and `sign_order` refuses
until it is filled from Polymarket's own published documentation or their
`py-clob-client` source. Filling it is a five-minute job for someone with the
docs open; inventing it from memory is how money is lost quietly.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import eip712
from .crypto import (address_from_private, backend, keccak256,
                     signature_bytes, sign as raw_sign)


@dataclass
class VenueProfile:
    """The venue-specific constants. Empty until a human fills them in."""

    name: str = ""
    version: str = ""
    chain_id: int = 0
    verifying_contract: str = ""
    order_type: str = "Order"
    order_fields: tuple = ()          # (("salt","uint256"), ("maker","address"), ...)

    @property
    def complete(self) -> bool:
        return bool(self.name and self.version and self.chain_id
                    and self.verifying_contract and self.order_fields)

    def domain(self) -> dict:
        return {"name": self.name, "version": self.version,
                "chainId": self.chain_id,
                "verifyingContract": self.verifying_contract}

    def types(self) -> dict:
        return {self.order_type: [{"name": n, "type": t}
                                  for n, t in self.order_fields]}

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["order_fields"] = [list(f) for f in self.order_fields]
        d["complete"] = self.complete
        return d


PROFILE_ENV = "PQV3_VENUE_PROFILE"


def load_profile(path: str = "") -> VenueProfile:
    """Read the venue profile from JSON. Never guessed, never defaulted.

    Point PQV3_VENUE_PROFILE at a file shaped like:

        {"name": "...", "version": "...", "chain_id": 137,
         "verifying_contract": "0x...",
         "order_fields": [["salt","uint256"], ["maker","address"], ...]}

    Every value comes from Polymarket's published EIP-712 definition. Copy it;
    do not reconstruct it from memory.
    """
    src = path or os.environ.get(PROFILE_ENV, "")
    if not src:
        return VenueProfile()
    p = Path(src).expanduser()
    if not p.exists():
        return VenueProfile()
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:                                         # noqa: BLE001
        return VenueProfile()
    return VenueProfile(
        name=d.get("name", ""), version=d.get("version", ""),
        chain_id=int(d.get("chain_id", 0) or 0),
        verifying_contract=d.get("verifying_contract", ""),
        order_type=d.get("order_type", "Order"),
        order_fields=tuple(tuple(f) for f in d.get("order_fields", [])))


def sign_typed_data(priv: int, domain: dict, primary: str, message: dict,
                    types: dict) -> dict:
    """Sign an arbitrary EIP-712 structure. Verified end to end by tests."""
    dg = eip712.digest(domain, primary, message, types)
    r, s, v = raw_sign(priv, dg)
    return {"digest": "0x" + dg.hex(),
            "signature": "0x" + signature_bytes(r, s, v).hex(),
            "r": hex(r), "s": hex(s), "v": v,
            "signer": address_from_private(priv),
            "backend": backend()["backend"]}


def sign_order(priv: int, order: dict, profile: VenueProfile) -> dict:
    """Sign a venue order — or refuse, with the exact reason."""
    if not profile.complete:
        missing = [k for k, v in (
            ("name", profile.name), ("version", profile.version),
            ("chainId", profile.chain_id),
            ("verifyingContract", profile.verifying_contract),
            ("order_fields", profile.order_fields)) if not v]
        raise NotImplementedError(
            "the venue profile is incomplete, so no order can be signed. "
            f"Missing: {', '.join(missing)}. These are Polymarket's published "
            "EIP-712 constants and its Order struct definition, and they must "
            "be COPIED from the venue's documentation or its py-clob-client "
            "source rather than reconstructed. A wrong domain produces a "
            "signature the venue rejects, which is harmless; a right domain "
            "with the struct fields in the wrong ORDER produces a valid "
            "signature over a different order, which is not. Set "
            f"{PROFILE_ENV} to a JSON file — see `load_profile`.")
    expected = {n for n, _t in profile.order_fields}
    supplied = set(order)
    if expected != supplied:
        raise ValueError(
            f"order fields do not match the venue profile. "
            f"Missing: {sorted(expected - supplied) or 'none'}. "
            f"Unexpected: {sorted(supplied - expected) or 'none'}. Signing a "
            f"struct that does not match the declared type would produce a "
            f"digest for something the venue will not recognise")
    return sign_typed_data(priv, profile.domain(), profile.order_type,
                           order, profile.types())


def status(profile: VenueProfile | None = None) -> dict:
    """What the signing path can do right now."""
    from ..secrets import wallet_configured
    profile = profile if profile is not None else load_profile()
    b = backend()
    return {
        "crypto": {
            "keccak256": "verified against 4 published vectors",
            "secp256k1": "verified against 3 known Ethereum addresses",
            "rfc6979": "verified against the standard vector",
            "eip712": "verified against the specification's example digest",
            "backend": b["backend"], "constant_time": b["constant_time"],
            "note": b["note"]},
        "venue_profile": profile.to_dict(),
        "wallet_present": wallet_configured(),
        "can_sign_arbitrary_typed_data": True,
        "can_sign_a_venue_order": profile.complete,
        "can_place_an_order": False,
        "note": (
            "arbitrary EIP-712 signing works and is verified against published "
            "vectors. Signing a Polymarket ORDER additionally needs the venue "
            "profile (§32). PLACING one needs that, a wallet credential, the "
            "CLOB submission path, and a human authorisation through "
            "`pqv3 authorize-live` — and nothing here has ever placed one, so "
            "no part of this is claimed to be venue-tested."),
    }
