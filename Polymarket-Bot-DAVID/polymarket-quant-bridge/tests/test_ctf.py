"""CTF on-chain reconstruction (§6, acceptance 11.3).

Proves the decoding and cash-flow reconstruction are correct without any RPC or
web3: raw log dicts in, normalised events and a CLOB-vs-CTF P&L delta out. The
live fetch (an RPC endpoint) is the only part these tests cannot exercise, and
it is a thin JSON-RPC wrapper over this same, verified decoder.
"""

from __future__ import annotations

from pqb.chain.ctf import (CTFEvent, TruePnL, decode_log, reconstruct)


def _word(n: int) -> str:
    return format(n, "064x")


def _topic_addr(addr: str) -> str:
    return "0x" + addr[2:].rjust(64, "0")


USDC = 10 ** 6
WALLET = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
OTHER = "0x1111111111111111111111111111111111111111"
COND = "0x" + "12" * 32

# Synthetic topic0 -> kind map, so the decoder needs no keccak.
KINDS = {
    "0xaa" + "0" * 62: "PositionSplit",
    "0xbb" + "0" * 62: "PositionsMerge",
    "0xcc" + "0" * 62: "PayoutRedemption",
    "0xdd" + "0" * 62: "TransferSingle",
    "0xee" + "0" * 62: "TransferBatch",
}
T = {v: k for k, v in KINDS.items()}


def split(amount_usdc):
    return {"topics": [T["PositionSplit"], _topic_addr(WALLET),
                       "0x" + "00" * 32, COND],
            "data": "0x" + _word(0) + _word(96) + _word(amount_usdc * USDC),
            "blockNumber": "0x10"}


def merge(amount_usdc):
    return {"topics": [T["PositionsMerge"], _topic_addr(WALLET),
                       "0x" + "00" * 32, COND],
            "data": "0x" + _word(0) + _word(96) + _word(amount_usdc * USDC),
            "blockNumber": "0x11"}


def redeem(payout_usdc):
    return {"topics": [T["PayoutRedemption"], _topic_addr(WALLET),
                       "0x" + "00" * 32, "0x" + "00" * 32],
            "data": "0x" + _word(int(COND, 16)) + _word(96) + _word(payout_usdc * USDC),
            "blockNumber": "0x12"}


# -- decoding ----------------------------------------------------------------

def test_split_is_cash_out():
    e = decode_log(split(100), KINDS)
    assert e.kind == "PositionSplit"
    assert e.wallet == WALLET
    assert e.amount_usdc == 100.0
    assert e.direction == "out"


def test_merge_and_redeem_are_cash_in():
    m = decode_log(merge(40), KINDS)
    r = decode_log(redeem(30), KINDS)
    assert m.amount_usdc == 40.0 and m.direction == "in"
    assert r.amount_usdc == 30.0 and r.direction == "in"


def test_transfer_single_decodes_parties_and_value():
    log = {"topics": [T["TransferSingle"], _topic_addr("0x0"),
                      _topic_addr(WALLET), _topic_addr(OTHER)],
           "data": "0x" + _word(5) + _word(7 * USDC), "blockNumber": "0x1"}
    e = decode_log(log, KINDS)
    assert e.wallet == WALLET and e.counterparty == OTHER
    assert e.token_ids == ["5"] and e.token_amounts == [7.0]


def test_transfer_batch_decodes_arrays():
    data = (_word(64) + _word(64 + 32 * 3) + _word(2) + _word(5) + _word(6)
            + _word(2) + _word(1 * USDC) + _word(2 * USDC))
    log = {"topics": [T["TransferBatch"], _topic_addr("0x0"),
                      _topic_addr(WALLET), _topic_addr(OTHER)],
           "data": "0x" + data, "blockNumber": "0x2"}
    e = decode_log(log, KINDS)
    assert e.token_ids == ["5", "6"]
    assert e.token_amounts == [1.0, 2.0]


def test_unknown_topic_is_ignored():
    log = {"topics": ["0xffff", _topic_addr(WALLET)], "data": "0x"}
    assert decode_log(log, KINDS) is None


def test_a_malformed_log_returns_none_not_raises():
    assert decode_log({"topics": [T["PositionSplit"]], "data": "0x"}, KINDS) is None


# -- reconstruction & true P&L -----------------------------------------------

def test_reconstruct_folds_flows():
    events = [decode_log(split(100), KINDS), decode_log(merge(40), KINDS),
              decode_log(redeem(30), KINDS)]
    s = reconstruct(WALLET, events)
    assert s.split_paid == 100.0
    assert s.merge_received == 40.0
    assert s.redeem_received == 30.0
    # net on-chain = money back in minus money out = 40 + 30 - 100
    assert s.net_onchain_usdc == -30.0


def test_true_pnl_differs_from_clob_only():
    """The whole point of 11.3: the CLOB-only view is wrong, and by how much."""
    s = reconstruct(WALLET, [decode_log(merge(40), KINDS),
                             decode_log(redeem(30), KINDS),
                             decode_log(split(100), KINDS)])
    # CLOB shows big buys and no sells — looks like it "never sold".
    t = TruePnL(wallet=WALLET, clob_bought=100.0, clob_sold=0.0, ctf=s)
    assert t.clob_only_pnl == -100.0
    assert t.true_pnl == -130.0
    assert t.hidden == -30.0    # the on-chain flows the CLOB view missed


def test_events_for_other_wallets_are_not_counted():
    other_split = {"topics": [T["PositionSplit"], _topic_addr(OTHER),
                              "0x" + "00" * 32, COND],
                   "data": "0x" + _word(0) + _word(96) + _word(500 * USDC),
                   "blockNumber": "0x9"}
    s = reconstruct(WALLET, [decode_log(other_split, KINDS),
                             decode_log(split(10), KINDS)])
    assert s.split_paid == 10.0     # only the target wallet's split
