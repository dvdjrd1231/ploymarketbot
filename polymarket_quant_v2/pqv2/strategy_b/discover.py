"""The discovery pass: reconstruct, sweep, validate, cluster, report.

Order matters and is not negotiable:

  1. split the tape by TIME; everything after the split is untouchable
  2. reconstruct wallets on the IN-SAMPLE side only
  3. generate the whole transformation grid BLIND -- no peeking at results to
     decide what to test, which is what makes the denominator honest
  4. score cheaply in-sample to triage, then spend the expensive OOS budget
  5. compute the BH threshold over the WHOLE pass, including the triage
  6. run the ladder
  7. look for cross-wallet agreement -- the only evidence not vulnerable to
     having chosen the wallet first

Step 4 needs care: triaging on in-sample results and then testing survivors
out-of-sample is legitimate, but the DENOMINATOR is still the full grid, not
the survivors. `hypotheses_tested` counts everything.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import Settings
from ..substrate import data as sdata
from ..substrate.data import PriceTape
from ..substrate.state import collect
from ..validation import backtest, stats
from ..validation.baseline import BaselineBook
from ..validation.registry import Registry
from ..validation.validate import evaluate, VALIDATED
from . import rn1, similarity
from .decompose import build_profile
from .strategy import CopyStrategy, candidates_for, grid_size, naive_copy


@dataclass
class PassReport:
    pass_id: int = 0
    reference: dict = field(default_factory=dict)
    wallets: list = field(default_factory=list)
    hypotheses_tested: int = 0
    selection_penalty: int = 0
    bh_threshold: float = 0.0
    bh_significant: int = 0
    status_histogram: list = field(default_factory=list)
    validated: list = field(default_factory=list)
    families: list = field(default_factory=list)
    agreement: list = field(default_factory=list)
    baselines: list = field(default_factory=list)
    seconds: float = 0.0
    notes: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


def _triage(strategy: CopyStrategy, obs: list, st: Settings,
            tape: PriceTape) -> tuple:
    """Cheap in-sample score. Returns (keep, t_stat, p, n_filled)."""
    r = backtest.run(strategy, obs, st, tape, collect_fills=False)
    if r.n_filled < 15 or r.expectancy <= 0:
        return False, 0.0, 1.0, r.n_filled
    t = r.t_stat()
    return True, t, stats.two_sided_p(t), r.n_filled


def run_pass(st: Settings, *, wallet: str | None = None,
             max_wallets: int | None = None, deep: bool = True,
             registry: Registry | None = None,
             progress=None) -> PassReport:
    """One complete discovery + validation pass."""
    t0 = time.time()
    st.ensure_dirs()
    cfg = st.strategy_b
    max_wallets = max_wallets or cfg.max_wallets
    owns_registry = registry is None
    registry = registry or Registry(st.work_dir / "research" / "registry.sqlite3")
    report = PassReport()

    split = sdata.oos_split_ts(st)
    lo, hi = sdata.time_bounds(st)
    report.notes.append(
        f"tape {lo}..{hi} ({(hi - lo) / 86400:.1f}d); in-sample < {split}, "
        f"out-of-sample >= {split} ({st.oos_fraction:.0%} of time)")

    # 1. reference wallet
    reference = rn1.select_reference(st, wallet)
    reconstruction = rn1.reconstruct(st, reference)
    report.reference = reconstruction.to_dict()
    report.selection_penalty = reference.selection_penalty()

    # 2. the wallet universe -- ranked by EVIDENCE VOLUME, never by profit.
    #    Ranking by profit here would select the wallets whose results are most
    #    inflated by luck and then test exactly those.
    counts = sdata.wallet_trade_counts(st, min_trades=cfg.min_wallet_trades)
    universe = [w for w, _ in counts][:max_wallets]
    if reference.wallet not in universe:
        universe.insert(0, reference.wallet)
        universe = universe[:max_wallets]
    report.wallets = universe
    report.notes.append(
        f"{len(counts)} wallets carry >= {cfg.min_wallet_trades} settled "
        f"trades; sweeping {len(universe)}")

    pass_id = registry.open_pass(st.to_dict())
    report.pass_id = pass_id

    tape = PriceTape(st)
    book = BaselineBook(st)

    # 3. one forward pass per wallet, split in time
    per_wallet_records: dict = {}
    all_pvalues: list = []
    triaged: list = []
    universe_returns: list = []

    for idx, w in enumerate(universe, 1):
        if progress:
            progress(f"[{idx}/{len(universe)}] {w[:12]} sweeping")
        is_obs = collect(st, wallets=[w], ts_to=split)
        oos_obs = collect(st, wallets=[w], ts_from=split)
        if len(is_obs) < 10 or len(oos_obs) < 5:
            report.notes.append(
                f"{w[:12]}: {len(is_obs)} in-sample / {len(oos_obs)} "
                "out-of-sample observations - skipped, cannot be split")
            continue

        universe_returns.extend(o.trade.gross_return() for o in oos_obs[:500])
        profile = build_profile(w, is_obs)

        # baseline: what naive copying this wallet earns, so a candidate that
        # cannot beat "just copy them" is visible as such
        base = backtest.run(naive_copy(w), oos_obs, st, tape,
                            collect_fills=False)
        report.baselines.append({
            "wallet": w, "in_sample_n": len(is_obs), "oos_n": len(oos_obs),
            "naive_oos_expectancy": round(base.expectancy, 5),
            "naive_oos_fills": base.n_filled,
            "naive_fill_rate": round(base.fill_rate, 3),
            "pit_evidence_share": round(profile.pit_evidence_share, 3),
        })

        kept = []
        for strategy in candidates_for(w, family=""):
            report.hypotheses_tested += 1
            keep, t, p, n = _triage(strategy, is_obs, st, tape)
            if keep:
                all_pvalues.append(p)
                kept.append((strategy, p, n))
        # Spend the expensive budget on the most promising, but the denominator
        # above already counted every one.
        kept.sort(key=lambda x: x[1])
        triaged.append((w, is_obs, oos_obs, profile, kept[:120]))
        per_wallet_records[w] = profile

    # 4. the BH threshold, over the WHOLE pass plus the selection penalty
    padded = all_pvalues + [1.0] * report.selection_penalty
    bh = stats.benjamini_hochberg(padded, fdr=0.10)
    report.bh_threshold = bh.threshold
    report.bh_significant = bh.n_significant
    report.notes.append(
        f"{report.hypotheses_tested:,} hypotheses tested, "
        f"{len(all_pvalues):,} reached out-of-sample triage; BH(FDR=10%) "
        f"threshold p<={bh.threshold:.5f} over {bh.n_tested:,} "
        f"(including {report.selection_penalty} for wallet selection)")

    # 5. the ladder
    verdicts_by_wallet: dict = {}
    to_record = []
    for w, is_obs, oos_obs, profile, kept in triaged:
        if progress:
            progress(f"{w[:12]} validating {len(kept)}")
        vs = []
        for strategy, _, _ in kept:
            v = evaluate(strategy, is_obs, oos_obs, st, tape, book,
                         bh=bh, universe_returns=universe_returns, deep=deep)
            vs.append(v)
            to_record.append((strategy, v, (lo, split), (split, hi)))
        verdicts_by_wallet[w] = vs

    registry.record_many(to_record)
    report.status_histogram = registry.status_histogram()

    validated = [v for vs in verdicts_by_wallet.values() for v in vs
                 if v.status == VALIDATED]
    validated.sort(key=lambda v: -v.score)
    report.validated = [v.to_dict() for v in validated[:50]]

    # 6. cross-wallet agreement -- the evidence that is not selection
    report.agreement = similarity.strategic_agreement(verdicts_by_wallet)
    families = similarity.cluster(list(per_wallet_records.values()))
    families = similarity.attach_agreement(families, report.agreement)
    registry.save_families(pass_id, families)
    report.families = [f.to_dict() for f in families]

    registry.close_pass(pass_id, wallets=len(universe),
                        hypotheses=report.hypotheses_tested,
                        selection_penalty=report.selection_penalty,
                        bh_threshold=bh.threshold,
                        bh_significant=bh.n_significant,
                        validated=len(validated),
                        notes=" | ".join(report.notes[:5]))
    if owns_registry:
        registry.close()

    report.seconds = round(time.time() - t0, 1)
    if not validated:
        report.notes.append(
            "0 strategies validated. That is a result, not a failure: read the "
            "status histogram to see WHICH bar each candidate failed, and the "
            "baselines table to see whether naive copying was profitable at "
            "all before conditioning.")
    return report
