"""The normalised event dataset, and an honest account of what is missing.

Part 4 of the brief lists ~18 fields per wallet event. The store carries some
of them and not others, and the difference matters more than the list does: a
feature reconstructed from a field that is not really there is worse than a
missing feature, because it looks like evidence.

So this module does two things. It reads the wallet tape into one normalised
chronological shape, and it AUDITS what that shape can actually support —
per field, with counts, from the real database rather than from the schema.
`audit()` is what every report prints at the top, and it is what justifies
marking a downstream feature UNAVAILABLE instead of inventing it.

Everything here is read-only (`mode=ro`). This subsystem never writes to the
intel store.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# Field-by-field verdict on Part 4's list. AVAILABLE means the store carries
# it for effectively every row; PARTIAL means for some rows and the count is
# reported; UNAVAILABLE means it is not in the data at all and any feature
# depending on it is skipped rather than approximated.
FIELD_STATUS: dict[str, tuple[str, str]] = {
    "wallet": ("AVAILABLE", "wallet_trades.wallet"),
    "condition_id": ("AVAILABLE", "wallet_trades.market_id"),
    "market_title": ("AVAILABLE", "wallet_trades.question"),
    "market_category": (
        "UNAVAILABLE",
        "the wallet tape carries no category; Gamma metadata is fetched live "
        "for the tracked universe only and is not persisted per historical "
        "trade. Category analysis falls back to a keyword classification of "
        "the question text, which is reported AS a heuristic and never as "
        "ground truth."),
    "outcome": ("AVAILABLE", "wallet_trades.outcome + token_id"),
    "side": ("AVAILABLE", "wallet_trades.side (BUY/SELL, explicit)"),
    "timestamp": ("AVAILABLE", "wallet_trades.ts (unix seconds, 1s grain)"),
    "price": ("AVAILABLE", "wallet_trades.price"),
    "shares": (
        "AVAILABLE",
        "wallet_trades.size is an EXPLICIT share count, so inventory never "
        "has to be inferred from cash/price"),
    "cash": ("AVAILABLE", "wallet_trades.usdc"),
    "tx_id": ("AVAILABLE", "wallet_trades.tx"),
    "resolution_result": (
        "PARTIAL",
        "resolutions.price exists for the settled tokens the sweep has "
        "reached; every P&L number that needs settlement is reported on that "
        "sample with its own count"),
    "redeem_events": (
        "PARTIAL",
        "REDEEM/MERGE/SPLIT are collected by `pqb activity` from the Data "
        "API's /activity feed, which /trades does not carry. Coverage is "
        "whatever that backfill has reached; the census reports it."),
    "resolution_timestamp": (
        "UNAVAILABLE",
        "resolutions.settled_ts is 0 for every row in this store — the source "
        "record carried no settlement moment, and resolutions.ts is OUR "
        "polling time, not the market's. Time-to-resolution features are "
        "therefore built from the market's last observed activity and are "
        "labelled as an approximation"),
    "best_bid": ("PARTIAL", "research_rows quotes, ~2.5k tokens, days not "
                            "months"),
    "best_ask": ("PARTIAL", "research_rows quotes, same coverage"),
    "liquidity_depth": ("PARTIAL", "research_rows bid_depth/ask_depth, same "
                                   "coverage"),
    "estimated_slippage": (
        "MODELLED",
        "not observed. The execution model charges it explicitly under three "
        "named assumptions rather than pretending it was measured"),
    "fees": (
        "MODELLED",
        "Polymarket publishes no per-fill fee today, so fees are charged from "
        "config rather than read; the report says so"),
    "block_timestamp": ("UNAVAILABLE", "not captured by the Data API feed"),
}


@dataclass(frozen=True)
class WalletEvent:
    """One normalised wallet trade. Immutable: the tape is a record."""

    wallet: str
    market_id: str
    token_id: str
    outcome: str
    side: str                 # BUY | SELL
    ts: float
    price: float
    shares: float
    usdc: float
    question: str = ""
    tx: str = ""

    @property
    def signed_shares(self) -> float:
        """BUY positive, SELL negative — the only correct way to accumulate.

        Part 4 is explicit and the reason is a real failure mode: treating
        gross purchases as inventory makes a wallet that bought 100 and sold
        95 look like it holds 100, which inverts the inventory ratio the whole
        classifier turns on.
        """
        return self.shares if self.side.upper() == "BUY" else -self.shares

    @property
    def signed_cash(self) -> float:
        """Cash OUT is positive. A sale returns cash and reduces cost basis."""
        return self.usdc if self.side.upper() == "BUY" else -self.usdc

    @property
    def is_buy(self) -> bool:
        return self.side.upper() == "BUY"


def _connect(path: str | Path) -> Optional[sqlite3.Connection]:
    path = Path(path)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def load_events(intel_path: str | Path, wallets: Optional[Iterable[str]] = None,
                since_ts: float = 0.0, until_ts: float = 0.0,
                min_shares: float = 0.0) -> list[WalletEvent]:
    """The whole tape, normalised and chronological.

    Sorted by `(wallet, market, ts, id)` rather than by `ts` alone: the id
    breaks ties inside a one-second timestamp, and a one-second grain over a
    tape where a wallet can place both legs of a rebalance in the same second
    is exactly where "which side did it buy FIRST" gets decided. Sorting by a
    stable, monotonic insertion id makes that answer deterministic instead of
    dependent on SQLite's row order.
    """
    conn = _connect(intel_path)
    if conn is None:
        return []
    # `side != ''` already excludes non-trade lifecycle events, but the
    # event_type filter says so explicitly: an episode is built from TRADES,
    # and a redemption is an event ABOUT the episode rather than a leg of it.
    clauses = ["market_id != ''", "token_id != ''", "side != ''",
               "COALESCE(event_type, 'TRADE') = 'TRADE'"]
    params: list = []
    if wallets:
        addresses = [str(w).lower() for w in wallets]
        clauses.append(f"lower(wallet) IN ({','.join('?' * len(addresses))})")
        params.extend(addresses)
    if since_ts:
        clauses.append("ts >= ?")
        params.append(float(since_ts))
    if until_ts:
        clauses.append("ts <= ?")
        params.append(float(until_ts))
    if min_shares > 0:
        clauses.append("size >= ?")
        params.append(float(min_shares))
    sql = ("SELECT wallet, market_id, token_id, outcome, side, ts, price, "
           "size, usdc, question, tx FROM wallet_trades WHERE "
           + " AND ".join(clauses)
           + " ORDER BY wallet, market_id, ts, id")
    try:
        rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()
    return [WalletEvent(
        wallet=str(r["wallet"]).lower(), market_id=str(r["market_id"]),
        token_id=str(r["token_id"]), outcome=str(r["outcome"] or ""),
        side=str(r["side"]).upper(), ts=float(r["ts"]),
        price=float(r["price"] or 0.0), shares=float(r["size"] or 0.0),
        usdc=float(r["usdc"] or 0.0), question=str(r["question"] or ""),
        tx=str(r["tx"] or "")) for r in rows]


@dataclass
class DataAudit:
    """What the store can actually support, measured rather than assumed."""

    trades: int = 0
    wallets: int = 0
    markets: int = 0
    tokens: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    buys: int = 0
    sells: int = 0
    with_explicit_shares: int = 0
    two_token_markets: int = 0
    settled_tokens: int = 0
    settled_markets: int = 0
    quote_tokens: int = 0
    quote_first_ts: float = 0.0
    quote_last_ts: float = 0.0
    fields: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    @property
    def span_days(self) -> float:
        return (self.last_ts - self.first_ts) / 86_400.0 if self.last_ts else 0.0

    @property
    def quote_span_days(self) -> float:
        return ((self.quote_last_ts - self.quote_first_ts) / 86_400.0
                if self.quote_last_ts else 0.0)

    def to_dict(self) -> dict:
        return {
            "trades": self.trades, "wallets": self.wallets,
            "markets": self.markets, "tokens": self.tokens,
            "spanDays": round(self.span_days, 2),
            "buys": self.buys, "sells": self.sells,
            "explicitShareCoverage": (
                round(self.with_explicit_shares / self.trades, 4)
                if self.trades else 0.0),
            "twoTokenMarkets": self.two_token_markets,
            "settledTokens": self.settled_tokens,
            "settledMarkets": self.settled_markets,
            "quoteTokens": self.quote_tokens,
            "quoteSpanDays": round(self.quote_span_days, 2),
            "fields": {k: {"status": v[0], "note": v[1]}
                       for k, v in self.fields.items()},
            "warnings": list(self.warnings),
        }


def audit(intel_path: str | Path) -> DataAudit:
    """Measure the store. Every research report opens with this.

    The warnings are the important half. They are the sentences that stop a
    reader treating a P&L number computed over 40 settled episodes as though
    it were computed over 20,000.
    """
    out = DataAudit(fields=dict(FIELD_STATUS))
    conn = _connect(intel_path)
    if conn is None:
        out.warnings.append(f"intel store not found at {intel_path}")
        return out
    try:
        row = conn.execute(
            "SELECT COUNT(*) n, MIN(ts) lo, MAX(ts) hi, "
            "COUNT(DISTINCT wallet) w, COUNT(DISTINCT market_id) m, "
            "COUNT(DISTINCT token_id) t, "
            "SUM(CASE WHEN side='BUY' THEN 1 ELSE 0 END) buys, "
            "SUM(CASE WHEN side='SELL' THEN 1 ELSE 0 END) sells, "
            "SUM(CASE WHEN size > 0 THEN 1 ELSE 0 END) sized "
            "FROM wallet_trades").fetchone()
        out.trades = int(row["n"] or 0)
        out.first_ts = float(row["lo"] or 0.0)
        out.last_ts = float(row["hi"] or 0.0)
        out.wallets = int(row["w"] or 0)
        out.markets = int(row["m"] or 0)
        out.tokens = int(row["t"] or 0)
        out.buys = int(row["buys"] or 0)
        out.sells = int(row["sells"] or 0)
        out.with_explicit_shares = int(row["sized"] or 0)
        out.two_token_markets = int(conn.execute(
            "SELECT COUNT(*) n FROM (SELECT market_id FROM wallet_trades "
            "WHERE market_id != '' GROUP BY market_id "
            "HAVING COUNT(DISTINCT token_id) >= 2)").fetchone()["n"] or 0)
        settled = conn.execute(
            "SELECT COUNT(*) t, COUNT(DISTINCT market_id) m "
            "FROM resolutions").fetchone()
        out.settled_tokens = int(settled["t"] or 0)
        out.settled_markets = int(settled["m"] or 0)
        quotes = conn.execute(
            "SELECT COUNT(DISTINCT token_id) t, MIN(ts) lo, MAX(ts) hi "
            "FROM research_rows").fetchone()
        out.quote_tokens = int(quotes["t"] or 0)
        out.quote_first_ts = float(quotes["lo"] or 0.0)
        out.quote_last_ts = float(quotes["hi"] or 0.0)
    except sqlite3.Error as exc:
        out.warnings.append(f"audit query failed: {exc}")
        return out
    finally:
        conn.close()

    if out.span_days < 180:
        out.warnings.append(
            f"the tape spans {out.span_days:.0f} days. A chronological "
            "development / validation / holdout split over this window gives "
            "each period weeks, not years, and no result here should be read "
            "as evidence about a market regime the window does not contain.")
    if out.settled_markets < 500:
        out.warnings.append(
            f"only {out.settled_markets} markets have a recorded settlement. "
            "P&L held to settlement is therefore a SMALL sub-sample; every "
            "such number is reported with its own count and is never merged "
            "with mark-to-market exits.")
    if out.quote_span_days < 30:
        out.warnings.append(
            f"order-book history covers {out.quote_tokens} tokens over "
            f"{out.quote_span_days:.1f} days. Most fills are priced from the "
            "TRADE TAPE (a print, not a quote); the execution report states "
            "the mix and the conservative assumption charges a full assumed "
            "spread on top wherever the book was unavailable.")
    if out.trades and out.with_explicit_shares / out.trades > 0.999:
        out.fields["shares"] = (
            "AVAILABLE",
            f"explicit for {out.with_explicit_shares:,} of {out.trades:,} "
            "rows — inventory is exact, never inferred from cash/price")
    return out
