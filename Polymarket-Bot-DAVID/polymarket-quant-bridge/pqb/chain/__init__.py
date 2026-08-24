"""On-chain reconstruction: the half of a wallet's story the order book can't see.

A Polymarket position leaves the order book in ways that are not CLOB trades —
splitting USDC into a complete set, merging a set back into USDC, and redeeming
winners at resolution — plus ERC-1155 transfers between wallets. None of these
appear in the trade tape, which is exactly why a wallet reconstructed from CLOB
trades alone looks like it "never sells" and "never loses". This package reads
those on-chain events so per-wallet P&L can be reconstructed honestly.
"""
