"""Feature generation and the dynamic wallet ranking."""

from __future__ import annotations

import time

from pqb.analytics.features import (
    MIN_SCORING_USDC, RETURN_CEILING, build_profiles, score_trade,
)
from pqb.analytics.ranking import (
    MIN_RANKED_SAMPLE, SHRINKAGE_K, rank_wallets, staleness_factor,
)


def row(wallet="0xaaa", ts=1000, token="T1", side="BUY", price=0.40,
        usdc=100.0, market="M1", size=None):
    # `size` matters: hedged books are netted in SHARES, so a row without it
    # cannot be valued. Derived from the notional unless given explicitly.
    return {"wallet": wallet, "ts": ts, "token_id": token, "side": side,
            "price": price, "usdc": usdc, "market_id": market,
            "size": size if size is not None else (usdc / price if price else 0.0)}


# --- scoring one trade ------------------------------------------------------

def test_buy_below_settlement_is_a_win():
    score = score_trade(row(price=0.25), {"T1": 1.0})
    assert score.win is True
    assert score.ret == 3.0                     # (1.00 - 0.25) / 0.25
    assert score.resolved is True


def test_sell_above_settlement_is_a_win():
    score = score_trade(row(side="SELL", price=0.80), {"T1": 0.0})
    assert score.win is True
    assert score.ret == 1.0                     # (0.80 - 0.00) / 0.80


def test_extreme_win_is_clipped():
    # A winning buy at a cent returns +9900%. Unclipped, one such trade would
    # dominate the wallet's average and the whole population prior with it.
    score = score_trade(row(price=0.01), {"T1": 1.0})
    assert score.ret == RETURN_CEILING


def test_unscoreable_trade_is_none_not_zero():
    # No settlement and no mark. Scoring it as 0.0 would drag every wallet's
    # average toward zero in proportion to how many markets are still open.
    assert score_trade(row(), {}) is None


def test_unresolved_falls_back_to_the_mark():
    score = score_trade(row(price=0.40), {}, mark_fn=lambda _t: 0.60)
    assert score is not None
    assert score.resolved is False
    assert round(score.ret, 4) == 0.5


def test_dust_trades_do_not_score():
    assert score_trade(row(usdc=MIN_SCORING_USDC - 0.01), {"T1": 1.0}) is None


# --- profiles ---------------------------------------------------------------

def test_size_z_is_relative_to_the_wallets_own_history():
    rows = [row(ts=1000 + i, usdc=100.0) for i in range(10)]
    rows.append(row(ts=2000, usdc=5_000.0))
    profiles = build_profiles(rows, {}, mark_fn=lambda _t: 0.40)
    profile = profiles["0xaaa"]
    assert profile.size_z(5_000.0) > 2.0
    assert profile.size_z(100.0) < 1.0


def test_size_z_needs_history():
    profiles = build_profiles([row()], {}, mark_fn=lambda _t: 0.4)
    assert profiles["0xaaa"].size_z(10_000.0) == 0.0


def test_realized_usdc_is_notional_weighted():
    # Right on the big trade, wrong on the small one. A plain win-rate cannot
    # tell this wallet from its mirror image; the weighted figure can.
    # Different MARKETS. Two outcomes bought in the SAME market is a hedged
    # book and is deliberately netted into one position instead (see
    # net_book_score), which is a different behaviour with its own test.
    rows = [row(ts=1, token="WIN", price=0.50, usdc=1_000.0, market="MW"),
            row(ts=2, token="LOSE", price=0.50, usdc=10.0, market="ML")]
    profiles = build_profiles(rows, {"WIN": 1.0, "LOSE": 0.0})
    profile = profiles["0xaaa"]
    assert profile.win_rate == 0.5
    assert profile.realized_usdc > 0


# --- ranking ----------------------------------------------------------------

def _profiles_for(specs, now):
    rows = []
    resolutions = {}
    for wallet, wins, losses in specs:
        index = 0
        for _ in range(wins):
            token = f"{wallet}-w{index}"
            rows.append(row(wallet=wallet, ts=int(now) - 3600, token=token,
                            price=0.50, market=f"M{index}"))
            resolutions[token] = 1.0
            index += 1
        for _ in range(losses):
            token = f"{wallet}-l{index}"
            rows.append(row(wallet=wallet, ts=int(now) - 3600, token=token,
                            price=0.50, market=f"M{index}"))
            resolutions[token] = 0.0
            index += 1
    return build_profiles(rows, resolutions, now=now)


def test_shrinkage_pulls_a_thin_record_toward_the_prior():
    now = time.time()
    # A population, so the prior is what a typical wallet looks like rather
    # than whichever single wallet happens to dominate the weighting.
    peers = [(f"0xp{i}", 10, 10) for i in range(8)]
    intel = rank_wallets(_profiles_for([("0xlucky", 3, 0)] + peers, now),
                         now=now)
    lucky = intel["0xlucky"]
    typical = sum(intel[f"0xp{i}"].score for i in range(8)) / 8

    # Its own record is flawless; the score it is *given* is mostly the prior.
    assert lucky.raw_score > 0.8
    assert abs(lucky.score - typical) < abs(lucky.raw_score - typical)
    assert lucky.confidence < 0.15


def test_a_lucky_wallet_is_not_ranked_at_all():
    now = time.time()
    peers = [(f"0xp{i}", 10, 10) for i in range(8)]
    intel = rank_wallets(
        _profiles_for([("0xlucky", 3, 0), ("0xproven", 40, 20)] + peers, now),
        now=now)
    # Without shrinkage and a sample floor, a 100%-record wallet ranks first
    # permanently — it never trades again to be proven wrong.
    assert intel["0xlucky"].rank == 0
    assert intel["0xproven"].rank == 1


def test_thin_wallets_are_tracked_but_unranked():
    now = time.time()
    profiles = _profiles_for([("0xthin", 2, 0), ("0xdeep", 30, 10)], now)
    intel = rank_wallets(profiles, now=now)
    assert intel["0xthin"].rank == 0            # unranked
    assert intel["0xthin"].sample < MIN_RANKED_SAMPLE
    assert "0xthin" in intel                    # ...but never dropped
    assert intel["0xdeep"].rank == 1


def test_cohort_is_a_label_not_a_filter():
    now = time.time()
    specs = [(f"0x{i:03d}", 20 - i, i) for i in range(8)]
    intel = rank_wallets(_profiles_for(specs, now), cohort_size=3, now=now)
    assert sum(1 for w in intel.values() if w.in_cohort) == 3
    # Everyone observed is still present, with a full profile.
    assert len(intel) == 8
    assert all(w.trades > 0 for w in intel.values())


def test_confidence_reflects_sample():
    now = time.time()
    profiles = _profiles_for([("0xa", 25, 25)], now)
    intel = rank_wallets(profiles, now=now)
    n = profiles["0xa"].sample
    assert abs(intel["0xa"].confidence - n / (n + SHRINKAGE_K)) < 1e-6


def test_pinning_labels_and_floors_but_does_not_rank():
    now = time.time()
    profiles = _profiles_for([("0xbad", 2, 30), ("0xgood", 30, 5)], now)
    intel = rank_wallets(profiles, pinned={"0xbad": "operator-pick"}, now=now)
    assert intel["0xbad"].label == "operator-pick"
    assert intel["0xbad"].pinned is True
    # An operator's interest is not evidence: the ranking is unmoved.
    assert intel["0xgood"].rank < intel["0xbad"].rank
    # But a pinned wallet keeps an influence floor while its record is thin.
    assert intel["0xbad"].weight >= 0.35


def test_losing_trades_are_counted():
    # The whole record has to be scoreable, not just the winning half: a
    # settlement of 0.0 is the most informative outcome a market produces.
    now = time.time()
    profiles = _profiles_for([("0xa", 5, 15)], now)
    assert profiles["0xa"].sample == 20
    assert profiles["0xa"].win_rate == 0.25
    assert profiles["0xa"].avg_return < 0


def test_staleness_decays_toward_the_prior():
    now = time.time()
    assert staleness_factor(int(now), int(now)) == 1.0
    assert 0.49 < staleness_factor(int(now - 45 * 86400), now) < 0.51
    assert staleness_factor(0, now) == 1.0      # never seen -> no decay applied


def test_going_quiet_moves_a_wallet_toward_the_prior_not_toward_zero():
    now = time.time()
    # A population, not one wallet: with a single wallet the prior IS that
    # wallet, so decay toward the prior is a no-op and proves nothing.
    peers = [(f"0xp{i}", 10, 10) for i in range(6)]

    fresh = rank_wallets(_profiles_for([("0xa", 30, 5)] + peers, now), now=now)

    stale_rows, resolutions = [], {}
    for wallet, wins, losses in [("0xa", 30, 5)] + peers:
        for i in range(wins + losses):
            token = f"{wallet}-{i}"
            age = 120 * 86400 if wallet == "0xa" else 3600
            stale_rows.append(row(wallet=wallet, ts=int(now - age),
                                  token=token, price=0.5, market=f"M{i}"))
            resolutions[token] = 1.0 if i < wins else 0.0
    stale = rank_wallets(build_profiles(stale_rows, resolutions, now=now),
                         now=now)

    assert stale["0xa"].score < fresh["0xa"].score
    # Toward the population's average wallet, not toward worthless.
    assert stale["0xa"].score > 0.3


# --- hedged books: the "plays both sides" pattern ---------------------------

def test_a_hedged_book_is_scored_as_one_position():
    """Buying BOTH outcomes for under $1.00 total cannot lose.

    Scored leg-by-leg it reads as one win and one loss — a ~50% win rate and a
    return near zero — so the wallet looks mediocre when it is in fact running
    a riskless book. On real data 55% of both-sides books were priced like this.
    """
    from pqb.analytics.features import build_profiles
    rows = [
        row(wallet="0xhedge", ts=1, token="YES", price=0.45, usdc=45.0,
            market="M1"),
        row(wallet="0xhedge", ts=2, token="NO", price=0.52, usdc=52.0,
            market="M1"),
    ]
    profiles = build_profiles(rows, {"YES": 1.0, "NO": 0.0})
    profile = profiles["0xhedge"]

    assert profile.hedged_books == 1
    assert profile.hedge_rate == 1.0
    # ONE netted observation, not two legs.
    assert profile.sample == 1
    assert profile.scores[0].side == "BOOK"
    # Paid 0.97 for a guaranteed 1.00 -> a genuine, small, riskless gain.
    assert profile.win_rate == 1.0
    assert 0.02 < profile.avg_return < 0.04


def test_a_hedged_book_priced_above_one_is_a_loss():
    from pqb.analytics.features import build_profiles
    rows = [
        row(wallet="0xbad", ts=1, token="YES", price=0.60, usdc=60.0, market="M1"),
        row(wallet="0xbad", ts=2, token="NO", price=0.55, usdc=55.0, market="M1"),
    ]
    profile = build_profiles(rows, {"YES": 1.0, "NO": 0.0})["0xbad"]
    assert profile.sample == 1
    assert profile.win_rate == 0.0        # paid 1.15 for 1.00


def test_one_sided_trading_is_not_treated_as_hedged():
    from pqb.analytics.features import build_profiles
    rows = [row(wallet="0xdir", ts=i, token=f"T{i}", price=0.4, usdc=50.0,
                market=f"M{i}") for i in range(4)]
    profile = build_profiles(rows, {f"T{i}": 1.0 for i in range(4)})["0xdir"]
    assert profile.hedged_books == 0
    assert profile.hedge_rate == 0.0
    assert profile.sample == 4            # four separate directional calls


def test_a_hedgers_influence_is_discounted():
    """Its BUY is one leg of a pair, not a view on who wins — so copying that
    leg alone takes the risk the wallet deliberately avoided."""
    from pqb.models import WalletIntel
    directional = WalletIntel(wallet="0xa", score=0.8, confidence=0.9,
                              hedge_rate=0.0)
    hedger = WalletIntel(wallet="0xb", score=0.8, confidence=0.9,
                         hedge_rate=0.9)
    assert hedger.weight < directional.weight * 0.3
    assert hedger.weight > 0              # reduced, never zero


def test_a_book_with_no_share_data_is_unscoreable_not_a_total_loss():
    """Netting is done in shares. Missing them must not invent a -100%."""
    from pqb.analytics.features import net_book_score
    rows = [
        {"wallet": "0xa", "ts": 1, "token_id": "YES", "side": "BUY",
         "price": 0.45, "usdc": 45.0, "market_id": "M1", "size": 0.0},
        {"wallet": "0xa", "ts": 2, "token_id": "NO", "side": "BUY",
         "price": 0.52, "usdc": 52.0, "market_id": "M1", "size": 0.0},
    ]
    assert net_book_score(rows, {"YES": 1.0, "NO": 0.0}) is None
