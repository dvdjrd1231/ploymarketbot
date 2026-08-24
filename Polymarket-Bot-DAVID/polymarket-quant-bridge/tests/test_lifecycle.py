"""Position lifecycle, market segmentation, and spike-capture exits.

David's sharper framing: don't ask what a wallet buys, reconstruct how it
manages the position — the adds, and the flip into the opposite outcome — and
refuse the "100% lossless" reading that ignores capital still at risk.
"""

from __future__ import annotations

from pqb.analytics import lifecycle as lc
from pqb.analytics import playbook as pb
from pqb.analytics import segments as sg


def buy(market, token, ts, price, size, q="Will X win?"):
    return {"ts": ts, "market_id": market, "token_id": token, "side": "BUY",
            "price": price, "size": size, "usdc": price * size, "question": q}


def sell(market, token, ts, price, size, q="Will X win?"):
    return {"ts": ts, "market_id": market, "token_id": token, "side": "SELL",
            "price": price, "size": size, "usdc": price * size, "question": q}


# -- segmentation ------------------------------------------------------------

def test_classifier_places_the_obvious_markets():
    assert sg.classify("Will Bitcoin hit $100k?") == "crypto"
    assert sg.classify("Will the Lakers beat the Celtics?") == "sports"
    assert sg.classify("Will Trump win the presidential election?") == "politics"
    assert sg.classify("Will the Fed cut interest rates?") == "economics"
    assert sg.classify("Will the film win Best Picture at the Oscars?") == "entertainment"


def test_unmatched_is_other_never_forced():
    assert sg.classify("An unusual question with no keywords") == "other"
    assert sg.classify("") == "other"


def test_split_groups_by_category():
    positions = [{"question": "Bitcoin to $100k?"},
                 {"question": "Will the Lakers make the NBA finals?"},
                 {"question": "Bitcoin dominance?"}]
    groups = sg.split(positions)
    assert len(groups["crypto"]) == 2
    assert len(groups["sports"]) == 1


# -- lifecycle ---------------------------------------------------------------

def test_entry_adds_and_averaging_down():
    rows = [buy("m", "A", 100, 0.65, 10),
            buy("m", "A", 200, 0.60, 10),   # below running avg -> averaged down
            buy("m", "A", 300, 0.55, 10)]
    life = lc.reconstruct(rows, {})[0]
    assert life.entry_token == "A"
    assert life.entry_price == 0.65
    assert life.adds == 2
    assert life.averaged_down == 2
    assert life.averaged_up == 0


def test_the_flip_into_the_opposite_outcome_is_detected():
    """Enters A, then buys B (the other side) — the flip, and its direction."""
    rows = [buy("m", "A", 100, 0.65, 10),
            buy("m", "B", 300, 0.42, 5)]   # implied A now ~0.58 < 0.65 entry
    life = lc.reconstruct(rows, {})[0]
    assert life.flipped
    assert life.both_sides
    assert life.flip_opp_price == 0.42
    assert round(life.flip_self_implied, 2) == 0.58
    # Entry outcome fell before the flip -> defending a loser, not locking a win.
    assert life.flip_defended is True


def test_cash_flow_exposes_capital_still_at_risk():
    """An unresolved position is money at risk that a win-rate stat hides."""
    rows = [buy("m", "A", 100, 0.60, 100)]     # $60 in, never resolved
    prof = lc.profile("0xw", lc.reconstruct(rows, {}))
    assert prof.cash_in == 60.0
    assert prof.still_at_risk == 60.0
    assert prof.resolved == 0
    assert any("NOT lossless" in n for n in prof.notes)


def test_resolved_winner_is_not_counted_as_at_risk():
    rows = [buy("m", "A", 100, 0.60, 100)]
    prof = lc.profile("0xw", lc.reconstruct(rows, {"A": 1.0}))
    assert prof.still_at_risk == 0.0
    assert round(prof.realized, 2) == 40.0     # 100 shares * $1 - $60 cost


def test_sixty_to_seventy_nine_band_is_measured():
    rows = [buy("m1", "A", 100, 0.70, 100), buy("m2", "B", 100, 0.30, 100)]
    prof = lc.profile("0xw", lc.reconstruct(rows, {"A": 1.0, "B": 1.0}))
    assert prof.band["n"] == 1          # only the 0.70 entry is in-band
    assert prof.band["resolved"] == 1


# -- spike-capture exits -----------------------------------------------------

def test_trailing_stop_captures_a_spike_instead_of_round_tripping():
    """Rides 0.60 -> 0.90 and exits at 0.78 when it gives back 10% of the peak."""
    path = [(0, 0.60), (60, 0.72), (120, 0.90), (180, 0.78), (240, 0.62)]
    _price, ret, early = pb.simulate(0.60, path, 1.0, pb.ExitRule("t", trail=0.10))
    assert early
    assert round(ret, 2) == 0.30


def test_trailing_stop_does_not_arm_below_entry():
    """A position only falling never triggers the trail as a second stop-loss."""
    path = [(0, 0.60), (60, 0.50), (120, 0.40)]
    price, ret, early = pb.simulate(0.60, path, 0.0, pb.ExitRule("t", trail=0.10))
    assert not early           # rode to settlement; trail never armed in profit
    assert ret < 0


def test_time_exit_leaves_after_the_window():
    path = [(0, 0.60), (7200, 0.66)]   # 2h later
    _price, _ret, early = pb.simulate(
        0.60, path, 1.0, pb.ExitRule("h", max_hold_hours=1.0))
    assert early
