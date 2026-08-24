"""Wallet behavioral discovery: the wallet is never the proof.

Pinned: reconstruction uses entry-time tape only (a later tape row can
never change an entry feature); a behavior must REPEAT within a wallet
before it may hypothesize; two wallets sharing one behavior merge into
ONE canonical candidate whose identity excludes the wallets and the
discovered thresholds; replay is wallet-free; resolution holds take one
observation per market and skip unsettled markets; source markets join
the permanent exclusions; and a validated wallet-pattern rule still
cannot vote in the live engine.
"""

from __future__ import annotations

from pqb.analytics.wallet_behavior import (Observation, band_bucket,
                                           behavioral_profile,
                                           classify_engagement, convergences,
                                           describe, engagements_of,
                                           frozen_replay, hold_class, mine,
                                           observations_from_token, study,
                                           two_sided_study)
from pqb.research import (DiscoveredStrategy, family_of, signature_of,
                          variant_expansions)

T0 = 1_000_000.0


def _row(ts, price, wallet="0xw1", token="tokA", market="M1",
         side="BUY", usdc=50.0):
    return {"wallet": wallet, "market_id": market, "token_id": token,
            "ts": ts, "price": price, "usdc": usdc, "side": side}


def _tape(*points):
    return [{"ts": ts, "price": price} for ts, price in points]


# -- reconstruction ----------------------------------------------------------

def test_entry_features_come_from_the_past_only():
    tape = _tape((T0 - 7200, 0.50), (T0 - 300, 0.40), (T0 + 60, 0.90))
    obs = observations_from_token(
        [_row(T0, 0.40)], tape, payout=1.0)[0]
    # move = last price before entry (0.40) minus last price at/before
    # the lookback window opening (0.50) - the future 0.90 never appears.
    assert abs(obs.move_before - (-0.10)) < 1e-9
    assert obs.market_age == T0 - (T0 - 7200)


def test_future_tape_rows_cannot_change_features():
    tape = _tape((T0 - 7200, 0.50), (T0 - 300, 0.40))
    with_future = tape + _tape((T0 + 10, 0.99), (T0 + 20, 0.01))
    a = observations_from_token([_row(T0, 0.40)], tape, 1.0)[0]
    b = observations_from_token([_row(T0, 0.40)], with_future, 1.0)[0]
    assert a.move_before == b.move_before
    assert a.market_age == b.market_age


def test_move_unavailable_without_old_enough_tape_is_none_not_zero():
    obs = observations_from_token(
        [_row(T0, 0.40)], _tape((T0 - 60, 0.41)), 1.0)[0]
    assert obs.move_before is None          # unavailable, never invented


def test_exit_is_the_wallets_own_first_sell():
    rows = [_row(T0, 0.40), _row(T0 + 3000, 0.55, side="SELL")]
    obs = observations_from_token(rows, _tape((T0 - 7200, 0.42)), 1.0)[0]
    assert obs.exit_price == 0.55
    assert obs.hold_seconds == 3000
    assert abs(obs.realized_return() - 0.15) < 1e-9


def test_no_sell_means_resolution_hold_and_payout_result():
    obs = observations_from_token(
        [_row(T0, 0.40)], _tape((T0 - 7200, 0.42), (T0 + 9000, 0.9)), 1.0)[0]
    assert obs.hold_seconds is None
    assert hold_class(obs.hold_seconds) == "resolution"
    assert abs(obs.realized_return() - 0.60) < 1e-9
    assert obs.time_to_resolution == 9000   # LABEL only


def test_adds_are_sizing_behavior_not_new_observations():
    rows = [_row(T0, 0.40), _row(T0 + 100, 0.42), _row(T0 + 200, 0.44)]
    out = observations_from_token(rows, _tape((T0 - 7200, 0.42)), 1.0)
    assert len(out) == 1
    assert out[0].adds == 2


def test_side_switching_is_a_label():
    opposite = [_row(T0 + 500, 0.55, token="tokB")]
    obs = observations_from_token(
        [_row(T0, 0.40)], _tape((T0 - 7200, 0.42)), 1.0, opposite)[0]
    assert obs.switched_sides
    assert obs.origin == "entry"           # switching AFTER is not a switch


# -- conditional side-switch reconstruction (operator's §4) ------------------

def test_prior_opposite_buy_makes_the_entry_a_side_switch():
    opposite = [_row(T0 - 5000, 0.70, token="tokB")]
    opposite_tape = _tape((T0 - 5000, 0.70), (T0 - 100, 0.55))
    obs = observations_from_token(
        [_row(T0, 0.40)], _tape((T0 - 7200, 0.42)), 1.0,
        opposite, opposite_tape)[0]
    assert obs.origin == "side_switch"
    assert obs.switch_gap_seconds == 5000
    # The old side was bought at 0.70 and marks 0.55 at the switch: the
    # wallet is switching away from a LOSING position.
    assert obs.prior_result == "losing"


def test_prior_result_uses_the_mark_before_the_switch_only():
    opposite = [_row(T0 - 5000, 0.70, token="tokB")]
    tape_before = _tape((T0 - 5000, 0.70), (T0 - 100, 0.80))
    tape_with_future = tape_before + _tape((T0 + 100, 0.05))
    a = observations_from_token([_row(T0, 0.40)],
                                _tape((T0 - 7200, 0.42)), 1.0,
                                opposite, tape_before)[0]
    b = observations_from_token([_row(T0, 0.40)],
                                _tape((T0 - 7200, 0.42)), 1.0,
                                opposite, tape_with_future)[0]
    assert a.prior_result == "winning"
    assert b.prior_result == "winning"     # the future crash never leaks


def test_switch_without_opposite_tape_marks_condition_unavailable():
    opposite = [_row(T0 - 5000, 0.70, token="tokB")]
    obs = observations_from_token(
        [_row(T0, 0.40)], _tape((T0 - 7200, 0.42)), 1.0, opposite)[0]
    assert obs.origin == "side_switch"
    assert obs.prior_result is None        # unavailable, never invented


# -- mining: repetition, dedup, convergence ----------------------------------

def _obs(wallet, market, price=0.30, move=-0.08, hold=None, payout=1.0):
    o = Observation(wallet=wallet, market=market, token=market + "tok",
                    entry_ts=T0, entry_price=price, move_before=move,
                    market_age=7200.0, payout=payout)
    if hold is not None:
        o.exit_ts = T0 + hold
        o.exit_price = min(0.99, price + 0.10)
    return o


def test_two_wallets_one_behavior_one_canonical_candidate():
    observations = []
    for wallet in ("0xa", "0xb"):
        for i in range(5):
            observations.append(_obs(wallet, f"M{i}"))
    mined = mine(observations, cost=0.02, min_trades=6, min_markets=4)
    assert len(mined["candidates"]) == 1
    rule = mined["candidates"][0]
    assert rule["supporting_wallets"] == 2
    assert rule["source_wallets"] == ["0xa", "0xb"]
    assert mined["funnel"]["duplicatesMerged"] == 1
    assert mined["funnel"]["multiWallet"] == 1
    assert rule["source_markets"] == 5
    assert set(rule["source_markets_list"]) == {f"M{i}" for i in range(5)}


def test_unrepeated_behavior_never_hypothesizes():
    # Ten wallets, one dip-buy each: a population accident, not a
    # repeating behavior of anyone.
    observations = [_obs(f"0x{i}", f"M{i}") for i in range(10)]
    mined = mine(observations, cost=0.02, min_trades=6, min_markets=4)
    assert mined["candidates"] == []
    assert "behavior never repeats within any single wallet" in \
        mined["funnel"]["rejectReasons"]


def test_market_breadth_is_required_not_trade_count():
    # 12 trades but only 2 markets: trades and independent markets stay
    # separate ledgers.
    observations = [_obs("0xa", f"M{i % 2}") for i in range(12)]
    mined = mine(observations, cost=0.02, min_trades=6, min_markets=4)
    assert mined["candidates"] == []
    assert "insufficient independent source markets" in \
        mined["funnel"]["rejectReasons"]


def test_losing_source_record_is_rejected():
    observations = [_obs("0xa", f"M{i}", payout=0.0) for i in range(6)]
    mined = mine(observations, cost=0.02, min_trades=5, min_markets=4)
    assert mined["candidates"] == []
    assert "source record cannot clear costs" in \
        mined["funnel"]["rejectReasons"]


def test_hold_class_comes_from_the_wallets_own_exits():
    observations = [_obs("0xa", f"M{i}", hold=2 * 3600.0) for i in range(6)]
    mined = mine(observations, cost=0.02, min_trades=5, min_markets=4)
    rule = mined["candidates"][0]
    assert rule["hold"] == "short"
    assert rule["hold_seconds"] == 2 * 3600.0


def test_discovered_band_stays_inside_the_identity_bucket():
    observations = [_obs("0xa", f"M{i}", price=0.22 + i * 0.03)
                    for i in range(6)]
    rule = mine(observations, cost=0.02, min_trades=5,
                min_markets=4)["candidates"][0]
    assert rule["band"] == "low"
    assert 0.20 <= rule["prob_lo"] < rule["prob_hi"] <= 0.40


def test_switch_and_entry_populations_never_merge():
    """§5: the switching and non-switching versions of one entry
    condition are competing hypotheses, mined and identified apart."""
    observations = []
    for i in range(6):
        observations.append(_obs("0xa", f"M{i}"))
        switch = _obs("0xb", f"S{i}")
        switch.origin = "side_switch"
        switch.prior_result = "losing"
        observations.append(switch)
    mined = mine(observations, cost=0.02, min_trades=5, min_markets=4,
                 top_n=4)
    origins = {c.get("origin") for c in mined["candidates"]}
    assert origins == {"entry", "side_switch"}
    assert mined["funnel"]["switchCells"] == 1
    assert mined["funnel"]["keptSwitch"] == 1
    switch_rule = next(c for c in mined["candidates"]
                       if c["origin"] == "side_switch")
    assert switch_rule["switch_after"] == "losing"
    assert switch_rule["switch_prior_split"] == {"losing": 6}
    entry_rule = next(c for c in mined["candidates"]
                      if c["origin"] == "entry")
    assert signature_of(switch_rule) != signature_of(entry_rule)


# -- identity: wallets and thresholds are not the signature ------------------

def test_signature_excludes_wallets_and_thresholds():
    a = {"type": "wallet_behavior", "direction": "long", "band": "low",
         "trigger": "after_drop", "hold": "resolution",
         "prob_lo": 0.20, "prob_hi": 0.35, "move_min": 0.04,
         "source_wallets": ["0xa"]}
    b = dict(a, prob_lo=0.25, prob_hi=0.40, move_min=0.08,
             source_wallets=["0xb", "0xc"])
    assert signature_of(a) == signature_of(b)
    assert signature_of(a).startswith("wpat|")
    assert family_of(a) == "wallet-pattern"


# -- wallet-free replay ------------------------------------------------------

_TIMED_RULE = {"type": "wallet_behavior", "direction": "long",
               "band": "low", "trigger": "after_drop", "hold": "short",
               "prob_lo": 0.20, "prob_hi": 0.40, "move_min": 0.05,
               "lookback_seconds": 3600.0, "hold_seconds": 3600.0}


def _bars(*points, lead=0.50):
    """A series long enough to clear the degenerate-series floor: seven
    flat lead-in bars, then the scripted points."""
    leading = [{"_ts": ts, "price": lead}
               for ts in range(-70_000, -5_000, 10_000)]
    return leading + [{"_ts": ts, "price": price} for ts, price in points]


def test_replay_enters_on_the_trigger_and_exits_at_the_hold():
    rows = _bars((0, 0.40), (4000, 0.30), (8000, 0.38))
    stats = frozen_replay(rows, _TIMED_RULE, payout=None, cost=0.02)
    assert stats["trades"] == 1
    assert abs(stats["pnl"] - (0.38 - 0.30 - 0.02)) < 1e-9
    assert stats["wins"] == 1


def test_replay_without_the_trigger_stays_out():
    # Flat 0.33 lead-in, tiny drift: never a >=5% drop, never an entry.
    rows = _bars((0, 0.31), (4000, 0.30), (8000, 0.38), lead=0.33)
    assert frozen_replay(rows, _TIMED_RULE, None, 0.02)["trades"] == 0


def test_short_direction_mirrors_the_pnl():
    rows = _bars((0, 0.40), (4000, 0.30), (8000, 0.38))
    stats = frozen_replay(rows, dict(_TIMED_RULE, direction="short"),
                          None, 0.02)
    assert abs(stats["pnl"] - (-(0.38 - 0.30) - 0.02)) < 1e-9


def test_resolution_hold_is_one_observation_and_needs_the_payout():
    rule = dict(_TIMED_RULE, hold="resolution")
    rows = _bars((0, 0.40), (4000, 0.30), (8000, 0.25), (12000, 0.22))
    stats = frozen_replay(rows, rule, payout=1.0, cost=0.02)
    assert stats["trades"] == 1                 # never stacks the payout
    assert abs(stats["pnl"] - (1.0 - 0.30 - 0.02)) < 1e-9
    assert frozen_replay(rows, rule, payout=None,
                         cost=0.02)["trades"] == 0   # unsettled: no verdict


# -- variants: direction and hold as discovery variables ---------------------

class _VariantLibrary:
    def all_strategies(self):
        return []


def _entry(rule, cum):
    return {"id": "wpat|long|low|after_drop|short#v1", "rule": rule,
            "signature": signature_of(rule), "describe": "WALLET-PATTERN",
            "status": "validating"}, cum


class _Cfg:
    assumed_spread = 0.01


def test_decisive_loser_spawns_the_inverse_direction():
    rule = dict(_TIMED_RULE)
    entry, cum = _entry(rule, {"trades": 20, "expectancy": -0.10})
    out = variant_expansions(_VariantLibrary(), entry, cum, _Cfg())
    inverses = [r for r, _ in out if r.get("variant") == "inverse"]
    assert len(inverses) == 1
    assert inverses[0]["direction"] == "short"


def test_promising_timed_hold_spawns_half_and_double():
    rule = dict(_TIMED_RULE)
    entry, cum = _entry(rule, {"trades": 20, "expectancy": 0.05})
    out = variant_expansions(_VariantLibrary(), entry, cum, _Cfg())
    holds = sorted(r["hold_seconds"] for r, _ in out
                   if r.get("variant") in ("half-hold", "double-hold"))
    assert holds == [1800.0, 7200.0]


def test_resolution_hold_has_no_hold_dial():
    rule = dict(_TIMED_RULE, hold="resolution")
    rule.pop("hold_seconds")
    entry, cum = _entry(rule, {"trades": 20, "expectancy": 0.05})
    out = variant_expansions(_VariantLibrary(), entry, cum, _Cfg())
    assert all(r.get("variant") not in ("half-hold", "double-hold")
               for r, _ in out)


# -- convergent discovery ----------------------------------------------------

def test_convergence_finds_the_independent_quant_twin():
    rule = dict(_TIMED_RULE)                        # buy weakness = reversion
    existing = [
        {"signature": "price_z|long", "rule": {
            "type": None, "direction": "long", "entry_op": "<",
            "entry_feature": "price_z"}},
        {"signature": "longshot|x", "rule": {
            "type": "longshot", "side": "low",
            "prob_lo": 0.15, "prob_hi": 0.25}},
        {"signature": "wpat|other", "rule": {"type": "wallet_behavior"}},
    ]
    matched = convergences(rule, existing)
    assert "price_z|long" in matched                # mean-reversion twin
    assert "longshot|x" in matched                  # overlapping low band
    assert "wpat|other" not in matched              # never its own kind


# -- the source markets can never testify ------------------------------------

def test_source_markets_join_the_permanent_exclusions(tmp_path):
    from pqb.library import StrategyLibrary

    rule = dict(_TIMED_RULE, source_markets_list=["M1", "M2"])
    library = StrategyLibrary(tmp_path / "lib.sqlite3")
    row_id = library.upsert_candidate(
        signature_of(rule), rule, describe(rule),
        discovery_markets={"P1"} | set(rule["source_markets_list"]),
        family=family_of(rule))
    excluded = library.excluded_markets(row_id)
    assert {"M1", "M2", "P1"} <= excluded
    library.close()


# -- the study end-to-end ----------------------------------------------------

class FakeStore:
    """Dispatches the three SQL shapes study() issues."""

    def __init__(self, trades, payouts):
        self._trades = trades
        self._payouts = payouts

    def resolutions(self):
        return dict(self._payouts)

    def query(self, sql, params=()):
        if "wallet_scores" in sql:
            return []
        if "lower(wallet) IN" in sql:
            wanted = {str(p).lower() for p in params}
            return [dict(t) for t in self._trades
                    if str(t["wallet"]).lower() in wanted]
        if "WHERE token_id" in sql:
            token = params[0]
            return [{"ts": t["ts"], "price": t["price"]}
                    for t in self._trades if t["token_id"] == token]
        raise AssertionError(f"unexpected query: {sql}")


def test_study_finds_the_repeated_dip_buy():
    trades, payouts = [], {}
    for i in range(6):
        token, market = f"tok{i}", f"M{i}"
        base = T0 + i * 100_000
        payouts[token] = 1.0
        # The tape: a drift down other wallets produce...
        trades.append(_row(base - 7200, 0.40, wallet="0xcrowd",
                           token=token, market=market))
        trades.append(_row(base - 600, 0.30, wallet="0xcrowd",
                           token=token, market=market))
        # ...and both studied wallets buying the dip, repeatedly.
        for wallet in ("0xa", "0xb"):
            trades.append(_row(base, 0.30, wallet=wallet, token=token,
                               market=market))
    result = study(FakeStore(trades, payouts), pinned=["0xa", "0xb"],
                   cost=0.02, min_trades=6, min_markets=4,
                   min_wallet_trades=4)
    assert result["funnel"]["walletsEligible"] == 2
    assert len(result["candidates"]) == 1
    rule = result["candidates"][0]
    assert rule["type"] == "wallet_behavior"
    assert rule["supporting_wallets"] == 2
    assert rule["hold"] == "resolution"
    report = result["perWallet"]["0xa"]
    assert report["settled"] == 6
    assert "resolution" in report["holdClasses"]


def test_study_reconstructs_conditional_switches():
    trades, payouts = [], {}
    for i in range(4):
        yes, no, market = f"yes{i}", f"no{i}", f"M{i}"
        base = T0 + i * 100_000
        payouts[yes], payouts[no] = 0.0, 1.0
        # Crowd tapes for both sides; YES decays (the wallet's first side
        # is losing), NO rises.
        trades += [_row(base - 7200, 0.70, wallet="0xc", token=yes,
                        market=market),
                   _row(base - 300, 0.55, wallet="0xc", token=yes,
                        market=market),
                   _row(base - 7200, 0.30, wallet="0xc", token=no,
                        market=market),
                   _row(base - 200, 0.42, wallet="0xc", token=no,
                        market=market)]
        # The studied wallet: buys YES, then switches to NO while losing.
        trades += [_row(base - 6000, 0.70, wallet="0xa", token=yes,
                        market=market),
                   _row(base, 0.42, wallet="0xa", token=no, market=market)]
    result = study(FakeStore(trades, payouts), pinned=["0xa"], cost=0.02,
                   min_trades=4, min_markets=3, min_wallet_trades=4)
    report = result["perWallet"]["0xa"]
    assert report["switches"] == 4
    assert report["switchAfterLosing"] == 4
    assert report["postSwitchWinRate"] == 1.0
    assert result["funnel"]["switchObservations"] == 4
    # And the behavior generalized into a wallet-free switch hypothesis.
    switch_rules = [c for c in result["candidates"]
                    if c.get("origin") == "side_switch"]
    assert switch_rules and switch_rules[0]["switch_after"] == "losing"


def test_multi_outcome_switch_marks_the_token_actually_held():
    """A three-outcome market: the prior position's win/lose mark must
    come from the token the wallet really held, not whichever sibling
    happened to be listed first."""
    trades, payouts = [], {}
    for i in range(3):
        held, other, entered = f"a{i}", f"b{i}", f"c{i}"
        market, base = f"M{i}", T0 + i * 100_000
        payouts.update({held: 0.0, other: 0.0, entered: 1.0})
        # The wallet holds `held` (bought 0.70, now marking 0.50 -> losing).
        trades += [_row(base - 6000, 0.70, wallet="0xa", token=held,
                        market=market),
                   _row(base - 100, 0.50, wallet="0xc", token=held,
                        market=market)]
        # A decoy sibling it never held, drifting the OTHER way.
        trades += [_row(base - 6000, 0.10, wallet="0xc", token=other,
                        market=market),
                   _row(base - 100, 0.90, wallet="0xc", token=other,
                        market=market)]
        # ...then switches into `entered`.
        trades += [_row(base - 7200, 0.30, wallet="0xc", token=entered,
                        market=market),
                   _row(base, 0.35, wallet="0xa", token=entered,
                        market=market)]
    result = study(FakeStore(trades, payouts), pinned=["0xa"], cost=0.02,
                   min_trades=3, min_markets=3, min_wallet_trades=3)
    report = result["perWallet"]["0xa"]
    assert report["switches"] == 3
    # Read off the decoy's tape this would have said "winning".
    assert report["switchAfterLosing"] == 3
    assert report["switchAfterWinning"] == 0


def test_study_without_wallets_says_so():
    result = study(FakeStore([], {}), pinned=[], cost=0.02)
    assert result["candidates"] == []
    assert "no ranked or pinned wallets yet" in \
        result["funnel"]["rejectReasons"]


# -- the two-sided taxonomy and hypothesis test ------------------------------

def _leg(wallet, market, token, ts, price, exit_ts=None, payout=1.0,
         usd=100.0):
    o = Observation(wallet=wallet, market=market, token=token, entry_ts=ts,
                    entry_price=price, move_before=-0.08, payout=payout,
                    entry_usd=usd)
    if exit_ts is not None:
        o.exit_ts = exit_ts
        o.exit_price = price + 0.05
    return o


def test_holding_both_sides_at_once_is_hedge_like():
    legs = [_leg("0xa", "M", "yes", T0, 0.45),           # never exited
            _leg("0xa", "M", "no", T0 + 600, 0.52)]
    kind, gap = classify_engagement(legs)
    assert kind == "simultaneous_two_sided"
    assert gap == 600


def test_exiting_before_taking_the_other_side_is_reversal_like():
    legs = [_leg("0xa", "M", "yes", T0, 0.45, exit_ts=T0 + 300),
            _leg("0xa", "M", "no", T0 + 600, 0.52)]
    kind, _gap = classify_engagement(legs)
    assert kind == "sequential_two_sided"


def test_one_leg_is_one_sided():
    assert classify_engagement([_leg("0xa", "M", "yes", T0, 0.45)])[0] \
        == "one_sided"


def test_engagements_group_by_market_and_book_return_is_weighted():
    legs = [_leg("0xa", "M1", "yes", T0, 0.40, payout=1.0, usd=900.0),
            _leg("0xa", "M1", "no", T0 + 60, 0.55, payout=0.0, usd=100.0)]
    engagement = engagements_of(legs)[0]
    assert engagement.market == "M1"
    # +0.60 on $900 and -0.55 on $100 -> notional-weighted, not a plain mean.
    assert abs(engagement.book_return()
               - ((0.60 * 900 - 0.55 * 100) / 1000)) < 1e-9


def test_two_sided_share_is_never_reported_as_an_edge():
    """The operator's rule: '40% plays both sides' is an observation."""
    legs = []
    for i in range(4):                       # 4 two-sided, 6 one-sided
        legs += [_leg("0xa", f"T{i}", "yes", T0, 0.45),
                 _leg("0xa", f"T{i}", "no", T0 + 60, 0.52, payout=0.0)]
    for i in range(6):
        legs.append(_leg("0xa", f"O{i}", "yes", T0, 0.45))
    result = two_sided_study(engagements_of(legs))
    assert result["twoSidedMarketShare"] == 0.4
    verdict = result["verdict"]
    assert "observation, not an edge" in verdict
    assert "40%" in verdict


def test_a_real_difference_is_called_measured_not_validated():
    legs = []
    for i in range(12):                      # two-sided books do better
        legs += [_leg("0xa", f"T{i}", "yes", T0, 0.45, payout=1.0),
                 _leg("0xa", f"T{i}", "no", T0 + 60, 0.45, payout=1.0)]
    for i in range(12):
        legs.append(_leg("0xa", f"O{i}", "yes", T0, 0.45, payout=0.0))
    result = two_sided_study(engagements_of(legs))
    verdict = result["verdict"]
    assert "measured" in verdict
    assert "not a validated edge" in verdict
    assert result["matchedIncremental"] > 0


def test_comparison_is_matched_on_entry_price():
    """A two-sided book at 90c must not be credited for beating one-sided
    longshots at 10c — only buckets holding both kinds can speak."""
    legs = []
    for i in range(10):                      # one-sided longshots only
        legs.append(_leg("0xa", f"L{i}", "yes", T0, 0.10, payout=0.0))
    for i in range(10):                      # two-sided favorites only
        legs += [_leg("0xa", f"F{i}", "yes", T0, 0.90, payout=1.0),
                 _leg("0xa", f"F{i}", "no", T0 + 60, 0.90, payout=1.0)]
    result = two_sided_study(engagements_of(legs))
    assert result["byEntryPrice"] == {}      # no bucket holds both kinds
    assert result["matchedIncremental"] is None
    assert "unmatched" in result["verdict"]


def test_profile_reports_behaviour_and_sample_quality():
    legs = []
    for i in range(30):
        legs.append(_leg("0xa", f"M{i}", "yes", T0 + i, 0.30,
                         exit_ts=T0 + i + 1800))
    profile = behavioral_profile(legs)
    assert profile["independentMarkets"] == 30
    assert profile["markets"] == profile["independentMarkets"]
    assert profile["medianHold"] == 1800.0
    assert profile["entryPriceBias"] == "low"
    assert "ENTRY-HOLD-EXIT" in profile["sequencePattern"]
    assert 0.0 < profile["sampleQuality"] <= 1.0


def test_sample_quality_punishes_narrow_market_coverage():
    """50 trades in 2 markets is not 50 trades in 25 markets."""
    narrow = behavioral_profile(
        [_leg("0xa", f"M{i % 2}", "yes", T0 + i, 0.30) for i in range(50)])
    broad = behavioral_profile(
        [_leg("0xa", f"M{i}", "yes", T0 + i, 0.30) for i in range(50)])
    assert narrow["independentMarkets"] == 2
    assert broad["independentMarkets"] == 50
    assert narrow["sampleQuality"] < broad["sampleQuality"] / 3


# -- the scoring audit the operator asked for --------------------------------

def test_a_spectacular_tiny_sample_cannot_outrank_a_long_record():
    """His exact scenario: +200% over 8 trades vs a 300-trade record."""
    from pqb.analytics.features import TradeScore, WalletProfile
    from pqb.analytics.ranking import rank_wallets

    def _profile(wallet, n, ret, wins, markets):
        p = WalletProfile(wallet=wallet, trades=n, markets=markets,
                          last_seen=2_000_000_000)
        for i in range(n):
            p.scores.append(TradeScore(
                ts=1, token_id=f"t{i}", market_id=f"m{i % markets}",
                side="BUY", price=0.5, usdc=100.0, value=1.0, ret=ret,
                win=i < wins, resolved=True))
        return p

    lucky = _profile("0xlucky", 8, 2.0, 8, 3)
    steady = _profile("0xsteady", 300, 0.08, 186, 120)
    intel = rank_wallets({p.wallet: p for p in (lucky, steady)},
                         now=2_000_000_000)
    assert intel["0xsteady"].rank == 1
    assert intel["0xlucky"].rank == 2
    # The estimate may still be flattering; the ORDERING is what is honest.
    assert intel["0xlucky"].score > intel["0xsteady"].score
    assert intel["0xlucky"].rank_score < intel["0xsteady"].rank_score


# -- nothing trades ----------------------------------------------------------

def test_validated_wallet_pattern_still_cannot_vote(tmp_path):
    from pqb.bridge.lean_engine import LeanDecisionEngine
    from pqb.config import Config

    cfg = Config()
    cfg.root = tmp_path
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    pattern = DiscoveredStrategy(rule=dict(_TIMED_RULE),
                                 signature="wpat|x", describe="WALLET-PATTERN")
    pattern.status = "validated"
    engine.strategies = [pattern]
    assert engine.trading_strategies == []


def test_describe_reads_naturally():
    rule = dict(_TIMED_RULE, supporting_wallets=3)
    text = describe(rule)
    assert "3 wallet" in text
    assert "20%-40%" in text
    assert "drop" in text


def test_band_bucket_edges():
    assert band_bucket(0.10) == "longshot"
    assert band_bucket(0.50) == "mid"
    assert band_bucket(0.85) == "favorite"
    assert band_bucket(0.005) == ""
