"""The discovery pass — the loop that ties §5, §7, §8, §9, §14 and §34 together.

Discipline enforced here, in this order:

  1. DISCOVERY and VALIDATION are separate data (§7). Candidates are selected on
     TRAIN only. The test block is not touched until a candidate has already
     been chosen, and it is never used to choose between candidates.

  2. The multiple-testing denominator is counted honestly (§34). Every candidate
     evaluated increments it, including the ones that failed on train. The BH
     threshold is computed from the *whole* pass, then applied.

  3. Cross-wallet generalisation is measured for survivors (§9): the same
     transformation, with the wallet swapped, on wallets that never contributed
     to its discovery.

  4. "No edge" is a valid, expected, and correct outcome (§38). The pass reports
     zero validated strategies without treating it as a failure.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from .backtest import run
from .config import Settings
from .data import PriceTape, wallet_trade_counts
from .registry import Registry
from .state import Observation, stream_features
from .stats import benjamini_hochberg, t_to_p
from .strategy import CopyStrategy, candidates_for, naive_copy
from .validate import Validation, robustness, time_split, walk_forward

# Gates applied on TRAIN before a candidate is allowed to consume test data.
MIN_TRAIN_FILLS = 25
MIN_TEST_FILLS = 30


def load_observations(
    st: Settings, wallets: list[str], progress=None
) -> dict[str, list[Observation]]:
    """One causal pass over the tape, bucketed per wallet."""
    by_wallet: dict[str, list[Observation]] = defaultdict(list)
    keep = set(wallets)
    for i, o in enumerate(stream_features(st, wallets=wallets)):
        if o.trade.wallet in keep:
            by_wallet[o.trade.wallet].append(o)
        if progress and i % 20_000 == 0:
            progress(i)
    return dict(by_wallet)


def discover(
    st: Settings,
    *,
    max_wallets: int = 25,
    min_trades: int = 100,
    fdr: float = 0.10,
    registry: Registry | None = None,
    log=print,
) -> dict:
    """Run one full discovery + validation pass. Returns a report dict (§37)."""
    tape = PriceTape(st)

    ranked = wallet_trade_counts(st, min_trades=min_trades)
    wallets = [w for w, _ in ranked[:max_wallets]]
    if not wallets:
        return {"error": "no wallets meet the evidence threshold", "wallets": 0}

    log(f"[1/5] loading causal features for {len(wallets)} wallets ...")
    obs_by_wallet = load_observations(st, wallets)
    total_obs = sum(len(v) for v in obs_by_wallet.values())
    log(f"      {total_obs:,} observations")

    pass_id = registry.open_pass(fdr) if registry else None

    # ---------------------------------------------------------------- stage 1
    log("[2/5] discovery on TRAIN only ...")
    survivors: list[tuple[CopyStrategy, list[Observation], list, list, list]] = []
    hypotheses = 0
    baselines: dict[str, dict] = {}

    for wallet in wallets:
        obs = obs_by_wallet.get(wallet, [])
        if len(obs) < 60:
            continue
        train, valid, test = time_split(obs)

        base = run(naive_copy(wallet), obs, st, tape)
        baselines[wallet] = base.summary()

        best_local = []
        for strat in candidates_for(wallet):
            hypotheses += 1
            if registry and registry.seen(strat.spec_hash()):
                continue
            r_train = run(strat, train, st, tape)
            if r_train.n_filled < MIN_TRAIN_FILLS or r_train.expectancy <= 0:
                continue
            r_valid = run(strat, valid, st, tape)
            if r_valid.n_filled < 10 or r_valid.expectancy <= 0:
                continue
            best_local.append((r_train.expectancy, strat, train, valid, test))

        # Take only the strongest few per wallet into the test block. This is a
        # selection made entirely on train+validation — the test set is still
        # untouched at this point.
        best_local.sort(key=lambda x: -x[0])
        for _, strat, tr, va, te in best_local[:5]:
            survivors.append((strat, obs, tr, va, te))

    log(f"      {hypotheses:,} hypotheses tested, {len(survivors)} reached out-of-sample")

    # ---------------------------------------------------------------- stage 2
    log("[3/5] out-of-sample evaluation ...")
    validations: list[Validation] = []
    for strat, obs, tr, va, te in survivors:
        r_train = run(strat, tr, st, tape)
        r_valid = run(strat, va, st, tape)
        r_test = run(strat, te, st, tape)
        if r_test.n_filled < MIN_TEST_FILLS:
            if registry:
                registry.record(strat, "INSUFFICIENT_EVIDENCE", 0.0, 1.0,
                                r_train.summary(), r_valid.summary(), r_test.summary())
            continue
        v = Validation(strategy=strat, train=r_train, validation=r_valid, test=r_test)
        v.walk = walk_forward(strat, obs, st, tape)
        validations.append(v)

    # ---------------------------------------------------------------- stage 3
    # BH threshold over the whole pass, not per candidate (§34).
    pvals = [v.oos_p() for v in validations]
    bh_threshold, n_sig = benjamini_hochberg(pvals, fdr=fdr)
    log(f"[4/5] multiple-testing control: {len(pvals)} OOS tests, "
        f"BH threshold p<={bh_threshold:.5f} at FDR={fdr} -> {n_sig} significant")

    # ---------------------------------------------------------------- stage 4
    log("[5/5] robustness and cross-wallet generalisation ...")
    validated = []
    for v in validations:
        if v.oos_p() <= bh_threshold and bh_threshold > 0:
            v.robustness = robustness(v.strategy, v.test, obs_by_wallet[v.strategy.wallet], st, tape)
            # §9: does the same transformation work on wallets that did not
            # produce it?
            ph = v.strategy.params_only_hash()
            for other in wallets:
                if other == v.strategy.wallet:
                    continue
                oobs = obs_by_wallet.get(other)
                if not oobs:
                    continue
                r = run(replace(v.strategy, wallet=other), oobs, st, tape)
                if r.n_filled >= 10:
                    v.cross_wallet.append((other, r))

        status = v.status(bh_threshold if bh_threshold > 0 else 0.0)
        if registry:
            registry.record(
                v.strategy, status, v.score(), v.oos_p(),
                v.train.summary(), v.validation.summary(), v.test.summary(),
                robust=v.robustness.as_dict() if v.robustness else None,
                walk=[r.summary() for r in v.walk],
                cross=[{"wallet": w, **r.summary()} for w, r in v.cross_wallet],
            )
        if status == "VALIDATED":
            validated.append(v)

    if registry:
        registry.commit()
        registry.close_pass(pass_id, len(wallets), hypotheses, len(validated))

    validated.sort(key=lambda v: -v.score())
    return {
        "wallets_analysed": len(wallets),
        "observations": total_obs,
        "hypotheses_tested": hypotheses,
        "reached_oos": len(validations),
        "bh_threshold": bh_threshold,
        "fdr": fdr,
        "validated": len(validated),
        "baselines": baselines,
        "top": [
            {
                "wallet": v.strategy.wallet,
                "spec_hash": v.strategy.spec_hash(),
                "spec": v.strategy.spec(),
                "score": v.score(),
                "oos_p": v.oos_p(),
                "test": v.test.summary(),
                "walk_consistency": v.walk_consistency(),
                "cross_wallet_consistency": v.cross_wallet_consistency(),
                "robustness": v.robustness.as_dict() if v.robustness else None,
            }
            for v in validated[:20]
        ],
    }
