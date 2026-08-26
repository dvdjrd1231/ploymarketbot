"""The missing link: validated strategies -> live trade candidates.

THE DISCONNECT THIS FIXES. The discovery pass produces validated strategies and
writes them to the store. The decision engine requires a strategy record to
clear `STATISTICAL_VALIDITY` and `OUT_OF_SAMPLE_VALIDITY`. Nothing connected
the two: `decide()` was always called with `strategy={}`, so those two gates
failed on every candidate by definition, and every market reported
DO_NOT_TRADE no matter what the evidence said.

Sixteen validated strategies sat unused in the database while the system
reported that nothing was tradeable.

WHY IT WAS NOT MERELY AN OVERSIGHT. The two halves speak about different
objects. A discovered strategy is a rule over a WALLET TRADE observation —
`w_seen_n >= 117`, `price_vs_wallet_norm >= 0.13`, `market_price_move <=
-0.06`. Those features describe *somebody placing a trade*. The opportunity
scanner emits MARKET-level candidates: a market, its current price, its
liquidity. A market has no `w_seen_n`.

So wiring them together naively — passing the top-ranked strategy to whatever
market the scanner happened to surface — would attach a strategy record to a
candidate the strategy never selected, and the two research gates would pass on
evidence that does not apply. That is worse than the disconnect: it converts an
honest refusal into a false authorisation.

WHAT THIS DOES INSTEAD. It evaluates validated strategies against wallet-trade
observations, which is what they were discovered over, and emits a candidate
only where a strategy genuinely fires. The strategy record travels with the
candidate, so the research gates are answering a question about evidence that
actually covers it.

SCOPE, STATED. Matching runs over the point-in-time observation matrix, so it
is exact for RESEARCH, BACKTEST and PAPER over the historical tape. Live
matching needs the same features computed incrementally as trades arrive, which
needs a live trade feed this project does not yet have — see `live_gap()`.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..research.hypothesis import Hypothesis, Rule
from ..research.matrix import Matrix


@dataclass
class CopySignal:
    """A wallet trade that a validated strategy selected."""

    row: int
    ts: int
    wallet: str
    market_id: str
    token_id: str
    price: float
    strategy_id: str
    strategy: dict = field(default_factory=dict)
    statement: str = ""

    def to_dict(self) -> dict:
        d = {k: v for k, v in self.__dict__.items() if k != "strategy"}
        d["evidence_quality"] = self.strategy.get("evidence_quality")
        d["oos_n"] = self.strategy.get("trade_count")
        return d


def load_validated(store, *, statuses=("VALIDATED",),
                   lifecycles=("PAPER", "APPROVED", "LIVE")) -> list:
    """Validated strategies, as (Hypothesis, gate-record) pairs.

    `lifecycles` restricts to strategies that have been allowed at least as far
    as paper. A VALIDATED strategy that a human later SUSPENDED must not come
    back to life through this path.
    """
    out = []
    for r in store.query("SELECT * FROM strategies ORDER BY expectancy DESC"):
        try:
            p = json.loads(r["params"] or "{}")
        except Exception:                                     # noqa: BLE001
            continue
        verdict = p.get("verdict") or {}
        if verdict.get("status") not in statuses:
            continue
        if r["status"] not in lifecycles:
            continue
        rules = tuple(Rule(x["feature"], x["op"], x["value"])
                      for x in p.get("rules", []))
        if not rules:
            continue
        h = Hypothesis(r["strategy_id"], r["family"], rules,
                       p.get("statement", ""))
        oos = p.get("out_of_sample") or {}
        wf = p.get("walkforward") or {}
        rb = p.get("robustness") or {}
        # The shape `decision/gates.py` expects. Every field is read back from
        # the persisted pass — none is invented here.
        record = {
            "strategy_id": r["strategy_id"],
            "version": r["version"],
            "family": r["family"],
            "status": r["status"],
            "statement": p.get("statement", ""),
            "trade_count": oos.get("n", 0),
            "win_rate": oos.get("win_rate", 0.0),
            "expectancy": oos.get("expectancy", 0.0),
            "oos_expectancy": oos.get("alpha_vs_baseline", 0.0),
            "is_expectancy": (p.get("in_sample") or {}).get(
                "alpha_vs_baseline", 0.0),
            "p_value": oos.get("p_value", 1.0),
            "bh_threshold": verdict.get("checks", {}).get("bh_threshold", 0.0),
            "hypotheses_tested": verdict.get("checks", {}).get(
                "hypotheses_tested", 0),
            "walkforward_positive": wf.get("positive_share", 0.0),
            "perturbation_survival": rb.get("survival", 0.0),
            "concentration": oos.get("concentration", 0.0),
            "n_params": p.get("n_params", len(rules)),
            "max_drawdown": r["max_drawdown"],
            "evidence_quality": r["evidence_quality"],
            "caveats": verdict.get("caveats", []),
            "pass_id": p.get("pass_id", ""),
            # Regime restriction is not yet recorded per strategy; an empty
            # list means unrestricted, which AGENT 18 reports rather than
            # silently assuming.
            "regimes": p.get("regimes", []),
        }
        out.append((h, record))
    return out


class StrategyMatcher:
    """Fires validated strategies over point-in-time wallet observations."""

    def __init__(self, st: Settings, store) -> None:
        self.st = st
        self.store = store
        self.strategies = load_validated(store)

    @property
    def available(self) -> bool:
        return bool(self.strategies)

    def match(self, m: Matrix, *, lo: int = 0, hi: int = 0,
              limit: int = 200, dedupe_by_market: bool = True) -> list:
        """Every observation in [lo, hi) that a validated strategy selects.

        Newest first: a copy signal decays, and an eight-week-old one is a
        research artefact rather than a candidate.
        """
        hi = hi or len(m)
        if not self.strategies or hi <= lo:
            return []

        out: list = []
        seen_markets: set = set()
        # Walk backwards so `limit` keeps the most recent signals.
        for i in range(hi - 1, lo - 1, -1):
            for h, record in self.strategies:
                ok = True
                for r in h.rules:
                    col = m.cols.get(r.feature)
                    if col is None or not r.holds(col[i]):
                        ok = False
                        break
                if not ok:
                    continue
                mk = m.market_id[i]
                if dedupe_by_market and mk in seen_markets:
                    break
                seen_markets.add(mk)
                out.append(CopySignal(
                    row=i, ts=m.ts[i], wallet=m.wallet[i], market_id=mk,
                    token_id=m.token_id[i], price=m.cols["price"][i],
                    strategy_id=h.hypothesis_id, strategy=record,
                    statement=h.statement))
                break                      # one strategy per observation
            if len(out) >= limit:
                break
        return out

    def funnel(self, m: Matrix, *, lo: int = 0, hi: int = 0) -> dict:
        """Where candidates go, stage by stage.

        This is the number the dashboard's pipeline panel needs: not "no
        trade", but how many observations entered, how many any strategy
        selected, and how many distinct markets that reduces to.
        """
        hi = hi or len(m)
        total = max(0, hi - lo)
        if not self.strategies:
            return {"observations": total, "strategies_loaded": 0,
                    "selected": 0, "distinct_markets": 0,
                    "note": ("no VALIDATED strategy has reached PAPER, so "
                             "nothing can fire. Run `pqv3 discover`.")}
        sigs = self.match(m, lo=lo, hi=hi, limit=10 ** 9, dedupe_by_market=False)
        by_strategy: dict = {}
        for s in sigs:
            by_strategy[s.strategy_id] = by_strategy.get(s.strategy_id, 0) + 1
        return {
            "observations": total,
            "strategies_loaded": len(self.strategies),
            "selected": len(sigs),
            "selection_rate": round(len(sigs) / total, 5) if total else 0.0,
            "distinct_markets": len({s.market_id for s in sigs}),
            "distinct_wallets": len({s.wallet for s in sigs}),
            "by_strategy": dict(sorted(by_strategy.items(),
                                       key=lambda kv: -kv[1])[:20]),
        }


def live_gap(st: Settings, store) -> dict:
    """What still stands between this and live copy-trading. Measured.

    Stated as a checklist rather than prose because each item is a concrete
    missing capability, and reporting them as "future work" would let the
    system look closer to live than it is.
    """
    from ..core.source import HistoricalSource
    src = HistoricalSource(st)
    lag = src.data_lag_secs() if src.available else -1
    return {
        "matching_works_over": ["RESEARCH", "BACKTEST", "PAPER"],
        "blocks_for_live": [
            {"item": "live trade feed",
             "have": False,
             "detail": (f"matching reads the historical tape, whose newest "
                        f"trade is {lag / 3600:.1f}h old. Live copy-trading "
                        f"needs a websocket trade stream and incremental "
                        f"wallet-state updates, neither of which exists.")},
            {"item": "order-book depth",
             "have": store.count("book_snapshots") > 0,
             "detail": "EXECUTION_VALIDITY refuses in LIVE without a "
                       "measured book"},
            {"item": "measured settlement times",
             "have": False,
             "detail": "capital sizing is modelled while the clock is "
                       "degenerate"},
            {"item": "human authorisation",
             "have": st.live_authorized,
             "detail": "`pqv3 authorize-live --yes`"},
        ],
        "note": ("Strategy matching is exact over history. It is NOT a live "
                 "signal path, and nothing here shortens the live checklist."),
    }
