"""
Conditional Token Framework (CTF) events — decoding and cash-flow reconstruction.

This is the answer to acceptance 11.3 and the honest version of §6: a wallet's
real economics live partly off the order book. The Gnosis ConditionalTokens
contract on Polygon emits three events that move money without ever producing a
CLOB trade, and ERC-1155 emits two that move tokens between wallets:

* **PositionSplit**  — deposit USDC, receive one of each outcome token. A cash
  *outflow*: this is buying a complete set.
* **PositionsMerge** — return one of each outcome token, receive USDC back. A
  cash *inflow*, and one of the two invisible exits.
* **PayoutRedemption** — at resolution, hand in winning tokens for USDC. The
  other invisible exit, and where a "held to a win" position actually pays out.
* **TransferSingle / TransferBatch** — outcome tokens moving between wallets,
  off the book entirely.

The module is split deliberately:

* The **decoder and reconstruction are pure** — they take raw log dicts (exactly
  what an ``eth_getLogs`` JSON-RPC call returns) and need no network, no web3,
  and no keccak. They are unit-tested with synthetic logs.
* The **reader** fetches those logs over plain JSON-RPC with httpx (no web3
  dependency) and needs keccak only to compute the event topic hashes. keccak
  comes from ``eth_utils``/``web3`` when present on the operator's machine; the
  reader raises a clear error if it is asked to run without them.

So the whole decoding path is provable here, and only the live fetch needs an
RPC endpoint and the operator's environment — which is why the live
demonstration is gated on the RPC decision, while the logic is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ConditionalTokens on Polygon (chain 137). Public, well-known address.
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# Event signatures. The topic0 (keccak of these strings) is computed at
# fetch time; the decoder is told which kind a topic0 maps to, so it never
# needs keccak itself.
SIGNATURES: dict[str, str] = {
    "PositionSplit":
        "PositionSplit(address,address,bytes32,bytes32,uint256[],uint256)",
    "PositionsMerge":
        "PositionsMerge(address,address,bytes32,bytes32,uint256[],uint256)",
    "PayoutRedemption":
        "PayoutRedemption(address,address,bytes32,bytes32,uint256[],uint256)",
    "TransferSingle":
        "TransferSingle(address,address,address,uint256,uint256)",
    "TransferBatch":
        "TransferBatch(address,address,address,uint256[],uint256[])",
}

# USDC and Polymarket outcome tokens both use 6 decimals.
COLLATERAL_DECIMALS = 6


@dataclass
class CTFEvent:
    """One decoded on-chain event, normalised to a wallet and a USDC amount."""

    kind: str
    wallet: str = ""            # the stakeholder / redeemer / relevant party
    counterparty: str = ""      # for transfers: the other side
    condition_id: str = ""
    amount_usdc: float = 0.0    # collateral moved (split/merge/redeem)
    token_ids: list[str] = field(default_factory=list)   # for transfers
    token_amounts: list[float] = field(default_factory=list)
    block: int = 0
    direction: str = ""         # "in" | "out" — sign of the cash/token flow

    def to_dict(self) -> dict:
        return {"kind": self.kind, "wallet": self.wallet,
                "conditionId": self.condition_id,
                "amountUsdc": round(self.amount_usdc, 4),
                "direction": self.direction, "block": self.block}


# -- pure decoding -----------------------------------------------------------

def _clean(hexstr: str) -> str:
    return hexstr[2:] if hexstr.startswith(("0x", "0X")) else hexstr


def _addr_from_topic(topic: str) -> str:
    """An indexed address is the low 20 bytes of a 32-byte topic."""
    h = _clean(topic)
    return "0x" + h[-40:].lower()


def _word(data: str, index: int) -> int:
    """The uint256 at 32-byte word ``index`` of the data blob."""
    h = _clean(data)
    chunk = h[index * 64:(index + 1) * 64]
    return int(chunk, 16) if chunk else 0


def _bytes32(data: str, index: int) -> str:
    h = _clean(data)
    return "0x" + h[index * 64:(index + 1) * 64]


def _dyn_array(data: str, offset_word: int) -> list[int]:
    """Decode a uint256[] whose head is a byte-offset at ``offset_word``."""
    h = _clean(data)
    byte_off = _word(data, offset_word)
    start = byte_off * 2                       # bytes -> hex chars
    length = int(h[start:start + 64] or "0", 16)
    out: list[int] = []
    for i in range(length):
        s = start + 64 * (1 + i)
        out.append(int(h[s:s + 64] or "0", 16))
    return out


def decode_log(log: dict, kind_of_topic0: dict[str, str],
               collateral_decimals: int = COLLATERAL_DECIMALS
               ) -> Optional[CTFEvent]:
    """Decode one raw log into a :class:`CTFEvent`, or ``None`` if unrecognised.

    ``kind_of_topic0`` maps a lower-cased topic0 hex string to an event name —
    supplied by the caller so this stays independent of keccak. Robust to
    malformed rows: anything it cannot parse becomes ``None`` rather than
    raising, because one bad log must not abort a whole reconstruction.
    """
    try:
        topics = [t.lower() for t in (log.get("topics") or [])]
        if not topics:
            return None
        kind = kind_of_topic0.get(topics[0])
        if kind is None:
            return None
        data = log.get("data") or "0x"
        block = int(str(log.get("blockNumber") or "0x0"), 16) \
            if isinstance(log.get("blockNumber"), str) else int(log.get("blockNumber") or 0)
        scale = 10 ** collateral_decimals

        if kind in ("PositionSplit", "PositionsMerge"):
            # indexed: stakeholder(1), parentCollectionId(2), conditionId(3)
            # data: collateralToken(0), partition offset(1), amount(2)
            wallet = _addr_from_topic(topics[1])
            condition = topics[3] if len(topics) > 3 else ""
            amount = _word(data, 2) / scale
            direction = "out" if kind == "PositionSplit" else "in"
            return CTFEvent(kind=kind, wallet=wallet, condition_id=condition,
                            amount_usdc=amount, block=block, direction=direction)

        if kind == "PayoutRedemption":
            # indexed: redeemer(1), collateralToken(2), parentCollectionId(3)
            # data: conditionId(0), indexSets offset(1), payout(2)
            wallet = _addr_from_topic(topics[1])
            condition = _bytes32(data, 0)
            payout = _word(data, 2) / scale
            return CTFEvent(kind=kind, wallet=wallet, condition_id=condition,
                            amount_usdc=payout, block=block, direction="in")

        if kind == "TransferSingle":
            # indexed: operator(1), from(2), to(3); data: id(0), value(1)
            frm, to = _addr_from_topic(topics[2]), _addr_from_topic(topics[3])
            token_id = str(_word(data, 0))
            value = _word(data, 1) / scale
            return CTFEvent(kind=kind, wallet=frm, counterparty=to,
                            token_ids=[token_id], token_amounts=[value],
                            block=block, direction="transfer")

        if kind == "TransferBatch":
            frm, to = _addr_from_topic(topics[2]), _addr_from_topic(topics[3])
            ids = _dyn_array(data, 0)
            values = _dyn_array(data, 1)
            scale = 10 ** collateral_decimals
            return CTFEvent(kind=kind, wallet=frm, counterparty=to,
                            token_ids=[str(i) for i in ids],
                            token_amounts=[v / scale for v in values],
                            block=block, direction="transfer")
    except Exception:                                    # noqa: BLE001
        return None
    return None


# -- reconstruction ----------------------------------------------------------

@dataclass
class CTFSummary:
    """A wallet's on-chain cash flow, the part the CLOB tape cannot see."""

    wallet: str
    split_paid: float = 0.0         # USDC out, buying complete sets
    merge_received: float = 0.0     # USDC in, merging sets back
    redeem_received: float = 0.0    # USDC in, redeeming winners
    splits: int = 0
    merges: int = 0
    redemptions: int = 0
    transfers_in: int = 0
    transfers_out: int = 0

    @property
    def net_onchain_usdc(self) -> float:
        """Cash the order book never saw: money back in, minus money out."""
        return self.merge_received + self.redeem_received - self.split_paid

    def to_dict(self) -> dict:
        return {"wallet": self.wallet,
                "splitPaid": round(self.split_paid, 2),
                "mergeReceived": round(self.merge_received, 2),
                "redeemReceived": round(self.redeem_received, 2),
                "splits": self.splits, "merges": self.merges,
                "redemptions": self.redemptions,
                "transfersIn": self.transfers_in,
                "transfersOut": self.transfers_out,
                "netOnchainUsdc": round(self.net_onchain_usdc, 2)}


def reconstruct(wallet: str, events: list[CTFEvent]) -> CTFSummary:
    """Fold a wallet's decoded events into its on-chain cash flow."""
    w = wallet.lower()
    s = CTFSummary(wallet=w)
    for e in events:
        if e.kind == "PositionSplit" and e.wallet == w:
            s.split_paid += e.amount_usdc
            s.splits += 1
        elif e.kind == "PositionsMerge" and e.wallet == w:
            s.merge_received += e.amount_usdc
            s.merges += 1
        elif e.kind == "PayoutRedemption" and e.wallet == w:
            s.redeem_received += e.amount_usdc
            s.redemptions += 1
        elif e.kind in ("TransferSingle", "TransferBatch"):
            if e.wallet == w:
                s.transfers_out += 1
            if e.counterparty.lower() == w:
                s.transfers_in += 1
    return s


@dataclass
class TruePnL:
    """CLOB-only vs CTF-inclusive P&L — the demonstration acceptance 11.3 asks for."""

    wallet: str
    clob_bought: float = 0.0
    clob_sold: float = 0.0
    ctf: Optional[CTFSummary] = None

    @property
    def clob_only_pnl(self) -> float:
        """What the order-book view alone says: sells minus buys."""
        return self.clob_sold - self.clob_bought

    @property
    def true_pnl(self) -> float:
        """CLOB plus the on-chain flows: the honest number."""
        onchain = self.ctf.net_onchain_usdc if self.ctf else 0.0
        return self.clob_only_pnl + onchain

    @property
    def hidden(self) -> float:
        """How much the CLOB-only view was off by."""
        return self.true_pnl - self.clob_only_pnl

    def to_dict(self) -> dict:
        return {"wallet": self.wallet,
                "clobBought": round(self.clob_bought, 2),
                "clobSold": round(self.clob_sold, 2),
                "clobOnlyPnl": round(self.clob_only_pnl, 2),
                "truePnl": round(self.true_pnl, 2),
                "hiddenByClobView": round(self.hidden, 2),
                "ctf": self.ctf.to_dict() if self.ctf else None}
