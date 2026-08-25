"""Live capture for the four layers that have no history.

Order book, news, chain and market metadata cannot be backfilled from anything
in this repository. They can only be accumulated from the moment collection
starts. So each collector below writes to a table that begins empty, and every
consumer of those tables reports `history_days` from the store rather than
assuming coverage.

The honesty rule that makes this useful rather than decorative: a layer with no
rows returns `Availability.UNAVAILABLE`, and the gates treat that as a refusal
to trade rather than as a clean bill of health. A freshly installed V3 will
therefore decline microstructure- and news-dependent trades and say exactly
why, instead of trading on a spread of 0.00 and a calm news environment that
were never measured.
"""

from __future__ import annotations

import hashlib
import re
import time
from xml.etree import ElementTree

from ..portfolio.correlation import salient_entities
from .base import Collector, CollectorRun, http_json, http_text


# ---------------------------------------------------------------------------
# Market metadata
# ---------------------------------------------------------------------------
class MarketCollector(Collector):
    """Market/event/condition identifiers, categories and close times.

    Populates the `event_id` grouping that the correlation engine and the
    cross-market agent both depend on. Without it, two markets on the same
    game look independent and the portfolio limits do not bind.
    """

    name = "markets"

    def _run(self, run: CollectorRun) -> None:
        base = self.st.collectors.gamma_base.rstrip("/")
        rows, offset, pages = [], 0, 0
        while pages < 20:
            data, err = http_json(f"{base}/markets",
                                  params={"limit": 100, "offset": offset,
                                          "closed": "false"},
                                  timeout=self.st.collectors.http_timeout_secs)
            if err:
                run.status = "ERROR" if not rows else "OK"
                run.error = err
                break
            if not isinstance(data, list) or not data:
                break
            for m in data:
                rows.append(self._row(m))
            offset += len(data)
            pages += 1
            if len(data) < 100:
                break

        if rows:
            run.rows = self.store.insert("markets", rows, source=self.name,
                                         replace=True)
        run.detail = f"{run.rows} markets across {pages} page(s)"

    def _row(self, m: dict) -> dict:
        import json as _json
        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = _json.loads(outcomes)
            except Exception:                                  # noqa: BLE001
                outcomes = [outcomes]
        ev = m.get("events") or []
        event_id = str(ev[0].get("id")) if ev and isinstance(ev[0], dict) else ""
        return {
            "market_id": str(m.get("id") or m.get("conditionId") or ""),
            "condition_id": str(m.get("conditionId") or ""),
            "event_id": event_id,
            "question": m.get("question") or "",
            "category": m.get("category") or (
                ev[0].get("category") if ev and isinstance(ev[0], dict) else ""),
            "outcomes": outcomes or [],
            "created_ts": _ts(m.get("createdAt")),
            "close_ts": _ts(m.get("endDate")),
            "resolved_ts": _ts(m.get("closedTime")),
            "status": "CLOSED" if m.get("closed") else "OPEN",
        }


# ---------------------------------------------------------------------------
# Order book
# ---------------------------------------------------------------------------
class OrderBookCollector(Collector):
    """Periodic top-of-book snapshots.

    Snapshots, not a full event stream. A stream would be strictly better and
    would also require a persistent websocket, reconnect logic and a write path
    fast enough to keep up — none of which is worth building before there is
    evidence that depth features carry signal. Snapshots at 30s intervals are
    enough to measure spread, depth and imbalance, which is enough to test
    that.

    What snapshots CANNOT recover, and the state builder says so: queue
    position, iceberg detection, and any event shorter than the poll interval.
    """

    name = "order_book"

    def _run(self, run: CollectorRun) -> None:
        tokens = self._tokens()
        if not tokens:
            run.detail = ("no tokens to watch. Run `pqv3 sync-markets` first, "
                          "or scan once so the tape supplies active tokens.")
            return

        base = self.st.collectors.clob_base.rstrip("/")
        results = self.fetch_many([f"{base}/book?token_id={t}" for t in tokens])
        now = int(time.time())
        rows, errs = [], 0
        for token_id, (data, err) in zip(tokens, results):
            if err or not isinstance(data, dict):
                errs += 1
                continue
            row = self._snapshot(token_id, data, now)
            if row:
                rows.append(row)

        if rows:
            run.rows = self.store.insert("book_snapshots", rows, source=self.name)
        if errs and not rows:
            run.status = "ERROR"
            run.error = f"all {errs} book requests failed"
        span = self.store.history_span_days("book_snapshots")
        run.detail = (f"{run.rows} snapshots ({errs} errors); "
                      f"{span}d of book history accumulated")
        if span < self.st.collectors.min_history_days:
            run.notes.append(
                f"depth-dependent strategies stay gated until "
                f"{self.st.collectors.min_history_days}d of history exists")

    def _tokens(self) -> list:
        rows = self.store.query(
            "SELECT DISTINCT token_id FROM book_snapshots "
            " WHERE capture_ts > ? LIMIT 60", (int(time.time()) - 86_400,))
        watched = [r["token_id"] for r in rows]
        if watched:
            return watched
        # Bootstrap from the tape's most active markets.
        from ..core.source import HistoricalSource
        src = HistoricalSource(self.st)
        if not src.available:
            return []
        now = int(time.time())
        out = []
        for m in src.active_markets(now, 7 * 86_400, 30):
            toks = src.tokens_for_market(m["market_id"], now)
            out.extend(t["token_id"] for t in toks[:2])
            if len(out) >= 40:
                break
        return out

    def _snapshot(self, token_id: str, data: dict, now: int) -> dict | None:
        bids = _levels(data.get("bids"))
        asks = _levels(data.get("asks"))
        if not bids and not asks:
            return None
        best_bid = max((p for p, _ in bids), default=None)
        best_ask = min((p for p, _ in asks), default=None)
        bid_depth = sum(p * s for p, s in bids)
        ask_depth = sum(p * s for p, s in asks)
        mid = ((best_bid + best_ask) / 2
               if best_bid is not None and best_ask is not None else None)
        spread = (best_ask - best_bid
                  if best_bid is not None and best_ask is not None else None)
        total = bid_depth + ask_depth
        n = self.st.collectors.orderbook_top_levels
        return {
            "token_id": token_id,
            "market_id": str(data.get("market") or ""),
            "best_bid": best_bid, "best_ask": best_ask, "mid": mid,
            "spread": spread, "bid_depth": round(bid_depth, 4),
            "ask_depth": round(ask_depth, 4),
            "imbalance": round((bid_depth - ask_depth) / total, 5)
            if total > 0 else 0.0,
            "levels": ([[p, s, "B"] for p, s in sorted(bids, reverse=True)[:n]]
                       + [[p, s, "A"] for p, s in sorted(asks)[:n]]),
            "ts": now, "capture_ts": now,
        }


# ---------------------------------------------------------------------------
# News
# ---------------------------------------------------------------------------
# Reliability is CONFIGURED, not learned. Learning source reliability from
# market outcomes over 90 days would fit noise and would also create a feedback
# loop: sources that happened to agree with profitable trades would be
# up-weighted, which is how a system convinces itself a rumour mill is an
# oracle.
SOURCE_CLASSES = {
    "OFFICIAL": 1.00,     # government, regulator, court, official body
    "WIRE": 0.85,         # established newswire
    "MEDIA": 0.65,        # reputable reporting
    "SOCIAL": 0.25,       # public posts; corroboration required
    "UNKNOWN": 0.30,
}


class NewsCollector(Collector):
    """RSS/Atom ingest with the three-timestamp discipline.

    `event_ts` (it happened), `ts` (it was published), `capture_ts` (we saw
    it). The state builder filters on `capture_ts`, so a backtest can never use
    an item before we would have had it — the difference between publication
    and capture is often minutes and occasionally hours, and collapsing them is
    how a news backtest quietly acquires hindsight.

    Feeds are configured, not hard-coded: `collectors.news_feeds` takes
    `(url, source_name, source_class)` triples. With none configured this
    collector reports NOT_CONFIGURED and the news layer stays honestly empty.
    """

    name = "news"
    requires_config = ("news_feeds",)

    def _run(self, run: CollectorRun) -> None:
        now = int(time.time())
        items, errs = [], 0
        for feed in self.st.collectors.news_feeds:
            url, source_name, source_class = _feed_triple(feed)
            text, err = http_text(
                url, timeout=self.st.collectors.http_timeout_secs)
            if err or not text:
                errs += 1
                continue
            try:
                items.extend(self._parse(text, source_name, source_class, now))
            except Exception:                                  # noqa: BLE001
                errs += 1

        if items:
            run.rows = self.store.insert("news_items", items, source=self.name)
            self._link(run, now)
        if errs and not items:
            run.status = "ERROR"
            run.error = f"{errs} feed(s) failed, none succeeded"
        span = self.store.history_span_days("news_items", "capture_ts")
        run.detail = (f"{run.rows} new items from "
                      f"{len(self.st.collectors.news_feeds)} feed(s), "
                      f"{errs} error(s); {span}d of news history")

    def _parse(self, text: str, source_name: str, source_class: str,
               now: int) -> list:
        root = ElementTree.fromstring(text)
        entries = (root.findall(".//item")
                   or root.findall(".//{http://www.w3.org/2005/Atom}entry"))
        out = []
        for e in entries[:60]:
            title = _text(e, "title") or ""
            if not title:
                continue
            link = _text(e, "link") or ""
            body = (_text(e, "description") or _text(e, "summary") or "")[:2000]
            published = _ts(_text(e, "pubDate") or _text(e, "published")
                            or _text(e, "updated"))
            uid = hashlib.sha256(
                f"{source_name}|{title}|{link}".encode()).hexdigest()[:32]
            out.append({
                "uid": uid, "source_name": source_name,
                "source_class": source_class,
                "reliability": SOURCE_CLASSES.get(source_class, 0.30),
                "title": title, "body": _strip_html(body), "url": link,
                "entities": salient_entities(title + " " + body, limit=12),
                "topics": [],
                # Confirmation starts at the floor implied by the source class.
                # Promotion to MULTI_SOURCE happens in `_link` when independent
                # outlets carry the same entities; nothing is OFFICIAL unless
                # its source is.
                "confirmation": ("OFFICIAL" if source_class == "OFFICIAL"
                                 else "UNCONFIRMED"),
                # event_ts is only knowable when the feed states it. Guessing
                # it from publication time would erase the very distinction
                # this table exists to preserve.
                "event_ts": 0,
                "ts": published or now, "capture_ts": now,
            })
        return out

    def _link(self, run: CollectorRun, now: int) -> None:
        """Link items to markets by shared salient entities.

        Crude and honest. `method` records how the link was made so a
        downstream consumer can discount it; `relevance` is the Jaccard overlap
        rather than a binary flag, so a market sharing one common word does not
        rank alongside one sharing three names.
        """
        recent = self.store.query(
            "SELECT id, title, entities FROM news_items WHERE capture_ts>=?",
            (now - 3600,))
        markets = self.store.query(
            "SELECT market_id, question FROM markets WHERE status='OPEN' "
            "LIMIT 3000")
        if not recent or not markets:
            run.notes.append("no market metadata to link against; run "
                             "`pqv3 sync-markets`")
            return
        import json as _json
        mkt_ents = [(m["market_id"], set(salient_entities(m["question"] or "")))
                    for m in markets]
        links = []
        for item in recent:
            try:
                ents = set(_json.loads(item["entities"] or "[]"))
            except Exception:                                  # noqa: BLE001
                continue
            if not ents:
                continue
            for market_id, ments in mkt_ents:
                if not ments:
                    continue
                shared = ents & ments
                if not shared:
                    continue
                rel = len(shared) / len(ents | ments)
                if rel < 0.08:
                    continue
                links.append({
                    "news_id": item["id"], "market_id": market_id,
                    "relevance": round(rel, 4),
                    # Direction is NOT inferred here. Sentiment on a headline
                    # says nothing about which side of a binary market it
                    # favours, and a wrong sign is worse than no sign.
                    "direction": 0.0,
                    "magnitude": round(min(1.0, rel * 2), 4),
                    "method": f"entity_jaccard:{','.join(sorted(shared)[:3])}",
                    "ts": now, "capture_ts": now})
        if links:
            self.store.insert("news_market_links", links, source=self.name,
                              replace=True)
            run.notes.append(f"{len(links)} news->market links by entity overlap")
            run.notes.append(
                "link direction is 0.0 by design: headline sentiment does not "
                "determine which side of a binary market benefits. Directional "
                "news signals require per-market rules, not a sentiment score.")


# ---------------------------------------------------------------------------
# Blockchain
# ---------------------------------------------------------------------------
class ChainCollector(Collector):
    """USDC / conditional-token movement for watched wallets.

    Requires an RPC endpoint. With none configured this reports NOT_CONFIGURED
    and the blockchain layer stays empty, which is the correct state — an
    empty chain layer causes Agent 3 to abstain rather than to conclude that
    nothing is happening on chain.
    """

    name = "chain"
    requires_config = ("chain_rpc",)

    def _run(self, run: CollectorRun) -> None:
        rpc = self.st.collectors.chain_rpc
        head, err = self._rpc(rpc, "eth_blockNumber", [])
        if err:
            run.status = "ERROR"
            run.error = err
            return
        try:
            head_n = int(str(head), 16)
        except Exception:                                      # noqa: BLE001
            run.status = "ERROR"
            run.error = f"unexpected block number: {head!r}"
            return

        last = int(self.store.get_meta("chain_last_block", "0") or 0)
        # First run starts near the head rather than at genesis: backfilling
        # the whole chain through a public RPC is not a thing that finishes.
        start = last + 1 if last else head_n - 500
        if start > head_n:
            run.detail = f"already synced to block {head_n}"
            return
        end = min(head_n, start + 1999)

        logs, err = self._rpc(rpc, "eth_getLogs", [{
            "fromBlock": hex(start), "toBlock": hex(end)}])
        if err:
            run.status = "ERROR"
            run.error = err
            return

        from .chain_decode import decode_many, interpret
        rows, dstats = decode_many((logs or [])[:5000], int(time.time()))
        if rows:
            run.rows = self.store.insert("chain_events", rows, source=self.name)
        self.store.set_meta("chain_last_block", str(end))
        run.detail = (f"blocks {start}-{end} of {head_n}; "
                      f"{run.rows} decoded event(s)")
        run.notes.append(dstats["note"])
        if rows:
            run.notes.append(str(interpret(rows).get("interpretation", "")))

    def _rpc(self, url: str, method: str, params: list):
        import json as _json
        import urllib.request
        body = _json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                            "params": params}).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "polymarket-quant-bridge-v3/1.0"})
        try:
            with urllib.request.urlopen(
                    req, timeout=self.st.collectors.http_timeout_secs) as r:
                data = _json.loads(r.read().decode())
            if "error" in data:
                return None, str(data["error"])[:200]
            return data.get("result"), ""
        except Exception as e:                                # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
ALL_COLLECTORS = (MarketCollector, OrderBookCollector, NewsCollector,
                  ChainCollector)


def run_all(st, store) -> list:
    from .settled_ts import SettlementTimeCollector
    from .social import SocialCollector
    out = []
    for cls in (*ALL_COLLECTORS, SocialCollector, SettlementTimeCollector):
        out.append(cls(st, store).run())
    return out


# ---------------------------------------------------------------------------
def _levels(raw) -> list:
    out = []
    for lv in (raw or []):
        try:
            if isinstance(lv, dict):
                out.append((float(lv["price"]), float(lv["size"])))
            else:
                out.append((float(lv[0]), float(lv[1])))
        except Exception:                                      # noqa: BLE001
            continue
    return out


def _feed_triple(feed) -> tuple:
    if isinstance(feed, (list, tuple)):
        url = feed[0]
        name = feed[1] if len(feed) > 1 else url
        cls = feed[2] if len(feed) > 2 else "UNKNOWN"
        return url, name, (cls if cls in SOURCE_CLASSES else "UNKNOWN")
    return str(feed), str(feed), "UNKNOWN"


def _text(elem, tag: str) -> str:
    for t in (tag, "{http://www.w3.org/2005/Atom}" + tag):
        node = elem.find(t)
        if node is not None:
            return (node.text or node.get("href") or "").strip()
    return ""


_TAG = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _TAG.sub(" ", s or "").replace("&nbsp;", " ").strip()


def _ts(value) -> int:
    from .settled_ts import _parse_ts
    v = _parse_ts(value)
    if v:
        return v
    if not value:
        return 0
    # RFC-822, which is what most RSS feeds emit.
    try:
        from email.utils import parsedate_to_datetime
        return int(parsedate_to_datetime(str(value)).timestamp())
    except Exception:                                          # noqa: BLE001
        return 0
