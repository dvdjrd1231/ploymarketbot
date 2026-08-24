"""The learning loop: journal outcomes tilting future decisions."""

from __future__ import annotations

import time

from pqb.analytics import feedback
from pqb.analytics.feedback import MIN_BOOK_SAMPLE, MIN_GROUP_SAMPLE


def close(journal, *, style="take_profit", category="Politics",
          liquidity="normal", ttr="6-24h", pnl=1.0, ret=0.10,
          mode="test", n=1):
    """Insert n closed lifecycles with these tags."""
    for _ in range(n):
        journal.execute(
            """INSERT INTO lifecycles(token_id, status, entry_ts, entry_cost,
                 exit_ts, exit_style, realized_pnl, return_pct, category,
                 liquidity_bucket, ttr_bucket, mode)
               VALUES('T', 'CLOSED', ?, 10.0, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time() - 3600, time.time(), style, pnl, ret, category,
             liquidity, ttr, mode))


def override_decision(journal, lifecycle_id: int):
    journal.execute(
        """INSERT INTO decisions(ts, action, exit_style, lifecycle_id, mode)
           VALUES(?, 'HOLD', 'wallet_override', ?, 'test')""",
        (time.time(), lifecycle_id))


# --- the guards -------------------------------------------------------------

def test_no_history_means_no_tilt(journal):
    memory = feedback.build(journal)
    assert memory.active is False
    adjustment, evidence = memory.tilt(category="Politics")
    assert adjustment == 0.0
    assert evidence["applied"] is False


def test_a_thin_book_still_does_not_tilt(journal):
    close(journal, n=MIN_BOOK_SAMPLE - 1)
    assert feedback.build(journal).active is False


def test_a_group_below_the_sample_floor_is_ignored(journal):
    close(journal, category="Politics", n=30)
    close(journal, category="Rare", ret=5.0, n=MIN_GROUP_SAMPLE - 1)
    memory = feedback.build(journal)
    assert memory.active is True
    assert memory.stat("category", "Rare") is None
    # A wildly profitable two-trade category must not move anything.
    adjustment, evidence = memory.tilt(category="Rare")
    assert adjustment == 0.0
    assert evidence["applied"] is False


def test_open_positions_never_vote(journal):
    close(journal, n=30)
    journal.execute(
        """INSERT INTO lifecycles(token_id, status, entry_cost, return_pct,
             category, mode) VALUES('T','OPEN',10.0,99.0,'Politics','test')""")
    assert feedback.build(journal).book_sample == 30


def test_another_modes_results_are_not_read(journal):
    close(journal, n=30, mode="test")
    close(journal, n=30, mode="live", ret=9.0)
    memory = feedback.build(journal)
    # journal.mode is "test" here: a paper session must not tilt live decisions
    # and a live book must not be diluted by simulated ones.
    assert memory.book_sample == 30
    assert memory.book_mean_return < 1.0


# --- the tilt ---------------------------------------------------------------

def test_a_better_than_average_group_tilts_up(journal):
    close(journal, category="Politics", ret=0.30, n=20)
    close(journal, category="Sports", ret=-0.10, n=20)
    memory = feedback.build(journal)

    good, _ = memory.tilt(category="Politics")
    bad, _ = memory.tilt(category="Sports")
    assert good > 0 > bad


def test_a_group_is_judged_against_the_book_not_against_zero(journal):
    # Everything makes money; one category makes less than the rest.
    close(journal, category="Great", ret=0.50, n=25)
    close(journal, category="Meh", ret=0.05, n=25)
    memory = feedback.build(journal)
    adjustment, _ = memory.tilt(category="Meh")
    # Positive in absolute terms, but an underperformer here — scoring it up
    # would reward the worst of what we do for merely being profitable.
    assert adjustment < 0


def test_the_tilt_is_bounded(journal):
    close(journal, category="Insane", ret=50.0, n=40)
    close(journal, category="Bad", ret=-0.9, n=40)
    memory = feedback.build(journal)
    up, _ = memory.tilt(category="Insane")
    down, _ = memory.tilt(category="Bad")
    assert up <= memory.max_tilt
    assert down >= -memory.max_tilt


def test_matching_on_many_tags_does_not_multiply_the_tilt(journal):
    close(journal, category="Politics", liquidity="deep", ttr="6-24h",
          style="take_profit", ret=0.40, n=30)
    close(journal, category="Other", liquidity="thin", ttr="1-7d",
          style="stop", ret=-0.20, n=30)
    memory = feedback.build(journal)

    one, _ = memory.tilt(category="Politics")
    four, _ = memory.tilt(category="Politics", liquidity_bucket="deep",
                          ttr_bucket="6-24h", exit_style="take_profit")
    # The tags are correlated: four views of the same evidence is not four
    # pieces of it, so dimensions are averaged rather than summed.
    assert four <= one * 1.01
    assert four > 0


def test_shrinkage_applies_to_groups_too(journal):
    close(journal, category="Bulk", ret=0.10, n=60)
    close(journal, category="Thin", ret=1.50, n=MIN_GROUP_SAMPLE)
    memory = feedback.build(journal)
    thin = memory.stat("category", "Thin")
    assert thin is not None
    # Its raw mean is 1.50; what it is credited with is far closer to the book.
    assert thin.mean_return == 1.5
    assert thin.shrunk_return < 0.7


# --- follow vs override -----------------------------------------------------

def test_override_arm_is_found_through_the_decision_not_the_exit_style(journal):
    """An override is a HOLD, so it never becomes a lifecycle's exit style.

    Reading only exit_style would compare followed exits against a permanently
    empty arm and conclude following is the only thing ever tried.
    """
    close(journal, style="wallet", ret=-0.05, n=10)
    # Positions we overrode: they closed for some other reason entirely.
    close(journal, style="take_profit", ret=0.40, n=10)
    rows = journal.query(
        "SELECT id FROM lifecycles WHERE exit_style='take_profit'")
    for row in rows:
        override_decision(journal, row["id"])

    memory = feedback.build(journal)
    assert memory.wallet_followed is not None
    assert memory.wallet_overridden is not None
    assert memory.wallet_overridden.n == 10

    bias, evidence = memory.wallet_exit_bias()
    assert evidence["applied"] is True
    assert bias > 0                       # overriding did better


def test_no_bias_until_both_arms_have_a_sample(journal):
    close(journal, style="wallet", ret=0.10, n=30)
    memory = feedback.build(journal)
    bias, evidence = memory.wallet_exit_bias()
    assert bias == 0.0
    assert evidence["applied"] is False
