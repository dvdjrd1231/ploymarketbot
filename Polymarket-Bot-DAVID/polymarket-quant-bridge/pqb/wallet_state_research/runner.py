"""One research run, in the order the brief specifies.

    audit data -> build episodes -> census
      -> EXACT frozen RN1 reproduction (untouched thresholds, first)
      -> profitability of that exact rule, three execution assumptions
      -> baselines over the same cases
      -> cross-wallet, cross-market
      -> walk-forward
      -> model discovery on development, chosen on validation
      -> FREEZE
      -> the untouched holdout, opened once
      -> leakage audit
      -> reports

The order is the method. The frozen rule is reproduced before anything is
fitted, the holdout is opened after `freeze()` and never before, and the
profitability question is asked separately from the behaviour question at
every stage rather than at the end.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from . import RN1_WALLET
from .backtest import baselines, compare, simulate
from .classifier import FrozenRN1, evaluate
from .discovery import compare_model_families, fit_threshold_rule
from .discovery_v1 import compare_v1_families
from .episodes import build_episodes, census
from .events import audit, load_events
from .features import (WalletHistory, build as build_features, category_of,
                       history_index, leakage_audit)
from .pricing import ASSUMPTIONS, BASE, PriceOracle
from .registry import default_registry
from . import states as states_mod
from . import structure as structure_mod
from . import quality as quality_mod
from . import cohorts as cohorts_mod
from . import promote as promote_mod
from .strategy_v1 import (DEFAULT_FRESHNESS_MINUTES, PROSPECTIVE_BOUNDARY_TS,
                          PROSPECTIVE_BOUNDARY_UTC, RN1StrategyModelV1,
                          capital_within_freshness, evaluate_v1,
                          initial_capital, initial_price)
from .validation import (chronological_split, cross_market, cross_wallet,
                         walk_forward)


@dataclass
class RunConfig:
    """Everything a run needs. Defaults match the brief's configuration."""

    intel_path: str = ""
    out_dir: str = ""
    horizon_minutes: float = 3.0
    extra_horizons: tuple = (1.0, 5.0, 10.0)
    frozen_rn1_enabled: bool = True
    cross_wallet_enabled: bool = True
    cross_market_enabled: bool = True
    discovery_enabled: bool = False
    minimum_wallet_samples: int = 12
    minimum_market_samples: int = 12
    stakes: tuple = (5.0, 10.0, 25.0)
    quiet_days: float = 2.0
    max_wallets: int = 0             # 0 = every wallet
    min_wallet_trades: int = 20
    walk_forward_folds: int = 4
    seed: int = 20260823
    # -- Strategy Model V1 (the PRIMARY frozen model) ----------------------
    frozen_v1_enabled: bool = True
    prospective_boundary_ts: float = PROSPECTIVE_BOUNDARY_TS
    prediction_freshness_minutes: float = DEFAULT_FRESHNESS_MINUTES
    transitions_enabled: bool = True
    structure_discovery_enabled: bool = False
    # Cap on the sample handed to the structure search. MDL over the whole
    # population is slow and adds nothing: the shuffled control needs many
    # passes, and 20k observations already settles every candidate here.
    structure_sample: int = 20_000

    def to_dict(self) -> dict:
        return {"horizonMinutes": self.horizon_minutes,
                "extraHorizons": list(self.extra_horizons),
                "frozenRN1Enabled": self.frozen_rn1_enabled,
                "crossWalletEnabled": self.cross_wallet_enabled,
                "crossMarketEnabled": self.cross_market_enabled,
                "discoveryEnabled": self.discovery_enabled,
                "minimumWalletSamples": self.minimum_wallet_samples,
                "minimumMarketSamples": self.minimum_market_samples,
                "stakes": list(self.stakes), "quietDays": self.quiet_days,
                "maxWallets": self.max_wallets,
                "minWalletTrades": self.min_wallet_trades,
                "walkForwardFolds": self.walk_forward_folds,
                "frozenV1Enabled": self.frozen_v1_enabled,
                "prospectiveBoundaryUtc": PROSPECTIVE_BOUNDARY_UTC.isoformat(),
                "predictionFreshnessMinutes":
                    self.prediction_freshness_minutes,
                "transitionsEnabled": self.transitions_enabled,
                "structureDiscoveryEnabled": self.structure_discovery_enabled}


def _log(message: str, say: Optional[Callable] = None) -> None:
    if say:
        say(message)


def run(config: RunConfig, say: Optional[Callable] = None) -> dict:
    """The whole study. Returns the machine-readable result."""
    started = time.time()
    out: dict = {"modelVersions": {
        "frozen": FrozenRN1.version,
        "universal": "UNIVERSAL_WALLET_STATE_V1",
        "walletSpecific": "WALLET_SPECIFIC_V1",
        "hybrid": "HYBRID_WALLET_STATE_V1"},
        "config": config.to_dict(), "startedTs": started}

    # -- 0. what the data can support -------------------------------------
    _log("[1/9] auditing the store", say)
    data_audit = audit(config.intel_path)
    out["dataAudit"] = data_audit.to_dict()
    if not data_audit.trades:
        out["available"] = False
        out["reason"] = "no wallet trades in the store"
        return out
    for warning in data_audit.warnings:
        _log("  ! " + warning, say)

    # -- 1. episodes -------------------------------------------------------
    _log("[2/9] reconstructing episodes", say)
    wallets = _wallet_shortlist(config)
    events = load_events(config.intel_path, wallets=wallets)
    settled = _settled_markets(config.intel_path)
    redemptions = _redemptions(config.intel_path)
    episodes = build_episodes(events, tape_end_ts=data_audit.last_ts,
                              quiet_days=config.quiet_days,
                              settled_markets=settled,
                              redemptions=redemptions)
    if redemptions:
        _log(f"  {len(redemptions):,} redemption(s) on record — a redeemed "
             "condition is FINISHED regardless of the quiet rule", say)
    switched = [e for e in episodes if e.switched]
    out["census"] = census(episodes, config.horizon_minutes).to_dict()
    _log(f"  {len(episodes):,} episodes, {len(switched):,} switched, "
         f"{out['census']['labelled']:,} with a finished lifecycle", say)
    if not switched:
        out["available"] = False
        out["reason"] = "no episode reached an opposite-side buy"
        return out
    out["available"] = True

    histories = history_index(episodes)

    def features_of(episode, snapshot):
        history = histories.get(
            (episode.wallet, episode.market_id,
             episode.first_opposite_ts or episode.first_buy_ts))
        return build_features(episode, snapshot, history=history)

    def feature_values(episode, snapshot):
        return features_of(episode, snapshot).values

    oracle = PriceOracle(config.intel_path)
    try:
        # -- 2. THE EXACT FROZEN REPRODUCTION, before anything is fitted ----
        _log("[3/9] exact frozen RN1 reproduction (thresholds untouched)", say)
        frozen = FrozenRN1()
        out["rn1"] = _rn1_block(frozen, episodes, config, oracle, say)

        # -- 2b. LABEL SENSITIVITY -----------------------------------------
        # The completeness cutoff is a judgement call, so it is shown as a
        # curve rather than asserted as a constant. A result that exists only
        # at one cutoff is an artifact of the cutoff, and this is where that
        # becomes visible instead of arguable.
        _log("  label-completeness sensitivity", say)
        out["labelSensitivity"] = {"cutoffDays": {}, "note": (
            "An episode is labelled only once the tape kept running for this "
            "long after its last activity. Stricter cutoffs are more honest "
            "per episode and leave fewer episodes; if accuracy moves a lot "
            "across this row, the headline is about the cutoff.")}
        for days in (1.0, 2.0, 3.0, 7.0):
            probe = build_episodes(events, tape_end_ts=data_audit.last_ts,
                                   quiet_days=days, settled_markets=settled)
            probe_switched = [e for e in probe if e.switched]
            report, _ = evaluate(FrozenRN1(), probe_switched,
                                 config.horizon_minutes)
            out["labelSensitivity"]["cutoffDays"][f"{days:g}d"] = {
                "graded": report.graded,
                "truncatedExcluded": report.truncated_excluded,
                "accuracy": round(report.accuracy, 4),
                "balancedAccuracy": round(report.balanced_accuracy, 4),
                "baseRateAggressive": round(report.base_rate, 4),
                "isDefault": days == config.quiet_days,
            }

        # -- 3. horizon sensitivity ----------------------------------------
        out["horizons"] = {}
        for horizon in (config.horizon_minutes,) + tuple(
                config.extra_horizons):
            report, _cases = evaluate(frozen, switched, horizon,
                                      feature_values)
            out["horizons"][f"+{horizon:g}m"] = report.to_dict()
        out["horizonNote"] = (
            "The source selected +3m from {1,3,5,10}. All four are reported "
            "here so the choice is visible as a choice; +3m is preserved as "
            "the frozen setting regardless of which scores best, because "
            "picking the best one now would be re-tuning the benchmark.")

        # -- 3b. STRATEGY MODEL V1 — the PRIMARY frozen model ---------------
        # Runs before anything is fitted and before the supporting +3m rule is
        # given any weight. Two separate reports, never merged: the
        # retrospective one over everything, and the CLEAN PROSPECTIVE one
        # over conditions after the frozen boundary. Reporting the first as
        # the second is exactly the contamination §52 permanently excludes.
        if config.frozen_v1_enabled:
            _log("[3b/9] RN1 Strategy Model V1 (frozen entry-time rule)", say)
            v1 = RN1StrategyModelV1()
            # §13's "no REDEEM may exist before the prediction" — a real
            # gate now that the /activity backfill supplies redemptions,
            # rather than the vacuous one it had to be when the tape carried
            # trades only.
            def _redeemed_before(episode) -> bool:
                stamp = getattr(episode, "redeemed_ts", 0.0)
                return bool(stamp) and stamp < episode.first_buy_ts

            retro, retro_cases = evaluate_v1(
                episodes, v1, config.prospective_boundary_ts,
                config.prediction_freshness_minutes, prospective_only=False,
                has_redeem_before=_redeemed_before)
            forward, forward_cases = evaluate_v1(
                episodes, v1, config.prospective_boundary_ts,
                config.prediction_freshness_minutes, prospective_only=True,
                has_redeem_before=_redeemed_before)
            out["strategyV1"] = {
                "modelVersion": v1.version,
                "prospectiveBoundaryUtc":
                    PROSPECTIVE_BOUNDARY_UTC.isoformat(),
                "freshnessMinutes": config.prediction_freshness_minutes,
                "retrospective": retro.to_dict(),
                "cleanProspective": forward.to_dict(),
                "rn1Only": _v1_for_wallet(episodes, v1, config, RN1_WALLET),
                "capitalInterpretation": _capital_interpretation(
                    episodes, config),
                "redeemCheck": _redeem_check(episodes, redemptions),
                "sourceBenchmark": {
                    "retrospective250": {"DIRECTIONAL": 0.208,
                                         "PROTECT_REBALANCE": 0.584,
                                         "AGGRESSIVE_OPPOSITE": 0.208},
                    "note": ("Source distribution over 250 conditions. A "
                             "benchmark only — never an expected outcome, "
                             "and never hard-coded as one."),
                },
            }
            _log(f"  retrospective: {retro.predictions:,} predictions, "
                 f"{retro.resolved:,} resolved, accuracy "
                 f"{retro.accuracy:.2%} (majority baseline "
                 f"{retro.majority_baseline:.2%})", say)
            _log(f"  clean prospective: {forward.predictions:,} predictions, "
                 f"{forward.resolved:,} resolved, "
                 f"{forward.unresolved:,} still pending", say)

        # -- 3c. STATE TRANSITIONS ------------------------------------------
        if config.transitions_enabled:
            _log("[3c/9] state-transition study", say)
            transition_study, _paths = states_mod.study(episodes, settled)
            out["stateTransitions"] = transition_study.to_dict()
            _log(f"  {transition_study.trajectories:,} trajectories, "
                 f"{transition_study.reached_two_sided:,} reached two-sided "
                 f"({transition_study.reached_two_sided / max(1, transition_study.trajectories):.1%})",
                 say)

        # -- 3c2. DATA QUALITY (§30) ----------------------------------------
        # Scored over a bounded sample: eight components per prediction is
        # cheap but not free, and the population statistic is stable long
        # before 282k of them.
        _log("  data-quality scoring", say)
        quality_scores = []
        for episode in switched[:20_000]:
            snapshot = episode.snapshot(config.horizon_minutes)
            history = histories.get(
                (episode.wallet, episode.market_id,
                 episode.first_opposite_ts or episode.first_buy_ts))
            quality_scores.append(quality_mod.score(
                episode, snapshot,
                quote=oracle.quote_at(episode.opposite_token, snapshot.ts)
                if snapshot.ts else None,
                settled=episode.market_id in settled,
                wallet_prior_conditions=(getattr(history, "episodes", 0)
                                         if history else 0)))
        out["dataQuality"] = quality_mod.summarise(quality_scores)
        _log(f"  mean data quality {out['dataQuality'].get('meanScore')} — "
             f"weakest component: "
             f"{list((out['dataQuality'].get('weakestComponentCounts') or {}))[:1]}",
             say)

        # -- 3d. POPULATION COMPOSITION -------------------------------------
        # The single most important comparison in this run, and the one that
        # decides whether V1's headline accuracy means what it looks like.
        # See `_composition` for why.
        if config.frozen_v1_enabled and config.transitions_enabled:
            out["populationComposition"] = _composition(
                out.get("strategyV1") or {}, out.get("stateTransitions") or {})
            _log("  " + str(out["populationComposition"]["reading"])[:170],
                 say)

        # -- 3e. BASE-RATE DECOMPOSITION and the FAIR reproduction ----------
        # The study's headline — V1 loses to a 92% majority baseline — is
        # computed over a population that is 93% DIRECTIONAL, while the
        # source's was 79% two-sided. That is not a like-for-like test. Here
        # each hypothesis about who the source was studying is MEASURED, and
        # the cohort whose base rate lands closest is used for a second,
        # fairer reproduction. The cohort is selected on base rate alone,
        # before any accuracy is computed on it.
        if config.frozen_v1_enabled:
            _log("[3e/9] base-rate decomposition", say)
            decomposition = cohorts_mod.decompose(episodes)
            block = decomposition.to_dict()
            cohort = cohorts_mod.select_cohort(episodes, decomposition)
            if cohort:
                fair, _ = evaluate_v1(
                    cohort, RN1StrategyModelV1(),
                    config.prospective_boundary_ts,
                    config.prediction_freshness_minutes,
                    prospective_only=False)
                fair_forward, _ = evaluate_v1(
                    cohort, RN1StrategyModelV1(),
                    config.prospective_boundary_ts,
                    config.prediction_freshness_minutes,
                    prospective_only=True)
                block["v1OnComparableCohort"] = {
                    "retrospective": fair.to_dict(),
                    "cleanProspective": fair_forward.to_dict(),
                    "note": ("V1 scored on the population whose two-sided "
                             "base rate is closest to the source's. The "
                             "cohort was chosen on base rate BEFORE any "
                             "accuracy was computed on it.")}
            out["baseRateDecomposition"] = block
            _log("  " + str(decomposition.selected_reason)[:160], say)

        # -- 4. cross-wallet ------------------------------------------------
        if config.cross_wallet_enabled:
            _log("[4/9] cross-wallet: the frozen rule, unoptimised, "
                 "everywhere", say)

            def trade_fn(cases):
                return simulate(cases, oracle, 10.0, BASE,
                                model_version=frozen.version,
                                category_of=category_of).to_dict()

            out["crossWallet"] = cross_wallet(
                episodes, frozen, config.horizon_minutes,
                min_samples=config.minimum_wallet_samples,
                features_of=feature_values, trade_fn=trade_fn)
            _log("  " + str(out["crossWallet"]["reading"]), say)

        # -- 5. cross-market ------------------------------------------------
        if config.cross_market_enabled:
            _log("[5/9] cross-market: category, size, switch speed", say)
            out["crossMarket"] = cross_market(
                episodes, frozen, config.horizon_minutes, category_of,
                min_samples=config.minimum_market_samples,
                features_of=feature_values)

        # -- 6. walk-forward on the frozen rule -----------------------------
        _log("[6/9] walk-forward (expanding window, chronological)", say)
        out["walkForward"] = walk_forward(
            episodes, lambda _train: frozen, config.horizon_minutes,
            folds=config.walk_forward_folds, features_of=feature_values)

        # -- 7. split, discover, choose — development + validation only -----
        _log("[7/9] chronological split and model discovery", say)
        # Split the GRADABLE population, not every switched episode.
        #
        # Splitting all of them puts the newest episodes in the holdout, and
        # the newest episodes are exactly the ones the tape has not finished
        # watching — so the holdout fills with truncated cases and grades
        # zero of them. That is not a bug in the split, it is what a 90-day
        # tape whose last five days hold 87% of its trades does to a
        # completeness rule, and the fix is to partition what can actually be
        # graded.
        #
        # The cost is stated rather than hidden: because completeness
        # correlates with age, this holdout skews OLDER than the development
        # window. It is still strictly later in time than what trained on it,
        # which is the property walk-forward validation actually needs.
        gradable = [e for e in switched if e.labelled and e.two_class]
        split = chronological_split(gradable)
        out["split"] = split.to_dict()
        out["split"]["population"] = "labelled two-class episodes only"
        out["split"]["excludedTruncated"] = len(switched) - len(gradable)
        out["split"]["note"] = (
            "Split over the GRADABLE population. Label completeness "
            "correlates with age, so this holdout skews older than the "
            "development window — a real limitation of a short tape, stated "
            "rather than hidden. It remains strictly later in time than the "
            "data any fitted model saw.")
        if config.discovery_enabled:
            # §17/§24 for the THREE-CLASS entry-time problem V1 actually
            # asks. Split over the labelled population by first-BUY time, so
            # the families are chosen on data strictly later than they were
            # fitted on and strictly earlier than the untouched holdout.
            v1_ordered = sorted((e for e in episodes if e.labelled),
                                key=lambda e: e.first_buy_ts)
            cut = int(len(v1_ordered) * 0.6)
            out["discoveryV1"] = compare_v1_families(
                v1_ordered[:cut], v1_ordered[cut:])
            _log("  V1 families: "
                 + str(out["discoveryV1"].get("verdict", ""))[:150], say)
            # `feature_values`, not `features_of`: the fitted models consume
            # a plain mapping, and handing them the stamped vector would make
            # the leakage metadata part of the model's input surface.
            out["discovery"] = compare_model_families(
                split.development, split.validation, config.horizon_minutes,
                feature_values, min_wallet_samples=40)
        else:
            out["discovery"] = {
                "enabled": False,
                "note": "discovery is off by default (Part 16). The frozen "
                        "reproduction and the generalisation study run "
                        "without it."}

        # -- 8. FREEZE, then the untouched holdout, once --------------------
        _log("[8/9] freezing, then opening the untouched holdout", say)
        split.freeze(
            "Frozen before the holdout was opened: the RN1 thresholds "
            "(0.91043 / 0.810012), the +3-minute horizon, the AGGRESSIVE-only "
            "trade rule, the three execution assumptions, the labelling "
            "boundary (1.40) and the wallet/market selection. No holdout "
            "episode informed any of them.")
        holdout = split.holdout()
        holdout_report, holdout_cases = evaluate(
            frozen, holdout, config.horizon_minutes, feature_values)
        out["holdout"] = {
            "frozenDescription": split.frozen_description,
            "episodes": len(holdout),
            "classification": holdout_report.to_dict(),
            "trading": {
                assumption.name: simulate(
                    holdout_cases, oracle, 10.0, assumption,
                    model_version=frozen.version,
                    category_of=category_of).to_dict()
                for assumption in ASSUMPTIONS},
        }
        out["holdout"]["sourceBenchmark"] = _benchmark(holdout_report)

        # -- 9. leakage audit -----------------------------------------------
        _log("[9/9] leakage audit", say)
        rows = []
        for episode in switched[:5000]:
            snapshot = episode.snapshot(config.horizon_minutes)
            if snapshot.ts:
                rows.append((snapshot, features_of(episode, snapshot)))
        out["leakageAudit"] = leakage_audit(rows).to_dict()

        # -- 8b. THE PROMOTION LADDER (§27, §49) ----------------------------
        # The discovery branch's wallet-history candidate has only ever been
        # fitted on development and scored on validation. That is not enough
        # to claim anything, so it goes up the ladder: freeze, then an
        # untouched holdout, then the clean prospective window.
        if config.discovery_enabled:
            _log("[8b/9] promotion ladder for the V2 candidate", say)
            try:
                out["promotion"] = _promote_v2(episodes).to_dict()
                _log("  " + str(out["promotion"].get("verdict", ""))[:170],
                     say)
            except Exception as exc:                     # noqa: BLE001
                out["promotion"] = {"available": False, "reason": repr(exc)}

        # -- structure discovery (§26). The null is allowed to win. ---------
        if config.structure_discovery_enabled:
            _log("[9b/9] hidden-structure discovery (MDL vs null)", say)
            rows, labels = _structure_rows(episodes, config.structure_sample)
            out["structureDiscovery"] = structure_mod.discover(
                rows, labels, seed=config.seed).to_dict()
            _log(f"  {out['structureDiscovery']['verdict']} — "
                 f"{out['structureDiscovery']['candidatesSurvived']} of "
                 f"{out['structureDiscovery']['candidatesExamined']} "
                 "candidates survived", say)
        else:
            out["structureDiscovery"] = {
                "enabled": False,
                "note": "off by default; enable with --structure"}

        # The versioned model registry, including the permanently quarantined
        # contaminated forward-validator run.
        out["modelRegistry"] = default_registry().to_dict()

        # -- the existing-engine integration report (Part 24 / 25) ----------
        out["integrationReport"] = _integration_report(out)

        out["executionRealism"] = {
            "priceProvenance": oracle.provenance(),
            "assumptions": [a.to_dict() for a in ASSUMPTIONS],
            "note": ("Every P&L figure exists in three versions. Where the "
                     "price came from a PRINT rather than a captured book, "
                     "the spread was ASSUMED, not observed — the provenance "
                     "mix above says how often that happened."),
        }
    finally:
        oracle.close()

    out["durationSeconds"] = round(time.time() - started, 1)
    if config.out_dir:
        _write(Path(config.out_dir), out)
    return out


def _promote_v2(episodes: list):
    """Run the wallet-history candidate up the §27 ladder.

    The candidate is the one the discovery branch found strongest — a
    logistic model over entry-time features PLUS the wallet's own prior
    two-sided rate. Fitting and scoring are wired here rather than in
    `promote`, so that module stays a procedure and knows nothing about any
    particular model family.
    """
    from .discovery_v1 import (V1_FEATURES, _fit_estimator, _score_estimator,
                               _score_rule, _rows, _subsample,
                               prior_history_index)

    histories = prior_history_index(episodes)

    def _fit(development):
        x, y, _kept = _rows(development, histories, V1_FEATURES)
        if len(x) < 300 or len(set(y)) < 2:
            return None
        x_fit, y_fit, _available = _subsample(x, y)
        return _fit_estimator(x_fit, y_fit, "logistic")

    def _score(model, rows, version):
        if callable(model) and not hasattr(model, "predict_proba"):
            return _score_rule(model, rows, histories, version).to_dict()
        return _score_estimator(model, V1_FEATURES, rows, histories,
                                version).to_dict()

    return promote_mod.evaluate(
        episodes, _fit, _score,
        candidate_version="UNIVERSAL_PLUS_WALLET_V1")


def _redeem_check(episodes, redemptions: dict) -> dict:
    """Coverage of the §13 REDEEM gate, measured rather than asserted."""
    if not redemptions:
        return {
            "status": "UNAVAILABLE",
            "note": ("no redemptions on record. Run `pqb activity` to "
                     "collect them from the Data API's /activity feed; "
                     "/trades does not carry them. Until then the gate is "
                     "wired but vacuous, and it is NOT approximated from "
                     "SELL, which is a different event.")}
    covered = sum(1 for e in episodes if getattr(e, "redeemed_ts", 0.0))
    total = len(episodes)
    return {
        "status": "ACTIVE",
        "redemptionsOnRecord": len(redemptions),
        "episodesWithARedemption": covered,
        "coverage": round(covered / total, 4) if total else 0.0,
        "note": ("A redeemed condition is FINISHED, whatever the tape's end "
                 "date says — so these episodes are labelled on fact rather "
                 "than on the quiet-period heuristic. Coverage is whatever "
                 "the /activity backfill has reached; run it wider to raise "
                 "it."),
    }


def _composition(v1_block: dict, transitions: dict) -> dict:
    """Is V1 wrong, or is it being asked about a different population?

    The source's 250-condition consolidation reports 20.8 / 58.4 / 20.8. If
    this tape's conditions are overwhelmingly DIRECTIONAL, V1's accuracy
    against a majority baseline says almost nothing about the three-mode
    structure — it says the two populations are not the same population.

    So the comparison is split in two:

    * the UNCONDITIONAL distribution, which is where any composition
      difference shows up; and
    * the distribution CONDITIONAL ON REACHING THE TWO-SIDED STATE, which is
      the source's own subject matter, since its 250 conditions were
      completed RN1 conditions of which 79.2% became two-sided.

    If the second matches and the first does not, the behavioural structure
    reproduced and the base rate did not — which is a completely different
    conclusion from "the model failed", and is not visible from an accuracy
    number.
    """
    actual = ((v1_block.get("retrospective") or {}).get("actualDistribution")
              or {})
    total = sum(actual.values())
    probabilities = (transitions.get("transitionProbabilities") or {}).get(
        "STATE_4_OPPOSITE_TRANSITION") or {}
    next_states = probabilities.get("next") or {}
    protect = float(next_states.get("STATE_5_TWO_SIDED_PROTECT_REBALANCE", 0.0))
    aggressive = float(next_states.get(
        "STATE_6_TWO_SIDED_AGGRESSIVE_OPPOSITE", 0.0))
    denominator = protect + aggressive

    # The source's own conditional split, derived from its published
    # unconditional one rather than assumed: 58.4 and 20.8 of the 79.2 that
    # became two-sided.
    source_two_sided = 0.584 + 0.208
    source_protect = 0.584 / source_two_sided
    source_aggressive = 0.208 / source_two_sided

    ours_protect = (protect / denominator) if denominator else None
    ours_aggressive = (aggressive / denominator) if denominator else None
    gap = (abs(ours_protect - source_protect)
           if ours_protect is not None else None)

    out = {
        "unconditional": {
            "source": {"DIRECTIONAL": 0.208, "PROTECT_REBALANCE": 0.584,
                       "AGGRESSIVE_OPPOSITE": 0.208},
            "thisData": {k: (round(v / total, 4) if total else 0.0)
                         for k, v in actual.items()},
            "twoSidedShare": transitions.get("twoSidedShare"),
            "sourceTwoSidedShare": round(source_two_sided, 4),
        },
        "conditionalOnTwoSided": {
            "source": {"PROTECT_REBALANCE": round(source_protect, 4),
                       "AGGRESSIVE_OPPOSITE": round(source_aggressive, 4)},
            "thisData": {"PROTECT_REBALANCE": (round(ours_protect, 4)
                                               if ours_protect is not None
                                               else None),
                         "AGGRESSIVE_OPPOSITE": (round(ours_aggressive, 4)
                                                 if ours_aggressive is not None
                                                 else None)},
            "observations": probabilities.get("observations"),
            "absoluteGap": round(gap, 4) if gap is not None else None,
        },
        "candidateExplanations": [
            "the source's 250 conditions were COMPLETED RN1 conditions, a "
            "filtered population; this tape's episodes are every "
            "(wallet, market) pair with a BUY, filtered by nothing",
            "a 90-day tape whose last five days carry 87% of its trades "
            "truncates late opposite-side buys, which pushes episodes toward "
            "DIRECTIONAL — the label-sensitivity curve bounds how much",
            "the wallet universe here is 70k wallets, most of whom place one "
            "trade in a market and never return; RN1 is not a typical wallet "
            "and the unconditional rate should not be expected to match",
        ],
    }
    if gap is None:
        out["reading"] = ("not enough two-sided transitions observed to "
                          "compare the conditional split")
    elif gap <= 0.10:
        out["reading"] = (
            f"THE CONDITIONAL STRUCTURE REPRODUCES: given a wallet has "
            f"reached the two-sided state, this tape splits "
            f"{ours_protect:.0%}/{ours_aggressive:.0%} PROTECT/AGGRESSIVE "
            f"against the source's {source_protect:.0%}/"
            f"{source_aggressive:.0%} — within {gap:.1%}. What does NOT match "
            f"is the base rate of becoming two-sided at all "
            f"({float(transitions.get('twoSidedShare') or 0.0):.1%} here vs "
            f"{source_two_sided:.1%} in the source). V1's accuracy against "
            "the majority baseline is therefore a statement about POPULATION "
            "COMPOSITION, not about the three-mode structure.")
    else:
        out["reading"] = (
            f"the conditional split does NOT reproduce: "
            f"{ours_protect:.0%}/{ours_aggressive:.0%} here against the "
            f"source's {source_protect:.0%}/{source_aggressive:.0%}, a gap of "
            f"{gap:.1%}. On this data the three-mode structure is not simply "
            "a composition difference.")
    return out


def _v1_for_wallet(episodes, model, config: RunConfig, wallet: str) -> dict:
    """V1 restricted to one wallet — RN1's own reproduction."""
    rows = [e for e in episodes if e.wallet == wallet]
    if not rows:
        return {"available": False, "reason": f"{wallet[:12]}... not in this "
                                              "store"}
    retro, _ = evaluate_v1(rows, model, config.prospective_boundary_ts,
                           config.prediction_freshness_minutes, False)
    forward, _ = evaluate_v1(rows, model, config.prospective_boundary_ts,
                             config.prediction_freshness_minutes, True)
    return {"available": True, "wallet": wallet, "episodes": len(rows),
            "retrospective": retro.to_dict(),
            "cleanProspective": forward.to_dict()}


def _capital_interpretation(episodes, config: RunConfig) -> dict:
    """How much the reading of `initialObservedCapital` actually matters.

    The frozen rule turns on `capital >= $5`, and "initial observed capital"
    admits more than one reading. The strict one (the first BUY's own fills)
    is what the rule uses; the looser one (everything bought inside the
    freshness window) is measured here so the choice is visible as a choice.
    If the two disagree often, that is a fact about the rule worth knowing
    before anyone reads its accuracy.
    """
    strict_aggressive = loose_aggressive = disagreements = considered = 0
    for episode in episodes:
        price = initial_price(episode)
        if price <= 0 or price > 0.20:
            continue                       # only the AGGRESSIVE arm turns on it
        considered += 1
        strict = initial_capital(episode) >= 5.0
        loose = capital_within_freshness(
            episode, config.prediction_freshness_minutes) >= 5.0
        strict_aggressive += 1 if strict else 0
        loose_aggressive += 1 if loose else 0
        disagreements += 1 if strict != loose else 0
    return {
        "conditionsUnderThePriceArm": considered,
        "aggressiveUnderStrictReading": strict_aggressive,
        "aggressiveUnderLooseReading": loose_aggressive,
        "disagreements": disagreements,
        "disagreementShare": (round(disagreements / considered, 4)
                              if considered else 0.0),
        "used": "strict — the first BUY's own fills",
        "note": ("'initialObservedCapital' could mean the first fill, the "
                 "first instant, or everything inside the freshness window. "
                 "The strict reading is used because it is the one knowable "
                 "at the prediction instant; the looser one is measured "
                 "beside it so the interpretation is auditable rather than "
                 "buried."),
    }


def _structure_rows(episodes, cap: int) -> tuple:
    """Observable rows and their labels, for the MDL search."""
    rows, labels = [], []
    for episode in episodes:
        if not episode.labelled or initial_price(episode) <= 0:
            continue
        same_side = [e for e in episode.events
                     if e.is_buy and e.token_id == episode.original_token]
        rows.append({
            "wallet": episode.wallet,
            "initial_price": initial_price(episode),
            "initial_capital": initial_capital(episode),
            "first_buy_ts": episode.first_buy_ts,
            "category": category_of(episode.question),
            "same_side_buys": len(same_side),
            "seconds_to_add": ((same_side[1].ts - episode.first_buy_ts)
                               if len(same_side) > 1 else 0.0),
        })
        labels.append(episode.label)
        if cap and len(labels) >= cap:
            break
    return rows, labels


def _integration_report(result: dict) -> dict:
    """What this would mean for the existing engine, and what is UNMEASURED.

    The brief's Part 25 asks for `baseline performance / performance with
    wallet features / delta / statistical significance`. That delta cannot be
    computed today and saying so is the answer rather than an evasion: the
    module is disabled, no engine module imports it, and the features have
    therefore never been in front of a strategy. Measuring the delta requires
    running the observation stage first — which is a decision for a person,
    and is exactly the gate Part 26 describes.
    """
    from .report import recommend
    from .signal import WalletStateSignalResult, feature_row

    exposed = sorted(feature_row(WalletStateSignalResult()).keys())
    holdout = (result.get("holdout") or {}).get("classification") or {}
    return {
        "currentStage": "research_only",
        "engineModulesImportingThis": 0,
        "featuresThatWouldBeExposed": exposed,
        "consumptionModes": [
            "additional alpha feature", "signal confirmation",
            "signal filtering", "position-sizing input", "risk adjustment",
            "wallet-quality ranking"],
        "baselinePerformance": "UNMEASURED",
        "performanceWithWalletFeatures": "UNMEASURED",
        "delta": "UNMEASURED",
        "statisticalSignificance": "UNMEASURED",
        "whyUnmeasured": (
            "The module is disabled and nothing in the engine imports it, so "
            "these features have never reached a strategy and there is no "
            "before/after to compare. Producing a delta would require first "
            "enabling the OBSERVE stage — features recorded beside decisions, "
            "influencing none of them — and letting it accumulate paired "
            "observations. That is a deliberate human decision, not something "
            "this run may take."),
        "proposedABDesign": [
            "1. OBSERVE: set stage=observe and integration_enabled=true. The "
            "features are written alongside each decision and change nothing.",
            "2. Accumulate paired records until the engine has enough "
            "decisions WITH a wallet-state signal present to compare against "
            "matched decisions without one.",
            "3. Compare on the existing engine's own measures — expectancy, "
            "win rate, drawdown — split by whether a signal was present, and "
            "bootstrap the difference rather than eyeballing it.",
            "4. Only if the difference is positive, survives the interval, "
            "and is not carried by one wallet or one market, consider "
            "stage=influence — which still requires a human edit.",
        ],
        "behaviouralEvidenceSoFar": {
            "holdoutGraded": holdout.get("graded"),
            "holdoutBalancedAccuracy": holdout.get("balancedAccuracy"),
            "note": ("Behavioural evidence only. It says the wallet's next "
                     "move is predictable; it says nothing about whether "
                     "knowing that improves a trading decision."),
        },
        "recommendation": recommend(result),
    }


def _rn1_block(frozen, episodes, config: RunConfig, oracle,
               say=None) -> dict:
    """Part 8: the exact reproduction, then its profitability."""
    rn1_episodes = [e for e in episodes if e.wallet == RN1_WALLET]
    switched = [e for e in rn1_episodes if e.switched]
    block: dict = {
        "wallet": RN1_WALLET,
        "episodes": len(rn1_episodes),
        "switchedConditions": len(switched),
    }
    if not switched:
        block["available"] = False
        block["reason"] = (
            "RN1 has no switched episode in this store. The exact "
            "reproduction cannot run; the cross-wallet study below still "
            "answers whether the RULE generalises.")
        return block
    block["available"] = True
    report, cases = evaluate(frozen, switched, config.horizon_minutes)
    block["classification"] = report.to_dict()
    _log(f"  RN1: {report.graded} graded, accuracy {report.accuracy:.2%}, "
         f"balanced {report.balanced_accuracy:.2%}", say)

    block["trading"] = {}
    for assumption in ASSUMPTIONS:
        per_stake = {}
        for stake in config.stakes:
            result = simulate(cases, oracle, stake, assumption,
                              model_version=frozen.version,
                              category_of=category_of)
            per_stake[f"${stake:g}"] = result.to_dict()
        block["trading"][assumption.name] = per_stake
    # PART 21 — capital and portfolio. Fixed stakes above; here the two
    # variations that change the shape of the risk rather than its size.
    # Both are reported and neither is tuned: a sizing rule fitted on this
    # sample would be fitted on 3 settled trades, which is not sizing, it is
    # decoration.
    block["portfolio"] = {
        "maxSimultaneous": {},
        "note": ("Proportional sizing is NOT fitted here. Part 21 permits "
                 "tuning it on development data only, and this window's "
                 "settled sample is far too small to tune anything — so the "
                 "honest version is a fixed stake plus an exposure cap, with "
                 "the cap's effect measured rather than optimised."),
    }
    for cap in (1, 3, 5):
        capped = simulate(cases, oracle, 10.0, BASE,
                          model_version=frozen.version,
                          category_of=category_of, max_simultaneous=cap)
        summary = capped.to_dict()
        block["portfolio"]["maxSimultaneous"][str(cap)] = {
            "signals": summary["signals"],
            "filled": summary["filled"],
            "blockedByPortfolio": summary["unfilledReasons"].get(
                "portfolio full", 0),
            "settled": summary["settled"],
        }
    block["baselines"] = baselines(cases, oracle, 10.0, BASE,
                                   category_of=category_of,
                                   seed=config.seed)
    block["signalAddsValue"] = compare(
        block["trading"]["BASE"]["$10"], block["baselines"])
    block["sourceBenchmark"] = _benchmark(report)
    return block


def _benchmark(report) -> dict:
    """Part 27: the source's numbers, and what ours actually are.

    Stated as a comparison rather than a target. The source's 235-condition
    holdout is not this store's population, so a difference is a difference in
    data before it is a difference in the rule — and saying otherwise would be
    exactly the "assume it reproduces" the brief forbids.
    """
    return {
        "source": {"holdoutConditions": 235, "holdoutAccuracy": 0.7489,
                   "holdoutBalancedAccuracy": 0.7301,
                   "prospectiveCases": 50, "prospectiveAccuracy": 0.78,
                   "prospectiveBalancedAccuracy": 0.7624},
        "reproduced": {"gradedCases": report.graded,
                       "accuracy": round(report.accuracy, 4),
                       "balancedAccuracy": round(
                           report.balanced_accuracy, 4)},
        "verdict": _benchmark_verdict(report),
    }


def _benchmark_verdict(report) -> str:
    if report.graded < 50:
        return (f"NOT COMPARABLE — {report.graded} graded case(s) against the "
                "source's 235. This store's window is far shorter than the "
                "one the source studied; the numbers are reported, not "
                "compared.")
    delta = report.balanced_accuracy - 0.7301
    if abs(delta) <= 0.05:
        return (f"balanced accuracy {report.balanced_accuracy:.2%} is within "
                "5 points of the source's 73.01% — consistent with the "
                "source finding on this data")
    if delta > 0:
        return (f"balanced accuracy {report.balanced_accuracy:.2%} EXCEEDS "
                "the source's 73.01%. Treat with suspicion before "
                "celebration: a different population can flatter a fixed rule "
                "as easily as it can punish it.")
    return (f"balanced accuracy {report.balanced_accuracy:.2%} is materially "
            "BELOW the source's 73.01% — on this data the frozen rule does "
            "not reproduce the source's behavioural result")


def _wallet_shortlist(config: RunConfig) -> Optional[list]:
    """Which wallets to load. RN1 is always included, whatever the cap."""
    if not config.max_wallets:
        return None
    import sqlite3

    path = Path(config.intel_path)
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT wallet, COUNT(*) n FROM wallet_trades "
            "WHERE market_id != '' GROUP BY wallet HAVING n >= ? "
            "ORDER BY n DESC LIMIT ?",
            (config.min_wallet_trades, config.max_wallets)).fetchall()
    finally:
        conn.close()
    wallets = {str(r[0]).lower() for r in rows}
    wallets.add(RN1_WALLET)
    return sorted(wallets)


def _redemptions(intel_path: str) -> dict:
    """`(wallet, market) -> redemption ts`, from the /activity backfill."""
    from ..analytics.store import IntelStore

    path = Path(intel_path)
    if not path.exists():
        return {}
    store = IntelStore(path)
    try:
        return store.redemptions()
    except Exception:                                     # noqa: BLE001
        return {}
    finally:
        store.close()


def _settled_markets(intel_path: str) -> set:
    import sqlite3

    path = Path(intel_path)
    if not path.exists():
        return set()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT market_id FROM resolutions "
            "WHERE market_id IS NOT NULL")}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _write(out_dir: Path, result: dict) -> None:
    """Machine-readable results plus the human report (Part 24)."""
    from .report import render

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "wallet_state_research.json").write_text(
        json.dumps(result, indent=1, default=str), encoding="utf-8")
    (out_dir / "wallet_state_research.txt").write_text(
        render(result), encoding="utf-8")
    for name, key in (("rn1_reproduction", "rn1"),
                      ("cross_wallet", "crossWallet"),
                      ("cross_market", "crossMarket"),
                      ("walk_forward", "walkForward"),
                      ("holdout", "holdout"),
                      ("leakage_audit", "leakageAudit"),
                      ("integration_report", "integrationReport"),
                      ("strategy_v1", "strategyV1"),
                      ("state_transitions", "stateTransitions"),
                      ("structure_discovery", "structureDiscovery"),
                      ("model_registry", "modelRegistry"),
                      ("data_quality", "dataQuality"),
                      ("base_rate_decomposition",
                       "baseRateDecomposition"),
                      ("promotion", "promotion"),
                      ("population_composition",
                       "populationComposition"),
                      ("execution_realism", "executionRealism"),
                      ("discovery", "discovery")):
        if key in result:
            (out_dir / f"{name}.json").write_text(
                json.dumps(result[key], indent=1, default=str),
                encoding="utf-8")
