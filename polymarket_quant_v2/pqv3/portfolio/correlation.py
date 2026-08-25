"""Detecting that two "separate" positions are one bet.

The failure this prevents: holding "Team A wins", "Team A wins by 3+" and
"Team B loses" looks like three positions and three diversified risks. It is
one event with 3x the size, and at $100 of equity three 5% positions on the
same game is 15% of the account on one referee's decision.

Correlation here is **structural**, derived from what the markets are about,
not statistical. A 90-day sample gives far too few resolved co-observations to
estimate a reliable correlation matrix over thousands of markets, and a
correlation estimated from noise is worse than an honest structural rule
because it carries a number that invites trust.

Three signals, strongest first:

  1. same `event_id`            — the venue itself says these are one event
  2. shared salient entities    — extracted from the question text
  3. shared category + window   — weak; only ever downgrades a limit
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Words that appear in most questions and identify nothing.
_STOP = frozenset("""
will the a an of in on at to for by be is are was were and or not no yes any
if then than that this these those with from as it its he she they them who
whom which what when where how much many more most least less before after
during between above below up down over under win wins won lose loses lost
game match race election day week month year end ends ending close closes
market question outcome result resolve resolves resolved date time next last
first second third new
""".split())

_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9'\-\.]{1,}")


def salient_entities(question: str, limit: int = 8) -> list[str]:
    """Capitalised, non-stopword tokens — a crude proper-noun extractor.

    Crude on purpose. A named-entity model would be better and would also be a
    dependency, a download and a thing to keep current; the capitalisation
    heuristic gets team names, candidate names, countries and tickers, which is
    most of what Polymarket questions turn on.
    """
    if not question:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for i, tok in enumerate(_TOKEN.findall(question)):
        low = tok.lower().strip(".'-")
        if not low or low in _STOP or len(low) < 3:
            continue
        # Skip the leading word: sentence-initial capitalisation is not a name.
        if i > 0 and not tok[0].isupper():
            continue
        if low not in seen:
            seen.add(low)
            out.append(low)
        if len(out) >= limit:
            break
    return out


@dataclass
class CorrelationVerdict:
    correlated: bool
    strength: float                      # 0..1
    basis: str                           # event_id | entities | category | none
    shared: list = field(default_factory=list)
    key: str = ""                        # the bucket exposure accrues to

    def to_dict(self) -> dict:
        return {"correlated": self.correlated, "strength": round(self.strength, 3),
                "basis": self.basis, "shared": self.shared, "key": self.key}


def correlation_key(market_id: str, event_id: str = "",
                    question: str = "") -> str:
    """The bucket a position's exposure accrues to.

    Preference order matters: `event_id` is the venue's own grouping and is
    exact, entities are inferred, and the market id alone means "we found no
    grouping" — which must remain distinguishable from "we checked and it is
    independent".
    """
    if event_id:
        return f"event:{event_id}"
    ents = salient_entities(question, limit=2)
    if ents:
        return "ent:" + "+".join(sorted(ents))
    return f"mkt:{market_id}"


def compare(a_question: str, b_question: str, *, a_event: str = "",
            b_event: str = "", a_category: str = "",
            b_category: str = "") -> CorrelationVerdict:
    if a_event and a_event == b_event:
        return CorrelationVerdict(True, 1.0, "event_id", [a_event],
                                  f"event:{a_event}")

    ea, eb = set(salient_entities(a_question)), set(salient_entities(b_question))
    shared = sorted(ea & eb)
    if shared:
        # Jaccard rather than raw count: two questions sharing "trump" out of
        # twenty tokens each are far less related than two sharing it out of
        # three.
        union = len(ea | eb) or 1
        strength = len(shared) / union
        if strength >= 0.2:
            return CorrelationVerdict(True, min(1.0, strength * 1.5), "entities",
                                      shared, "ent:" + "+".join(shared[:2]))

    if a_category and a_category == b_category:
        return CorrelationVerdict(True, 0.15, "category", [a_category],
                                  f"cat:{a_category}")

    return CorrelationVerdict(False, 0.0, "none", [], "")


def aggregate_exposure(positions: list[dict]) -> dict:
    """Group open positions into true underlying exposures.

    Returns bucket -> {usdc, positions, questions}. The dashboard's PORTFOLIO
    tab renders this rather than the raw position list, because the raw list is
    exactly the view that makes three correlated bets look diversified.
    """
    buckets: dict = {}
    for p in positions:
        key = p.get("correlation_key") or correlation_key(
            p.get("market_id", ""), p.get("event_id", ""), p.get("question", ""))
        b = buckets.setdefault(key, {"usdc": 0.0, "positions": 0,
                                     "questions": [], "markets": []})
        b["usdc"] += float(p.get("size_usdc") or 0.0)
        b["positions"] += 1
        if p.get("question"):
            b["questions"].append(p["question"])
        if p.get("market_id"):
            b["markets"].append(p["market_id"])
    for b in buckets.values():
        b["usdc"] = round(b["usdc"], 4)
    return dict(sorted(buckets.items(), key=lambda kv: -kv[1]["usdc"]))
