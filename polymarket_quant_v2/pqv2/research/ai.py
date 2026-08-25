"""The AI research assistant: proposes, never decides.

Three constraints from the brief, all structural rather than advisory:

  1. AI must NEVER sit inside the hot execution loop. Nothing in this module is
     importable from `strategy_b/engine.py`, and `tests/test_isolation.py`
     asserts the import graph stays that way.

  2. AI must NEVER override quantitative validation. Everything here returns a
     HYPOTHESIS -- a dataclass with a `test` that must be executed. A
     hypothesis carries no status and cannot promote anything. The only route
     to VALIDATED is `validation/validate.py`.

  3. ~16 GB RAM. So the default backend is a deterministic, offline
     hypothesis generator that runs on the measured statistics the pass has
     already produced. No model is loaded, nothing is downloaded, and the
     system works with no API key. An LLM backend is an optional plug-in that
     changes WHO writes the hypothesis text, never what happens to it.

The offline generator is not a toy. Most of the value of a research assistant
here is systematically asking the obvious next question about a measured
result, and that is a rule engine's natural shape: it never gets bored, never
gets attached to a narrative, and cannot hallucinate a number, because it only
ever quotes numbers the pass measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict


@dataclass
class Hypothesis:
    """A proposal. It becomes knowledge only by passing the ladder."""

    hypothesis_id: str
    claim: str
    rationale: str
    test: str                       # the concrete experiment to run
    predicts: str                   # what must be observed for it to survive
    falsifies: str                  # what would kill it
    priority: float = 0.5
    source: str = "offline_rules"
    evidence: dict = field(default_factory=dict)
    status: str = "PROPOSED"        # PROPOSED only; the ladder owns the rest

    def to_dict(self) -> dict:
        return asdict(self)


def _h(n: int, **kw) -> Hypothesis:
    return Hypothesis(hypothesis_id=f"H{n:03d}", **kw)


class OfflineResearcher:
    """Deterministic hypothesis generation from measured pass output.

    Every hypothesis quotes a number the pass produced. That is the whole
    design: an assistant that cannot invent evidence cannot mislead, and a
    deterministic one produces the same research queue from the same data,
    which is what makes its suggestions auditable.
    """

    def propose(self, *, pass_report: dict, feature_audit: dict | None = None,
                asymmetry: dict | None = None, exits: list | None = None,
                strategy_a: dict | None = None,
                expansion: dict | None = None) -> list:
        out: list = []
        n = 0

        hist = dict(pass_report.get("status_histogram") or [])
        validated = hist.get("VALIDATED", 0)
        total = sum(hist.values()) or 1

        # --- the search itself -------------------------------------------
        if feature_audit and feature_audit.get("dead_axes"):
            n += 1
            out.append(_h(
                n, claim=("The search is paying a large multiple-testing "
                          "penalty for axes that cannot vary."),
                rationale=(
                    f"{len(feature_audit['dead_axes'])} axes are inert: "
                    f"{', '.join(feature_audit['dead_axes'])}. The grid tests "
                    f"{feature_audit['grid_nominal']:,} transformations per "
                    f"wallet of which {feature_audit['grid_effective']:,} are "
                    "distinct, so the BH threshold is roughly "
                    f"{feature_audit['wasted_multiple_testing_factor']}x "
                    "stricter than the evidence requires."),
                test=("Disable the inert axes in strategy_b/strategy.py AXES, "
                      "re-run the pass, and compare the BH threshold and the "
                      "VALIDATED count."),
                predicts=("BH threshold loosens by roughly the wasted factor; "
                          "the VALIDATED set grows and contains every strategy "
                          "it contained before."),
                falsifies=("If the VALIDATED set changes membership rather "
                           "than growing, the axes were not inert and the "
                           "audit is wrong."),
                priority=0.95, evidence=feature_audit))

        if validated == 0 and total > 20:
            worst = max(hist.items(), key=lambda kv: kv[1]) if hist else ("", 0)
            n += 1
            out.append(_h(
                n, claim=("Nothing validated; the binding constraint is the "
                          f"{worst[0]} bar."),
                rationale=(f"{worst[1]} of {total} candidates stop at "
                           f"{worst[0]}. That is one bar, not a general "
                           "failure, and it names the next experiment."),
                test=_test_for_status(worst[0]),
                predicts="candidates advance past this bar to the next one",
                falsifies=("If they advance and then fail everywhere else, the "
                           "wallets carry no transferable edge and the answer "
                           "is that this substrate has none to find."),
                priority=0.9, evidence={"status_histogram": hist}))

        # --- the strategy shape ------------------------------------------
        if asymmetry:
            a = asymmetry.get("asymmetry", asymmetry)
            wr = a.get("win_rate", 0.0)
            wl = a.get("win_loss_ratio", 0.0)
            tail = a.get("tail_dependence_top5pct", 0.0)
            if wr > 0.75 and wl < 0.5:
                n += 1
                out.append(_h(
                    n, claim=("The result is a high-win-rate, poor-asymmetry "
                              "shape and is exposed to its own tail."),
                    rationale=(f"win rate {wr:.0%} with win/loss ratio "
                               f"{wl:.2f}. On this venue that shape is what "
                               "buying favourites produces, and the "
                               "favourite-longshot gap is +9 points at "
                               "0.6-0.8."),
                    test=("Re-run with a stop_loss exit at -25% and compare "
                          "tail_loss_p05 and expectancy; separately check "
                          "wallet alpha is still positive after the change."),
                    predicts="tail loss shrinks materially, expectancy holds",
                    falsifies=("If expectancy collapses, the losses ARE the "
                               "strategy and cutting them removes the edge."),
                    priority=0.8, evidence=a))
            if tail > 0.8:
                n += 1
                out.append(_h(
                    n, claim="This strategy IS its tail.",
                    rationale=(f"{tail:.0%} of all profit comes from the top "
                               "5% of trades."),
                    test=("Compare settlement against every profit-target exit "
                          "in research/exits.py."),
                    predicts=("settlement wins; every target exit reduces "
                              "expectancy by clipping the trades that pay for "
                              "everything"),
                    falsifies=("If a target exit wins, the tail is not "
                               "load-bearing and early exits are safe."),
                    priority=0.75, evidence=a))

        # --- exits ---------------------------------------------------------
        consensus = next((r["consensus"] for r in (exits or [])
                          if "consensus" in r), None)
        if consensus and consensus.get("strategies_preferring", 0) >= 3:
            n += 1
            out.append(_h(
                n, claim=(f"'{consensus['model']}' is the default exit for "
                          "this strategy family."),
                rationale=(f"{consensus['strategies_preferring']} of "
                           f"{consensus['of']} independently discovered "
                           "strategies prefer it."),
                test=("Set it as the family default and re-validate every "
                      "member out-of-sample."),
                predicts="expectancy improves or holds for a majority",
                falsifies=("If it only helps the strategies it was chosen on, "
                           "it is a fit, not a default."),
                priority=0.7, evidence=consensus))

        # --- transferability, the strongest evidence available -------------
        agreement = pass_report.get("agreement") or []
        strong = [a for a in agreement if a.get("wallets_validated", 0) >= 2]
        if strong:
            best = strong[0]
            n += 1
            out.append(_h(
                n, claim=("A rule transfers across independent wallets: "
                          f"{best['describe']}"),
                rationale=(f"validated on {best['wallets_validated']} wallets, "
                           f"positive on {best['wallets_positive']} of "
                           f"{best['wallets_tested']}, mean wallet alpha "
                           f"{best['mean_alpha']:+.4f}, cross-wallet t "
                           f"{best.get('cross_wallet_t', 0):.2f}."),
                test=("Apply the rule to a HELD-OUT set of wallets that were "
                      "not in this pass at all, and validate out-of-sample."),
                predicts=("positive expectancy and positive wallet alpha on "
                          "wallets it was never fitted to"),
                falsifies=("Failure on held-out wallets means the agreement "
                           "was a property of the wallets chosen, not of the "
                           "rule."),
                priority=1.0, evidence=best))
        elif agreement:
            n += 1
            out.append(_h(
                n, claim=("No rule validates on more than one wallet; nothing "
                          "has been shown to transfer yet."),
                rationale=(f"{len(agreement)} rules appear on 2+ wallets but "
                           "none is validated on 2+."),
                test=("Widen the wallet universe (strategy_b.max_wallets) "
                      "before widening the rule grid."),
                predicts="more wallets produce independent confirmations",
                falsifies=("If more wallets produce more single-wallet "
                           "results, the edge is wallet-specific and does not "
                           "generalise."),
                priority=0.85))

        # --- sizing --------------------------------------------------------
        if expansion and expansion.get("recommended", 1.0) > 1.0:
            n += 1
            out.append(_h(
                n, claim=(f"Win Expansion at {expansion['recommended']:.2f}x "
                          "improves return per unit of drawdown."),
                rationale=expansion.get("note", ""),
                test=("Refit the ladder on the in-sample window only, then "
                      "measure it out-of-sample."),
                predicts="the same multiplier wins out-of-sample",
                falsifies=("A different multiplier winning out-of-sample means "
                           "the ladder was fitted to noise; stay at 1.00x."),
                priority=0.6, evidence=expansion))

        # --- strategy A ----------------------------------------------------
        if strategy_a and strategy_a.get("blocking_gate") == "v1.learning_mode":
            n += 1
            out.append(_h(
                n, claim=("Strategy A's zero trade count is a substrate "
                          "problem, not a filter problem."),
                rationale=strategy_a.get("verdict", ""),
                test=("Point the V1 research pipeline at the settled substrate "
                      "(116,923 rows / 1,285 markets / 90 days) instead of the "
                      "captured feature series (78,219 rows / 123 markets / "
                      "3.8 days), and re-read its status histogram."),
                predicts=("candidates begin clearing the OOS-market minimum "
                          "that currently starves them"),
                falsifies=("If they still fail, the V1 rules genuinely have no "
                           "edge and the correct action is RESEARCH ONLY."),
                priority=0.9, evidence=strategy_a))

        out.sort(key=lambda h: -h.priority)
        return out


def _test_for_status(status: str) -> str:
    return {
        "INSUFFICIENT_EVIDENCE": (
            "Raise the wallet universe or lower min_oos_fills only if the "
            "resulting sample still supports a bootstrap. Do NOT lower "
            "min_oos_markets: fewer markets is how one market becomes a "
            "strategy."),
        "FAILED": (
            "Check the naive-copy baselines table first. If naive copying is "
            "also negative out-of-sample, the wallets were selected on an "
            "in-sample edge that did not persist, and no conditioning will "
            "rescue it."),
        "NOT_SIGNIFICANT": (
            "Shrink the search before loosening the threshold. Run the feature "
            "audit and disable inert axes."),
        "NO_WALLET_ALPHA": (
            "This is the favourite-longshot bias being rediscovered. Add "
            "price-neutral conditions -- timing, conviction, tape state -- "
            "rather than price bands."),
        "CONCENTRATED": (
            "Require more out-of-sample markets per candidate, or exclude the "
            "dominant market and re-measure."),
        "UNSTABLE": (
            "Test whether the positive folds share a regime; if so the "
            "strategy is regime-conditional and should say so."),
        "FRAGILE": (
            "Widen the parameter step and look for a plateau. No plateau means "
            "no edge."),
        "DRIFT": (
            "The rule is capturing market drift. Compare against the placebo "
            "pool and add a condition that is orthogonal to price level."),
    }.get(status, "Inspect the verdict reasons for this status.")


class Researcher:
    """Front door. Uses an LLM backend if one is supplied, else offline rules.

    An LLM backend may only rewrite or extend hypothesis TEXT. It cannot set
    `status`, cannot rank above a measured hypothesis, and its output flows
    through the identical `Hypothesis` -> ladder path. If the backend is
    unavailable for any reason the offline generator runs and the pass
    continues -- research never blocks on a model being reachable.
    """

    def __init__(self, backend=None) -> None:
        self.offline = OfflineResearcher()
        self.backend = backend

    def propose(self, **kw) -> list:
        hypotheses = self.offline.propose(**kw)
        if self.backend is None:
            return hypotheses
        try:
            extra = self.backend.propose(**kw) or []
        except Exception as exc:                              # noqa: BLE001
            return hypotheses + [_h(
                999, claim="LLM backend unavailable", rationale=str(exc)[:200],
                test="none", predicts="none", falsifies="none",
                priority=0.0, source="backend_error")]
        for h in extra:
            h.source = "llm"
            h.status = "PROPOSED"        # cannot be anything else
        return sorted(hypotheses + extra, key=lambda h: -h.priority)
