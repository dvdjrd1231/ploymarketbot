"""The human-readable dashboard (Parts 24 and 25).

Written to be read by someone deciding whether to keep going, so it leads with
what the data could support, then answers the two questions in separate
sections, and ends with a promotion recommendation that defaults to "stay
research-only" and has to be argued out of it.

Nothing is computed here. Every number is read from the run result, so the
report and the JSON can never disagree.
"""

from __future__ import annotations

from typing import Any

from .signal import STAGE_OBSERVE, STAGE_PAPER, STAGE_RESEARCH_ONLY


def _rule(title: str) -> str:
    return f"\n{title}\n{'=' * len(title)}"


def _sub(title: str) -> str:
    return f"\n{title}\n{'-' * len(title)}"


def _pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):.{digits}%}"
    except (TypeError, ValueError):
        return "n/a"


def _num(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return "n/a"


def _wrap(text: str, width: int) -> list:
    """Wrap a sentence for the fixed-width report. No dependency, no drama."""
    words, lines, current = str(text).split(), [], ""
    for word in words:
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines or [""]


def _v1_block(add, block, indent="  "):
    """One three-class report, printed the same way everywhere."""
    if not block:
        add(indent + "  no report")
        return
    add(f"{indent}  eligible {block.get('eligibleConditions', 0):,} - "
        f"predictions {block.get('predictions', 0):,} - resolved "
        f"{block.get('resolved', 0):,} - unresolved "
        f"{block.get('unresolved', 0):,}")
    rejected = block.get("rejected") or {}
    if rejected:
        add(f"{indent}  rejected: " + ", ".join(
            f"{v:,} {k}" for k, v in list(rejected.items())[:4]))
    if not block.get("resolved"):
        add(f"{indent}  no resolved predictions yet - accuracy is PENDING, "
            "not zero. Predictions accumulate first and resolve naturally.")
        return
    add(f"{indent}  accuracy {_pct(block.get('accuracy'))} - balanced "
        f"{_pct(block.get('balancedAccuracy'))} - majority baseline "
        f"{_pct(block.get('majorityBaseline'))} - beats it: "
        f"{block.get('beatsMajorityBaseline')}")
    interval = block.get("confidenceInterval95") or {}
    if interval.get("available"):
        add(f"{indent}  95% CI on accuracy "
            f"[{_pct(interval['low'], 1)}, {_pct(interval['high'], 1)}] "
            f"over n={interval['n']:,}")
    for cls, stats in (block.get("perClass") or {}).items():
        add(f"{indent}  {cls:<22} precision {_pct(stats['precision'], 1):>7} "
            f"recall {_pct(stats['recall'], 1):>7} F1 "
            f"{_pct(stats['f1'], 1):>7}  (predicted {stats['predicted']:,}, "
            f"actual {stats['actual']:,})")
    matrix = sorted((block.get("confusionMatrix") or {}).items(),
                    key=lambda kv: -kv[1])[:6]
    add(f"{indent}  confusion (predicted|actual): "
        + ", ".join(f"{k} {v:,}" for k, v in matrix))
    prob = block.get("probabilistic") or {}
    if prob.get("available"):
        calib = prob.get("calibration") or {}
        add(f"{indent}  log loss {prob.get('logLoss')} - Brier "
            f"{prob.get('brierScore')} - expected calibration error "
            f"{calib.get('expectedCalibrationError')}   (lower is better; a "
            "hard rule claims certainty and is punished for it)")


def render(result: dict) -> str:
    lines: list[str] = []
    add = lines.append

    add("WALLET STATE TRANSITION RESEARCH")
    add("=" * 32)
    add("")
    add("Two questions, answered separately and never averaged:")
    add("  A. can we predict what the wallet does next?")
    add("  B. can we make money acting on that prediction?")
    add("A good answer to A is not evidence about B.")

    if not result.get("available"):
        add("")
        add(f"NO RESULT: {result.get('reason', 'unknown')}")
        return "\n".join(lines)

    # -- data ---------------------------------------------------------------
    audit = result.get("dataAudit") or {}
    add(_rule("WHAT THE DATA SUPPORTS"))
    add(f"  {audit.get('trades', 0):,} trades · "
        f"{audit.get('wallets', 0):,} wallets · "
        f"{audit.get('markets', 0):,} markets · "
        f"{audit.get('spanDays', 0)} day window")
    add(f"  explicit share size on "
        f"{_pct(audit.get('explicitShareCoverage'), 1)} of rows — inventory "
        "is exact, not inferred from cash/price")
    add(f"  settled markets: {audit.get('settledMarkets', 0):,} · "
        f"order-book tokens: {audit.get('quoteTokens', 0):,} over "
        f"{audit.get('quoteSpanDays', 0)} days")
    for warning in audit.get("warnings", []):
        add(f"  ! {warning}")
    unavailable = [k for k, v in (audit.get("fields") or {}).items()
                   if v.get("status") == "UNAVAILABLE"]
    if unavailable:
        add(f"  UNAVAILABLE fields (not fabricated): {', '.join(unavailable)}")

    census = result.get("census") or {}
    add(_sub("POPULATION"))
    add(f"  {census.get('episodes', 0):,} (wallet, condition) episodes")
    add(f"  {census.get('switched', 0):,} reached an opposite-side buy")
    add(f"  {census.get('labelled', 0):,} have a finished lifecycle; "
        f"{census.get('truncated', 0):,} are TRUNCATED by the end of the tape "
        "and are excluded from accuracy")
    add(f"  labels: {census.get('aggressive', 0):,} aggressive, "
        f"{census.get('protect', 0):,} protect, "
        f"{census.get('directional', 0):,} directional "
        "(directional is a third outcome the two-class rule cannot predict; "
        "it is excluded and counted, never folded in)")
    add(f"  snapshots at the signal horizon: "
        f"{census.get('validSnapshots', 0):,} valid, "
        f"{census.get('invalidSnapshots', 0):,} invalid")

    sensitivity = result.get("labelSensitivity") or {}
    if sensitivity:
        add(_sub("LABEL-COMPLETENESS SENSITIVITY"))
        for label, block in (sensitivity.get("cutoffDays") or {}).items():
            marker = "  <- default" if block.get("isDefault") else ""
            add(f"    {label:>4}  graded {block.get('graded', 0):>6}  "
                f"accuracy {_pct(block.get('accuracy'), 1):>7}  "
                f"balanced {_pct(block.get('balancedAccuracy'), 1):>7}  "
                f"base rate {_pct(block.get('baseRateAggressive'), 1):>7}"
                f"{marker}")
        add(f"    {sensitivity.get('note', '')}")

    # -- STRATEGY MODEL V1 - THE PRIMARY FROZEN MODEL -----------------------
    v1 = result.get("strategyV1") or {}
    if v1:
        add(_rule("RN1 STRATEGY MODEL V1 - the authoritative frozen model"))
        add("  initialPrice <= 0.20 AND initialCapital >= $5 -> AGGRESSIVE")
        add("  initialPrice >= 0.80                          -> DIRECTIONAL")
        add("  otherwise                                     -> PROTECT")
        add(f"  prospective boundary: {v1.get('prospectiveBoundaryUtc')} - "
            f"freshness window {v1.get('freshnessMinutes')} min")
        redeem = v1.get("redeemCheck") or {}
        add(f"  REDEEM gate: {redeem.get('status')} - {redeem.get('note', '')}")
        for title, key in (
                ("RETROSPECTIVE (all conditions - NOT forward)",
                 "retrospective"),
                ("CLEAN PROSPECTIVE (after the frozen boundary)",
                 "cleanProspective")):
            add(_sub("  " + title))
            _v1_block(add, v1.get(key) or {})
        rn1_v1 = v1.get("rn1Only") or {}
        if rn1_v1.get("available"):
            add(_sub("  RN1 ITSELF"))
            add(f"    {rn1_v1.get('episodes', 0)} condition(s) in this store")
            _v1_block(add, rn1_v1.get("retrospective") or {}, indent="    ")
        source = (v1.get("sourceBenchmark") or {}).get("retrospective250") or {}
        if source:
            add(_sub("  vs SOURCE BENCHMARK (250-condition retrospective)"))
            add(f"    source:    DIRECTIONAL {source.get('DIRECTIONAL', 0):.1%}"
                f"  PROTECT {source.get('PROTECT_REBALANCE', 0):.1%}"
                f"  AGGRESSIVE {source.get('AGGRESSIVE_OPPOSITE', 0):.1%}")
            actual = (v1.get("retrospective") or {}).get(
                "actualDistribution") or {}
            total = sum(actual.values()) or 1
            add(f"    this data: DIRECTIONAL "
                f"{actual.get('DIRECTIONAL', 0) / total:.1%}"
                f"  PROTECT {actual.get('PROTECT_REBALANCE', 0) / total:.1%}"
                f"  AGGRESSIVE "
                f"{actual.get('AGGRESSIVE_OPPOSITE', 0) / total:.1%}")
            add("    A benchmark, not an expected outcome. A difference here "
                "is a difference in POPULATION before it is a difference in "
                "the wallet.")
        capital = v1.get("capitalInterpretation") or {}
        if capital.get("conditionsUnderThePriceArm"):
            add(_sub("  interpretation check: what 'initialCapital' means"))
            add(f"    {capital['conditionsUnderThePriceArm']:,} condition(s) "
                f"under the <=$0.20 arm - strict reading fires "
                f"{capital['aggressiveUnderStrictReading']:,}, loose reading "
                f"{capital['aggressiveUnderLooseReading']:,} - they disagree "
                f"on {capital['disagreements']:,} "
                f"({_pct(capital['disagreementShare'], 1)})")
            add(f"    {capital.get('note', '')}")

    transitions = result.get("stateTransitions") or {}
    if transitions:
        add(_rule("STATE TRANSITIONS - the object of the research"))
        add(f"  {transitions.get('trajectories', 0):,} trajectories - "
            f"{transitions.get('reachedTwoSided', 0):,} reached two-sided "
            f"({_pct(transitions.get('twoSidedShare'), 1)})")
        add("  terminal states:")
        for state, count in list(
                (transitions.get("terminalStates") or {}).items())[:8]:
            add(f"    {count:>7,}  {state}")
        add("  P(next | current), where the sample supports it:")
        for state, block in (transitions.get("transitionProbabilities")
                             or {}).items():
            if not block.get("sufficient"):
                add(f"    {state[:38]:<40} {block.get('reason', '')}")
                continue
            nxt = ", ".join(f"{k.replace('STATE_', '')[:22]} {v:.0%}"
                            for k, v in list(block["next"].items())[:3])
            add(f"    {state[:38]:<40} n={block['observations']:<7,} {nxt}")
        add(f"  {transitions.get('note', '')}")

    composition = result.get("populationComposition") or {}
    if composition:
        add(_rule("POPULATION COMPOSITION - read this before V1's accuracy"))
        unconditional = composition.get("unconditional") or {}
        source = unconditional.get("source") or {}
        ours = unconditional.get("thisData") or {}
        add("  UNCONDITIONAL (every condition):")
        add(f"    source:    DIRECTIONAL {_pct(source.get('DIRECTIONAL'), 1)}"
            f"  PROTECT {_pct(source.get('PROTECT_REBALANCE'), 1)}"
            f"  AGGRESSIVE {_pct(source.get('AGGRESSIVE_OPPOSITE'), 1)}")
        add(f"    this data: DIRECTIONAL {_pct(ours.get('DIRECTIONAL'), 1)}"
            f"  PROTECT {_pct(ours.get('PROTECT_REBALANCE'), 1)}"
            f"  AGGRESSIVE {_pct(ours.get('AGGRESSIVE_OPPOSITE'), 1)}")
        add(f"    became two-sided: {_pct(unconditional.get('twoSidedShare'), 1)}"
            f" here vs {_pct(unconditional.get('sourceTwoSidedShare'), 1)} in "
            "the source")
        conditional = composition.get("conditionalOnTwoSided") or {}
        csource = conditional.get("source") or {}
        cours = conditional.get("thisData") or {}
        add("  CONDITIONAL ON REACHING THE TWO-SIDED STATE "
            "(the source's own subject matter):")
        add(f"    source:    PROTECT {_pct(csource.get('PROTECT_REBALANCE'), 1)}"
            f"  AGGRESSIVE {_pct(csource.get('AGGRESSIVE_OPPOSITE'), 1)}")
        add(f"    this data: PROTECT {_pct(cours.get('PROTECT_REBALANCE'), 1)}"
            f"  AGGRESSIVE {_pct(cours.get('AGGRESSIVE_OPPOSITE'), 1)}"
            f"   (n={conditional.get('observations') or 0:,})")
        add("")
        for line in _wrap(str(composition.get("reading", "")), 74):
            add("  " + line)
        add("  candidate explanations for the base-rate gap:")
        for reason in composition.get("candidateExplanations", []):
            for index, line in enumerate(_wrap(reason, 70)):
                add(("    - " if index == 0 else "      ") + line)

    # -- QUESTION A: the supporting benchmark --------------------------------
    add(_rule("SUPPORTING BENCHMARK - post-opposite-buy +3m rule"))
    add("  A DIFFERENT and easier question: it fires three minutes after the")
    add("  opposite buy has already happened, on a two-class population. Not")
    add("  comparable with V1's three-class numbers.")
    add(_rule("QUESTION A - RN1 EXACT REPLICATION (frozen, untouched)"))
    rn1 = result.get("rn1") or {}
    if not rn1.get("available"):
        add(f"  {rn1.get('reason', 'unavailable')}")
    else:
        classification = rn1.get("classification") or {}
        add(f"  switched conditions : {classification.get('switchedConditions', 0)}")
        add(f"  valid snapshots     : {classification.get('validSnapshots', 0)}")
        add(f"  invalid snapshots   : {classification.get('invalidSnapshots', 0)}")
        for reason, count in (classification.get("invalidReasons")
                              or {}).items():
            add(f"      {count} x {reason}")
        add(f"  graded              : {classification.get('graded', 0)} "
            f"(excluded: {classification.get('directionalExcluded', 0)} "
            f"directional, {classification.get('truncatedExcluded', 0)} "
            "truncated)")
        add(f"  protect / aggressive: {classification.get('protectCount', 0)} "
            f"/ {classification.get('aggressiveCount', 0)}  "
            f"(base rate {_pct(classification.get('baseRateAggressive'), 1)})")
        add(f"  accuracy            : "
            f"{_pct(classification.get('accuracy'))}")
        add(f"  balanced accuracy   : "
            f"{_pct(classification.get('balancedAccuracy'))}   <- read this one")
        add(f"  aggressive P / R    : "
            f"{_pct(classification.get('aggressivePrecision'))} / "
            f"{_pct(classification.get('aggressiveRecall'))}")
        add(f"  protect    P / R    : "
            f"{_pct(classification.get('protectPrecision'))} / "
            f"{_pct(classification.get('protectRecall'))}")
        matrix = classification.get("confusionMatrix") or {}
        add("  confusion matrix    : "
            f"TP {matrix.get('predAggressive_actualAggressive', 0)} · "
            f"FP {matrix.get('predAggressive_actualProtect', 0)} · "
            f"TN {matrix.get('predProtect_actualProtect', 0)} · "
            f"FN {matrix.get('predProtect_actualAggressive', 0)}")
        benchmark = rn1.get("sourceBenchmark") or {}
        add(f"  vs source benchmark : {benchmark.get('verdict', '')}")

    horizons = result.get("horizons") or {}
    if horizons:
        add(_sub("HORIZON SENSITIVITY (all wallets, frozen rule)"))
        for label, report in horizons.items():
            add(f"  {label:>5}  graded {report.get('graded', 0):>6}  "
                f"accuracy {_pct(report.get('accuracy'), 1):>7}  "
                f"balanced {_pct(report.get('balancedAccuracy'), 1):>7}")
        add(f"  {result.get('horizonNote', '')}")

    # -- QUESTION B ---------------------------------------------------------
    add(_rule("QUESTION B — DOES TRADING IT MAKE MONEY?"))
    if not rn1.get("available"):
        add("  no RN1 trading result: see above")
    else:
        add("  Only AGGRESSIVE predictions trade. Buy the OPPOSITE outcome at")
        add("  the signal snapshot, hold, settle. Three execution assumptions,")
        add("  three stakes. SETTLED is the realised number.")
        for assumption, stakes in (rn1.get("trading") or {}).items():
            add(_sub(f"  execution: {assumption}"))
            for stake, block in stakes.items():
                settled = block.get("settled") or {}
                marked = block.get("markedToMarket") or {}
                add(f"    {stake:>4}  signals {block.get('signals', 0):>4}  "
                    f"filled {block.get('filled', 0):>4}  "
                    f"unfilled {block.get('unfilled', 0):>4}  "
                    f"no-exit {block.get('skippedNoExit', 0):>4}")
                if settled.get("trades"):
                    add(f"          SETTLED  n={settled['trades']:<4} "
                        f"win {_pct(settled.get('winRate'), 1):>6}  "
                        f"net {_num(settled.get('netPnl')):>9}  "
                        f"ROI {_pct(settled.get('roi'), 1):>8}  "
                        f"maxDD {_num(settled.get('maxDrawdown')):>8}")
                    boot = settled.get("bootstrap") or {}
                    if boot.get("available"):
                        add(f"                   95% CI on mean trade ROI "
                            f"[{_pct(boot['ci95Low'], 1)}, "
                            f"{_pct(boot['ci95High'], 1)}] — "
                            f"{boot.get('reading')}")
                    else:
                        add(f"                   {boot.get('reason', '')}")
                else:
                    add("          SETTLED  none — no traded market resolved "
                        "inside this tape")
                if marked.get("trades"):
                    add(f"          MARKED   n={marked['trades']:<4} "
                        f"net {_num(marked.get('netPnl')):>9} "
                        "(open positions, NOT profit)")

        portfolio = rn1.get("portfolio") or {}
        if portfolio:
            add(_sub("  portfolio: capping simultaneous positions"))
            for cap, block in (portfolio.get("maxSimultaneous")
                               or {}).items():
                settled = block.get("settled") or {}
                add(f"    max {cap:>2} open  filled {block.get('filled', 0):>4}"
                    f"  blocked {block.get('blockedByPortfolio', 0):>4}"
                    f"  settled {settled.get('trades', 0):>4}"
                    f"  ROI {_pct(settled.get('roi'), 1):>8}")
            add(f"    {portfolio.get('note', '')}")

        add(_sub("  baselines, over the same cases"))
        for name, block in (rn1.get("baselines") or {}).items():
            if name == "no_trade":
                add("    no_trade             ROI    0.00%   (doing nothing)")
                continue
            settled = (block.get("settled") or {})
            add(f"    {name:<20} n={settled.get('trades', 0):<4} "
                f"ROI {_pct(settled.get('roi'), 1):>8}  "
                f"net {_num(settled.get('netPnl')):>9}")
        verdict = rn1.get("signalAddsValue") or {}
        add(f"    -> {verdict.get('verdict', '')}")

    # -- generalisation -----------------------------------------------------
    cross = result.get("crossWallet") or {}
    if cross:
        add(_rule("CROSS-WALLET — does the frozen rule generalise?"))
        add(f"  wallets with switched episodes : "
            f"{cross.get('walletsWithSwitches', 0):,}")
        add(f"  wallets with enough data       : "
            f"{cross.get('walletsWithEnoughData', 0):,} "
            f"(>= {cross.get('minSamples')} graded cases)")
        add(f"  statistically meaningful       : "
            f"{cross.get('statisticallyMeaningful', 0)}")
        add(f"  negative evidence              : "
            f"{cross.get('negativeEvidence', 0)}")
        add(f"  promising but inconclusive     : {cross.get('promising', 0)}")
        add(f"  median / mean accuracy         : "
            f"{_pct(cross.get('medianAccuracy'), 1)} / "
            f"{_pct(cross.get('meanAccuracy'), 1)}")
        best, worst = cross.get("best"), cross.get("worst")
        if best:
            add(f"  best  {best['key'][:14]}... n={best.get('graded')} "
                f"accuracy {_pct(best.get('accuracy'), 1)} — "
                f"{best.get('tierReason')}")
        if worst:
            add(f"  worst {worst['key'][:14]}... n={worst.get('graded')} "
                f"accuracy {_pct(worst.get('accuracy'), 1)} — "
                f"{worst.get('tierReason')}")
        add(f"  -> {cross.get('reading', '')}")
        add("  (wallets are ranked by the LOWER BOUND of their interval, not "
            "by accuracy: 5-of-5 is a better score and worse evidence than "
            "40-of-55)")

    market = result.get("crossMarket") or {}
    if market:
        add(_rule("CROSS-MARKET — where does it work?"))
        add(f"  {market.get('note', '')}")
        for dimension, block in (market.get("dimensions") or {}).items():
            add(_sub(f"  by {dimension.replace('_', ' ')}"))
            for bucket in block.get("buckets", [])[:8]:
                flag = "" if bucket.get("graded", 0) >= market.get(
                    "minSamples", 12) else "   (insufficient)"
                add(f"    {bucket['key'][:26]:<28} n={bucket.get('graded', 0):<5} "
                    f"accuracy {_pct(bucket.get('accuracy'), 1):>7}"
                    f"{flag}")
            if block.get("spread") is not None:
                add(f"    best {block.get('best')} vs worst "
                    f"{block.get('worst')} — spread "
                    f"{_pct(block.get('spread'), 1)}")

    # -- walk-forward and holdout -------------------------------------------
    walk = result.get("walkForward") or {}
    add(_rule("WALK-FORWARD (expanding window, chronological)"))
    if not walk.get("available"):
        add(f"  {walk.get('reason', 'unavailable')}")
    else:
        add(f"  mean accuracy {_pct(walk.get('meanAccuracy'), 1)} · "
            f"balanced {_pct(walk.get('meanBalancedAccuracy'), 1)} · "
            f"worst fold {_pct(walk.get('worstFoldAccuracy'), 1)} · "
            f"stdev {_num(walk.get('accuracyStdev'), 3)}")
        add(f"  {walk.get('stability', '')}")

    holdout = result.get("holdout") or {}
    if holdout:
        add(_rule("UNTOUCHED HOLDOUT (opened once, after freezing)"))
        add(f"  {holdout.get('frozenDescription', '')}")
        classification = holdout.get("classification") or {}
        add(f"  episodes {holdout.get('episodes', 0)} · graded "
            f"{classification.get('graded', 0)} · accuracy "
            f"{_pct(classification.get('accuracy'))} · balanced "
            f"{_pct(classification.get('balancedAccuracy'))}")
        add(f"  vs source: "
            f"{(holdout.get('sourceBenchmark') or {}).get('verdict', '')}")
        for assumption, block in (holdout.get("trading") or {}).items():
            settled = block.get("settled") or {}
            add(f"  {assumption:<13} filled {block.get('filled', 0):>4}  "
                f"settled {settled.get('trades', 0):>4}  "
                f"ROI {_pct(settled.get('roi'), 1):>8}  "
                f"net {_num(settled.get('netPnl')):>9}")

    # -- discovery ----------------------------------------------------------
    discovery = result.get("discovery") or {}
    add(_rule("MODEL DISCOVERY (development fit, validation score)"))
    if discovery.get("enabled") is False:
        add(f"  {discovery.get('note', 'disabled')}")
    else:
        add(f"  scikit-learn available: {discovery.get('sklearnAvailable')}")
        for name, block in (discovery.get("models") or {}).items():
            if block.get("available") is False:
                add(f"  {name:<32} unavailable — {block.get('reason', '')}")
                continue
            validation = block.get("validation") or {}
            if validation:
                add(f"  {name:<32} n={validation.get('graded', 0):<5} "
                    f"accuracy {_pct(validation.get('accuracy'), 1):>7} "
                    f"balanced {_pct(validation.get('balancedAccuracy'), 1):>7}")
            elif name == "wallet_specific":
                add(f"  {name:<32} {block.get('walletsFitted', 0)} wallet(s) "
                    f"fitted at >= {block.get('minSamples')} cases")
        add("  The FROZEN rule is never replaced by any of these. An optimised")
        add("  model is a separate version reported beside it (Part 14).")

    # -- integrity ----------------------------------------------------------
    leakage = result.get("leakageAudit") or {}
    add(_rule("LEAKAGE AUDIT"))
    add(f"  features checked {leakage.get('featuresChecked', 0):,} · "
        f"snapshots {leakage.get('snapshotsChecked', 0):,} · "
        f"violations {leakage.get('violationCount', 0)}")
    add(f"  CLEAN: {leakage.get('clean')}")
    add(f"  {leakage.get('note', '')}")
    top_unavailable = list((leakage.get("unavailableCounts") or {}).items())[:6]
    if top_unavailable:
        add("  most frequently unavailable features:")
        for name, count in top_unavailable:
            add(f"    {count:>6} x {name}")

    execution = result.get("executionRealism") or {}
    if execution:
        add(_rule("EXECUTION REALISM"))
        provenance = execution.get("priceProvenance") or {}
        add(f"  price lookups {provenance.get('lookups', 0):,} — "
            f"book {_pct(provenance.get('bookShare'), 1)}, "
            f"print {_pct(provenance.get('printShare'), 1)}, "
            f"unavailable {_pct(provenance.get('unavailableShare'), 1)}")
        add(f"  {execution.get('note', '')}")

    decomposition = result.get("baseRateDecomposition") or {}
    if decomposition:
        add(_rule("BASE-RATE DECOMPOSITION - why the populations differ"))
        add(f"  source two-sided rate {_pct(decomposition.get('sourceTwoSidedRate'), 1)}"
            f" vs this tape overall "
            f"{_pct(decomposition.get('baselineTwoSidedRate'), 1)}")
        add("  each row is a HYPOTHESIS about who the source was studying,")
        add("  applied to this tape and measured:")
        add(f"    {'cohort':<24} {'n':>9}  {'2-sided':>8}  {'gap':>7}  "
            f"{'gap closed':>10}")
        for row in (decomposition.get("cohorts") or [])[:14]:
            flag = "" if row.get("eligibleForSelection") else "  (too small)"
            add(f"    {row['cohort'][:23]:<24} {row['conditions']:>9,}  "
                f"{_pct(row['twoSidedRate'], 1):>8}  "
                f"{_pct(row['gapToSource'], 1):>7}  "
                f"{_pct(row['shareOfGapClosed'], 0):>10}{flag}")
        add("")
        for line in _wrap(str(decomposition.get("selectionReason", "")), 72):
            add("  " + line)
        fair = decomposition.get("v1OnComparableCohort") or {}
        if fair:
            add(_sub("  V1 ON THE COMPARABLE COHORT (the fair reproduction)"))
            _v1_block(add, fair.get("retrospective") or {}, indent="  ")
            for line in _wrap(str(fair.get("note", "")), 70):
                add("    " + line)

    promotion = result.get("promotion") or {}
    if promotion and promotion.get("available") is not False:
        add(_rule("PROMOTION LADDER - the V2 candidate (§27, §49)"))
        add(f"  candidate: {promotion.get('candidateVersion')}")
        add(f"  development {promotion.get('developmentConditions', 0):,} "
            "conditions")
        for label, key, base in (
                ("validation", "validation", "validation"),
                ("HOLDOUT (untouched)", "holdout", "holdout"),
                ("clean prospective", "cleanProspective", "cleanProspective")):
            block = promotion.get(key) or {}
            baseline = ((promotion.get("frozenV1Baseline") or {})
                        .get(base) or {})
            if not block.get("resolved"):
                continue
            add(f"  {label:<22} n={block.get('resolved', 0):<8,} "
                f"candidate balanced {_pct(block.get('balancedAccuracy'), 1):>7}"
                f"   frozen V1 "
                f"{_pct(baseline.get('balancedAccuracy'), 1):>7}")
        add("  gates:")
        for check in promotion.get("checks", []):
            mark = "PASS" if check.get("passed") else "FAIL"
            add(f"    [{mark}] {check['check']}")
        add("")
        for line in _wrap(str(promotion.get("verdict", "")), 72):
            add("  " + line)
        add(f"  recommended stage: {promotion.get('recommendedStage')}")

    dq = result.get("dataQuality") or {}
    if dq.get("available"):
        add(_rule("DATA QUALITY (§30)"))
        add(f"  mean {dq.get('meanScore')} - median {dq.get('medianScore')} "
            f"over {dq.get('predictions', 0):,} prediction(s)")
        tiers = dq.get("tiers") or {}
        add("  tiers: " + ", ".join(f"{v:,} {k}" for k, v in tiers.items()))
        add("  component means (multiplicative - a zero anywhere sinks it):")
        for name, value in sorted((dq.get("componentMeans") or {}).items(),
                                  key=lambda kv: kv[1]):
            add(f"    {value:.3f}  {name}")
        weakest = list((dq.get("weakestComponentCounts") or {}).items())[:3]
        if weakest:
            add("  most often the weakest link: " + ", ".join(
                f"{k} ({v:,})" for k, v in weakest))
        for line in _wrap(str(dq.get("note", "")), 72):
            add("  " + line)

    v1_families = result.get("discoveryV1") or {}
    if v1_families:
        add(_rule("STRATEGY_MODEL_V2_DISCOVERY - three-class model families"))
        add(f"  development {v1_families.get('developmentConditions', 0):,} / "
            f"validation {v1_families.get('validationConditions', 0):,} "
            "conditions, entry-time features only")
        for name, entry in (v1_families.get("models") or {}).items():
            if (entry or {}).get("available") is False:
                add(f"  {name:<32} unavailable - {entry.get('reason', '')}")
                continue
            validation = (entry or {}).get("validation") or entry or {}
            if not validation.get("resolved"):
                continue
            mark = " <- FROZEN" if name == "frozen_v1_benchmark" else ""
            add(f"  {name:<32} n={validation.get('resolved', 0):<8,} "
                f"accuracy {_pct(validation.get('accuracy'), 1):>7} "
                f"balanced {_pct(validation.get('balancedAccuracy'), 1):>7}"
                f"{mark}")
        tuned = (v1_families.get("models") or {}).get("tuned_rule") or {}
        if tuned.get("aggressivePriceMax") is not None:
            frozen = tuned.get("frozenV1") or {}
            add(f"  retuned thresholds: price<={tuned['aggressivePriceMax']} "
                f"cap>=${tuned['aggressiveCapitalMin']} "
                f"dir>={tuned['directionalPriceMin']}   vs FROZEN "
                f"{frozen.get('aggressivePriceMax')} / "
                f"${frozen.get('aggressiveCapitalMin')} / "
                f"{frozen.get('directionalPriceMin')}   "
                f"({tuned.get('searchScale', 0)} combinations examined)")
        wallet_specific = ((v1_families.get("models") or {})
                           .get("wallet_specific") or {})
        if wallet_specific.get("walletsFitted") is not None:
            add(f"  wallet-specific rules fitted: "
                f"{wallet_specific['walletsFitted']} "
                f"(>= {wallet_specific.get('minSamples')} conditions each)")
        add("")
        for line in _wrap(str(v1_families.get("verdict", "")), 72):
            add("  " + line)
        add("")
        for line in _wrap(str(v1_families.get("note", "")), 72):
            add("  " + line)

    discovery_structure = result.get("structureDiscovery") or {}
    if discovery_structure and discovery_structure.get("enabled") is not False:
        add(_rule("HIDDEN-STRUCTURE DISCOVERY (MDL, null allowed to win)"))
        add(f"  VERDICT: {discovery_structure.get('verdict')}")
        add(f"  {discovery_structure.get('candidatesSurvived', 0)} of "
            f"{discovery_structure.get('candidatesExamined', 0)} candidates "
            f"survived over "
            f"{discovery_structure.get('observations', 0):,} observations")
        for candidate in discovery_structure.get("allCandidates", [])[:8]:
            mark = "KEPT" if candidate.get("survived") else "    "
            add(f"  {mark} {candidate['name'][:26]:<28} "
                f"gain {candidate['gainBits']:>11,.0f} bits  "
                f"null best {candidate['shuffledGainMax']:>11,.0f}")
            add(f"       {candidate['reason'][:112]}")
        add(f"  {discovery_structure.get('note', '')}")

    registry_block = result.get("modelRegistry") or {}
    if registry_block:
        add(_rule("MODEL REGISTRY"))
        for record in registry_block.get("models", []):
            add(f"  [{record['status']:<17}] {record['version']}")
            if record.get("quarantineReason"):
                add(f"       EXCLUDED: {record['quarantineReason'][:150]}")
        add(f"  {registry_block.get('note', '')}")

    integration = result.get("integrationReport") or {}
    if integration:
        add(_rule("EXISTING-ENGINE IMPACT"))
        add(f"  current stage: {integration.get('currentStage')} · engine "
            f"modules importing this package: "
            f"{integration.get('engineModulesImportingThis')}")
        add(f"  baseline / with-features / delta / significance: "
            f"{integration.get('baselinePerformance')} / "
            f"{integration.get('performanceWithWalletFeatures')} / "
            f"{integration.get('delta')} / "
            f"{integration.get('statisticalSignificance')}")
        add(f"  {integration.get('whyUnmeasured', '')}")
        add("  features that WOULD be exposed at the observe stage:")
        for name in integration.get("featuresThatWouldBeExposed", []):
            add(f"    {name}")
        add("  proposed A/B design:")
        for step in integration.get("proposedABDesign", []):
            add(f"    {step}")

    # -- the recommendation -------------------------------------------------
    add(_rule("RECOMMENDATION"))
    for line in recommend(result):
        add("  " + line)
    return "\n".join(lines)


def recommend(result: dict) -> list:
    """Part 25's last block and Part 26's gate.

    Starts at "stay research-only" and requires evidence to move. The bar is
    deliberately awkward: behaviour prediction that beats the majority class,
    AND settled trading that beats always-enter, AND a bootstrap interval that
    excludes zero, AND a result that is not carried by one wallet. Any one of
    those missing leaves it where it is.
    """
    lines: list[str] = []
    rn1 = result.get("rn1") or {}
    cross = result.get("crossWallet") or {}
    holdout = result.get("holdout") or {}

    # -- Strategy Model V1 comes FIRST: it is the primary model -------------
    v1 = result.get("strategyV1") or {}
    composition = result.get("populationComposition") or {}
    if v1:
        forward = v1.get("cleanProspective") or {}
        retro = v1.get("retrospective") or {}
        pending = int(forward.get("unresolved") or 0)
        resolved = int(forward.get("resolved") or 0)
        if resolved:
            lines.append(
                f"V1 CLEAN PROSPECTIVE: {resolved} resolved of "
                f"{forward.get('predictions', 0)}, accuracy "
                f"{_pct(forward.get('accuracy'))} against a "
                f"{_pct(forward.get('majorityBaseline'), 1)} majority "
                f"baseline -> "
                f"{'SUPPORTED' if forward.get('beatsMajorityBaseline') else 'NOT ESTABLISHED'}")
        else:
            lines.append(
                f"V1 CLEAN PROSPECTIVE: {forward.get('predictions', 0):,} "
                f"prediction(s) made, {pending:,} still PENDING, 0 resolved "
                "-> NOT YET ANSWERABLE. This is the expected state of a fresh "
                "forward experiment and is not a negative result.")
        lines.append(
            f"V1 RETROSPECTIVE: accuracy {_pct(retro.get('accuracy'))} "
            f"against a {_pct(retro.get('majorityBaseline'), 1)} majority "
            f"baseline over {int(retro.get('resolved') or 0):,} resolved -> "
            f"{'beats it' if retro.get('beatsMajorityBaseline') else 'does NOT beat it'}"
            " (retrospective, NEVER forward evidence)")
    if composition:
        for line in _wrap(str(composition.get("reading", "")), 68):
            lines.append("   " + line)
    lines.append("")

    lines.append("Supporting benchmark (+3m post-opposite rule, two-class):")
    classification = (holdout.get("classification")
                      or rn1.get("classification") or {})
    graded = int(classification.get("graded") or 0)
    balanced = float(classification.get("balancedAccuracy") or 0.0)
    base_rate = float(classification.get("baseRateAggressive") or 0.0)
    majority = max(base_rate, 1.0 - base_rate)

    behaviour_ok = graded >= 50 and balanced > 0.55
    lines.append(
        f"A. Behaviour: {graded} graded case(s), balanced accuracy "
        f"{_pct(balanced)} against a {_pct(majority, 1)} majority-class "
        f"baseline -> {'SUPPORTED' if behaviour_ok else 'NOT ESTABLISHED'}")

    trading = ((rn1.get("trading") or {}).get("BASE") or {}).get("$10") or {}
    settled = trading.get("settled") or {}
    settled_n = int(settled.get("trades") or 0)
    boot = settled.get("bootstrap") or {}
    beats_baseline = "did NOT beat" not in str(
        (rn1.get("signalAddsValue") or {}).get("verdict", "did NOT beat"))
    profit_ok = (settled_n >= 20 and float(settled.get("roi") or 0) > 0
                 and boot.get("excludesZero") and beats_baseline)
    lines.append(
        f"B. Profit: {settled_n} SETTLED trade(s), ROI "
        f"{_pct(settled.get('roi'), 1)}, interval "
        f"{'excludes' if boot.get('excludesZero') else 'includes'} zero "
        f"-> {'SUPPORTED' if profit_ok else 'NOT ESTABLISHED'}")

    meaningful = int(cross.get("statisticallyMeaningful") or 0)
    graded_wallets = int(cross.get("walletsWithEnoughData") or 0)
    generalises = graded_wallets >= 5 and meaningful >= max(
        2, graded_wallets // 4)
    lines.append(
        f"C. Generalisation: {meaningful} of {graded_wallets} wallet(s) beat "
        f"their own majority-class baseline -> "
        f"{'SUPPORTED' if generalises else 'NOT ESTABLISHED'}")

    concentration = (settled.get("concentration")
                     or (trading.get("concentration") or {}))
    if concentration.get("dominated"):
        lines.append("D. Concentration: FLAGGED — "
                     + "; ".join(concentration.get("flags", [])))
    else:
        lines.append("D. Concentration: no single wallet, market or handful "
                     "of trades dominates the result")

    leakage = result.get("leakageAudit") or {}
    lines.append(f"E. Leakage: {leakage.get('violationCount', 0)} violation(s)"
                 + ("" if leakage.get("clean") else " — MUST BE FIXED BEFORE "
                                                    "ANY OTHER CONCLUSION"))

    lines.append("")
    if not leakage.get("clean", True):
        lines.append("VERDICT: BLOCKED. Fix the leakage before reading "
                     "anything else in this report.")
        stage = STAGE_RESEARCH_ONLY
    elif profit_ok and behaviour_ok and generalises:
        lines.append(
            "VERDICT: all three legs are supported on this window. The "
            "defensible next step is PAPER TRADING, not integration — one "
            "window is one window, and this one is short.")
        stage = STAGE_PAPER
    elif behaviour_ok and not profit_ok:
        lines.append(
            "VERDICT: the BEHAVIOUR is predictable and the PROFIT is not "
            "established. That is the single most likely outcome of this "
            "research and it is not a failure — it means the signal may be "
            "worth exposing as an observational FEATURE, and is not worth "
            "trading on its own. Part 28 exactly.")
        stage = STAGE_OBSERVE
    else:
        lines.append(
            "VERDICT: RESEARCH ONLY. The evidence on this window does not "
            "support exposing the signal to the engine in any form.")
        stage = STAGE_RESEARCH_ONLY
    if v1 and not int((v1.get("cleanProspective") or {}).get("resolved") or 0):
        stage = STAGE_RESEARCH_ONLY
        lines.append(
            "OVERRIDE: the primary model's clean prospective experiment has "
            "no resolved predictions yet. Nothing may advance past "
            "research-only until it does - that is what §50 protects.")
    lines.append(f"RECOMMENDED STAGE: {stage}")
    lines.append(
        "Nothing here promotes anything. The module stays disabled until a "
        "person changes configuration, and `integration_enabled` is a second, "
        "separate flag beyond `enabled`.")
    return lines


def summary(result: dict) -> dict:
    """Part 25's dashboard, machine-readable."""
    rn1 = result.get("rn1") or {}
    classification = rn1.get("classification") or {}
    trading = ((rn1.get("trading") or {}).get("BASE") or {}).get("$10") or {}
    settled = trading.get("settled") or {}
    cross = result.get("crossWallet") or {}
    holdout = (result.get("holdout") or {}).get("classification") or {}
    return {
        "rn1Replication": {
            "accuracy": classification.get("accuracy"),
            "balancedAccuracy": classification.get("balancedAccuracy"),
            "aggressivePrecision": classification.get("aggressivePrecision"),
            "aggressiveRecall": classification.get("aggressiveRecall"),
            "signals": classification.get("graded"),
        },
        "rn1Trading": {
            "trades": settled.get("trades"),
            "winRate": settled.get("winRate"),
            "roi": settled.get("roi"),
            "netPnl": settled.get("netPnl"),
            "maxDrawdown": settled.get("maxDrawdown"),
        },
        "crossWallet": {
            "walletsTested": cross.get("walletsWithEnoughData"),
            "positiveEvidence": cross.get("statisticallyMeaningful"),
            "medianAccuracy": cross.get("medianAccuracy"),
            "best": (cross.get("best") or {}).get("key"),
            "worst": (cross.get("worst") or {}).get("key"),
        },
        "holdout": {
            "graded": holdout.get("graded"),
            "accuracy": holdout.get("accuracy"),
            "balancedAccuracy": holdout.get("balancedAccuracy"),
        },
        "recommendation": recommend(result),
    }
