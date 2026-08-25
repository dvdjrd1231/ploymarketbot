"""Wallet similarity and strategy families.

The question this answers is the one that decides whether the whole project has
found anything: does a profitable behaviour recur ACROSS INDEPENDENT WALLETS?

One wallet with a good record is a sample of one, and the market contains
28,034 wallets, so some of them have excellent records for the same reason some
coins land heads eight times. Four unrelated wallets independently doing the
same measurable thing and independently profiting from it is a different kind
of evidence -- not proof, but the first evidence that is not explainable by
selection alone.

Two similarity notions, deliberately kept apart:

  BEHAVIOURAL   do these wallets act alike? (profile signatures)
  STRATEGIC     does the same RULE work on both? (params_only_hash agreement)

Behavioural similarity proposes; strategic agreement disposes. Wallets that
look alike but on which the same rule fails are not a family, and the engine
reports that rather than clustering harder until something appears.

Agreement is never treated as causality (the brief is explicit). A family is a
research priority and a candidate for validation, never a promotion.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field

from .decompose import WalletProfile


# The signature keys, and the scale each is compared on. Comparing raw values
# would let `notional_p50` (log dollars, range ~10) dominate `opening_share`
# (range 0-1) and turn the clustering into a size sort.
_SCALES = {
    "price_p50": 0.25, "price_p10": 0.25, "price_p90": 0.25, "price_std": 0.20,
    "notional_p50": 2.0, "size_dispersion": 1.0, "conviction_ratio": 2.0,
    "horizon_p50": 3.0, "opening_share": 0.4, "new_market_share": 0.4,
    "trades_per_day": 1.5, "market_hhi": 0.3, "size_after_loss": 0.5,
    "chases": 0.05, "thin_tape_tolerance": 1.5,
}


def distance(a: WalletProfile, b: WalletProfile) -> float:
    """Scaled Euclidean distance between two behavioural signatures.

    Missing dimensions contribute their full scale rather than zero: two
    wallets that cannot be compared on a dimension are not similar on it.
    """
    sa, sb = a.signature(), b.signature()
    acc = 0.0
    for k, scale in _SCALES.items():
        x, y = sa.get(k), sb.get(k)
        if x is None or y is None:
            acc += 1.0
            continue
        acc += ((x - y) / scale) ** 2
    return math.sqrt(acc / len(_SCALES))


def similarity(a: WalletProfile, b: WalletProfile) -> float:
    """1.0 identical, decaying to 0. Monotone in distance, nothing more."""
    return 1.0 / (1.0 + distance(a, b))


@dataclass
class Family:
    """A group of wallets that behave alike, and what they agree on."""

    family_id: str
    wallets: list = field(default_factory=list)
    cohesion: float = 0.0                # mean pairwise similarity
    shared_rules: list = field(default_factory=list)
    independent_support: int = 0         # wallets on which a shared rule works
    centroid: dict = field(default_factory=dict)
    note: str = ""

    @property
    def size(self) -> int:
        return len(self.wallets)

    def to_dict(self) -> dict:
        return {"family_id": self.family_id, "wallets": self.wallets,
                "size": self.size, "cohesion": round(self.cohesion, 4),
                "independent_support": self.independent_support,
                "shared_rules": self.shared_rules[:10],
                "centroid": {k: round(v, 4) for k, v in self.centroid.items()},
                "note": self.note}


def cluster(profiles: list, threshold: float = 0.55,
            min_size: int = 2) -> list:
    """Agglomerative single-link clustering on behavioural similarity.

    Single-link on purpose: it will chain, and chaining is visible in the
    cohesion number, which is reported. A tighter linkage would produce
    prettier clusters and hide the fact that the behavioural space is mostly
    continuous.
    """
    usable = [p for p in profiles if p.n_observations >= 20]
    if len(usable) < min_size:
        return []

    clusters = [[i] for i in range(len(usable))]
    sims = {}
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            sims[(i, j)] = similarity(usable[i], usable[j])

    def between(c1, c2) -> float:
        return max(sims.get((min(a, b), max(a, b)), 0.0)
                   for a in c1 for b in c2)

    merged = True
    while merged:
        merged = False
        best = (threshold, None, None)
        for x in range(len(clusters)):
            for y in range(x + 1, len(clusters)):
                s = between(clusters[x], clusters[y])
                if s > best[0]:
                    best = (s, x, y)
        if best[1] is not None:
            x, y = best[1], best[2]
            clusters[x] = clusters[x] + clusters[y]
            clusters.pop(y)
            merged = True

    out = []
    for n, members in enumerate(sorted(clusters, key=len, reverse=True)):
        if len(members) < min_size:
            continue
        pairs = [sims[(min(a, b), max(a, b))]
                 for i, a in enumerate(members) for b in members[i + 1:]]
        cohesion = sum(pairs) / len(pairs) if pairs else 1.0
        wallets = [usable[i].wallet for i in members]
        centroid = {}
        for k in _SCALES:
            vals = [usable[i].signature().get(k, 0.0) for i in members]
            centroid[k] = sum(vals) / len(vals)
        fam = Family(family_id=f"FAM_{n:03d}", wallets=wallets,
                     cohesion=cohesion, centroid=centroid)
        if cohesion < 0.7:
            fam.note = ("loose cluster - single-link chaining likely; treat as "
                        "a search hint, not a family")
        out.append(fam)
    return out


def strategic_agreement(verdicts_by_wallet: dict,
                        min_wallets: int = 2) -> list:
    """Find RULES that work on several wallets independently.

    Grouped on `params_only_hash`, which is the strategy WITHOUT the wallet --
    so this is literally "the same idea, tested on different people". The
    strongest evidence this engine can produce, and the only kind that is not
    vulnerable to having picked the wallet first.

    `verdicts_by_wallet` maps wallet -> list[Verdict].
    """
    by_rule: dict = defaultdict(list)
    for wallet, verdicts in verdicts_by_wallet.items():
        for v in verdicts:
            by_rule[v.strategy_id].append((wallet, v))

    out = []
    for rule_id, entries in by_rule.items():
        wallets = {w for w, _ in entries}
        if len(wallets) < min_wallets:
            continue
        positive = [(w, v) for w, v in entries
                    if v.oos.get("expectancy", 0) > 0]
        validated = [(w, v) for w, v in entries if v.status == "VALIDATED"]
        pos_wallets = {w for w, _ in positive}
        if len(pos_wallets) < min_wallets:
            continue
        exps = [v.oos.get("expectancy", 0.0) for _, v in entries]
        alphas = [v.alpha.get("alpha", 0.0) for _, v in entries]
        mean_exp = sum(exps) / len(exps)
        # Dispersion across wallets is the honest measure of transferability:
        # a rule that earns +0.30 on one wallet and -0.05 on three others is
        # one wallet's result, not a family's.
        var = (sum((e - mean_exp) ** 2 for e in exps) / (len(exps) - 1)
               if len(exps) > 1 else 0.0)
        sd = math.sqrt(max(var, 0.0))
        out.append({
            "rule_id": rule_id,
            "describe": entries[0][1].describe,
            "wallets_tested": len(wallets),
            "wallets_positive": len(pos_wallets),
            "wallets_validated": len({w for w, _ in validated}),
            "mean_expectancy": round(mean_exp, 5),
            "sd_expectancy": round(sd, 5),
            "mean_alpha": round(sum(alphas) / len(alphas), 5),
            "consistency": round(len(pos_wallets) / len(wallets), 3),
            # t-like across wallets: is the rule's edge distinguishable from
            # the variation between the wallets it was tested on?
            "cross_wallet_t": round(
                mean_exp / (sd / math.sqrt(len(exps))), 3) if sd > 0 else 0.0,
        })
    out.sort(key=lambda d: (-d["wallets_validated"], -d["consistency"],
                            -d["mean_alpha"]))
    return out


def attach_agreement(families: list, agreement: list) -> list:
    """Record, per family, which shared rules its members support."""
    for fam in families:
        members = set(fam.wallets)
        rules = [r for r in agreement if r["wallets_positive"] >= 2]
        fam.shared_rules = [
            {"rule_id": r["rule_id"], "describe": r["describe"],
             "consistency": r["consistency"],
             "mean_alpha": r["mean_alpha"]}
            for r in rules][:10]
        fam.independent_support = sum(
            1 for r in rules if r["wallets_validated"] >= 2)
        if not fam.shared_rules:
            fam.note = (fam.note + " | " if fam.note else "") + (
                "behaviourally alike but no rule transfers between them - "
                "this is a resemblance, not a strategy family")
    return families
