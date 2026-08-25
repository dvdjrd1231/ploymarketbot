"""The cross-wallet relationship graph.

Nodes are wallets; edges are measured co-behaviours. What the graph is FOR is
answering three questions the per-wallet view cannot:

  * Is this wallet's signal independent, or is it one of twelve accounts doing
    the same thing? Copying a cluster is one bet at twelve times the size.
  * Does anyone reliably trade BEFORE this wallet? A leader is worth more than
    a follower, and following a follower is following stale information.
  * Is apparent consensus actually consensus? Twenty wallets agreeing is only
    informative if they are twenty independent opinions.

**Inference confidence is represented, never asserted.** Two wallets trading
the same market within a minute may be one operator, two bots reading the same
feed, or coincidence in a busy market. The graph records the observation and a
confidence; it does NOT conclude common control. `same_entity_suspected` is
deliberately absent as a boolean — the closest thing is a `coordination_score`
with its inputs attached.

Cost control: the naive approach compares every wallet against every other,
which is 28,034^2 on this tape. Instead the graph is built from CO-OCCURRENCE
— wallets are only compared when they appear in the same market — which is
linear in trades rather than quadratic in wallets.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field

from ..config import Settings
from ..core.source import HistoricalSource

# Two trades in the same market within this window are "near-simultaneous".
NEAR_SECS = 300
# Below this many shared markets, any co-behaviour statistic is noise.
MIN_SHARED = 4


@dataclass
class Edge:
    a: str
    b: str
    shared_markets: int = 0
    same_direction: int = 0
    near_simultaneous: int = 0
    a_before_b: int = 0
    b_before_a: int = 0
    median_lag_secs: float = 0.0
    behavioural_similarity: float = 0.0
    coordination_score: float = 0.0
    confidence: float = 0.0
    relationship: str = "CO_TRADED"

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Cluster:
    members: list = field(default_factory=list)
    size: int = 0
    cohesion: float = 0.0
    leader: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class WalletGraph:
    edges: list = field(default_factory=list)
    clusters: list = field(default_factory=list)
    nodes: int = 0
    wallets: list = field(default_factory=list)
    built_ts: int = 0
    note: str = ""

    def neighbours(self, wallet: str) -> list:
        return [e for e in self.edges if e.a == wallet or e.b == wallet]

    def independence(self, wallets: list) -> dict:
        """How independent is a set of wallets that appear to agree?

        Returns an effective count. Twelve wallets in one cluster count as
        roughly one opinion, and a consensus computed over the raw twelve is
        twelve times more confident than the evidence supports.
        """
        if not wallets:
            return {"n": 0, "effective_n": 0.0, "note": "no wallets"}
        member_of: dict = {}
        for k, c in enumerate(self.clusters):
            for w in c.members:
                member_of[w] = k
        groups: dict = defaultdict(int)
        singles = 0
        for w in wallets:
            k = member_of.get(w)
            if k is None:
                singles += 1
            else:
                groups[k] += 1
        # Each cluster contributes sqrt(members) rather than members: not fully
        # independent, not fully redundant.
        eff = singles + sum(math.sqrt(v) for v in groups.values())
        return {"n": len(wallets), "effective_n": round(eff, 2),
                "clusters_involved": len(groups),
                "note": (f"{len(wallets)} wallets reduce to {eff:.1f} "
                         f"independent opinions once {len(groups)} cluster(s) "
                         f"are accounted for")
                if groups else "no clustering detected among these wallets"}

    def to_dict(self) -> dict:
        return {"nodes": self.nodes, "edges": len(self.edges),
                "clusters": [c.to_dict() for c in self.clusters],
                "built_ts": self.built_ts, "note": self.note,
                "top_edges": [e.to_dict() for e in
                              sorted(self.edges,
                                     key=lambda e: -e.coordination_score)[:60]]}


def build(st: Settings, source: HistoricalSource, *, wallets: list,
          as_of: int = 0, max_markets_per_wallet: int = 400) -> WalletGraph:
    """Build the graph over a chosen wallet set.

    Restricted to a wallet set (normally the profiled ones) rather than all
    28,034: an edge between two wallets we know nothing about is not
    actionable, and building it costs the same as building a useful one.
    """
    g = WalletGraph(built_ts=as_of or 0)
    wanted = set(wallets)
    g.wallets = sorted(wanted)
    g.nodes = len(wanted)
    if len(wanted) < 2 or not source.available:
        g.note = "fewer than two profiled wallets; no graph to build"
        return g

    # market -> [(wallet, ts, price, side)]
    by_market: dict = defaultdict(list)
    per_wallet_markets: dict = defaultdict(set)
    for w in wanted:
        for t in source.wallet_trades(w, as_of=as_of,
                                      limit=max_markets_per_wallet * 3):
            if t.get("event_type") != "TRADE" or not t.get("market_id"):
                continue
            by_market[t["market_id"]].append(
                (w, int(t["ts"]), float(t["price"] or 0), t.get("side") or ""))
            per_wallet_markets[w].add(t["market_id"])

    pair: dict = defaultdict(lambda: {"shared": 0, "same_dir": 0, "near": 0,
                                      "ab": 0, "ba": 0, "lags": []})
    for market, rows in by_market.items():
        actors = {r[0] for r in rows}
        if len(actors) < 2:
            continue
        rows.sort(key=lambda r: r[1])
        first: dict = {}
        for w, ts, px, side in rows:
            if w not in first:
                first[w] = (ts, px, side)
        names = sorted(first)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                a, b = names[i], names[j]
                key = (a, b)
                d = pair[key]
                d["shared"] += 1
                ta, pa, sa = first[a]
                tb, pb, sb = first[b]
                if sa and sa == sb:
                    d["same_dir"] += 1
                lag = tb - ta
                d["lags"].append(lag)
                if abs(lag) <= NEAR_SECS:
                    d["near"] += 1
                elif lag > 0:
                    d["ab"] += 1
                else:
                    d["ba"] += 1

    for (a, b), d in pair.items():
        if d["shared"] < MIN_SHARED:
            continue
        e = Edge(a=a, b=b, shared_markets=d["shared"],
                 same_direction=d["same_dir"], near_simultaneous=d["near"],
                 a_before_b=d["ab"], b_before_a=d["ba"])
        e.median_lag_secs = round(statistics.median(d["lags"]), 1)

        # Overlap of the two wallets' market universes: two wallets that trade
        # the same 200 markets are more related than two that share 5 of 400.
        ua, ub = per_wallet_markets[a], per_wallet_markets[b]
        e.behavioural_similarity = round(
            len(ua & ub) / len(ua | ub), 4) if (ua | ub) else 0.0

        near_share = d["near"] / d["shared"]
        dir_share = d["same_dir"] / d["shared"]
        # Coordination combines three independent observations. None of them
        # alone means much; together they are what "these two move together"
        # actually looks like in data.
        e.coordination_score = round(
            (0.45 * near_share + 0.35 * dir_share
             + 0.20 * e.behavioural_similarity), 4)
        # Confidence grows with sample and is capped: 40 shared markets is
        # where the estimate stops improving materially.
        e.confidence = round(min(1.0, d["shared"] / 40.0), 4)

        lead = d["ab"] - d["ba"]
        if near_share > 0.5 and d["shared"] >= 8:
            e.relationship = "SYNCHRONISED"
        elif abs(lead) >= max(4, 0.4 * d["shared"]):
            e.relationship = "LEADER_FOLLOWER"
        elif dir_share > 0.8:
            e.relationship = "CONSENSUS"
        elif dir_share < 0.2:
            e.relationship = "CONTRARIAN"
        g.edges.append(e)

    g.clusters = _cluster(g.edges)
    g.note = (f"{len(g.edges)} edges over {g.nodes} wallets from co-occurrence "
              f"in {len(by_market)} markets. An edge is an OBSERVATION of "
              f"co-behaviour with a confidence, never a conclusion about "
              f"common control.")
    return g


def _cluster(edges: list, *, threshold: float = 0.55) -> list:
    """Connected components over strong, well-evidenced edges.

    Union-find over edges above the threshold. Deliberately simple: a
    community-detection algorithm would impose structure the data cannot
    support at this sample size, and its parameters would be unexplainable.
    """
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    strong = [e for e in edges
              if e.coordination_score >= threshold and e.confidence >= 0.25]
    for e in strong:
        union(e.a, e.b)

    groups: dict = defaultdict(list)
    for w in parent:
        groups[find(w)].append(w)

    out = []
    for members in groups.values():
        if len(members) < 2:
            continue
        inside = [e for e in strong
                  if e.a in members and e.b in members]
        cohesion = round(
            sum(e.coordination_score for e in inside) / len(inside), 4) \
            if inside else 0.0
        # The leader is the wallet that most often traded first.
        lead_count: dict = defaultdict(int)
        for e in inside:
            if e.relationship == "LEADER_FOLLOWER":
                lead_count[e.a if e.a_before_b > e.b_before_a else e.b] += 1
        leader = max(lead_count, key=lead_count.get) if lead_count else ""
        out.append(Cluster(
            members=sorted(members), size=len(members), cohesion=cohesion,
            leader=leader,
            note=(f"{len(members)} wallets co-trade strongly. Treat their "
                  f"agreement as roughly {math.sqrt(len(members)):.1f} "
                  f"independent opinions, not {len(members)}."
                  + (f" {leader[:12]} most often trades first."
                     if leader else ""))))
    out.sort(key=lambda c: -c.size)
    return out
