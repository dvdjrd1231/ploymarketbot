"""Public information streams. Signals requiring corroboration, never truth.

The brief's rule is the whole design: **never treat social posts as truth.**
So a social item enters the store at the bottom of the confirmation ladder and
can only climb it by corroboration from INDEPENDENT sources:

    RUMOR             one source, low reliability
    UNCONFIRMED       one source, or several that are not independent
    MULTI_SOURCE      two or more independent sources carry the same entities
    OFFICIAL          an official body said it
    MARKET_PRICED     the market has already moved on it

"Independent" is doing real work there. Ten accounts reposting one claim is one
source, not ten, and counting it as ten is precisely how a rumour becomes a
consensus. `_independent_sources` counts distinct *source names*, and
propagation through the same source is treated as repetition.

What is measured, per the brief: information velocity, source repetition,
independent confirmation, official confirmation, contradiction, market
reaction and wallet reaction. Each is a number with a definition, not a
sentiment score.

**Nothing here dials out unless `collectors.enabled` and `social_feeds` are
both set.** Unconfigured means NOT_CONFIGURED, which the evidence layer
distinguishes from "checked and found nothing".
"""

from __future__ import annotations

import hashlib
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ..portfolio.correlation import salient_entities
from .base import Collector, CollectorRun, http_text
from .collectors import SOURCE_CLASSES, _strip_html, _ts

CONFIRMATION_LADDER = ("RUMOR", "UNCONFIRMED", "MULTI_SOURCE", "OFFICIAL",
                       "MARKET_PRICED")


@dataclass
class PropagationReport:
    entity: str = ""
    items: int = 0
    independent_sources: int = 0
    repetitions: int = 0
    official: int = 0
    first_seen_ts: int = 0
    last_seen_ts: int = 0
    velocity_per_hour: float = 0.0
    contradicted: bool = False
    confirmation: str = "RUMOR"
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class SocialCollector(Collector):
    """Ingests public streams exposed as RSS/Atom.

    Only feed formats are supported, deliberately. Platform APIs need
    credentials and carry terms that vary by jurisdiction and account; a feed
    URL the operator supplies is theirs to supply. Configure with
    `collectors.social_feeds` as (url, source_name) pairs.
    """

    name = "social"
    requires_config = ("social_feeds",)

    def _run(self, run: CollectorRun) -> None:
        now = int(time.time())
        items, errs = [], 0
        for feed in self.st.collectors.social_feeds:
            url, source_name = (feed[0], feed[1]) if isinstance(
                feed, (list, tuple)) else (str(feed), str(feed))
            text, err = http_text(
                url, timeout=self.st.collectors.http_timeout_secs)
            if err or not text:
                errs += 1
                continue
            try:
                items.extend(self._parse(text, source_name, now))
            except Exception:                                 # noqa: BLE001
                errs += 1

        if items:
            run.rows = self.store.insert("news_items", items, source=self.name)
        self._promote(now, run)
        span = self.store.history_span_days("news_items", "capture_ts")
        run.detail = (f"{run.rows} social item(s) from "
                      f"{len(self.st.collectors.social_feeds)} feed(s), "
                      f"{errs} error(s); {span}d of history")
        if errs and not items:
            run.status = "ERROR"
            run.error = f"{errs} feed(s) failed, none succeeded"
        run.notes.append(
            "social items enter at RUMOR and can only be promoted by "
            "corroboration from a DIFFERENT source. They are never treated as "
            "fact and cannot drive a trade on their own.")

    def _parse(self, text: str, source_name: str, now: int) -> list:
        from xml.etree import ElementTree
        from .collectors import _text as node_text
        root = ElementTree.fromstring(text)
        entries = (root.findall(".//item")
                   or root.findall(".//{http://www.w3.org/2005/Atom}entry"))
        out = []
        for e in entries[:80]:
            title = node_text(e, "title") or ""
            if not title:
                continue
            body = _strip_html(node_text(e, "description")
                               or node_text(e, "summary") or "")[:1200]
            link = node_text(e, "link") or ""
            published = _ts(node_text(e, "pubDate")
                            or node_text(e, "published")
                            or node_text(e, "updated"))
            uid = hashlib.sha256(
                f"social|{source_name}|{title}|{link}".encode()).hexdigest()[:32]
            out.append({
                "uid": uid, "source_name": source_name,
                "source_class": "SOCIAL",
                "reliability": SOURCE_CLASSES["SOCIAL"],
                "title": title, "body": body, "url": link,
                "entities": salient_entities(title + " " + body, limit=12),
                "topics": [],
                # Always the bottom rung on arrival, whatever it claims.
                "confirmation": "RUMOR",
                "event_ts": 0,
                "ts": published or now, "capture_ts": now,
            })
        return out

    def _promote(self, now: int, run: CollectorRun) -> None:
        """Climb the ladder only on INDEPENDENT corroboration."""
        rows = self.store.query(
            "SELECT id, uid, source_name, source_class, entities, confirmation "
            "  FROM news_items WHERE capture_ts >= ?", (now - 24 * 3600,))
        if not rows:
            return
        import json as _json
        by_entity: dict = defaultdict(list)
        for r in rows:
            try:
                ents = _json.loads(r["entities"] or "[]")
            except Exception:                                 # noqa: BLE001
                continue
            for e in ents:
                by_entity[e].append(r)

        promoted = 0
        for entity, group in by_entity.items():
            sources = {g["source_name"] for g in group}
            official = any(g["source_class"] == "OFFICIAL" for g in group)
            if official:
                target = "OFFICIAL"
            elif len(sources) >= 2:
                target = "MULTI_SOURCE"
            else:
                continue
            for g in group:
                cur = g["confirmation"] or "RUMOR"
                if CONFIRMATION_LADDER.index(target) > \
                        CONFIRMATION_LADDER.index(cur):
                    self.store.conn().execute(
                        "UPDATE news_items SET confirmation=? WHERE id=?",
                        (target, g["id"]))
                    promoted += 1
        if promoted:
            self.store.conn().commit()
            run.notes.append(f"{promoted} item(s) promoted on independent "
                             f"corroboration")


def propagation(store, entity: str, *, as_of: int = 0,
                window_secs: int = 24 * 3600) -> PropagationReport:
    """Measure how a claim spread, and whether it was ever corroborated."""
    as_of = as_of or int(time.time())
    rows = store.query(
        "SELECT id, source_name, source_class, confirmation, capture_ts, title "
        "  FROM news_items WHERE capture_ts <= ? AND capture_ts > ? "
        "   AND entities LIKE ? ORDER BY capture_ts ASC",
        (as_of, as_of - window_secs, f'%"{entity}"%'))
    rep = PropagationReport(entity=entity, items=len(rows))
    if not rows:
        rep.note = "no items mention this entity in the window"
        return rep

    sources = [r["source_name"] for r in rows]
    rep.independent_sources = len(set(sources))
    rep.repetitions = len(sources) - rep.independent_sources
    rep.official = sum(1 for r in rows if r["source_class"] == "OFFICIAL")
    rep.first_seen_ts = int(rows[0]["capture_ts"])
    rep.last_seen_ts = int(rows[-1]["capture_ts"])
    span_h = max((rep.last_seen_ts - rep.first_seen_ts) / 3600.0, 1e-6)
    rep.velocity_per_hour = round(len(rows) / span_h, 3)

    if rep.official:
        rep.confirmation = "OFFICIAL"
    elif rep.independent_sources >= 2:
        rep.confirmation = "MULTI_SOURCE"
    elif rep.repetitions > 0:
        rep.confirmation = "UNCONFIRMED"
    else:
        rep.confirmation = "RUMOR"

    rep.note = (
        f"{len(rows)} item(s) from {rep.independent_sources} independent "
        f"source(s), {rep.repetitions} repetition(s), at "
        f"{rep.velocity_per_hour:.1f} items/hour. "
        + ("High velocity from ONE source is propagation, not corroboration — "
           "the same claim repeated is still one claim."
           if rep.independent_sources < 2 and len(rows) > 3 else
           "Corroborated by independent sources."
           if rep.independent_sources >= 2 else
           "Single source, low volume."))
    return rep
