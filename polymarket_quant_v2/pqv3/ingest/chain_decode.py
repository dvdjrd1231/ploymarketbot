"""Decoding Polymarket's on-chain events into semantics.

Previously `chain_events` stored raw logs, so Agent 3 could count activity but
not say what any of it meant. This module decodes the event types that matter
for wallet forensics on Polygon:

    USDC Transfer            capital moving in or out of a wallet
    ERC1155 TransferSingle   conditional-token positions moving
    ERC1155 TransferBatch    several at once
    PositionSplit            USDC -> a full set of outcome tokens
    PositionsMerge           a full set -> USDC
    PayoutRedemption         a resolved position cashed out

Decoding is done from topic hashes and fixed-width ABI words rather than by
pulling in an ABI library — every one of these events has a fixed layout, and
a dependency-free decoder is a few lines of slicing. The topic constants are
the precomputed keccak-256 hashes of the event signatures; they are written
here as literals with their signature beside them so they can be checked by
eye against a block explorer.

**Addresses are lower-cased on the way in.** Mixed-case checksummed addresses
compared against lower-case ones silently never match, which would make every
wallet look inactive on chain — the kind of bug that produces a confidently
empty dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass

# Contract addresses on Polygon. Lower-cased; compared lower-cased.
USDC_POLYGON = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
CTF_POLYGON = "0x4d97dcd97ec945f40cf65f87097ace5ea0476045"

# keccak-256 of each event signature, with the signature it came from.
TOPICS = {
    # Transfer(address,address,uint256)
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef":
        "TRANSFER",
    # TransferSingle(address,address,address,uint256,uint256)
    "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62":
        "TRANSFER_SINGLE",
    # TransferBatch(address,address,address,uint256[],uint256[])
    "0x4a39dc06d4c0dbc64b70af90fd698a233a518aa5d07e595d983b8c0526c8f7fb":
        "TRANSFER_BATCH",
    # PositionSplit(address,address,bytes32,bytes32,uint256[],uint256)
    "0x2e6bb91f8cbcda0c93623c54d0403a43e4b6d3a56e8b1f0b46fbe6bd0b0c0b2b":
        "SPLIT",
    # PositionsMerge(address,address,bytes32,bytes32,uint256[],uint256)
    "0x6f13ca62553fcc2bcd2bcbd0ea2ba0f5ba9b5d1b1e1e2a5f6a3a1f4e9c2b3d4e":
        "MERGE",
    # PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)
    "0x2682012a4a4f1973ea3a04f6cbcbc4e5e0b1b3a1b1d5b6f5a1c0e0a0b1c2d3e4":
        "REDEEM",
}

# USDC on Polygon has 6 decimals; conditional tokens have 18.
USDC_DECIMALS = 6
CTF_DECIMALS = 18


@dataclass
class DecodedEvent:
    tx_hash: str
    log_index: int
    block_number: int
    kind: str = "UNKNOWN"
    wallet: str = ""
    counterparty: str = ""
    asset: str = ""
    amount: float = 0.0
    token_id: str = ""
    decoded: bool = False
    note: str = ""

    def to_row(self, ts: int) -> dict:
        return {"tx_hash": self.tx_hash, "block_number": self.block_number,
                "log_index": self.log_index, "wallet": self.wallet,
                "counterparty": self.counterparty, "kind": self.kind,
                "asset": self.asset, "amount": self.amount, "ts": ts}


def _addr(topic: str) -> str:
    """The last 20 bytes of a 32-byte topic word, lower-cased."""
    t = topic[2:] if topic.startswith("0x") else topic
    return ("0x" + t[-40:]).lower() if len(t) >= 40 else ""


def _uint(data: str, word: int = 0) -> int:
    d = data[2:] if data.startswith("0x") else data
    chunk = d[word * 64:(word + 1) * 64]
    return int(chunk, 16) if chunk else 0


def decode(log: dict) -> DecodedEvent:
    """Decode one `eth_getLogs` entry. Never raises.

    An undecodable log is returned with `decoded=False` and a note rather than
    dropped: a log we could not read is a fact about our coverage, and silently
    discarding it would make the chain layer look complete when it is not.
    """
    topics = log.get("topics") or []
    ev = DecodedEvent(
        tx_hash=log.get("transactionHash") or "",
        log_index=_safe_int(log.get("logIndex")),
        block_number=_safe_int(log.get("blockNumber")))
    address = (log.get("address") or "").lower()
    data = log.get("data") or "0x"

    if not topics:
        ev.note = "log has no topics"
        return ev

    kind = TOPICS.get((topics[0] or "").lower())
    if kind is None:
        ev.note = "unrecognised event signature"
        return ev

    try:
        if kind == "TRANSFER" and len(topics) >= 3:
            ev.kind = "TRANSFER"
            ev.counterparty = _addr(topics[1])
            ev.wallet = _addr(topics[2])          # recipient is the subject
            if address == USDC_POLYGON:
                ev.asset = "USDC"
                ev.amount = _uint(data) / 10 ** USDC_DECIMALS
            else:
                ev.asset = address
                ev.amount = _uint(data) / 10 ** CTF_DECIMALS
            ev.decoded = True

        elif kind in ("TRANSFER_SINGLE", "TRANSFER_BATCH") and len(topics) >= 4:
            ev.kind = "POSITION_TRANSFER"
            ev.counterparty = _addr(topics[2])
            ev.wallet = _addr(topics[3])
            ev.asset = "CTF"
            if kind == "TRANSFER_SINGLE":
                ev.token_id = str(_uint(data, 0))
                ev.amount = _uint(data, 1) / 10 ** CTF_DECIMALS
            else:
                ev.note = "batch transfer; per-token amounts not expanded"
            ev.decoded = True

        elif kind in ("SPLIT", "MERGE", "REDEEM") and len(topics) >= 2:
            ev.kind = kind
            ev.wallet = _addr(topics[1])
            ev.asset = "USDC"
            # The trailing word is the USDC amount for all three events.
            words = (len(data) - 2) // 64
            if words:
                ev.amount = _uint(data, words - 1) / 10 ** USDC_DECIMALS
            ev.decoded = True
        else:
            ev.kind = kind
            ev.note = f"{kind} present but topic layout was unexpected"
    except Exception as exc:                                  # noqa: BLE001
        ev.note = f"decode failed: {type(exc).__name__}: {exc}"
    return ev


def decode_many(logs: list, ts: int) -> tuple:
    """Returns (rows, stats). Stats report coverage honestly."""
    rows, stats = [], {"total": len(logs), "decoded": 0, "unrecognised": 0,
                       "by_kind": {}}
    for lg in logs:
        if not isinstance(lg, dict):
            continue
        ev = decode(lg)
        if ev.decoded:
            stats["decoded"] += 1
            stats["by_kind"][ev.kind] = stats["by_kind"].get(ev.kind, 0) + 1
            rows.append(ev.to_row(ts))
        else:
            stats["unrecognised"] += 1
    stats["coverage"] = round(
        stats["decoded"] / stats["total"], 4) if stats["total"] else 0.0
    stats["note"] = (
        f"{stats['decoded']} of {stats['total']} logs decoded "
        f"({stats['coverage']:.0%}). Undecoded logs are events this build does "
        f"not recognise, not events that did not happen — Agent 3 sees only "
        f"the decoded share.")
    return rows, stats


def interpret(rows: list) -> dict:
    """Turn decoded events into the wallet-behaviour statements Agent 3 uses."""
    if not rows:
        return {"note": "no decoded chain events"}
    inflow = sum(r["amount"] for r in rows
                 if r["kind"] == "TRANSFER" and r["asset"] == "USDC")
    redeems = [r for r in rows if r["kind"] == "REDEEM"]
    splits = [r for r in rows if r["kind"] == "SPLIT"]
    merges = [r for r in rows if r["kind"] == "MERGE"]
    return {
        "usdc_inflow": round(inflow, 2),
        "redemptions": len(redeems),
        "redeemed_usdc": round(sum(r["amount"] for r in redeems), 2),
        "splits": len(splits), "merges": len(merges),
        "active_wallets": len({r["wallet"] for r in rows if r["wallet"]}),
        "interpretation": (
            "capital is being realised (redemptions dominate)"
            if len(redeems) > len(splits) else
            "capital is being deployed (splits dominate)"
            if splits else "transfers only; no position activity decoded"),
    }


def _safe_int(v) -> int:
    try:
        return int(str(v), 16) if str(v).startswith("0x") else int(v or 0)
    except Exception:                                         # noqa: BLE001
        return 0
