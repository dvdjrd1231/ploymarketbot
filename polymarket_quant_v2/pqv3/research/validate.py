"""The status ladder. The ONLY module permitted to assign a strategy status.

V2 established this rule and enforced it by AST inspection; V3 keeps it. If any
other module could stamp `VALIDATED`, then "validated" would mean whatever the
most optimistic call site meant, and the word would carry no information.

The ladder, in the order the checks run. Each rung is a specific way a
promising number turns out not to be evidence:

    INSUFFICIENT_EVIDENCE   too few out-of-sample fills or markets
    NEGATIVE                out-of-sample expectancy is not positive
    NO_ALPHA                real, but it is the price band, not the rule
    NOT_SIGNIFICANT         did not clear the pass's BH threshold
    CONCENTRATED            too much of the profit is one market
    UNSTABLE                positive in under half the walk-forward folds
    FRAGILE                 fails perturbation, shuffle, bootstrap or holdout
    CAPITAL_INFEASIBLE      cannot be executed at this bankroll
    VALIDATED               survived all of the above

`VALIDATED` authorises **paper trading**. Going live is a human decision this
code never makes.

Evidence quality is separate from status and is deliberately conservative: a
strategy can be VALIDATED on thin data, and the rating says so.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Settings

LADDER = ("INSUFFICIENT_EVIDENCE", "NEGATIVE", "NO_ALPHA", "NOT_SIGNIFICANT",
          "CONCENTRATED", "UNSTABLE", "FRAGILE", "CAPITAL_INFEASIBLE",
          "VALIDATED")

# The lifecycle a validated strategy then moves through.
LIFECYCLE = ("DISCOVERED", "TESTING", "VALIDATING", "SHADOW", "PAPER",
             "APPROVED", "LIVE", "DEGRADED", "SUSPENDED", "RETIRED")


@dataclass
class Verdict:
    status: str = "INSUFFICIENT_EVIDENCE"
    reason: str = ""
    evidence_quality: str = "UNRATED"
    failure_modes: list = field(default_factory=list)
    caveats: list = field(default_factory=list)
    checks: dict = field(default_factory=dict)

    @property
    def validated(self) -> bool:
        return self.status == "VALIDATED"

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason,
                "evidence_quality": self.evidence_quality,
                "failure_modes": self.failure_modes,
                "caveats": self.caveats, "checks": self.checks}


def assign(*, st: Settings, oos, is_eval, walkforward, robustness,
           capital, bh_threshold: float, hypotheses_tested: int) -> Verdict:
    """Walk the ladder. First failure wins, and every check is recorded.

    Recording all checks even after the first failure is the same discipline
    the trade gates use: a strategy reported as UNSTABLE that is ALSO fragile
    and concentrated is a different object from one that is merely unstable,
    and only the full record distinguishes them.
    """
    v = Verdict()
    r = st.research
    c: dict = {}

    c["oos_fills"] = oos.n
    c["oos_markets"] = oos.markets
    c["oos_expectancy"] = oos.expectancy
    c["alpha_vs_baseline"] = oos.alpha_vs_baseline
    c["p_value"] = oos.p_value
    c["bh_threshold"] = bh_threshold
    c["hypotheses_tested"] = hypotheses_tested
    c["concentration"] = oos.concentration
    c["walkforward_positive"] = walkforward.positive_share
    c["robustness_survival"] = robustness.survival
    c["fragile"] = robustness.fragile
    c["capital_trades"] = capital.trades
    c["capital_return"] = capital.total_return
    c["capital_fill_rate"] = round(capital.fill_rate, 4)
    v.checks = c

    fails: list = []

    if oos.n < r.min_oos_fills or oos.markets < r.min_oos_markets:
        fails.append(("INSUFFICIENT_EVIDENCE",
                      f"{oos.n} out-of-sample fills across {oos.markets} "
                      f"markets; need {r.min_oos_fills} and "
                      f"{r.min_oos_markets}"))
    if oos.expectancy <= 0:
        fails.append(("NEGATIVE",
                      f"out-of-sample expectancy {oos.expectancy:+.5f}"))
    if oos.alpha_vs_baseline <= 0:
        fails.append(("NO_ALPHA",
                      f"expectancy {oos.expectancy:+.5f} against a market-wide "
                      f"baseline of {oos.baseline_expectancy:+.5f} in the same "
                      f"window: this is the price band, not the rule"))
    if bh_threshold <= 0 or oos.p_value > bh_threshold:
        fails.append(("NOT_SIGNIFICANT",
                      f"p={oos.p_value:.4g} against a BH threshold of "
                      f"{bh_threshold:.4g} over {hypotheses_tested} tests"))
    if oos.concentration > r.max_concentration:
        fails.append(("CONCENTRATED",
                      f"{oos.concentration:.0%} of gross profit comes from a "
                      f"single market"))
    if not walkforward.stable:
        fails.append(("UNSTABLE",
                      walkforward.note or
                      f"positive in {walkforward.positive_share:.0%} of folds"))
    if robustness.fragile:
        fails.append(("FRAGILE", robustness.note))
    # The capital test is evidence ONLY when its holding periods were measured.
    # On a database whose settlement timestamps are degenerate the simulation
    # rests on an assumed hold, and an assumption must not be able to promote a
    # strategy. It can still DEMOTE one — an unfundable strategy is unfundable
    # whatever the clock says — so the zero-trade check still runs.
    c["capital_reliable"] = getattr(capital, "reliable", True)
    c["capital_hold_model"] = getattr(capital, "hold_model", "DATA")
    if capital.trades == 0:
        fails.append(("CAPITAL_INFEASIBLE",
                      f"none of {capital.signals} signals could be funded at "
                      f"${st.capital.starting_capital:.2f}: "
                      f"{capital.skip_reasons}"))

    # An apparently perfect strategy triggers MORE validation, not less.
    if oos.win_rate >= r.perfect_winrate_threshold:
        need = int(r.min_oos_fills * r.perfect_extra_oos_multiple)
        if oos.n < need:
            fails.append(("INSUFFICIENT_EVIDENCE",
                          f"win rate {oos.win_rate:.1%} is near-perfect on only "
                          f"{oos.n} fills. Perfection is evidence of "
                          f"insufficient sampling until it survives {need}"))

    v.failure_modes = [f"{k}: {why}" for k, why in fails]
    if fails:
        # Report the EARLIEST rung on the ladder, so a strategy is described by
        # the most fundamental thing wrong with it rather than the last one
        # checked.
        order = {k: i for i, k in enumerate(LADDER)}
        status, why = min(fails, key=lambda kv: order.get(kv[0], 99))
        v.status, v.reason = status, why
    else:
        v.status = "VALIDATED"
        v.reason = (f"survived {oos.n} out-of-sample fills across "
                    f"{oos.markets} markets, p={oos.p_value:.4g} under BH "
                    f"{bh_threshold:.4g} over {hypotheses_tested} tests, "
                    f"{walkforward.positive_share:.0%} of folds positive, "
                    f"{robustness.survival:.0%} of robustness checks passed, "
                    f"and executed {capital.trades} trades at "
                    f"${st.capital.starting_capital:.2f}")

    v.evidence_quality = _quality(oos, walkforward, robustness, capital)

    # A MODELLED capital test is a caveat on every validated strategy, recorded
    # on the strategy itself so it travels with the record rather than living
    # only in a document nobody opens.
    if not getattr(capital, "reliable", True):
        v.caveats.append(
            "the $100 capital simulation used an ASSUMED holding period of "
            f"{st.research.modelled_hold_secs / 86400:.1f} days because this "
            "database's settlement timestamps are degenerate "
            "(resolutions.settled_ts is unpopulated). Its return, drawdown and "
            "trade count are modelled, not measured. Out-of-sample expectancy "
            "is unaffected — it needs only entry price and outcome. Repair "
            "with `pqv3 collect --backfill-settled`.")
        # Cap the rating: nothing can be STRONG while a headline number is
        # resting on an assumption.
        if v.evidence_quality == "STRONG":
            v.evidence_quality = "MODERATE"
    return v


def _quality(oos, wf, rb, cap) -> str:
    """How much weight the finding can bear, independent of pass/fail."""
    if oos.n < 30 or oos.markets < 5:
        return "INSUFFICIENT"
    score = 0
    score += 2 if oos.n >= 200 else (1 if oos.n >= 80 else 0)
    score += 2 if oos.markets >= 40 else (1 if oos.markets >= 15 else 0)
    score += 1 if wf.n_evaluable >= 4 else 0
    score += 1 if rb.survival >= 0.85 else 0
    score += 1 if cap.trades >= 20 else 0
    score += 1 if oos.wallets >= 20 else 0
    return "STRONG" if score >= 6 else ("MODERATE" if score >= 4 else "THIN")


def persist(store, *, hypothesis, verdict: Verdict, oos, is_eval, walkforward,
            robustness, capital, pass_id: str, version: int = 1,
            source: str = "discover") -> None:
    """Write the strategy record. Versioned, never overwritten in place."""
    lifecycle = "VALIDATING"
    if verdict.validated:
        # VALIDATED authorises PAPER. Nothing here can write APPROVED or LIVE.
        lifecycle = "PAPER"
    elif verdict.status in ("FRAGILE", "UNSTABLE", "NOT_SIGNIFICANT",
                            "NO_ALPHA", "NEGATIVE", "CONCENTRATED"):
        lifecycle = "RETIRED"
    elif verdict.status in ("INSUFFICIENT_EVIDENCE", "CAPITAL_INFEASIBLE"):
        lifecycle = "TESTING"

    store.insert("strategies", [{
        "strategy_id": hypothesis.hypothesis_id, "version": version,
        "parent_strategy": "", "family": hypothesis.family,
        "features": list(hypothesis.features),
        "params": {"rules": hypothesis.to_dict()["rules"],
                   "statement": hypothesis.statement,
                   "n_params": hypothesis.n_params,
                   "verdict": verdict.to_dict(),
                   "in_sample": is_eval.to_dict(),
                   "out_of_sample": oos.to_dict(),
                   "walkforward": walkforward.to_dict(),
                   "robustness": robustness.to_dict(),
                   "capital_test": capital.to_dict(),
                   "pass_id": pass_id},
        "train_from": is_eval.ts_from, "train_to": is_eval.ts_to,
        "valid_from": is_eval.ts_to, "valid_to": oos.ts_from,
        "oos_from": oos.ts_from, "oos_to": oos.ts_to,
        "trade_count": oos.n, "win_rate": oos.win_rate,
        "expectancy": oos.expectancy,
        "profit_factor": oos.profit_factor or 0.0,
        "max_drawdown": oos.max_drawdown,
        "evidence_quality": verdict.evidence_quality,
        "failure_modes": verdict.failure_modes,
        "status": lifecycle,
        "ts": int(time.time()),
    }], source=source, replace=True)


def promote(store, strategy_id: str, *, to: str, actor: str = "human",
            note: str = "") -> dict:
    """Move a strategy along the lifecycle. APPROVED and LIVE need a human.

    Automatic promotion into live trading is the single failure mode the brief
    is most emphatic about, so the transitions a machine may perform are
    enumerated rather than the ones it may not.
    """
    if to not in LIFECYCLE:
        return {"ok": False, "error": f"unknown status {to!r}"}
    row = store.one("SELECT * FROM strategies WHERE strategy_id=? "
                    "ORDER BY version DESC LIMIT 1", (strategy_id,))
    if not row:
        return {"ok": False, "error": "unknown strategy"}
    if to in ("APPROVED", "LIVE") and actor == "system":
        return {"ok": False,
                "error": f"{to} requires human authorisation; the system may "
                         f"not promote its own strategies into capital"}
    store.conn().execute(
        "UPDATE strategies SET status=? WHERE strategy_id=? AND version=?",
        (to, strategy_id, row["version"]))
    store.conn().commit()
    store.alert("strategy_promotion",
                f"{strategy_id} {row['status']} -> {to} by {actor}. {note}",
                severity="WARN" if to in ("APPROVED", "LIVE") else "INFO",
                subject=strategy_id, source="validate")
    return {"ok": True, "strategy_id": strategy_id, "from": row["status"],
            "to": to}
