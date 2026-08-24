"""
Market segmentation: treating one wallet as several strategies, not one.

David's observation, and it is a good one: a wallet that ranks well is almost
certainly **not** running a single algorithm. It is running one method for
sports, another for politics, another for crypto — different markets have
different dynamics, so a serious operator uses different rules for each. Scoring
the whole wallet as one strategy averages those together and hides what actually
works.

So before any exit rule is fitted, positions are split by the market's category.
The categoriser is deliberately simple and transparent — keyword matching on the
market question — because a category it cannot explain is worse than one it can.
Everything it cannot place lands in ``other`` rather than being forced into a
bucket it does not belong in.

This is a coarse cut, not a taxonomy. It exists so the playbook can answer
"what is this wallet's *sports* exit rule, separately from its *crypto* one,"
which is exactly the question that was asked.
"""

from __future__ import annotations

import re

# Category -> keywords that place a market in it. Ordering of the dict is the
# tie-break priority when a question hits more than one category equally: the
# earlier category wins. Crypto and economics come before sports/politics
# because their keywords are more specific and less likely to be incidental.
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "crypto": (
        "bitcoin", "btc", "ethereum", "eth", "solana", "sol", "crypto",
        "dogecoin", "doge", "xrp", "ripple", "cardano", "binance", "coinbase",
        "memecoin", "altcoin", "stablecoin", "nft", "satoshi", "halving",
        "hit $", "reach $", "flip", "market cap",
    ),
    "economics": (
        "fed", "fomc", "interest rate", "rate cut", "rate hike", "inflation",
        "cpi", "gdp", "recession", "unemployment", "jobs report", "payroll",
        "tariff", "ecb", "treasury", "yield", "s&p 500", "nasdaq", "dow",
    ),
    "politics": (
        "election", "president", "presidential", "senate", "congress", "vote",
        "governor", "primary", "parliament", "prime minister", "democrat",
        "republican", "gop", "cabinet", "nominee", "nomination", "impeach",
        "referendum", "poll", "approval rating", "trump", "biden", "harris",
        "putin", "zelensky", "ceasefire", "resign", "elected",
    ),
    "sports": (
        "nba", "nfl", "mlb", "nhl", "ncaa", "soccer", "football", "basketball",
        "baseball", "hockey", "premier league", "champions league", "la liga",
        "serie a", "bundesliga", "ufc", "boxing", "tennis", "world cup",
        "super bowl", "playoffs", "playoff", "finals", "mvp", "f1", "formula 1",
        "grand prix", "golf", "pga", "cricket", "olympic", " vs ", " vs.",
        "beat the", "win the game", "make the", "defeat",
    ),
    "entertainment": (
        "oscar", "academy award", "grammy", "emmy", "box office", "movie",
        "film", "album", "spotify", "billboard", "netflix", "rotten tomatoes",
        "taylor swift", "celebrity", "tiktok", "twitter", "tweet", "elon",
        "time person of the year", "release", "trailer",
    ),
}

# The bucket everything unmatched falls into. Named, not silent, so a wallet
# whose activity is mostly here is visibly under-categorised rather than
# looking like it trades one clean segment.
OTHER = "other"

CATEGORIES = (*_KEYWORDS.keys(), OTHER)

_WORD = re.compile(r"[a-z0-9$&. ]+")


def classify(question: str) -> str:
    """Place one market in a category from its question text.

    Returns the category with the most keyword hits; ties break by the priority
    order of ``_KEYWORDS`` above. A question that matches nothing is ``other`` —
    never force-fit, because a wrong category quietly corrupts every rule fitted
    to the segment it lands in.
    """
    text = f" {(question or '').lower().strip()} "
    if not text.strip():
        return OTHER

    best_cat = OTHER
    best_hits = 0
    for category, words in _KEYWORDS.items():
        hits = sum(1 for w in words if w in text)
        if hits > best_hits:
            best_cat, best_hits = category, hits
    return best_cat


def split(positions, key=lambda p: p.get("question", "")) -> dict[str, list]:
    """Group an iterable of positions by market category.

    ``key`` extracts the question text from each item, so this works for both
    the dict-shaped positions the playbook consumes and the ``Position`` objects
    the reverse-engineering layer produces (pass ``key=lambda p: p.question``).
    """
    buckets: dict[str, list] = {}
    for position in positions:
        buckets.setdefault(classify(key(position)), []).append(position)
    return buckets
