"""The discovery pass: the loop that turns data into a validated strategy, or
into a recorded reason there isn't one.

    materialise once  ->  generate the search space  ->  screen in-sample
                      ->  BH over the WHOLE denominator
                      ->  out-of-sample on data never used to discover
                      ->  walk-forward  ->  robustness  ->  $100 capital test
                      ->  status ladder  ->  persist, versioned

The ordering is the safeguard. Screening happens on in-sample data only;
out-of-sample is touched exactly once per surviving candidate, at the end. The
BH threshold is computed over every transformation the grid defined, not over
the handful that reached the end — correcting against survivors is the most
common way a sweep claims significance it never earned.

Every pass writes a `research_passes` row with its full denominator, so no
p-value in this system can ever be quoted without the search that produced it.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from ..config import Settings
from . import robustness as robustness_mod
from . import sweep
from . import stats, validate, walkforward
from .backtest import baseline_returns, capital_test, evaluate
from .hypothesis import generate
from .matrix import Matrix, build


@dataclass
class PassResult:
    pass_id: str
    started_ts: int = 0
    finished_ts: int = 0
    matrix: dict = field(default_factory=dict)
    search_space: dict = field(default_factory=dict)
    screen: dict = field(default_factory=dict)
    tested: int = 0
    distinct_tested: int = 0
    screened: int = 0
    bh_threshold: float = 0.0
    bh_significant: int = 0
    evaluated_oos: int = 0
    validated: int = 0
    distinct_findings: int = 0
    finding_groups: list = field(default_factory=list)
    by_status: dict = field(default_factory=dict)
    top: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    elapsed_secs: float = 0.0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def run(st: Settings, store, *, depth: int = 2, max_hypotheses: int = 20_000,
        screen_top: int = 300, min_screen_n: int = 25, families: tuple = (),
        rebuild_matrix: bool = False, progress=None, limit_rows: int = 0
        ) -> PassResult:
    t0 = time.perf_counter()
    res = PassResult(pass_id=uuid.uuid4().hex[:16], started_ts=int(time.time()))

    def say(msg: str) -> None:
        if progress:
            progress(msg)

    # -- 1. materialise the causal observation matrix ----------------------
    say("materialising the observation matrix (one causal pass over the tape)")
    m: Matrix = build(st, rebuild=rebuild_matrix, limit=limit_rows)
    res.matrix = m.describe()
    if m.n < 200:
        res.notes.append(f"only {m.n} evaluable observations; a discovery pass "
                         f"needs materially more to mean anything")
        res.finished_ts = int(time.time())
        res.elapsed_secs = round(time.perf_counter() - t0, 2)
        _persist_pass(store, res)
        return res

    split = m.split_ts(st.research.oos_fraction)
    is_lo, is_hi = m.index_range(0, split)
    oos_lo, oos_hi = m.index_range(split, 0)
    res.notes.append(
        f"in-sample {is_hi - is_lo} rows to {split}; out-of-sample "
        f"{oos_hi - oos_lo} rows after it. Split by TIME, never by row.")

    # -- 2. the search space, with its true size ---------------------------
    say("generating the search space")
    space = generate(m, depth=depth, max_hypotheses=max_hypotheses,
                     families=families)
    res.search_space = space.to_dict()
    res.tested = space.tested
    res.distinct_tested = space.distinct
    if space.note:
        res.notes.append(space.note)
    if not space.hypotheses:
        res.finished_ts = int(time.time())
        res.elapsed_secs = round(time.perf_counter() - t0, 2)
        _persist_pass(store, res)
        return res

    # -- 3. in-sample screen ----------------------------------------------
    say(f"screening {space.distinct} hypotheses in-sample")
    sc = sweep.screen(m, space.hypotheses, st, lo=is_lo, hi=is_hi,
                      min_n=min_screen_n, progress=say if progress else None)
    res.screen = sc.to_dict()
    screened = list(sc.kept)
    res.screened = len(screened)
    if sc.note:
        res.notes.append(sc.note)
    res.notes.append(
        f"{len(screened)} of {space.distinct} hypotheses beat the market-wide "
        f"baseline in-sample AND made money in absolute terms. "
        f"{sc.excess_only} beat their price band but still lost money; "
        f"{sc.absolute_only} made money but only by selecting the band. "
        f"Rejected hypotheses are recorded, not deleted — they are the "
        f"denominator.")

    if not screened:
        res.finished_ts = int(time.time())
        res.elapsed_secs = round(time.perf_counter() - t0, 2)
        _persist_pass(store, res)
        _persist_hypotheses(store, space.hypotheses, {}, res.pass_id)
        return res

    # Keep the strongest by in-sample alpha, but the BH denominator stays the
    # FULL search. Trimming the candidate list is a compute decision; pretending
    # the search was smaller would be a statistical claim.
    finalists = screened[:screen_top]
    if len(screened) > screen_top:
        res.notes.append(
            f"only the top {screen_top} by in-sample alpha were carried to "
            f"out-of-sample; the BH correction still uses the full "
            f"denominator of {space.tested}")

    # -- 4. out-of-sample, once ------------------------------------------
    say(f"evaluating {len(finalists)} finalists out-of-sample")
    oos_base = baseline_returns(m, st, oos_lo, oos_hi, stride=3)
    oos_results = []
    is_base_full = baseline_returns(m, st, is_lo, is_hi, stride=3)
    for h, _n, _a, _abs in finalists:
        # Re-measure in-sample on the COMPLETE window. The screen ran on a
        # sample; nothing that reaches a verdict is scored from a sample.
        is_ev = evaluate(m, h, st, lo=is_lo, hi=is_hi,
                         baseline_returns=is_base_full, with_stats=False)
        oos = evaluate(m, h, st, lo=oos_lo, hi=oos_hi,
                       baseline_returns=oos_base, with_stats=True)
        oos_results.append((h, is_ev, oos))
    res.evaluated_oos = len(oos_results)

    # -- 5. BH over the whole pass ---------------------------------------
    pvals = [o.p_value for _, _, o in oos_results]
    bh = stats.benjamini_hochberg(pvals, alpha=st.research.bh_alpha,
                                  n_tests=max(space.tested, len(pvals)))
    res.bh_threshold = round(bh.threshold, 8)
    res.bh_significant = bh.n_significant
    res.notes.append(
        f"Benjamini-Hochberg at alpha={st.research.bh_alpha} over "
        f"{bh.n_tests} tests gives a threshold of {bh.threshold:.3g}; "
        f"{bh.n_significant} candidates clear it")

    # -- 6. the full battery on candidates that could still be real -------
    say("running walk-forward, robustness and the capital test")
    verdicts = []
    for h, is_ev, oos in oos_results:
        # Skip the expensive battery for candidates already dead on cheap
        # criteria — but still record them, with the battery marked not-run.
        cheap_dead = (oos.n < st.research.min_oos_fills
                      or oos.alpha_vs_baseline <= 0
                      or oos.p_value > bh.threshold or bh.threshold <= 0)
        if cheap_dead:
            wf = walkforward.WalkForward(note="not run: failed a cheaper check")
            rb = robustness_mod.Robustness(note="not run: failed a cheaper check",
                                           fragile=True)
            ct = capital_test(m, h, st, lo=oos_lo, hi=oos_hi)
        else:
            wf = walkforward.run(m, h, st, schedule="expanding")
            rb = robustness_mod.run(m, h, st, lo=oos_lo, hi=oos_hi,
                                    reference_alpha=oos.alpha_vs_baseline)
            ct = capital_test(m, h, st, lo=oos_lo, hi=oos_hi)

        v = validate.assign(st=st, oos=oos, is_eval=is_ev, walkforward=wf,
                            robustness=rb, capital=ct,
                            bh_threshold=bh.threshold,
                            hypotheses_tested=bh.n_tests)
        validate.persist(store, hypothesis=h, verdict=v, oos=oos,
                         is_eval=is_ev, walkforward=wf, robustness=rb,
                         capital=ct, pass_id=res.pass_id)
        verdicts.append((h, is_ev, oos, v, ct))
        res.by_status[v.status] = res.by_status.get(v.status, 0) + 1

    res.validated = res.by_status.get("VALIDATED", 0)

    # -- 6b. how many DISTINCT findings is that, really? -------------------
    # A grid search returns near-duplicates: `price >= 0.36`, `price >= 0.53`
    # and `price >= 0.695` combined with the same second rule are three
    # spellings of one effect. Counting them as three validated strategies
    # would overstate the evidence threefold and would also let a portfolio
    # hold "three uncorrelated strategies" that are one bet.
    val = [(h, o) for h, i, o, v, ct in verdicts if v.status == "VALIDATED"]
    groups = _group_findings(m, [h for h, _ in val], oos_lo, oos_hi)
    res.finding_groups = groups
    res.distinct_findings = len(groups)
    if res.validated:
        res.notes.append(
            f"{res.validated} strategies reached VALIDATED, but they collapse "
            f"to {res.distinct_findings} DISTINCT finding(s) once "
            f"near-duplicates are merged by overlap of the trades they admit. "
            f"Treat the pass as having found {res.distinct_findings} thing(s), "
            f"not {res.validated}.")

    # -- 7. report --------------------------------------------------------
    verdicts.sort(key=lambda x: (x[3].status != "VALIDATED",
                                 -x[2].alpha_vs_baseline))
    res.top = [{
        "strategy_id": h.hypothesis_id, "statement": h.statement,
        "family": h.family, "status": v.status, "reason": v.reason,
        "evidence_quality": v.evidence_quality,
        "oos_n": o.n, "oos_markets": o.markets,
        "oos_expectancy": o.expectancy,
        "baseline": o.baseline_expectancy,
        "alpha_vs_baseline": o.alpha_vs_baseline,
        "win_rate": o.win_rate, "p_value": o.p_value,
        "capital_trades": ct.trades, "capital_return": ct.total_return,
        "capital_fill_rate": round(ct.fill_rate, 4),
    } for h, i, o, v, ct in verdicts[:40]]

    _persist_hypotheses(store, space.hypotheses,
                        {h.hypothesis_id: (o, v) for h, _, o, v, _ in verdicts},
                        res.pass_id)

    res.finished_ts = int(time.time())
    res.elapsed_secs = round(time.perf_counter() - t0, 2)
    _persist_pass(store, res)

    if res.validated:
        store.alert("new_pattern",
                    f"{res.validated} strategy(ies) reached VALIDATED in pass "
                    f"{res.pass_id}", severity="INFO", source="discover")
    return res


def _group_findings(m, hypotheses: list, lo: int, hi: int,
                    threshold: float = 0.6) -> list:
    """Merge hypotheses that admit substantially the same trades.

    Jaccard overlap of admitted row sets, single-linkage. Two rules that select
    60% of the same observations are describing one effect however different
    their statements look.
    """
    from .hypothesis import admit_mask
    if not hypotheses:
        return []
    sets = [set(admit_mask(m, h, lo, hi)) for h in hypotheses]
    parent = list(range(len(hypotheses)))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = len(sets[i] | sets[j])
            if not union:
                continue
            if len(sets[i] & sets[j]) / union >= threshold:
                a, b = find(i), find(j)
                if a != b:
                    parent[a] = b

    buckets: dict = {}
    for i, h in enumerate(hypotheses):
        buckets.setdefault(find(i), []).append(i)

    out = []
    for members in buckets.values():
        # The representative is the member admitting the most trades: the
        # broadest statement of the effect rather than the luckiest slice.
        rep = max(members, key=lambda i: len(sets[i]))
        out.append({
            "representative": hypotheses[rep].hypothesis_id,
            "statement": hypotheses[rep].statement,
            "n_variants": len(members),
            "variants": [hypotheses[i].hypothesis_id for i in members],
            "admitted": len(sets[rep]),
            "note": (f"{len(members)} validated variant(s) of one effect"
                     if len(members) > 1 else "a distinct finding"),
        })
    out.sort(key=lambda d: -d["n_variants"])
    return out


def _persist_pass(store, res: PassResult) -> None:
    store.insert("research_passes", [{
        "pass_id": res.pass_id, "started_ts": res.started_ts,
        "finished_ts": res.finished_ts or int(time.time()),
        "tested": res.tested, "distinct_tested": res.distinct_tested,
        "surviving": res.validated,
        "bh_alpha": 0.0, "bh_threshold": res.bh_threshold,
        "detail": {"matrix": res.matrix, "search_space": res.search_space,
                   "screened": res.screened,
                   "evaluated_oos": res.evaluated_oos,
                   "bh_significant": res.bh_significant,
                   "by_status": res.by_status, "notes": res.notes,
                   "distinct_findings": res.distinct_findings,
                   "finding_groups": res.finding_groups,
                   "elapsed_secs": res.elapsed_secs},
        "ts": res.started_ts,
    }], source="discover", replace=True)


def _persist_hypotheses(store, hypotheses: list, outcomes: dict,
                        pass_id: str) -> None:
    """Record EVERY hypothesis, including the rejected ones.

    The rejected set is the denominator. Deleting it would make the surviving
    p-values uninterpretable, and it is also the raw material for the
    meta-researcher agent.
    """
    rows = []
    for h in hypotheses:
        got = outcomes.get(h.hypothesis_id)
        if got:
            o, v = got
            rows.append({"hypothesis_id": h.hypothesis_id, "family": h.family,
                         "statement": h.statement, "params": h.to_dict(),
                         "tested": 1, "p_value": o.p_value,
                         "effect": o.alpha_vs_baseline, "n": o.n,
                         "outcome": v.status, "pass_id": pass_id})
        else:
            rows.append({"hypothesis_id": h.hypothesis_id, "family": h.family,
                         "statement": h.statement, "params": h.to_dict(),
                         "tested": 1, "p_value": None, "effect": None, "n": 0,
                         "outcome": "SCREENED_OUT", "pass_id": pass_id})
    for i in range(0, len(rows), 5000):
        store.insert("hypotheses", rows[i:i + 5000], source="discover",
                     replace=True)
