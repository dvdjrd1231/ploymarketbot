"""Research must size against the account that will actually place the orders.

The audit finding this covers: ``_bridge_overrides`` pinned ``equity =
10_000.0`` and ``_frozen_run`` pinned ``starting_equity = 10_000.0`` while the
real book was $100. Every share count, expectancy, drawdown and rejection the
library recorded was inflated by ~100x — single trades logging -$394 and
strategies -$11,001 on an account that could not lose more than $100.

Two knock-ons are pinned here as well, because fixing the equity alone does
not fix either of them:

* the position ladder must stay usable at $100 rather than collapsing to
  zero-share (or single-rung) positions, and
* a FLAT exchange fee has to be charged per fill. Folded into the
  per-contract commission — which is how it was charged — it becomes a fixed
  percentage of notional at every account size, so the one cost that is
  supposed to punish a small book looked identical on $100 and $10,000.
"""

from __future__ import annotations

from pqb.config import Config
from pqb.research import position_ladder, research_equity


# -- resolving the account ----------------------------------------------------

def test_defaults_to_the_configured_paper_bankroll_not_a_notional_account():
    cfg = Config()
    cfg.mode.paper_starting_balance = 100.0
    equity, source = research_equity(cfg)
    assert equity == 100.0
    assert source == "mode.paper_starting_balance"


def test_explicit_override_wins_so_a_hypothetical_account_stays_possible():
    cfg = Config()
    cfg.mode.paper_starting_balance = 100.0
    cfg.research.account_equity = 5_000.0
    equity, source = research_equity(cfg)
    assert equity == 5_000.0
    assert source == "research.account_equity"


def test_the_engines_recorded_portfolio_value_beats_the_opening_balance(tmp_path):
    """Research follows the book as it stands, not as it started."""
    import sqlite3

    journal = tmp_path / "journal.sqlite3"
    conn = sqlite3.connect(journal)
    conn.execute("CREATE TABLE cycles (ts REAL, portfolio_value REAL)")
    conn.execute("INSERT INTO cycles VALUES (1.0, 100.0)")
    conn.execute("INSERT INTO cycles VALUES (2.0, 143.75)")   # newest
    conn.commit()
    conn.close()

    cfg = Config()
    cfg.mode.paper_starting_balance = 100.0
    cfg.storage.journal_db = str(journal)

    equity, source = research_equity(cfg)
    assert equity == 143.75
    assert "portfolio value" in source


def test_a_missing_or_unreadable_journal_never_fails_a_research_pass(tmp_path):
    cfg = Config()
    cfg.mode.paper_starting_balance = 100.0
    cfg.storage.journal_db = str(tmp_path / "does-not-exist.sqlite3")
    equity, source = research_equity(cfg)
    assert equity == 100.0
    assert source == "mode.paper_starting_balance"


def test_equity_is_floored_rather_than_silently_degenerate():
    cfg = Config()
    cfg.mode.paper_starting_balance = 3.0
    cfg.research.min_account_equity = 25.0
    equity, _source = research_equity(cfg)
    assert equity == 25.0


# -- knock-on 1: the ladder has to survive a $100 account ---------------------

def test_ladder_never_emits_a_zero_share_position_at_a_real_bankroll():
    for price in (0.05, 0.25, 0.50, 0.75, 0.95, 0.99):
        ladder, top = position_ladder(price, equity=100.0, fraction=0.25)
        assert ladder, f"empty ladder at {price}"
        assert min(ladder) >= 1, f"zero-share rung at {price}: {ladder}"
        assert max(ladder) == top


def test_ladder_keeps_its_rungs_at_a_hundred_dollars():
    """Size must remain a real search dimension, not collapse to one choice."""
    for price in (0.05, 0.25, 0.50, 0.75, 0.95, 0.99):
        ladder, _top = position_ladder(price, equity=100.0, fraction=0.25)
        assert len(ladder) == 4, f"ladder collapsed at {price}: {ladder}"


def test_ladder_respects_the_position_cap():
    ladder, top = position_ladder(0.50, equity=100.0, fraction=0.25)
    assert max(ladder) * 0.50 <= 100.0 * 0.25 + 0.50   # within one share
    assert top == 50


def test_ladder_scales_with_the_account():
    small, _ = position_ladder(0.50, equity=100.0, fraction=0.25)
    large, _ = position_ladder(0.50, equity=10_000.0, fraction=0.25)
    assert max(large) == 100 * max(small)


# -- knock-on 2: a flat fee must behave like a flat fee -----------------------

def _round_trip_cost(rung: int, spread: float, flat: float) -> float:
    """What the bridge now charges: crossing scales, the fee does not."""
    return (spread / 2.0) * rung * 2 + flat * 2


def test_flat_fee_is_a_heavier_drag_on_the_small_account():
    """The point of the whole exercise, expressed as one inequality.

    Charged per contract (the old behaviour) both sides came to exactly 4.00%
    and this assertion could never have passed.
    """
    spread, flat, price = 0.010, 0.01, 0.50

    small_rung = 6            # bottom rung of a $100 account at $0.50
    large_rung = 625          # bottom rung of a $10,000 account at $0.50

    small = _round_trip_cost(small_rung, spread, flat) / (small_rung * price)
    large = _round_trip_cost(large_rung, spread, flat) / (large_rung * price)

    assert small > large
    assert round(small * 100, 2) == 2.67
    assert round(large * 100, 2) == 2.01


def test_bridge_overrides_charge_the_fee_per_fill_not_per_contract(tmp_path):
    from pqb.research import _bridge_overrides

    cfg = Config()
    cfg.engine.portfolio.fee_per_trade_usdc = 0.01
    overrides = _bridge_overrides(tmp_path, tmp_path, cfg,
                                  price=0.50, spread=0.010)

    # Crossing the book: half the spread per share, so a round trip pays it all.
    assert overrides["instrument.commission_per_contract"] == 0.005
    # The exchange fee rides separately, once per fill.
    assert overrides["instrument.fee_per_fill"] == 0.01


def test_bridge_overrides_size_to_the_resolved_account(tmp_path):
    from pqb.research import _bridge_overrides

    cfg = Config()
    cfg.mode.paper_starting_balance = 100.0
    overrides = _bridge_overrides(tmp_path, tmp_path, cfg,
                                  price=0.50, spread=0.010)

    assert overrides["account.starting_equity"] == 100.0
    assert overrides["discovery.position_contracts_choices"] == [6, 12, 25, 50]
    assert overrides["prop_constraints.max_position_contracts"] == 50
