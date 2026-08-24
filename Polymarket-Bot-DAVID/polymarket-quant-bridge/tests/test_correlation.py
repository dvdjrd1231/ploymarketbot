"""Correlated wallets must not be counted as independent opinions.

Five wallets buying the same outcome is five times the evidence only if they
decided separately. Measured on the estimator, three correlated wallets moved
the implied probability from 0.49 to 0.72 - a 23-point move on what may be a
single opinion copied twice.
"""

from __future__ import annotations

from pqb.analytics.correlation import (
    CORRELATED_AT, FOLLOW_WINDOW_SECONDS, MIN_SHARED_TOKENS, build_clusters,
    independent_subset, overlap,
)

NOW = 1_800_000_000


def buy(wallet, token, ts):
    return {"wallet": wallet, "token_id": token, "side": "BUY", "ts": ts,
            "market_id": f"M-{token}", "usdc": 100.0, "price": 0.5, "size": 200.0}


def test_a_copier_is_detected():
    """Follows the leader into the same tokens, minutes later, every time."""
    rows = []
    for i in range(6):
        rows.append(buy("0xleader", f"T{i}", NOW + i * 86400))
        rows.append(buy("0xcopier", f"T{i}", NOW + i * 86400 + 300))
    clusters = build_clusters(rows)
    assert "0xleader" in clusters and "0xcopier" in clusters
    assert clusters["0xleader"] is clusters["0xcopier"]
    assert clusters["0xcopier"].leader == "0xleader"


def test_independent_wallets_are_not_merged():
    """Same tokens, but days apart - two people reaching the same view."""
    rows = []
    for i in range(6):
        rows.append(buy("0xa", f"T{i}", NOW + i * 86400))
        rows.append(buy("0xb", f"T{i}", NOW + i * 86400 + 5 * 86400))
    assert build_clusters(rows) == {}


def test_incidental_overlap_is_not_correlation():
    """Both traded the day's biggest market. That says nothing."""
    rows = [buy("0xa", "BIG", NOW), buy("0xb", "BIG", NOW + 60)]
    for i in range(5):
        rows.append(buy("0xa", f"A{i}", NOW + i * 3600))
        rows.append(buy("0xb", f"B{i}", NOW + i * 3600))
    assert build_clusters(rows) == {}


def test_overlap_is_normalised_by_the_smaller_wallet():
    """A copier mirroring 5 of a prolific wallet's 100 trades is correlated
    FROM THE COPIER'S SIDE - and it is the copier that adds no information."""
    prolific = {f"T{i}": NOW + i * 3600 for i in range(100)}
    copier = {f"T{i}": NOW + i * 3600 + 120 for i in range(5)}
    assert overlap(prolific, copier) == 1.0


def test_consensus_collapses_to_one_voice():
    rows = []
    for i in range(6):
        for n, wallet in enumerate(("0xa", "0xb", "0xc")):
            rows.append(buy(wallet, f"T{i}", NOW + i * 86400 + n * 200))
    clusters = build_clusters(rows)
    kept = independent_subset(["0xa", "0xb", "0xc"], clusters)
    assert len(kept) == 1, "three copiers are one opinion"


def test_an_uncorrelated_wallet_survives_alongside_a_cluster():
    rows = []
    for i in range(6):
        rows.append(buy("0xa", f"T{i}", NOW + i * 86400))
        rows.append(buy("0xb", f"T{i}", NOW + i * 86400 + 200))
        rows.append(buy("0xsolo", f"S{i}", NOW + i * 86400))
    clusters = build_clusters(rows)
    kept = independent_subset(["0xa", "0xb", "0xsolo"], clusters)
    assert len(kept) == 2
    assert "0xsolo" in kept


def test_wallets_with_no_history_pass_through_untouched():
    assert independent_subset(["0xnew"], {}) == ["0xnew"]


def test_the_probability_model_sees_fewer_voices_after_collapsing():
    """End to end: the whole point of the exercise."""
    import time
    from pqb.decision.probability import estimate
    from pqb.models import (AccountState, BridgeContext, MarketFeatures,
                            MarketStatus, OutcomeQuote, WalletIntel)

    quote = OutcomeQuote(token_id="T1", outcome="Yes", bid=0.48, ask=0.50,
                         mid=0.49, spread=0.02, source="stream",
                         updated_ts=time.time())
    market = MarketFeatures(market_id="M1", status=MarketStatus.ACTIVE,
                            end_ts=int(time.time()) + 86400, liquidity=50_000.0)
    market.quotes = {"T1": quote}
    ctx = BridgeContext(cycle_id="c", ts=time.time(),
                        account=AccountState(balance=100.0))

    three = [(WalletIntel(wallet=f"0x{i}", score=0.85, sample=300), 2_000.0)
             for i in range(3)]
    one = three[:1]
    with_three = estimate(quote, market, ctx, three).probability
    with_one = estimate(quote, market, ctx, one).probability
    # Three voices move it further than one — which is correct ONLY if they
    # really are three. Collapsing copiers first is what makes that true.
    assert with_three > with_one
