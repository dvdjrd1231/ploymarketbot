"""Convergence, inversion, failure states and synthesis — the research cycle.

The four questions the layer exists to ask, in order:

1. What independent discoveries point at the same market phenomenon?
2. What happens if we trade the opposite way?
3. Under what conditions does the relationship work or fail?
4. Can a simpler combination explain it?

Only after all four does anything become a candidate, and becoming a candidate
only means being allowed to queue for the same OOS markets as everything else.
Nothing in this module can promote, validate or trade. It writes to the
hypothesis store and, at the very end, registers ordinary `CandidateSpec`s
through the one registry.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import Callable, Iterable, Optional

from .hypothesis import (ADVERSARIAL_TEST, CONVERGING, NEW, OBSERVING,
                         OOS_PENDING, PROMOTED_TO_CANDIDATE, REJECTED,
                         SUPPORTED, WEAKENING, Hypothesis, HypothesisStore,
                         Observation, convergence_priority,
                         independent_sources, normalize)
from .sources import (SOURCE_CONVERGENT, SOURCE_INVERSE_ADVERSARIAL,
                      CandidateSpec, rule_complexity, source_of)

# Two observations are treated as describing one phenomenon at or above this
# similarity. Set by what it costs to be wrong in each direction: too low and
# unrelated rules are declared convergent, which manufactures the exact false
# confirmation the layer is supposed to detect; too high and the layer never
# fires. 0.7 means they agree on most of what either one says.
SIMILARITY_THRESHOLD = 0.70

# A hypothesis needs this many supporting observations before it is worth
# attacking. Below it there is nothing to attack.
MIN_OBSERVATIONS = 3


# -- 1. from discoveries to hypotheses ---------------------------------------

def hypotheses_from_candidates(rows: Iterable[dict]) -> list[Hypothesis]:
    """Group library rows by the RELATIONSHIP they describe.

    Rows are candidates that already exist — this reads the registry, it does
    not duplicate it. The output is one hypothesis per distinct normalised
    pattern, carrying the candidate IDs that led to it so every claim here
    can be traced back to a real row (§20).
    """
    by_signature: dict[str, Hypothesis] = {}
    for row in rows:
        rule = row.get("rule") or {}
        pattern = normalize(rule)
        signature = pattern.signature()
        if not signature:
            continue
        source = str(row.get("source") or "") or source_of(rule)
        hypothesis = by_signature.get(signature)
        if hypothesis is None:
            hypothesis = Hypothesis(
                signature=signature, pattern=pattern, status=NEW,
                direction=pattern.direction,
                inverse=_flip_direction(pattern.direction),
                relationship=_describe(pattern))
            by_signature[signature] = hypothesis
        hypothesis.candidates.append(str(row.get("id") or ""))
        if source not in hypothesis.sources:
            hypothesis.sources.append(source)
        family = str(row.get("family") or "")
        if family and family not in hypothesis.families:
            hypothesis.families.append(family)
        hypothesis.markets |= {str(m) for m in (row.get("markets") or [])}
        hypothesis.periods |= {str(p) for p in (row.get("periods") or [])}
        hypothesis.regimes |= {str(r) for r in (row.get("regimes") or [])}
        # An observation SUPPORTS when the candidate's own unseen record is
        # positive and CONTRADICTS when it is negative. A candidate with no
        # evidence yet votes neither way — silence is not agreement.
        trades = int(row.get("oos_trades") or 0)
        expectancy = float(row.get("oos_expectancy") or 0.0)
        if trades > 0:
            if expectancy > 0:
                hypothesis.supporting += 1
            else:
                hypothesis.contradicting += 1
    for hypothesis in by_signature.values():
        if hypothesis.supporting + hypothesis.contradicting > 0:
            hypothesis.status = OBSERVING
    return list(by_signature.values())


def _flip_direction(direction: str) -> str:
    return {"long": "short", "short": "long"}.get(direction, "")


def _describe(pattern: Observation) -> str:
    parts = []
    if pattern.price_movement:
        parts.append(pattern.price_movement.replace("_", " "))
    if pattern.sequence_order:
        parts.append("then ".join(pattern.sequence_order.split(">")))
    elif pattern.entry_state:
        parts.append(f"at {pattern.entry_state}")
    if pattern.probability_region:
        parts.append(f"in the {pattern.probability_region} band")
    if pattern.direction:
        parts.append(f"favours {pattern.direction}")
    if pattern.holding_behavior:
        parts.append(f"held {pattern.holding_behavior}")
    return ", ".join(parts) or "unclassified relationship"


# -- 2. cross-source convergence ---------------------------------------------

def convergence_groups(hypotheses: list[Hypothesis],
                       threshold: float = SIMILARITY_THRESHOLD
                       ) -> list[dict]:
    """Link hypotheses that appear to describe one phenomenon.

    A RELATIONSHIP, never a merge (§5). The members keep their identities and
    their separate records; what is created is the observation that they may
    be the same thing. Merging them would destroy the very independence that
    makes the convergence interesting.
    """
    groups: list[dict] = []
    assigned: set[str] = set()
    ordered = sorted(hypotheses, key=lambda h: -h.supporting)
    for anchor in ordered:
        key = anchor.signature
        if key in assigned:
            continue
        members = [anchor]
        assigned.add(key)
        for other in ordered:
            if other.signature in assigned:
                continue
            if anchor.pattern.similarity(other.pattern) >= threshold:
                members.append(other)
                assigned.add(other.signature)
        if len(members) < 2:
            continue
        contributions = [{"source": source, "markets": member.markets}
                         for member in members
                         for source in (member.sources or [""])]
        independent = independent_sources(contributions)
        groups.append({
            "id": f"cg:{anchor.signature}",
            "pattern": anchor.signature,
            "members": [m.signature for m in members],
            "sources": sorted({s for m in members for s in m.sources}),
            # THE NUMBER THAT MATTERS. Not how many members — how many of them
            # are genuinely independent. Three quant rules over one series are
            # one observation in three hats, and counting them as three is
            # precisely how a research system talks itself into a phantom.
            "independent": independent,
            "members_total": len(members),
        })
    return groups


# -- 3. inverse and adversarial ----------------------------------------------

# The adversarial battery (§13). Each entry is a name and what a PASS means,
# so a result is never ambiguous when read back months later.
ADVERSARIAL_TESTS = (
    ("inverse", "the opposite direction does not do better"),
    ("placebo", "a randomised version of the signal does not reproduce it"),
    ("neighbour_thresholds", "nearby thresholds behave similarly"),
    ("alternative_windows", "the effect survives a different measurement window"),
    ("market_subsets", "it holds on a different subset of markets"),
    ("cost_stress", "it survives realistic costs"),
    ("slippage_stress", "it survives adverse fills"),
    ("liquidity_stress", "it survives in thinner markets"),
)


def inverse_question(hypothesis: Hypothesis) -> Optional[Observation]:
    """The opposite research question (§6). Never assumed profitable — the
    point is to find out whether the original relationship is directional at
    all. A relationship whose inverse pays equally well is not an edge."""
    if not hypothesis.pattern.direction:
        return None
    return replace(hypothesis.pattern,
                   direction=_flip_direction(hypothesis.pattern.direction))


def adversarial_verdict(results: dict) -> tuple[str, str]:
    """Fold the battery into a status and a reason.

    The asymmetry is intentional. One decisive failure rejects; surviving
    everything only earns SUPPORTED, which still is not validation. A
    hypothesis whose inverse wins is not rejected — it is a real finding
    pointing the other way, and it earns its own hypothesis rather than
    being deleted (§13: record failures instead of deleting them).
    """
    outcomes = {name: _result_of(results.get(name))
                for name, _meaning in ADVERSARIAL_TESTS}
    tested = [name for name, value in outcomes.items() if value]
    if not tested:
        return ADVERSARIAL_TEST, "not yet attacked"
    if outcomes.get("inverse") == "inverse_won":
        return WEAKENING, ("the inverse performs better — the relationship "
                           "may be real and backwards")
    failed = [name for name, value in outcomes.items() if value == "failed"]
    if failed:
        return REJECTED, "failed " + ", ".join(sorted(failed))
    weakened = [name for name, value in outcomes.items()
                if value == "inconclusive"]
    if len(tested) < 3:
        return ADVERSARIAL_TEST, f"only {len(tested)} of the battery run"
    if weakened:
        return WEAKENING, "inconclusive on " + ", ".join(sorted(weakened))
    return SUPPORTED, f"survived {len(tested)} adversarial tests"


def _result_of(value) -> str:
    if isinstance(value, dict):
        return str(value.get("result") or "")
    return str(value or "")


# -- 4. failure states --------------------------------------------------------

def failure_states(hypothesis: Hypothesis, conditionals: dict) -> list[str]:
    """Where the edge disappears (§7).

    `conditionals` maps a condition label to its measured expectancy under
    that condition. A condition where the relationship reverses is recorded
    as a failure state, and a failure state that survives independent testing
    becomes a usable conditional FILTER — which is why they are stored rather
    than merely reported.
    """
    out = []
    for condition, expectancy in sorted(conditionals.items()):
        if float(expectancy) <= 0:
            out.append(f"{condition} (expectancy {float(expectancy):+.4f})")
    return out


# -- 5. synthesis -------------------------------------------------------------

def staged_combinations(components: list[str], max_size: int = 3
                        ) -> list[tuple[str, ...]]:
    """Combinations to try, smallest first, and never the full lattice (§8).

    Staged means exactly that: singles, then pairs, then triples, and the
    caller is expected to stop expanding when a smaller combination has not
    justified going further. Four components would give fifteen subsets and a
    fifth would give thirty-one — a combinatorial explosion is not a search,
    it is a guarantee of finding something.
    """
    out: list[tuple[str, ...]] = []
    ordered = sorted(components)
    for size in range(1, max(1, min(max_size, len(ordered))) + 1):
        out.extend(_combinations(ordered, size))
    return out


def _combinations(items: list[str], size: int) -> list[tuple[str, ...]]:
    if size == 0:
        return [()]
    if size > len(items):
        return []
    out = []
    for index, item in enumerate(items):
        for rest in _combinations(items[index + 1:], size - 1):
            out.append((item,) + rest)
    return out


def beats_baseline(candidate_expectancy: float, baseline_expectancy: float,
                   candidate_complexity: int, baseline_complexity: int,
                   margin_per_condition: float = 0.005) -> bool:
    """§9 and §14: complexity must be paid for, not merely tolerated.

    A more complicated strategy has to clear its simpler baseline by a margin
    that grows with the extra conditions it carries. Without the margin, any
    added condition wins on noise alone roughly half the time, and the library
    fills with elaborate versions of simple ideas.
    """
    extra = max(0, candidate_complexity - baseline_complexity)
    required = baseline_expectancy + extra * margin_per_condition
    return candidate_expectancy > required


def synthesize(hypothesis: Hypothesis, rule_template: dict,
               components: dict[str, dict], max_size: int = 3
               ) -> list[CandidateSpec]:
    """Turn a surviving hypothesis into candidates for the ONE pipeline.

    Every returned spec is an ordinary candidate: its own identity, its own
    version, zero inherited evidence, and the hypothesis's supporting markets
    permanently excluded — the markets that suggested the idea can never
    testify for it. Registering these is the only way anything from this layer
    reaches validation, and registration confers nothing.
    """
    if hypothesis.status not in (SUPPORTED, OOS_PENDING):
        return []
    specs: list[CandidateSpec] = []
    for combination in staged_combinations(list(components), max_size):
        rule = dict(rule_template)
        for name in combination:
            rule.update(components[name])
        rule["synthesized_from"] = hypothesis.id
        rule["components"] = list(combination)
        source = (SOURCE_CONVERGENT if len(hypothesis.sources) > 1
                  else source_of(rule_template))
        specs.append(CandidateSpec(
            signature=f"syn|{hypothesis.signature}|{'+'.join(combination)}",
            rule=rule,
            describe=f"SYNTHESIZED {'+'.join(combination)} from "
                     f"{hypothesis.relationship}"[:120],
            family=f"synth-{hypothesis.signature.split('|')[0]}",
            source=source,
            # The hypothesis's own markets are its discovery data.
            discovery_markets=set(hypothesis.markets),
            parent_id=hypothesis.id))
    # Simplest first, so the cheap explanation is tested before the elaborate
    # one and can pre-empt it entirely.
    specs.sort(key=lambda spec: rule_complexity(spec.rule))
    return specs


# -- 6. lifting candidate attacks to the hypothesis level ---------------------

def fold_adversarial(members: Iterable[str],
                     reports: dict) -> tuple[dict, dict, list[str]]:
    """Combine the attacks on a hypothesis's candidates into one verdict.

    The fold is deliberately PESSIMISTIC — a test is recorded as failed if it
    failed for any candidate expressing this relationship, and as survived
    only if some candidate ran it and none failed it. The asymmetry is the
    same one `adversarial_verdict` uses and rests on the same reasoning: a
    relationship is a claim about the market, so one candidate demonstrating
    that the claim breaks under attack is information about the relationship,
    while one candidate surviving is information only about that candidate.

    Averaging instead would let a hypothesis with eight weak candidates and
    one broken one report as mostly fine, which is how a research system
    launders a refutation into a statistic.

    Returns (results, details, failure states). The details travel with the
    results because a verdict read back in six months has to say WHICH
    candidate broke and by how much — "failed market_subsets" on its own is a
    label, and §14 asks for a decision that can be reconstructed.
    """
    from .adversarial import FAILED, INVERSE_WON, SURVIVED

    folded: dict[str, str] = {}
    details: dict[str, str] = {}
    states: list[str] = []
    for candidate_id in members:
        report = reports.get(candidate_id)
        if report is None:
            continue
        states.extend(report.failure_states)
        for test, result in report.results.items():
            if not result:
                continue
            current = folded.get(test)
            if current == FAILED:
                continue
            if result == FAILED or current is None:
                folded[test] = result
                details[test] = (f"{candidate_id}: "
                                 f"{report.details.get(test, '')}")[:200]
            elif current == SURVIVED and result == INVERSE_WON:
                folded[test] = INVERSE_WON
                details[test] = (f"{candidate_id}: "
                                 f"{report.details.get(test, '')}")[:200]
    return folded, details, sorted(set(states))


# -- 7. composition -----------------------------------------------------------

# Conditions a composite may carry, and where they come from. Restricted on
# purpose to fields the replay engines actually read: a composite that adds a
# key nothing honours replays identically to its parent, registers as a
# separate candidate, and then reports its parent's evidence as independent
# confirmation of the combination. That is a fabricated result, and it is the
# specific failure the `delay_bars` variant had before this pass.
COMPOSABLE = {
    "sequence": ("hold_bars", "gap_bars", "delay_bars", "direction"),
    "sharp_move": ("hold_bars", "delay_bars", "direction"),
    "wallet_behavior": ("hold_seconds", "direction"),
    "longshot": ("side",),
}


def compose(anchor: dict, partner: dict) -> Optional[dict]:
    """Build one composite rule from two independently discovered ones.

    §10: strategy A identifies the opportunity, strategy B the timing. The
    composite takes A's rule whole and imports only B's timing/direction
    parameters — never B's entry condition, because two entry conditions
    ANDed together is a new rule with a much smaller sample, and the shrinking
    sample is invisible in the composite's own statistics.

    Returns None when the two are the same rule type but identical in every
    composable field, since combining a rule with a copy of itself produces a
    candidate whose evidence would be pure duplication.
    """
    kind = str(anchor.get("type") or "threshold")
    if kind != str(partner.get("type") or "threshold"):
        return None
    fields = COMPOSABLE.get(kind)
    if not fields:
        return None
    composite = dict(anchor)
    changed = []
    for name in fields:
        value = partner.get(name)
        if value is not None and value != anchor.get(name):
            composite[name] = value
            changed.append(name)
    if not changed:
        return None
    composite["composed_from"] = [str(anchor.get("__id") or ""),
                                  str(partner.get("__id") or "")]
    composite["composed_fields"] = changed
    composite.pop("__id", None)
    return composite


def composites(hypothesis: Hypothesis, group_members: list[Hypothesis],
               rules_by_candidate: dict[str, dict],
               max_new: int = 2) -> list[CandidateSpec]:
    """Composite candidates from one convergence group. §10.

    Every spec returned is an ordinary candidate: a fresh identity, zero
    inherited evidence, and the UNION of both parents' supporting markets
    permanently excluded. That union is the load-bearing part — a composite
    that could be validated on a market which suggested either of its
    components would be proving a combination on the data that produced it,
    and the fact that only one component came from that market makes no
    difference to the leak.
    """
    if hypothesis.status not in (SUPPORTED, OOS_PENDING):
        return []
    anchor_id = next((c for c in hypothesis.candidates
                      if c in rules_by_candidate), "")
    if not anchor_id:
        return []
    anchor = dict(rules_by_candidate[anchor_id])
    anchor["__id"] = anchor_id
    out: list[CandidateSpec] = []
    for member in group_members:
        if member.signature == hypothesis.signature or len(out) >= max_new:
            continue
        for candidate_id in member.candidates:
            rule = rules_by_candidate.get(candidate_id)
            if rule is None:
                continue
            partner = dict(rule)
            partner["__id"] = candidate_id
            composite = compose(anchor, partner)
            if composite is None:
                continue
            out.append(CandidateSpec(
                signature=f"comp|{hypothesis.signature}|{member.signature}",
                rule=composite,
                describe=f"COMPOSITE of {hypothesis.relationship} with "
                         f"{member.relationship}"[:120],
                family=f"comp-{hypothesis.signature.split('|')[0]}",
                source=SOURCE_CONVERGENT,
                discovery_markets=set(hypothesis.markets) | set(member.markets),
                parent_id=anchor_id))
            break
    return out


# -- the pass -----------------------------------------------------------------

def run_pass(library, store: HypothesisStore, view_rows: Iterable[dict],
             log: Optional[Callable[[str], None]] = None,
             reports: Optional[dict] = None,
             compose_enabled: bool = True) -> dict:
    """One turn of the layer: observe, converge, attack, prioritise, persist.

    Inversion, adversarial testing and synthesis are driven from here but
    their MEASUREMENTS come from the existing replay machinery — this
    function never computes an expectancy of its own, because a second
    opinion about performance is exactly the duplicate evidence system §1
    forbids. `reports` carries the per-candidate `AdversarialReport`s, which
    were produced by attacking evidence the validation engine wrote; folding
    them here is the first time the declared battery has actually reached the
    hypothesis records it was written for.
    """
    say = log or (lambda _m: None)
    reports = reports or {}
    rows = list(view_rows)
    rules_by_candidate = {str(r.get("id") or ""): (r.get("rule") or {})
                          for r in rows}
    hypotheses = hypotheses_from_candidates(rows)
    for hypothesis in hypotheses:
        store.upsert(hypothesis)

    groups = convergence_groups(hypotheses)
    by_signature = {h.signature: h for h in hypotheses}
    for group in groups:
        members = [by_signature[s] for s in group["members"]
                   if s in by_signature]
        best = max((convergence_priority(m, group["independent"])
                    for m in members), default=0.0)
        store.upsert_group(group["id"], group["pattern"], group["members"],
                           group["sources"], group["independent"], best)
        for member in members:
            if member.status in (NEW, OBSERVING):
                store.set_status(
                    member.id, CONVERGING,
                    f"{group['independent']} independent source(s) describe "
                    "the same relationship")

    # THE BATTERY, folded from candidate level to relationship level. Until
    # this existed, `adversarial` was empty on every record and
    # `convergence_priority` read every hypothesis as "not attacked yet".
    attacked = 0
    for hypothesis in hypotheses:
        folded, details, states = fold_adversarial(hypothesis.candidates,
                                                   reports)
        if not folded:
            continue
        attacked += 1
        # Held in the SAME structured shape `record_adversarial` writes.
        # Holding the bare results here instead would work, and then the
        # `store.upsert` in the priority loop below would serialise them over
        # the top of the detailed rows — silently reducing a reconstructible
        # verdict to a one-word label a pass after it was recorded.
        hypothesis.adversarial = {
            test: {"result": result, "detail": details.get(test, "")}
            for test, result in folded.items()}
        hypothesis.failure_states = states
        for test, result in folded.items():
            store.record_adversarial(hypothesis.id, test, result,
                                     details.get(test, ""))
        verdict, reason = adversarial_verdict(folded)
        if verdict != hypothesis.status:
            # SUPPORTED is the strongest thing this layer may say and it is
            # still not validation — see `Hypothesis.is_supported`. A verdict
            # here changes research priority and nothing else; the candidates
            # expressing this relationship keep whatever status the ladder
            # gave them.
            store.set_status(hypothesis.id, verdict, reason)
            hypothesis.status = verdict

    promoted = 0
    for hypothesis in hypotheses:
        in_group = next((g for g in groups
                         if hypothesis.signature in g["members"]), None)
        independent = int(in_group["independent"]) if in_group else \
            independent_sources([{"source": s, "markets": hypothesis.markets}
                                 for s in hypothesis.sources])
        hypothesis.priority = convergence_priority(hypothesis, independent)
        store.upsert(hypothesis)
        if hypothesis.supporting >= MIN_OBSERVATIONS \
                and hypothesis.status in (NEW, OBSERVING, CONVERGING):
            store.set_status(hypothesis.id, ADVERSARIAL_TEST,
                             "enough observations to be worth attacking")

        # §10: combine independently discovered signals — but only for a
        # relationship that has already survived the battery, and only into
        # ordinary candidates that must earn their own evidence from zero on
        # markets neither parent has touched.
        if compose_enabled and in_group and hypothesis.status == SUPPORTED:
            members = [h for h in hypotheses
                       if h.signature in in_group["members"]]
            for spec in composites(hypothesis, members, rules_by_candidate):
                spec.register(library)
                promoted += 1
                say(f"  composite registered: {spec.describe[:70]} "
                    f"(no inherited evidence; "
                    f"{len(spec.exclusions())} market(s) excluded)")
            if promoted:
                store.set_status(
                    hypothesis.id, PROMOTED_TO_CANDIDATE,
                    "composite candidate(s) registered - they queue for the "
                    "same OOS markets as everything else")

    summary = store.summary()
    summary["convergenceGroupsThisPass"] = len(groups)
    summary["hypothesesAttackedThisPass"] = attacked
    summary["compositesRegistered"] = promoted
    say(f"  hypotheses: {summary['hypothesesTotal']} tracked, "
        f"{len(groups)} convergence group(s), "
        f"max {summary['convergenceIndependentMax']} independent source(s), "
        f"{attacked} carrying adversarial results")
    return summary
