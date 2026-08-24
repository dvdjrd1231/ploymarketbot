"""The RN1 behavioral model: no future information, ever.

Pinned: episodes read BOTH outcome tokens of a market as one bet;
qualification freezes at the checkpoints (a later opposite-side buy never
retroactively disqualifies Candidate A, but does disqualify Candidate B
through minute 30); eventual switching is a LABEL; a cell must beat
blindly copying the wallet; and a validated wallet-state rule still
cannot vote in the live engine.
"""

from __future__ import annotations

import pytest

from pqb.analytics.wallet_states import (Episode, describe,
                                         episodes_from_market,
                                         frozen_replay, study)

T0 = 1_000_000.0
YES, NO = "tokYES", "tokNO"
PAYOUTS = {YES: 1.0, NO: 0.0}


def _trade(wallet, ts, token, price=0.65, usd=50.0, side="BUY"):
    return {"wallet": wallet, "market_id": "M1", "token_id": token,
            "ts": ts, "price": price, "usdc": usd, "side": side}


# -- episode extraction ------------------------------------------------------

def test_episode_reads_both_tokens_as_one_bet():
    trades = [_trade("0xrn1", T0, YES, 0.65),
              _trade("0xrn1", T0 + 600, NO, 0.30)]
    episodes = episodes_from_market(trades, PAYOUTS)
    assert len(episodes) == 1
    episode = episodes[0]
    assert episode.token == YES
    assert len(episode.same_buys) == 1
    assert len(episode.opposite_buys) == 1
    assert episode.eventually_two_sided()          # label, not feature


def test_sells_do_not_participate():
    trades = [_trade("0xrn1", T0, YES, 0.65),
              _trade("0xrn1", T0 + 60, YES, side="SELL")]
    episode = episodes_from_market(trades, PAYOUTS)[0]
    assert len(episode.same_buys) == 1


# -- checkpoint freezing: the no-future-information rule ---------------------

def _quiet_episode(**kwargs):
    e = Episode(wallet="0xrn1", market="M1", token=YES, first_ts=T0,
                first_price=0.65, payout=1.0)
    e.same_buys = [(T0, 50.0)]
    for key, value in kwargs.items():
        setattr(e, key, value)
    return e


def test_candidate_a_quiet_at_three_minutes():
    assert _quiet_episode().qualifies(0.60, 0.80, 3, 0)


def test_candidate_a_rejects_an_add_inside_the_window():
    e = _quiet_episode(same_buys=[(T0, 50.0), (T0 + 120, 50.0)])
    assert not e.qualifies(0.60, 0.80, 3, 0)


def test_candidate_a_ignores_future_switching():
    """The spec's hardest rule: a later opposite-side buy must NEVER
    retroactively disqualify a signal frozen at minute 3."""
    e = _quiet_episode(opposite_buys=[(T0 + 3600, 100.0)])   # 1 hour later
    assert e.qualifies(0.60, 0.80, 3, 0)


def test_candidate_b_allows_adds_but_not_opposition():
    adds_later = _quiet_episode(same_buys=[(T0, 50.0), (T0 + 600, 50.0)])
    assert adds_later.qualifies(0.60, 0.80, 3, 30)   # adds 3-30m: allowed
    opposed = _quiet_episode(opposite_buys=[(T0 + 900, 50.0)])
    assert not opposed.qualifies(0.60, 0.80, 3, 30)  # opposition at 15m: no
    # Opposition at 40 minutes is OUTSIDE the 30-minute persistence window:
    # frozen features may not see it, so the signal still qualifies.
    opposed_after = _quiet_episode(opposite_buys=[(T0 + 2400, 50.0)])
    assert opposed_after.qualifies(0.60, 0.80, 3, 30)


def test_price_band_is_enforced():
    cheap = _quiet_episode(first_price=0.30)
    assert not cheap.qualifies(0.60, 0.80, 3, 0)


# -- the study: baseline control decides -------------------------------------

class FakeStore:
    def __init__(self, markets):
        # markets: list of (market_id, yes_token, trades, yes_payout)
        self._markets = markets

    def resolutions(self):
        out = {}
        for market_id, yes_token, _trades, payout in self._markets:
            out[yes_token] = 1.0 if payout else 0.0
            out[yes_token + "-no"] = 0.0 if payout else 1.0
        return out

    def query(self, sql, params=()):
        rows = []
        for market_id, yes_token, trades, _p in self._markets:
            for t in trades:
                row = dict(t)
                row["market_id"] = market_id
                rows.append(row)
        return rows


def _market(i, quiet=True, payout=True, price=0.65):
    yes = f"tok{i}"
    trades = [{"wallet": "0xrn1", "token_id": yes, "ts": T0 + i * 10_000,
               "price": price, "usdc": 50.0, "side": "BUY"}]
    if not quiet:                       # an add inside the 3-minute window
        trades.append({"wallet": "0xrn1", "token_id": yes,
                       "ts": T0 + i * 10_000 + 100, "price": price,
                       "usdc": 50.0, "side": "BUY"})
    return (f"M{i}", yes, trades, payout)


def test_state_cell_must_beat_blind_copying():
    # Quiet episodes win 90%; noisy ones win 20% -> the quiet cell beats
    # the blind-copy baseline and is kept.
    markets = [_market(i, quiet=True, payout=(i % 10 < 9))
               for i in range(20)]
    markets += [_market(100 + i, quiet=False, payout=(i % 10 < 2))
                for i in range(20)]
    result = study(FakeStore(markets), ["0xrn1"], cost=0.02,
                   premium=0.03, min_markets=10)
    kept = result["candidates"]
    assert kept, f"nothing kept: {result['funnel']}"
    best = kept[0]
    assert best["wallet"] == "0xrn1"
    assert best["netExpectancy"] > best["baselineNet"]


def test_uniform_behavior_yields_nothing():
    """If quiet and noisy episodes win at the same rate, the state adds
    selection, not information — nothing may be kept."""
    markets = [_market(i, quiet=(i % 2 == 0), payout=(i % 3 == 0))
               for i in range(30)]
    result = study(FakeStore(markets), ["0xrn1"], cost=0.02,
                   premium=0.03, min_markets=10)
    assert result["candidates"] == []


def test_no_wallets_is_reported_not_crashed():
    result = study(FakeStore([]), [], cost=0.02)
    assert result["candidates"] == []
    assert result["funnel"]["rejectReasons"]


# -- frozen replay -----------------------------------------------------------

_RULE = {"type": "wallet_state", "wallet": "0xrn1", "price_lo": 0.60,
         "price_hi": 0.80, "quiet_minutes": 3, "persist_minutes": 0,
         "max_premium": 0.03, "side": "follow"}


def test_frozen_replay_scores_a_qualified_market():
    trades = [_trade("0xrn1", T0, YES, 0.65)]
    stats = frozen_replay(trades, _RULE, PAYOUTS, cost=0.02)
    assert stats["trades"] == 1
    assert stats["pnl"] == pytest.approx(1.0 - 0.68 - 0.02)


def test_frozen_replay_skips_unqualified_markets():
    trades = [_trade("0xother", T0, YES, 0.65)]     # wrong wallet
    assert frozen_replay(trades, _RULE, PAYOUTS, cost=0.02)["trades"] == 0
    noisy = [_trade("0xrn1", T0, YES, 0.65),
             _trade("0xrn1", T0 + 60, YES, 0.66)]   # add inside 3m
    assert frozen_replay(noisy, _RULE, PAYOUTS, cost=0.02)["trades"] == 0


# -- identity and the execution bar ------------------------------------------

def test_signature_and_family():
    from pqb.research import family_of, signature_of

    assert signature_of(_RULE) == "wstate|0xrn1|0.6|0.8|3|0"
    assert family_of(_RULE) == "wallet-behavior"
    repriced = dict(_RULE, max_premium=0.05)
    assert signature_of(repriced) == signature_of(_RULE)   # same family


def test_validated_wallet_states_cannot_vote(tmp_path):
    from pqb.bridge.lean_engine import LeanDecisionEngine
    from pqb.config import Config
    from pqb.research import DiscoveredStrategy

    cfg = Config()
    cfg.root = tmp_path
    engine = LeanDecisionEngine(cfg.engine, config=cfg)
    rule = DiscoveredStrategy(rule=dict(_RULE), signature="wstate|x",
                              describe="WALLET")
    rule.status = "validated"
    engine.strategies = [rule]
    assert engine.trading_strategies == []


def test_describe_reads_naturally():
    text = describe(_RULE)
    assert "60%-80%" in text and "quiet" in text
    persistent = describe(dict(_RULE, persist_minutes=30))
    assert "30m" in persistent
