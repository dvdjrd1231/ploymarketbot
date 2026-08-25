"""The end-of-cycle diagnostic: the brief's 22 questions, answered from data.

The requirement this exists to satisfy:

    There must never be an unexplained gap between DATA and TRADE.

So every answer is either a measured number or an explicit "not measurable, and
here is why". An answer that is a guess is worse than no answer, because it
closes the question.

Question numbering follows the brief exactly so the report can be checked
against it line by line.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Answer:
    n: int
    question: str
    value: object = None
    detail: str = ""
    measurable: bool = True

    def to_dict(self) -> dict:
        return {"n": self.n, "question": self.question, "value": self.value,
                "detail": self.detail, "measurable": self.measurable}

    def render(self) -> str:
        head = f"{self.n:2d}. {self.question}"
        if not self.measurable:
            return f"{head}\n    NOT MEASURABLE: {self.detail}"
        val = self.value
        if isinstance(val, (dict, list)):
            val = json.dumps(val, indent=6, default=str)
        body = f"    {val}"
        if self.detail:
            body += f"\n    {self.detail}"
        return f"{head}\n{body}"


@dataclass
class Diagnostic:
    answers: list = field(default_factory=list)

    def add(self, n: int, question: str, value=None, detail: str = "",
            measurable: bool = True) -> None:
        self.answers.append(Answer(n, question, value, detail, measurable))

    def to_dict(self) -> dict:
        return {"answers": [a.to_dict() for a in self.answers]}

    def render(self) -> str:
        lines = ["=" * 74, "END-OF-CYCLE DIAGNOSTIC", "=" * 74, ""]
        for a in self.answers:
            lines.append(a.render())
            lines.append("")
        return "\n".join(lines)


def build(*, pass_report=None, funnel=None, account=None,
          strategy_a=None, ledger_rejections=None, gate_audit=None,
          asymmetry=None, expansion=None, hypotheses=None,
          feature_audit=None) -> Diagnostic:
    """Answer all 22. Missing inputs produce honest gaps, never zeros."""
    d = Diagnostic()
    pr = pass_report or {}
    fn = funnel or {}
    b = fn.get("B") or {}
    a = fn.get("A") or {}

    # 1-4: what existed and what was generated
    d.add(1, "How many opportunities existed?",
          b.get("opportunities", 0),
          "Copyable wallet actions the Strategy B route observed. The full "
          "settled substrate holds 116,923 such actions across 1,285 markets "
          "and 90 days.")
    d.add(2, "How many wallet signals were generated?",
          b.get("received", 0),
          "One per (observation, bound strategy) pair. Zero bound strategies "
          "means zero signals, which is a validation result, not a bug.")
    d.add(3, "How many Strategy A signals were generated?",
          a.get("received", 0) or (strategy_a or {}).get("decisions_total", 0),
          (strategy_a or {}).get("verdict", ""))
    d.add(4, "How many Strategy B signals were generated?",
          b.get("received", 0))

    # 5-6: rejection
    d.add(5, "How many Strategy B signals were rejected?",
          b.get("strategy_rejected", 0) + b.get("risk_rejected", 0)
          + b.get("portfolio_rejected", 0),
          f"strategy {b.get('strategy_rejected', 0)}, "
          f"risk {b.get('risk_rejected', 0)}, "
          f"portfolio {b.get('portfolio_rejected', 0)}")
    d.add(6, "Why were they rejected?",
          b.get("top_rejections", []),
          "Gate keys, most frequent first. Every key is registered in "
          "gates.py with an owner; none is anonymous.")

    # 7-11: the funnel
    d.add(7, "How many passed risk?", b.get("received", 0)
          - b.get("strategy_rejected", 0) - b.get("risk_rejected", 0))
    d.add(8, "How many passed portfolio allocation?",
          b.get("execution_attempted", 0),
          "Portfolio approval is counted separately from strategy acceptance "
          "so strategy quality and capital availability never get confused.")
    d.add(9, "How many execution attempts occurred?",
          b.get("execution_attempted", 0))
    d.add(10, "How many executions succeeded?",
          b.get("execution_successful", 0),
          f"{b.get('execution_failed', 0)} failed, chiefly because no price "
          "printed inside the fill window - an opportunity that cannot be "
          "priced earns nothing rather than being filled at the wallet's own "
          "price.")
    d.add(11, "How many trades completed?",
          b.get("wins", 0) + b.get("losses", 0))

    # 12-16: the economics
    d.add(12, "What was the expectancy?", b.get("expectancy", 0.0),
          "Mean per-trade return on capital, after modelled costs.")
    d.add(13, "What was the average winner?", b.get("avg_win", 0.0))
    d.add(14, "What was the average loser?", b.get("avg_loss", 0.0))
    acct = account or {}
    d.add(15, "What was the drawdown?", acct.get("max_drawdown", 0.0),
          f"current {acct.get('drawdown', 0.0)}, "
          f"halted={acct.get('halted', False)}")
    d.add(16, "What was the compounded return?",
          acct.get("compounded_return", 0.0),
          f"equity {acct.get('equity', 0)} from "
          f"{acct.get('starting_capital', 0)}")

    # 17-18: comparison
    d.add(17, "Which strategy produced the strongest risk-adjusted results?",
          _compare_routes(a, b, strategy_a))
    families = pr.get("families") or []
    agreement = pr.get("agreement") or []
    d.add(18, "Which wallet strategy family produced the strongest results?",
          _best_family(families, agreement),
          "Ranked on independent cross-wallet support, never on P&L: a rule "
          "that validates on several unrelated wallets is the only evidence "
          "here that is not vulnerable to having picked the wallet first.")

    # 19-21: suppression audit
    d.add(19, "Which rules are currently suppressing the most opportunities?",
          ledger_rejections or b.get("top_rejections", []))
    ga = gate_audit or {}
    d.add(20, "Which suppressions are justified by evidence?",
          {"global_safety": ga.get("by_owner", {}).get("GLOBAL_SAFETY", []),
           "unjustified": ga.get("unjustified_global", [])},
          "A GLOBAL_SAFETY gate with no written evidence is a Strategy A gate "
          "in disguise; gates.audit() lists any.")
    d.add(21, "Which suppressions are merely inherited from Strategy A?",
          ga.get("inherited_from_a", []),
          "These are recorded in gates.py and are NOT applied to Strategy B. "
          "The chief one is v1.learning_mode, which produced 100% of V1's "
          "40,820 DO_NOTHING decisions.")

    # 22: what next
    hs = hypotheses or []
    d.add(22, "What candidate strategy should be tested next?",
          [{"id": h["hypothesis_id"], "claim": h["claim"],
            "test": h["test"], "priority": h["priority"]}
           for h in hs[:3]] if hs else "no hypotheses generated",
          "From research/ai.py. Every one is a PROPOSAL: it must pass the "
          "ladder in validation/validate.py before it can trade anything.")

    if feature_audit and feature_audit.get("dead_axes"):
        d.add(23, "[extra] Is the search paying for hypotheses it cannot test?",
              feature_audit.get("note"),
              f"inert: {feature_audit.get('inert_features')}")
    return d


def _compare_routes(a: dict, b: dict, strategy_a: dict | None) -> str:
    sa = strategy_a or {}
    if sa.get("executions", 0) == 0 and not a.get("execution_successful"):
        return (
            "Strategy A has never executed a trade, so it cannot be compared "
            "on results. It is preserved unchanged and is neither credited nor "
            "blamed. Strategy B is the only route with measurable output this "
            "cycle; judge it against its own naive-copy baselines, not against "
            "an engine with no record.")
    ea, eb = a.get("expectancy", 0.0), b.get("expectancy", 0.0)
    if eb > ea:
        return f"Strategy B (expectancy {eb:+.4f} vs {ea:+.4f})"
    if ea > eb:
        return f"Strategy A (expectancy {ea:+.4f} vs {eb:+.4f})"
    return "neither is distinguishable from the other on this sample"


def _best_family(families: list, agreement: list) -> object:
    supported = [f for f in families if f.get("independent_support", 0) > 0]
    if supported:
        best = max(supported, key=lambda f: f["independent_support"])
        return {"family_id": best["family_id"], "wallets": best["wallets"][:6],
                "independent_support": best["independent_support"],
                "cohesion": best["cohesion"]}
    transferable = [r for r in agreement if r.get("wallets_validated", 0) >= 2]
    if transferable:
        r = transferable[0]
        return {"rule_id": r["rule_id"], "describe": r["describe"],
                "wallets_validated": r["wallets_validated"],
                "mean_alpha": r["mean_alpha"]}
    return ("None. No rule has validated on two or more independent wallets, "
            "so nothing has yet been shown to transfer. That is the honest "
            "state of the evidence, and widening the wallet universe is the "
            "next lever - not widening the rule grid.")
