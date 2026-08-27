"""EIP-712 typed-data hashing, checked against the specification's own example.

An order is not signed as bytes. It is signed as a STRUCTURE, hashed under a
scheme that binds it to one chain and one contract, so a signature captured
from one venue cannot be replayed against another. Getting any part of the
encoding wrong produces a digest that is perfectly well-formed and describes a
different order than the one you meant.

That is the failure mode worth naming. A wrong signature is rejected loudly and
costs nothing. A signature over a MISCONSTRUCTED order is accepted, and buys
something other than what was intended. The difference is entirely in this
file, which is why it is verified against the canonical example published in
the EIP itself rather than against my own expectations.

The type-hash rules, in the order they bite:

  * `encodeType` sorts referenced struct types alphabetically and appends them
    after the primary type. Miss a referenced type and the hash changes.
  * dynamic values (`string`, `bytes`) are hashed, then the hash is encoded —
    not the value.
  * every atomic value is padded to 32 bytes; `address` is left-padded to 32,
    not packed to 20.
  * the digest is keccak256(0x19 0x01 || domainSeparator || hashStruct).
"""

from __future__ import annotations

import re

from .crypto import keccak256

_ARRAY = re.compile(r"^(.*)\[(\d*)\]$")


def encode_type(primary: str, types: dict) -> str:
    """`Mail(Person from,Person to,string contents)Person(...)`.

    Referenced types are appended in alphabetical order, which is the part
    people get wrong; the primary type always comes first regardless.
    """
    deps: set = set()

    def walk(name: str) -> None:
        if name in deps or name not in types:
            return
        deps.add(name)
        for field in types[name]:
            base = _ARRAY.match(field["type"])
            walk(base.group(1) if base else field["type"])

    walk(primary)
    deps.discard(primary)
    out = ""
    for name in [primary] + sorted(deps):
        fields = ",".join(f"{f['type']} {f['name']}" for f in types[name])
        out += f"{name}({fields})"
    return out


def type_hash(primary: str, types: dict) -> bytes:
    return keccak256(encode_type(primary, types).encode())


def _encode_value(kind: str, value, types: dict) -> bytes:
    arr = _ARRAY.match(kind)
    if arr:
        inner = arr.group(1)
        return keccak256(b"".join(_encode_value(inner, v, types)
                                  for v in value))
    if kind in types:
        return hash_struct(kind, value, types)
    if kind == "string":
        return keccak256(value.encode() if isinstance(value, str) else value)
    if kind == "bytes":
        return keccak256(value if isinstance(value, bytes)
                         else bytes.fromhex(str(value).replace("0x", "")))
    if kind == "address":
        v = int(str(value), 16) if isinstance(value, str) else int(value)
        return v.to_bytes(32, "big")
    if kind == "bool":
        return (1 if value else 0).to_bytes(32, "big")
    if kind.startswith("bytes"):            # bytes1 .. bytes32, right-padded
        raw = value if isinstance(value, bytes) else \
            bytes.fromhex(str(value).replace("0x", ""))
        return raw + b"\x00" * (32 - len(raw))
    if kind.startswith(("uint", "int")):
        v = int(str(value), 0) if isinstance(value, str) else int(value)
        if v < 0:                            # two's complement
            v += 1 << 256
        return v.to_bytes(32, "big")
    raise ValueError(f"unsupported EIP-712 type: {kind}")


def hash_struct(primary: str, data: dict, types: dict) -> bytes:
    parts = [type_hash(primary, types)]
    for field in types[primary]:
        parts.append(_encode_value(field["type"], data[field["name"]], types))
    return keccak256(b"".join(parts))


DOMAIN_FIELDS = (
    ("name", "string"), ("version", "string"), ("chainId", "uint256"),
    ("verifyingContract", "address"), ("salt", "bytes32"),
)


def domain_separator(domain: dict) -> bytes:
    """Only the fields actually present are included, in EIP order."""
    fields = [{"name": n, "type": t} for n, t in DOMAIN_FIELDS
              if n in domain and domain[n] is not None]
    return hash_struct("EIP712Domain", domain,
                       {"EIP712Domain": fields})


def digest(domain: dict, primary: str, message: dict, types: dict) -> bytes:
    """The 32 bytes that get signed. keccak256(0x1901 || domain || struct)."""
    inner = {k: v for k, v in types.items() if k != "EIP712Domain"}
    return keccak256(b"\x19\x01" + domain_separator(domain)
                     + hash_struct(primary, message, inner))
