"""
Which wallets are actually independent? (spec point 10)

Consensus is only evidence if the voters thought for themselves. Five wallets
buying the same outcome is five times the evidence *if* they reached that view
separately — and one piece of evidence counted five times if four of them are
copying the first, or all five are reacting to the same headline.

The engine could not previously tell those apart. Measured on the estimator,
three wallets buying moved the implied probability from 0.49 to 0.72. If they
were one wallet and two followers, that is a 23-point move on a single opinion
— a false edge appearing exactly when convergence fires, which is precisely
when the engine is most inclined to act.

Correlation is inferred from behaviour rather than assumed: wallets that keep
buying the same tokens shortly after one another are treated as one voice.
That is deliberately a *behavioural* test, not a claim about identity — copy
bots, shared signal groups and people watching the same alert channel all show
the same footprint, and for our purposes they are the same problem.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Iterable, Optional

# Two buys of the same token inside this window count as "together". Long
# enough to catch a copier reacting within minutes, short enough that two
# people independently reaching the same view a day apart are not merged.
FOLLOW_WINDOW_SECONDS = 3_600

# Shared tokens needed before a pair is judged at all. Below this, overlap is
# chance: two wallets both trading the biggest market of the day says nothing.
MIN_SHARED_TOKENS = 3

# Overlap above which a pair is treated as one voice. 0.5 means half of the
# smaller wallet's activity shadows the other's.
CORRELATED_AT = 0.5

# Ceiling on how many wallets are pair-compared. See build_clusters: the
# comparison is quadratic, and the trading loop cannot wait on it.
MAX_CLUSTERED_WALLETS = 1_500


@dataclass
class Cluster:
    """A set of wallets that behave as one voice."""

    members: set[str] = field(default_factory=set)
    leader: str = ""              # earliest mover, by median timing

    def __len__(self) -> int:
        return len(self.members)


def _token_times(rows: Iterable[dict]) -> dict[str, dict[str, int]]:
    """wallet -> {token: first buy time}. First buys only.

    Adding to a position already held says nothing about who moved first, so
    only the initial entry counts toward a follow relationship.
    """
    out: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for row in rows:
        if str(row.get("side") or "BUY").upper() != "BUY":
            continue
        wallet = str(row.get("wallet") or "").lower()
        token = str(row.get("token_id") or "")
        ts = int(row.get("ts") or 0)
        if not wallet or not token:
            continue
        seen = out[wallet].get(token)
        if seen is None or ts < seen:
            out[wallet][token] = ts
    return out


def overlap(a: dict[str, int], b: dict[str, int],
            window: int = FOLLOW_WINDOW_SECONDS) -> float:
    """How much of the smaller wallet's activity shadows the other's, 0-1.

    Normalised by the SMALLER wallet on purpose. A prolific wallet and a
    copier that mirrors ten of its trades are correlated from the copier's
    point of view, and that is the direction that matters — the copier adds no
    information. Normalising by the larger would hide exactly that case.
    """
    shared = set(a) & set(b)
    if len(shared) < MIN_SHARED_TOKENS:
        return 0.0
    together = sum(1 for token in shared if abs(a[token] - b[token]) <= window)
    smaller = min(len(a), len(b))
    return together / smaller if smaller else 0.0


def build_clusters(rows: Iterable[dict],
                   window: int = FOLLOW_WINDOW_SECONDS,
                   threshold: float = CORRELATED_AT) -> dict[str, Cluster]:
    """Group wallets that behave as one voice. Returns wallet -> cluster."""
    times = _token_times(rows)
    wallets = [w for w, tokens in times.items()
               if len(tokens) >= MIN_SHARED_TOKENS]

    # Comparing every pair is quadratic: 1,700 wallets is 1.4M comparisons and
    # about 1.6s, but 5,000 would be 12.5M and would stall the trading loop.
    # Only the most active wallets are worth clustering — a wallet that trades
    # rarely cannot be somebody's copy engine, and cannot contribute much
    # consensus either.
    if len(wallets) > MAX_CLUSTERED_WALLETS:
        wallets.sort(key=lambda w: len(times[w]), reverse=True)
        wallets = wallets[:MAX_CLUSTERED_WALLETS]

    # Union-find: correlation is transitive enough for this purpose. If A
    # follows B and B follows C, treating all three as one voice is the
    # conservative reading, and being conservative about consensus is the
    # entire point.
    parent = {w: w for w in wallets}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(wallets):
        for b in wallets[i + 1:]:
            if overlap(times[a], times[b], window) >= threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb

    groups: dict[str, set[str]] = collections.defaultdict(set)
    for wallet in wallets:
        groups[find(wallet)].add(wallet)

    clusters: dict[str, Cluster] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        # The leader is whoever tends to be in first — the one whose view the
        # others are echoing, and therefore the one worth listening to.
        leader = min(members, key=lambda w: sum(times[w].values()) / len(times[w]))
        cluster = Cluster(members=members, leader=leader)
        for wallet in members:
            clusters[wallet] = cluster
    return clusters


def independent_subset(wallets: list[str],
                       clusters: dict[str, Cluster]) -> list[str]:
    """Keep one representative per cluster: the leader where present.

    This is what turns "five wallets bought it" into "how many separate
    opinions was that". Everything else in the pipeline can then treat the
    result as genuinely independent evidence.
    """
    kept: list[str] = []
    seen: set[int] = set()
    for wallet in wallets:
        cluster = clusters.get(wallet)
        if cluster is None:
            kept.append(wallet)
            continue
        key = id(cluster)
        if key in seen:
            continue
        seen.add(key)
        # Prefer the leader if it is among the wallets actually acting now.
        kept.append(cluster.leader if cluster.leader in wallets else wallet)
    return kept
