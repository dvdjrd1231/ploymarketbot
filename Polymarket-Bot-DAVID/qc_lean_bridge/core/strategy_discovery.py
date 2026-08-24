"""Phase 3 - Automated Strategy Discovery.

Systematically searches the space of entry/exit/stop/target/filter combinations
and backtests each one under the prop constraints. The search METHOD is
pluggable (see core/optimizers.py) and shares one interface:

  * random   - sample `max_candidates` combinations (default).
  * grid     - deterministic sweep, truncated at `max_candidates`.
  * genetic  - evolutionary optimization (selection/crossover/mutation).

A feature-selection step first ranks structural features by their univariate
association with forward price movement, keeping only the top
`max_features_scanned` - the "efficient search algorithm (not brute-force)" the
brief asks for. Adding a new optimizer later requires no change here.
"""
from __future__ import annotations

import numpy as np

from .backtester import Backtester, Strategy, BacktestResult
from .config import Config
from .features import FeatureSet
from .optimizers import SearchSpace, make_optimizer
from .risk_management import PropConstraints


class StrategyDiscovery:
    def __init__(self, feature_set: FeatureSet, config: Config,
                 constraints: PropConstraints, logger=None,
                 progress=None):
        self.fs = feature_set
        self.cfg = config
        self.constraints = constraints
        self.log = logger or (lambda m: None)
        self.progress = progress or (lambda done, total: None)

        instr = config.section("instrument")
        acct = config.section("account")
        self.bt = Backtester(
            features=feature_set.frame,
            price=feature_set.price,
            constraints=constraints,
            point_value=float(instr.get("point_value", 5.0)),
            commission=float(instr.get("commission_per_contract", 0.62)),
            starting_equity=float(acct.get("starting_equity", 50000.0)),
            fee_per_fill=float(instr.get("fee_per_fill", 0.0)),
        )
        self.d = config.section("discovery")

    # ----------------------------------------------------------------- run
    def run(self) -> list[BacktestResult]:
        rng = np.random.RandomState(int(self.d.get("random_seed", 7)))
        selected = self._select_features()
        self.log(f"  feature selection kept {len(selected)} of "
                 f"{len(self.fs.names)} features")

        space = self._build_space(selected)
        method = str(self.d.get("method", "random"))

        # evaluator: assigns a stable id per unique genome, caches by signature,
        # runs the backtest. Shared by every optimizer.
        cache: dict[tuple, BacktestResult] = {}
        counter = {"n": 0}

        def evaluate(strat: Strategy) -> BacktestResult:
            sig = self._signature(strat)
            hit = cache.get(sig)
            if hit is not None:
                return hit
            strat.id = f"S{counter['n']:04d}"
            counter["n"] += 1
            res = self.bt.run(strat)
            cache[sig] = res
            return res

        params = dict(self.d)
        params["max_candidates"] = int(self.d.get("max_candidates", 400))
        params.update(self.d.get("genetic", {}) or {})   # GA params flattened in

        optimizer = make_optimizer(method, space, evaluate, params, rng,
                                   log=self.log, progress=self.progress)
        self.log(f"  discovering strategies via '{optimizer.name}' search")
        all_results = optimizer.run()

        # de-duplicate (optimizers may revisit genomes) and keep tradeable ones
        min_trades_keep = 5
        unique = list({id(r): r for r in all_results}.values())
        results = [r for r in unique if r.metrics["trades"] >= min_trades_keep]
        results.sort(key=lambda r: r.metrics.get("sharpe", 0.0), reverse=True)
        self.log(f"  evaluated {len(cache)} unique strategies; "
                 f"{len(results)} produced >= {min_trades_keep} trades")
        return results

    # ------------------------------------------------------ feature select
    def _select_features(self) -> list[str]:
        """Features allowed to carry a rule, best correlation first.

        Thresholds are read off each feature's OWN quantiles, so any feature
        that survives this list will produce a rule whether or not it has
        anything to say. A velocity column whose underlying metric barely
        moves is a run of exact zeros with floating-point dust on top; its
        90th percentile lands inside that dust, and the search dutifully
        reports `np_ask_quote_vel > 3.8e-10` — a real rule in the library,
        found in the delivered data, which says nothing more than "this
        differencing did not return exactly zero this time".

        Rejecting only `std == 0` does not catch that: dust has a non-zero
        standard deviation. Three floors do, and all three are about the
        feature having to MOVE before it may carry a rule:

        * **Effect size** does the real work, and the bar is set by chance
          rather than by taste. Under the null, a correlation measured over
          `m` observations has a standard error of about `1/sqrt(m)`, so the
          floor is `feature_effect_sigma` of those — a feature must beat
          what noise alone produces at the amount of evidence IT offers.
          That last part matters: a column that sits at one value for 96% of
          the series is telling us about 40 rows, not 1,000, and `2/sqrt(40)
          = 0.32` is a far higher bar than `2/sqrt(1000) = 0.06`. A fixed
          floor cannot express that, which is why `np_ask_quote_vel > 3.8e-10`
          survived: its dust correlated 0.04 with the forward move purely by
          chance, comfortably above any constant one would think to type.
        * **Modal share** is a cheap backstop for total degeneracy, and it
          is deliberately set loose (99.5%). A rare-event column — a
          liquidation cascade that fires on 1% of bars — is exactly the kind
          of feature worth trading, and a tight flat-share floor would throw
          it away for being rare. Rarity is not dust; the effect floor is
          what tells them apart.
        * **Distinct values** only rules out what cannot be split at all.
          Two is enough: a binary flag thresholded at `> 0` is a perfectly
          meaningful rule.

        Both structural floors compare values at a tolerance RELATIVE to the
        column's own magnitude, so a genuinely small-scale feature is never
        punished for its units. Set any of the three to 0 to disable it.
        """
        cap = int(self.d.get("max_features_scanned", 40))
        min_effect = float(self.d.get("min_feature_effect", 0.02))
        effect_sigma = float(self.d.get("feature_effect_sigma", 2.0))
        max_flat = float(self.d.get("max_feature_flat_share", 0.995))
        min_distinct = int(self.d.get("min_feature_distinct", 2))

        frame = self.fs.frame
        price = self.fs.price
        fwd = price.shift(-1) - price          # 1-bar forward change
        fwd = fwd.fillna(0.0).values
        if fwd.std() == 0:
            return list(frame.columns[:cap])

        scores = []
        dropped = {"flat": 0, "weak": 0, "constant": 0}
        for col in frame.columns:
            v = frame[col].values.astype(float)
            if np.nanstd(v) == 0:
                dropped["constant"] += 1
                continue
            informative = self._informative_rows(v, max_flat, min_distinct)
            if informative <= 0:
                dropped["flat"] += 1
                continue
            # |Pearson corr| with forward move
            vc = v - np.nanmean(v)
            fc = fwd - fwd.mean()
            denom = (np.sqrt((vc ** 2).sum()) * np.sqrt((fc ** 2).sum()))
            corr = abs((vc * fc).sum() / denom) if denom > 0 else 0.0
            # The bar is whatever noise would clear at this much evidence.
            noise_floor = effect_sigma / np.sqrt(max(informative, 2))
            if corr < max(min_effect, noise_floor):
                dropped["weak"] += 1
                continue
            scores.append((corr, col))
        scores.sort(reverse=True)

        held_back = dropped["flat"] + dropped["weak"]
        if held_back:
            self.log(f"  feature floors rejected {held_back} column(s): "
                     f"{dropped['flat']} too flat to threshold, "
                     f"{dropped['weak']} short of the effect floor "
                     f"(max of {min_effect:g} and {effect_sigma:g}/sqrt(m), "
                     "m = rows the column is informative on)")
        if not scores:
            # Saying so beats fitting the search to whatever dust is left.
            self.log("  no feature cleared the floors; nothing to discover "
                     "on this series")
        return [c for _, c in scores[:cap]]

    @staticmethod
    def _informative_rows(v, max_flat: float, min_distinct: int) -> int:
        """How many rows this column actually says something on. 0 = none.

        A column parked at one value carries information only where it
        departs from it, so that departure count — not the length of the
        series — is the sample size any statement about the column rests on.
        Returning it lets the caller scale the effect floor to the evidence
        rather than to the row count.

        Values are compared at a tolerance scaled to the column's own
        magnitude (1e-9 of its largest absolute value), so "distinct" means
        distinct as data rather than distinct as floating point: a column of
        zeros with 1e-18 jitter has one value here, not thousands. That also
        keeps a genuinely small-scale feature from being punished for its
        units.
        """
        finite = v[np.isfinite(v)]
        if finite.size == 0:
            return 0
        scale = float(np.max(np.abs(finite)))
        if scale <= 0:
            return 0                          # all zeros

        tolerance = scale * 1e-9
        keyed = np.round(finite / tolerance)
        values, counts = np.unique(keyed, return_counts=True)

        if min_distinct > 0 and values.size < min_distinct:
            return 0
        modal = int(counts.max())
        if max_flat > 0 and (modal / finite.size) > max_flat:
            return 0
        # Everything that is not the modal value is where this column speaks.
        # A column with no modal clump (every row different) speaks
        # everywhere, which is the full length.
        return int(finite.size - modal) if modal > 1 else int(finite.size)

    # ------------------------------------------------------ build space
    def _build_space(self, features: list[str]) -> SearchSpace:
        d = self.d
        quantiles = d.get("entry_quantiles", [0.7, 0.8, 0.9])
        qtable: dict[tuple[str, float], float] = {}
        for f in features:
            vals = self.fs.frame[f].values
            for q in quantiles:
                qtable[(f, q)] = float(np.nanquantile(vals, q))
                qtable[(f, round(1 - q, 4))] = float(np.nanquantile(vals, 1 - q))
        return SearchSpace(
            features=features,
            directions=d.get("directions", ["long", "short"]),
            ops=d.get("entry_operators", [">", "<"]),
            quantiles=quantiles,
            stops=d.get("stop_pct_choices", [2.0, 4.0]),
            targets=d.get("target_pct_choices", [2.0, 4.0]),
            times=d.get("time_exit_bars_choices", [0, 10]),
            sizes=d.get("position_contracts_choices", [1, 2]),
            qtable=qtable,
            frame=self.fs.frame,
            filter_prob=float(d.get("filter_probability", 0.4)),
        )

    @staticmethod
    def _signature(s: Strategy) -> tuple:
        return (s.direction, s.entry_feature, s.entry_op, round(s.entry_threshold, 6),
                s.stop_pct, s.target_pct, s.time_exit_bars, s.contracts,
                s.filter_feature, s.filter_op,
                None if s.filter_threshold is None else round(s.filter_threshold, 6))
