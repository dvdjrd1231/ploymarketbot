"""Settlement versus early exit: measured per strategy, never assumed.

The brief is explicit -- do not impose a generic exit. So this module runs the
SAME strategy through every exit model and reports the comparison, letting the
evidence choose. If RN1 historically holds strong positions to settlement, that
possibility is preserved. If it takes profits early under specific conditions,
that is reproducible. Both get tested.

THE HONEST LIMITATION, stated up front because it changes how these numbers
should be read:

    There is no historical order book. Early exits are priced off the TAPE --
    the aggregate prints of ~70k wallets. A tape print is a real price someone
    paid, so it is not invented; but it is not a continuous path, so:

      * a profit target between two prints fills at the first print PAST it,
        never at the target (pessimistic, correct)
      * a stop can be jumped straight through (pessimistic, correct)
      * a token with sparse prints cannot support an early exit at all, and is
        reported as such rather than filled at a modelled price

    So settlement results are EXACT and early-exit results are MODELLED. They
    are not directly comparable, and `confidence` on every row says which is
    which. An early-exit model that beats settlement by less than the tape's
    own coverage gap has not been shown to beat it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import Settings
from ..strategy_b.strategy import (CopyStrategy, ExitRule, EXIT_PARTIAL,
                                   EXIT_SETTLEMENT, EXIT_STOP, EXIT_TARGET,
                                   EXIT_TIME, EXIT_TRAIL)
from ..validation import backtest


# The grid of exits to compare. Small and interpretable: each is a distinct
# thesis about how a position should end, not a parameter to tune.
def exit_grid() -> list:
    out = [ExitRule(model=EXIT_SETTLEMENT)]
    for target in (0.15, 0.30, 0.60):
        out.append(ExitRule(model=EXIT_TARGET, target_return=target))
        out.append(ExitRule(model=EXIT_PARTIAL, target_return=target,
                            partial_fraction=0.5))
    for trail in (0.15, 0.30):
        out.append(ExitRule(model=EXIT_TRAIL, trail_return=trail))
    for stop in (-0.25, -0.50):
        out.append(ExitRule(model=EXIT_STOP, stop_return=stop))
    for hours in (6, 24, 72):
        out.append(ExitRule(model=EXIT_TIME, max_hold_secs=hours * 3600))
    return out


def label(rule: ExitRule) -> str:
    if rule.model == EXIT_SETTLEMENT:
        return "settlement"
    if rule.model == EXIT_TARGET:
        return f"target +{rule.target_return:.0%}"
    if rule.model == EXIT_PARTIAL:
        return f"half at +{rule.target_return:.0%}, rest to settlement"
    if rule.model == EXIT_TRAIL:
        return f"trail {rule.trail_return:.0%}"
    if rule.model == EXIT_STOP:
        return f"stop {rule.stop_return:.0%}"
    if rule.model == EXIT_TIME:
        return f"time stop {rule.max_hold_secs // 3600}h"
    return rule.model


@dataclass
class ExitComparison:
    strategy_id: str
    rows: list
    best: dict
    settlement: dict
    verdict: str

    def to_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "rows": self.rows,
                "best": self.best, "settlement": self.settlement,
                "verdict": self.verdict}


def tape_coverage(fills, tape, st: Settings) -> float:
    """What fraction of positions had a tape rich enough to exit early on?

    The honesty term. An early-exit study over positions whose tokens print
    twice is not a study.
    """
    if not fills:
        return 0.0
    rich = 0
    for f in fills[:400]:
        if tape.coverage(f.token_id) >= 10:
            rich += 1
    return rich / min(len(fills), 400)


def compare(strategy: CopyStrategy, observations: list, st: Settings,
            tape, *, min_fills: int = 25) -> ExitComparison:
    """Run one strategy under every exit model and rank the outcomes."""
    rows = []
    settlement_row = None

    for rule in exit_grid():
        variant = strategy.with_exit(rule)
        res = backtest.run(variant, observations, st, tape)
        if res.n_filled < min_fills:
            continue
        a = res.asymmetry()
        exits = {}
        for f in res.fills:
            exits[f.exit_reason] = exits.get(f.exit_reason, 0) + 1
        row = {
            "exit": label(rule), "model": rule.model,
            "n_filled": res.n_filled,
            "expectancy": round(res.expectancy, 5),
            "win_rate": round(res.win_rate, 4),
            "profit_factor": round(a["profit_factor"], 3),
            "avg_win": round(a["avg_win"], 4),
            "avg_loss": round(a["avg_loss"], 4),
            "win_loss_ratio": round(a["win_loss_ratio"], 3),
            "max_drawdown": round(res.max_drawdown(), 2),
            "tail_loss_p05": round(a["tail_loss_p05"], 4),
            "confidence": res.exit_confidence,
            "exit_reasons": exits,
            "early_exit_share": round(
                1.0 - exits.get("settlement", 0) / max(res.n_filled, 1), 3),
        }
        rows.append(row)
        if rule.model == EXIT_SETTLEMENT:
            settlement_row = row

    if not rows:
        return ExitComparison(strategy.strategy_id, [], {}, {},
                              "insufficient fills to compare exits")

    # Ranked on expectancy, but the verdict below refuses to crown a modelled
    # result over an exact one on a thin margin.
    rows.sort(key=lambda r: -r["expectancy"])
    best = rows[0]
    settlement_row = settlement_row or {}
    verdict = _verdict(best, settlement_row, rows)
    return ExitComparison(strategy.strategy_id, rows, best, settlement_row,
                          verdict)


def _verdict(best: dict, settlement: dict, rows: list) -> str:
    if not settlement:
        return ("settlement could not be measured on these fills; every row "
                "here is modelled")
    if best["model"] == EXIT_SETTLEMENT:
        return ("holding to settlement is best, and it is the only exit whose "
                "payoff is exact rather than modelled - prefer it")
    lift = best["expectancy"] - settlement["expectancy"]
    rel = lift / abs(settlement["expectancy"]) if settlement["expectancy"] else 0.0
    if lift <= 0:
        return "no early exit beat settlement"
    if rel < 0.15:
        return (f"{best['exit']} beats settlement by {lift:+.4f} "
                f"({rel:.0%}), but it is a MODELLED result against an EXACT "
                "one and the margin is inside the tape's own uncertainty. Not "
                "sufficient to switch.")
    # Does the early exit improve the SHAPE, not just the mean?
    shape = ""
    if best["win_loss_ratio"] > settlement.get("win_loss_ratio", 0) * 1.2:
        shape = " and it improves winner/loser asymmetry"
    elif best["tail_loss_p05"] > settlement.get("tail_loss_p05", 0):
        shape = " and it cuts the loss tail"
    return (f"{best['exit']} beats settlement by {lift:+.4f} ({rel:+.0%})"
            f"{shape}. Modelled off tape prints - validate out-of-sample "
            "before trusting it.")


def study(strategies, observations, st: Settings, tape,
          limit: int = 10) -> list:
    """Compare exits across several strategies and look for a common answer.

    One strategy preferring a trailing exit is a parameter. Six independent
    strategies preferring it is a finding.
    """
    out = []
    for s in strategies[:limit]:
        c = compare(s, observations, st, tape)
        if c.rows:
            out.append(c.to_dict())
    if out:
        winners = {}
        for c in out:
            winners[c["best"]["model"]] = winners.get(c["best"]["model"], 0) + 1
        consensus = max(winners.items(), key=lambda kv: kv[1])
        out.append({"consensus": {
            "model": consensus[0], "strategies_preferring": consensus[1],
            "of": len(out),
            "note": ("a majority preference across independently discovered "
                     "strategies is worth testing as a default; a plurality is "
                     "not")}})
    return out
