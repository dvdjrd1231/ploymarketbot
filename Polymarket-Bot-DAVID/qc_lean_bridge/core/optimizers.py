"""Optimization methods for strategy discovery.

All optimizers share ONE interface (`Optimizer.run() -> list[BacktestResult]`)
so new search methods can be added without touching the discovery pipeline or
the GUI. Three are provided:

  * RandomSearchOptimizer - sample N genomes uniformly (fast, good default).
  * GridSearchOptimizer   - deterministic sweep, truncated at a budget.
  * GeneticOptimizer      - evolutionary search: tournament selection, uniform
                            crossover, per-gene mutation, elitism. Converges the
                            population toward high-Sharpe rule combinations.

A "genome" is a plain dict of genes that maps 1:1 onto a Strategy. The search
space knows how to make a random genome and how to build a Strategy from one, so
the optimizers stay generic and know nothing about trading.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .backtester import Strategy, BacktestResult


# ---------------------------------------------------------------- search space
@dataclass
class SearchSpace:
    features: list[str]
    directions: list[str]
    ops: list[str]
    quantiles: list[float]
    stops: list[float]
    targets: list[float]
    times: list[int]
    sizes: list[int]
    qtable: dict
    frame: pd.DataFrame
    filter_prob: float = 0.4

    # ---- threshold lookup (feature value at a quantile, given the operator)
    def threshold(self, feature: str, op: str, q: float) -> float:
        thr_q = q if op == ">" else round(1 - q, 4)
        val = self.qtable.get((feature, thr_q))
        if val is None:
            val = float(np.nanquantile(self.frame[feature].values, thr_q))
        return val

    # ---- random genome
    def random_genome(self, rng) -> dict:
        g = {
            "feature": self.features[rng.randint(len(self.features))],
            "direction": self.directions[rng.randint(len(self.directions))],
            "op": self.ops[rng.randint(len(self.ops))],
            "quantile": float(self.quantiles[rng.randint(len(self.quantiles))]),
            "stop": float(self.stops[rng.randint(len(self.stops))]),
            "target": float(self.targets[rng.randint(len(self.targets))]),
            "time": int(self.times[rng.randint(len(self.times))]),
            "size": int(self.sizes[rng.randint(len(self.sizes))]),
            "filter": None,
        }
        if rng.rand() < self.filter_prob and len(self.features) > 1:
            g["filter"] = self._random_filter(rng, exclude=g["feature"])
        return g

    def _random_filter(self, rng, exclude: str) -> dict | None:
        ff = self.features[rng.randint(len(self.features))]
        if ff == exclude:
            return None
        return {
            "feature": ff,
            "op": self.ops[rng.randint(len(self.ops))],
            "quantile": float(self.quantiles[rng.randint(len(self.quantiles))]),
        }

    # ---- mutate a single gene in place (returns a new dict)
    def mutate(self, genome: dict, rng, rate: float) -> dict:
        g = dict(genome)
        if rng.rand() < rate:
            g["feature"] = self.features[rng.randint(len(self.features))]
        if rng.rand() < rate:
            g["direction"] = self.directions[rng.randint(len(self.directions))]
        if rng.rand() < rate:
            g["op"] = self.ops[rng.randint(len(self.ops))]
        if rng.rand() < rate:
            g["quantile"] = float(self.quantiles[rng.randint(len(self.quantiles))])
        if rng.rand() < rate:
            g["stop"] = float(self.stops[rng.randint(len(self.stops))])
        if rng.rand() < rate:
            g["target"] = float(self.targets[rng.randint(len(self.targets))])
        if rng.rand() < rate:
            g["time"] = int(self.times[rng.randint(len(self.times))])
        if rng.rand() < rate:
            g["size"] = int(self.sizes[rng.randint(len(self.sizes))])
        if rng.rand() < rate:
            g["filter"] = (None if g.get("filter") else
                           self._random_filter(rng, exclude=g["feature"]))
        return g

    # ---- build a Strategy from a genome (id assigned later by the evaluator)
    def build(self, genome: dict) -> Strategy:
        op = genome["op"]
        thr = self.threshold(genome["feature"], op, genome["quantile"])
        strat = Strategy(
            id="",
            direction=genome["direction"],
            entry_feature=genome["feature"],
            entry_op=op,
            entry_threshold=thr,
            stop_pct=genome["stop"],
            target_pct=genome["target"],
            time_exit_bars=int(genome["time"]),
            contracts=int(genome["size"]),
        )
        filt = genome.get("filter")
        if filt:
            strat.filter_feature = filt["feature"]
            strat.filter_op = filt["op"]
            strat.filter_threshold = self.threshold(
                filt["feature"], filt["op"], filt["quantile"])
        return strat


# ---------------------------------------------------------------- base optimizer
class Optimizer:
    """Common interface. `evaluate(strategy) -> BacktestResult` is injected by the
    discovery pipeline (it assigns ids, caches by signature, runs the backtest)."""

    name = "base"

    def __init__(self, space: SearchSpace, evaluate, params: dict,
                 rng, log=None, progress=None):
        self.space = space
        self.evaluate = evaluate
        self.params = params or {}
        self.rng = rng
        self.log = log or (lambda m: None)
        self.progress = progress or (lambda d, t: None)

    def run(self) -> list[BacktestResult]:
        raise NotImplementedError

    # shared fitness used by evolutionary search / ranking within an optimizer
    @staticmethod
    def fitness(res: BacktestResult) -> float:
        m = res.metrics
        trades = m.get("trades", 0)
        if trades < 5:
            # climb toward 0 as a genome starts producing trades
            return -1.0 + 0.1 * trades
        return float(m.get("sharpe", 0.0))


# ---------------------------------------------------------------- random search
class RandomSearchOptimizer(Optimizer):
    name = "random"

    def run(self) -> list[BacktestResult]:
        budget = int(self.params.get("max_candidates", 400))
        out, seen = [], set()
        for k in range(budget):
            genome = self.space.random_genome(self.rng)
            res = self.evaluate(self.space.build(genome))
            if id(res) not in seen:
                seen.add(id(res))
                out.append(res)
            if (k + 1) % 25 == 0 or k + 1 == budget:
                self.progress(k + 1, budget)
        return out


# ---------------------------------------------------------------- grid search
class GridSearchOptimizer(Optimizer):
    name = "grid"

    def run(self) -> list[BacktestResult]:
        budget = int(self.params.get("max_candidates", 400))
        s = self.space
        combo = itertools.product(s.features, s.directions, s.ops, s.quantiles,
                                  s.stops, s.targets, s.times, s.sizes)
        out, seen, k = [], set(), 0
        for (f, dirn, op, q, st, tg, tm, sz) in combo:
            if k >= budget:
                break
            genome = {"feature": f, "direction": dirn, "op": op, "quantile": q,
                      "stop": st, "target": tg, "time": tm, "size": sz,
                      "filter": None}
            res = self.evaluate(s.build(genome))
            if id(res) not in seen:
                seen.add(id(res))
                out.append(res)
            k += 1
            if k % 25 == 0 or k == budget:
                self.progress(k, budget)
        return out


# ---------------------------------------------------------------- genetic
class GeneticOptimizer(Optimizer):
    name = "genetic"

    def run(self) -> list[BacktestResult]:
        p = self.params
        pop_size = int(p.get("population_size", 40))
        generations = int(p.get("generations", 12))
        mut_rate = float(p.get("mutation_rate", 0.2))
        cx_rate = float(p.get("crossover_rate", 0.7))
        elite = int(p.get("elite", 4))
        tourn_k = max(2, int(p.get("tournament_k", 3)))

        rng = self.rng
        space = self.space
        total_budget = pop_size * generations

        population = [space.random_genome(rng) for _ in range(pop_size)]
        collected: dict[int, BacktestResult] = {}
        best_history = []
        evals = 0

        for gen in range(generations):
            scored = []
            for genome in population:
                res = self.evaluate(space.build(genome))
                collected[id(res)] = res
                fit = self.fitness(res)
                scored.append((fit, genome, res))
                evals += 1
                if evals % 25 == 0 or evals == total_budget:
                    self.progress(min(evals, total_budget), total_budget)

            scored.sort(key=lambda x: x[0], reverse=True)
            best_fit = scored[0][0]
            best_history.append(best_fit)
            self.log(f"    gen {gen + 1}/{generations}: best Sharpe "
                     f"{best_fit:.3f}, pop {pop_size}")

            if gen == generations - 1:
                break

            # --- build next generation ---
            next_pop = [g for (_, g, _) in scored[:elite]]      # elitism
            while len(next_pop) < pop_size:
                pa = self._tournament(scored, tourn_k, rng)
                pb = self._tournament(scored, tourn_k, rng)
                child = (self._crossover(pa, pb, rng) if rng.rand() < cx_rate
                         else dict(pa))
                child = space.mutate(child, rng, mut_rate)
                next_pop.append(child)
            population = next_pop

        conv = self._convergence(best_history)
        self.log(f"  genetic convergence: {[round(b,3) for b in best_history]} "
                 f"(improved {conv:+.3f} over {generations} generations)")
        return list(collected.values())

    # ---- tournament selection: pick k at random, return the fittest genome
    @staticmethod
    def _tournament(scored, k, rng) -> dict:
        idx = rng.randint(0, len(scored), size=k)
        best = max((scored[i] for i in idx), key=lambda x: x[0])
        return best[1]

    # ---- uniform crossover of two genomes
    @staticmethod
    def _crossover(a: dict, b: dict, rng) -> dict:
        child = {}
        for key in ("feature", "direction", "op", "quantile", "stop",
                    "target", "time", "size", "filter"):
            child[key] = a[key] if rng.rand() < 0.5 else b[key]
        return child

    @staticmethod
    def _convergence(history) -> float:
        if len(history) < 2:
            return 0.0
        return float(history[-1] - history[0])


# ---------------------------------------------------------------- factory
_OPTIMIZERS = {
    "random": RandomSearchOptimizer,
    "grid": GridSearchOptimizer,
    "genetic": GeneticOptimizer,
}


def make_optimizer(method: str, space, evaluate, params, rng, log=None,
                   progress=None) -> Optimizer:
    cls = _OPTIMIZERS.get((method or "random").lower(), RandomSearchOptimizer)
    return cls(space, evaluate, params, rng, log=log, progress=progress)


def available_methods() -> list[str]:
    return list(_OPTIMIZERS.keys())
