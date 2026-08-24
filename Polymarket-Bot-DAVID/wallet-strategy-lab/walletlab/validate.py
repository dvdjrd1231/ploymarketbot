"""Chronological validation and the generalisation matrix (§8, §9).

Splits are always on TIME, at the signal instant — never random (§8). A
prediction market pays at resolution, so a random split would put a trade's
neighbours in both train and test and leak the outcome through the market
rather than through the feature vector.

The generalisation matrix (§9) is the output that matters:

                      SAME MARKETS      NEW MARKETS
    SAME WALLET            A                 B
    NEW WALLETS            C                 D

A alone is worthless — it is where the strategy was fitted. B says the rule is
about behaviour rather than a market. C says the *transformation* generalises
even if the wallet does not. D is the only cell that indicates a mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .backtest import Result, run
from .config import Settings
from .data import PriceTape
from .state import Observation
from .stats import RobustnessReport, bootstrap_p, placebo_p, t_to_p
from .strategy import CopyStrategy


def time_split(obs: list[Observation], fractions=(0.5, 0.25, 0.25)) -> list[list[Observation]]:
    """Chronological train / validation / test."""
    ordered = sorted(obs, key=lambda o: o.trade.ts)
    n = len(ordered)
    out, start = [], 0
    for f in fractions[:-1]:
        k = int(n * f)
        out.append(ordered[start:start + k])
        start += k
    out.append(ordered[start:])
    return out


def walk_forward(
    strategy: CopyStrategy,
    obs: list[Observation],
    st: Settings,
    tape: PriceTape,
    folds: int = 4,
) -> list[Result]:
    """Anchored walk-forward: expand the past, test on the next block only."""
    ordered = sorted(obs, key=lambda o: o.trade.ts)
    n = len(ordered)
    if n < folds * 10:
        return []
    block = n // (folds + 1)
    results = []
    for i in range(1, folds + 1):
        test = ordered[i * block:(i + 1) * block]
        if test:
            results.append(run(strategy, test, st, tape))
    return results


@dataclass
class Validation:
    strategy: CopyStrategy
    train: Result
    validation: Result
    test: Result
    walk: list[Result] = field(default_factory=list)
    robustness: RobustnessReport | None = None

    # §46 population control — see baseline.py for why this decides everything.
    alpha: float = 0.0
    population: dict = field(default_factory=dict)

    # generalisation cells
    same_wallet_new_markets: Result | None = None
    cross_wallet: list[tuple[str, Result]] = field(default_factory=list)

    def walk_consistency(self) -> float:
        """Fraction of walk-forward folds with positive expectancy."""
        if not self.walk:
            return 0.0
        return sum(1 for r in self.walk if r.expectancy > 0) / len(self.walk)

    def cross_wallet_consistency(self) -> float:
        if not self.cross_wallet:
            return 0.0
        ok = sum(1 for _, r in self.cross_wallet if r.expectancy > 0 and r.n_filled >= 10)
        return ok / len(self.cross_wallet)

    def oos_p(self) -> float:
        return t_to_p(self.test.t_stat(), self.test.n_filled)

    def score(self) -> float:
        """Composite quality (§15). Rewards robustness, penalises fragility.

        Explicitly NOT sorted by P&L: a strategy earns its score by surviving
        out-of-sample, across folds, across markets, and by not being one
        market in disguise.
        """
        t = self.test
        if t.n_filled < 30:
            return 0.0

        # The economic term is ALPHA, not expectancy. A rule that earns +20%
        # by buying the same underpriced favourites everyone else is buying
        # scores zero here, however good its P&L looks (see baseline.py).
        oos = max(-1.0, min(1.0, self.alpha * 10))
        consistency = self.walk_consistency()               # 0..1
        breadth = min(1.0, len(t.markets) / 20.0)           # market diversity
        conc_pen = 1.0 - t.concentration()                  # one-market penalty
        dd_pen = 1.0 - min(1.0, t.max_drawdown() / max(1.0, abs(t.pnl) + 1.0))
        fill = t.fill_rate

        # NOTE the sign. High cross-wallet consistency is *evidence against* a
        # wallet-specific edge: if the same filter works on everyone else's
        # trades, the wallet is contributing nothing and the effect is market
        # structure. §9 asks whether a rule generalises; this project asks
        # whether a WALLET is worth following, and those pull opposite ways.
        wallet_specific = 1.0 - self.cross_wallet_consistency()

        raw = (
            0.35 * oos
            + 0.20 * consistency
            + 0.15 * wallet_specific
            + 0.10 * breadth
            + 0.10 * conc_pen
            + 0.05 * dd_pen
            + 0.05 * fill
        )
        if self.robustness and self.robustness.verdict == "FRAGILE":
            raw *= 0.5
        return round(raw, 4)

    def status(self, fdr_threshold: float) -> str:
        """The ladder (§13). Only evidence moves a strategy up it."""
        t = self.test
        if t.n_filled < 30:
            return "INSUFFICIENT_EVIDENCE"
        if t.expectancy <= 0:
            return "FAILED"
        if self.oos_p() > fdr_threshold:
            return "NOT_SIGNIFICANT"
        # The population control decides before robustness does. A market-wide
        # effect can be perfectly robust and still say nothing about the wallet.
        if self.population and self.population.get("n", 0) >= 30 and self.alpha <= 0:
            return "NO_WALLET_ALPHA"
        if self.robustness and self.robustness.verdict == "FRAGILE":
            return "OVERFIT"
        if t.concentration() > 0.60:
            return "CONCENTRATED"
        if self.walk_consistency() < 0.5:
            return "UNSTABLE"
        return "VALIDATED"


def parameter_stability(
    strategy: CopyStrategy,
    obs: list[Observation],
    st: Settings,
    tape: PriceTape,
) -> float:
    """§14: does the edge survive nudging every threshold it depends on?

    A rule that works at 0.7314 and dies at 0.72 is a curve fit. Neighbours are
    generated by perturbing each numeric field it actually constrains.
    """
    from dataclasses import replace

    neighbours: list[CopyStrategy] = []
    for fld, deltas in (
        ("min_price", (-0.05, 0.05)),
        ("max_price", (-0.05, 0.05)),
        ("min_rel_notional", (-0.5, 0.5)),
        ("min_roll_win_rate", (-0.05, 0.05)),
        ("min_settled_n", (-10, 10)),
        ("delay_secs", (-30, 30)),
    ):
        cur = getattr(strategy, fld, None)
        if cur in (None, 0):
            continue
        for d in deltas:
            val = type(cur)(cur + d)
            if fld.endswith("price") and not (0.0 < val < 1.0):
                continue
            if val < 0:
                continue
            neighbours.append(replace(strategy, **{fld: val}))

    if not neighbours:
        return 1.0  # nothing to perturb: an unconditional rule cannot be curve-fit
    ok = 0
    for nb in neighbours:
        r = run(nb, obs, st, tape)
        if r.n_filled >= 10 and r.expectancy > 0:
            ok += 1
    return ok / len(neighbours)


def robustness(
    strategy: CopyStrategy,
    test: Result,
    obs: list[Observation],
    st: Settings,
    tape: PriceTape,
) -> RobustnessReport:
    stability = parameter_stability(strategy, obs, st, tape)
    bp = bootstrap_p(test.returns, seed=int(strategy.spec_hash()[:8], 16) % 10**6)
    population = [
        (o.trade.resolution - o.price) / o.price
        for o in obs
        if o.trade.wallet == strategy.wallet
    ]
    pp = placebo_p(test.returns, population,
                   seed=int(strategy.spec_hash()[8:16], 16) % 10**6)

    # Cost stress: does it survive double the modelled slippage?
    harsher = Settings(
        data_db=st.data_db, work_dir=st.work_dir,
        costs=type(st.costs)(
            slippage_bps=st.costs.slippage_bps * 2,
            fee_bps=st.costs.fee_bps,
            min_price=st.costs.min_price, max_price=st.costs.max_price,
            max_notional=st.costs.max_notional,
        ),
    )
    stressed = run(strategy, obs, harsher, tape)
    survives = stressed.expectancy > 0

    fragile = stability < 0.5 or bp > 0.10 or pp > 0.10 or not survives
    return RobustnessReport(
        parameter_stability=stability,
        bootstrap_p=bp,
        placebo_p=pp,
        survives_costs=survives,
        verdict="FRAGILE" if fragile else "ROBUST",
    )
