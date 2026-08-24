"""
The live CTF reader: fetch on-chain events over JSON-RPC, decode them purely.

Deliberately dependency-light. Log fetching is plain ``eth_getLogs`` JSON-RPC
over httpx — no web3 needed for the network path. The only thing that needs a
crypto primitive is turning an event signature into its topic0 hash, and that
uses ``eth_utils``/``web3`` keccak when present (both ship with the CLOB client
already on the operator's machine). Asked to run without keccak, it says so
plainly rather than guessing a hash.

Nothing here trades or decides. It reconstructs history the CLOB tape cannot see.
"""

from __future__ import annotations

from typing import Optional

from .ctf import (CONDITIONAL_TOKENS, SIGNATURES, CTFEvent, CTFSummary,
                  decode_log, reconstruct)


def _keccak():
    """Return a keccak256 function, or raise a clear, actionable error."""
    try:
        from eth_utils import keccak       # ships with py-clob-client
        return keccak
    except Exception:                       # noqa: BLE001
        pass
    try:
        from web3 import Web3
        return lambda b: Web3.keccak(b)
    except Exception:                       # noqa: BLE001
        pass
    raise RuntimeError(
        "CTF reading needs keccak (eth_utils or web3). Both ship with "
        "py-clob-client; install the connectivity extras on the machine that "
        "runs the reader. Decoding and tests do not need this - only the live "
        "fetch does.")


def topic0_map() -> dict[str, str]:
    """topic0 hex -> event kind, for every signature we decode."""
    keccak = _keccak()
    out: dict[str, str] = {}
    for kind, sig in SIGNATURES.items():
        out["0x" + keccak(sig.encode()).hex()] = kind
    return out


def _addr_topic(address: str) -> str:
    """A 20-byte address as a 32-byte, left-padded, lower-cased topic."""
    h = address.lower()
    h = h[2:] if h.startswith("0x") else h
    return "0x" + h.rjust(64, "0")


class CTFReader:
    """Reads a wallet's Conditional-Token events from a Polygon RPC endpoint."""

    def __init__(self, rpc_url: str, http, contract: str = CONDITIONAL_TOKENS,
                 chunk_blocks: int = 100_000):
        if not rpc_url:
            raise ValueError("no polygon_rpc_url configured — the CTF reader "
                             "needs an endpoint (set it under polymarket / "
                             "chain in the config, or via env).")
        self.rpc_url = rpc_url
        self.http = http
        self.contract = contract
        self.chunk_blocks = max(1, int(chunk_blocks))
        self._kinds = topic0_map()

    async def _rpc(self, method: str, params: list):
        resp = await self.http.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"RPC {method} failed: {payload['error']}")
        return payload.get("result")

    async def latest_block(self) -> int:
        return int(str(await self._rpc("eth_blockNumber", [])), 16)

    async def _get_logs(self, topics: list, from_block: int,
                        to_block: int) -> list[dict]:
        """eth_getLogs over one block range, chunked to respect provider caps."""
        logs: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + self.chunk_blocks - 1, to_block)
            result = await self._rpc("eth_getLogs", [{
                "address": self.contract,
                "topics": topics,
                "fromBlock": hex(start),
                "toBlock": hex(end),
            }])
            if isinstance(result, list):
                logs.extend(result)
            start = end + 1
        return logs

    async def fetch_events(self, wallet: str, from_block: int = 0,
                           to_block: Optional[int] = None) -> list[CTFEvent]:
        """Every CTF event that involves ``wallet``, decoded.

        The wallet appears in different indexed positions per event (stakeholder
        for split/merge/redeem, ``from`` or ``to`` for transfers), so each is
        queried with the wallet pinned to the right topic slot and the results
        merged.
        """
        if to_block is None:
            to_block = await self.latest_block()
        wtopic = _addr_topic(wallet)

        # topic0 for each kind, so we can pin the right filter per event.
        by_kind = {kind: t0 for t0, kind in self._kinds.items()}
        queries = [
            # split / merge / redeem: wallet is the first indexed arg (topic1).
            [by_kind["PositionSplit"], wtopic],
            [by_kind["PositionsMerge"], wtopic],
            [by_kind["PayoutRedemption"], wtopic],
            # transfers: wallet as sender (from = topic2) or receiver (to = t3).
            [by_kind["TransferSingle"], None, wtopic],
            [by_kind["TransferSingle"], None, None, wtopic],
            [by_kind["TransferBatch"], None, wtopic],
            [by_kind["TransferBatch"], None, None, wtopic],
        ]

        seen: set[tuple] = set()
        events: list[CTFEvent] = []
        for topics in queries:
            for log in await self._get_logs(topics, from_block, to_block):
                key = (log.get("transactionHash"), log.get("logIndex"))
                if key in seen:
                    continue
                seen.add(key)
                event = decode_log(log, self._kinds)
                if event is not None:
                    events.append(event)
        return events

    async def summarise(self, wallet: str, from_block: int = 0) -> CTFSummary:
        events = await self.fetch_events(wallet, from_block=from_block)
        return reconstruct(wallet, events)
