"""Cross-wallet graph, sequence analysis, news causality, social, chain, LLM,
online learning.

The recurring assertion: a module that cannot establish something must say so
rather than produce a confident default. Most of these tests check the refusal
path, because the refusal path is the one that protects capital.
"""

from __future__ import annotations

import random

import pytest

from pqv3.core.canon import Availability, EvidenceState
from pqv3.intelligence import sequences as S


# --------------------------------------------------------------- sequences
def test_random_walk_yields_no_reliable_structure():
    """The most important negative result in the system.

    A random walk must not produce a confident finding. Some tests will fire by
    chance — the report says how many to expect — but the summary must not
    claim structure.
    """
    rng = random.Random(17)
    px, p = [0.5], 0.5
    for _ in range(600):
        p = max(0.02, min(0.98, p + rng.gauss(0, 0.01)))
        px.append(p)
    rep = S.analyse(px)
    assert rep.n_significant <= 4, (
        f"{rep.n_significant}/{len(rep.tests)} tests fired on a random walk")
    assert "chance" in rep.note


def test_perfect_alternation_is_detected():
    px = [0.5 + (0.01 if i % 2 else -0.01) for i in range(200)]
    rep = S.analyse(px)
    assert rep.structure_found
    names = {t.name for t in rep.tests if t.passed}
    assert "markov_independence" in names or "runs" in names


def test_every_test_reports_its_critical_value():
    rng = random.Random(2)
    px = [0.5 + rng.gauss(0, 0.02) for _ in range(200)]
    rep = S.analyse(px)
    assert rep.tests
    for t in rep.tests:
        assert t.detail, f"{t.name} reported no interpretation"
        assert isinstance(t.critical, float)


def test_short_series_is_refused_not_guessed():
    rep = S.analyse([0.5, 0.51, 0.49])
    assert not rep.structure_found
    assert "too short" in rep.note


def test_change_points_are_found_when_planted():
    """A level shift must be found even with almost no directional moves."""
    px = [0.30] * 80 + [0.70] * 80
    rep = S.analyse(px)
    assert "dependence battery cannot run" in rep.note
    assert rep.change_points, "a 0.30 -> 0.70 level shift was not detected"
    assert 60 <= rep.change_points[0]["index"] <= 100


def test_hidden_state_fit_is_interpretable():
    px = [0.5]
    for i in range(300):
        px.append(px[-1] + (0.01 if (i // 10) % 2 else -0.01))
    rep = S.analyse(px)
    hs = rep.hidden_states
    assert "interpretation" in hs and hs["interpretation"]
    assert 0.0 <= hs["persistence"] <= 1.0


# ------------------------------------------------------------------- graph
def test_graph_needs_two_wallets(st):
    from pqv3.core.source import HistoricalSource
    from pqv3.intelligence import graph as G
    g = G.build(st, HistoricalSource(st), wallets=["0xa"])
    assert g.nodes == 1 and not g.edges
    assert "fewer than two" in g.note


def test_graph_discounts_a_cluster_to_independent_opinions():
    from pqv3.intelligence.graph import Cluster, WalletGraph
    g = WalletGraph()
    g.clusters = [Cluster(members=["a", "b", "c", "d"], size=4)]
    out = g.independence(["a", "b", "c", "d"])
    assert out["effective_n"] == pytest.approx(2.0)
    assert out["effective_n"] < out["n"], (
        "four wallets in one cluster counted as four opinions")


def test_independent_wallets_are_not_discounted():
    from pqv3.intelligence.graph import WalletGraph
    g = WalletGraph()
    out = g.independence(["a", "b", "c"])
    assert out["effective_n"] == 3


# --------------------------------------------------------------- causality
def _news_state(**kw) -> EvidenceState:
    ev = EvidenceState(as_of=1_700_000_000, market_id="m", token_id="t")
    ev.news.availability = Availability.OK
    ev.news.data = {"items": 3, "relevant": 2, "max_magnitude": 0.6,
                    "weighted_direction": 0.4,
                    "latest": [{"title": "x", "class": "WIRE",
                                "confirmation": "MULTI_SOURCE",
                                "capture_ts": 1_700_000_000}]}
    ev.price.availability = Availability.OK
    ev.price.data = {"last": 0.5, "velocity_1h": 0.0}
    ev.news.data.update(kw)
    return ev


def test_unconfirmed_news_is_never_actionable(store):
    from pqv3.news import causality
    ev = _news_state(latest=[{"title": "rumour", "class": "SOCIAL",
                              "confirmation": "RUMOR",
                              "capture_ts": 1_700_000_000}])
    sig = causality.analyse(ev, store)
    assert sig.classification is causality.NewsClass.RUMOR_UNCONFIRMED
    assert not sig.actionable


def test_direction_is_never_invented_from_sentiment(store):
    """No rule, no agreeing analogues -> direction UNKNOWN, magnitude only."""
    from pqv3.news import causality
    sig = causality.analyse(_news_state(), store)
    assert sig.direction_source == "NONE"
    assert sig.direction == 0.0
    assert not sig.actionable
    assert "sentiment does not determine" in sig.note


def test_an_explicit_rule_supplies_direction(store):
    from pqv3.news import causality
    sig = causality.analyse(_news_state(), store, rules={"m": 0.8})
    assert sig.direction_source == "RULE"
    assert sig.direction == pytest.approx(0.8)


def test_already_priced_is_detected(store):
    """The classification that saves money."""
    from pqv3.news import causality
    ev = _news_state()
    ev.price.data = {"last": 0.5, "velocity_1h": 0.10}
    # The market moved 0.10 over the last hour, but we only became able to read
    # this item 5 minutes ago. So ~92% of the move predates our knowledge: the
    # price already reflects it and entering now pays for stale information.
    ev.news.data["latest"] = [{"title": "x", "class": "WIRE",
                               "confirmation": "OFFICIAL",
                               "capture_ts": ev.as_of - 300}]
    sig = causality.analyse(ev, store, rules={"m": 0.5})
    assert sig.classification is causality.NewsClass.NEWS_ALREADY_PRICED
    assert not sig.actionable


def test_no_news_layer_abstains(store):
    from pqv3.news import causality
    sig = causality.analyse(EvidenceState(as_of=1), store)
    assert sig.classification is causality.NewsClass.NO_MATERIAL_NEWS
    assert not sig.actionable


# ------------------------------------------------------------------ social
def test_repetition_from_one_source_is_not_corroboration(store):
    from pqv3.ingest.social import propagation
    now = 1_700_000_000
    store.insert("news_items", [
        {"uid": f"u{i}", "source_name": "oneguy", "source_class": "SOCIAL",
         "title": "claim", "entities": ["acme"], "confirmation": "RUMOR",
         "ts": now, "capture_ts": now - i * 60} for i in range(6)],
        source="test")
    rep = propagation(store, "acme", as_of=now)
    assert rep.items == 6
    assert rep.independent_sources == 1
    assert rep.confirmation in ("RUMOR", "UNCONFIRMED")
    assert "propagation, not corroboration" in rep.note


def test_independent_sources_are_corroboration(store):
    from pqv3.ingest.social import propagation
    now = 1_700_000_000
    store.insert("news_items", [
        {"uid": "a", "source_name": "one", "source_class": "SOCIAL",
         "title": "claim", "entities": ["acme"], "ts": now, "capture_ts": now},
        {"uid": "b", "source_name": "two", "source_class": "MEDIA",
         "title": "claim", "entities": ["acme"], "ts": now, "capture_ts": now},
    ], source="test")
    rep = propagation(store, "acme", as_of=now)
    assert rep.independent_sources == 2
    assert rep.confirmation == "MULTI_SOURCE"


# ------------------------------------------------------------------- chain
def test_usdc_transfer_decodes_with_correct_decimals():
    from pqv3.ingest.chain_decode import USDC_POLYGON, decode
    ev = decode({
        "address": USDC_POLYGON,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x" + "0" * 24 + "aa" * 20,
            "0x" + "0" * 24 + "bb" * 20],
        "data": "0x" + format(2_500_000, "064x"),   # 2.5 USDC at 6 decimals
        "transactionHash": "0xdead", "logIndex": "0x1",
        "blockNumber": "0x10"})
    assert ev.decoded and ev.kind == "TRANSFER"
    assert ev.asset == "USDC"
    assert ev.amount == pytest.approx(2.5)
    assert ev.wallet.startswith("0xbb")


def test_addresses_are_lowercased():
    """Mixed-case vs lower-case comparisons silently never match."""
    from pqv3.ingest.chain_decode import _addr
    assert _addr("0x" + "0" * 24 + "AB" * 20) == "0x" + "ab" * 20


def test_unknown_events_are_reported_not_dropped():
    from pqv3.ingest.chain_decode import decode_many
    rows, stats = decode_many([{"topics": ["0xdeadbeef"], "data": "0x"}], 1)
    assert rows == []
    assert stats["unrecognised"] == 1
    assert stats["coverage"] == 0.0
    assert "not events that did not happen" in stats["note"]


# --------------------------------------------------------------------- llm
def test_llm_absent_does_not_break_anything(st):
    from pqv3.agents.llm import LocalLLM
    llm = LocalLLM(st)
    assert not llm.configured
    r = llm.ask("anything")
    assert not r.available and not r.error
    assert "no local model" in llm.status()["note"]


def test_llm_cannot_supply_numbers_in_load_bearing_roles(st, monkeypatch):
    from pqv3.agents import llm as L
    st.agents.llm_provider = "x"
    st.agents.llm_endpoint = "http://localhost:1/v1"
    st.agents.llm_model = "m"
    obj = L.LocalLLM(st)
    monkeypatch.setattr(
        L.LocalLLM, "ask",
        lambda self, prompt, role="narrative", system="":
        L.LLMResult(available=True, role=role,
                    text=L._NUM.sub("[number withheld]", "probability is 0.87")
                    if role in L.NUMERIC_FORBIDDEN_ROLES
                    else "probability is 0.87"))
    assert "0.87" not in obj.ask("q", role="probability").text
    assert "0.87" in obj.ask("q", role="narrative").text


# ---------------------------------------------------------------- online
def test_online_weights_start_neutral(st, store):
    from pqv3.learning.online import CHANNELS, OnlineLearner
    w = OnlineLearner(st, store).weights()
    assert set(w.values) == set(CHANNELS)
    assert all(v == 1.0 for v in w.values.values())


def test_online_ignores_open_positions(st, store):
    from pqv3.learning.online import OnlineLearner
    store.insert("positions", [
        {"position_id": "p1", "mode": "PAPER", "market_id": "m",
         "opened_ts": 1, "size_usdc": 5.0, "status": "OPEN"}], source="test")
    rep = OnlineLearner(st, store).update(mode="PAPER")
    assert rep.applied == 0, "an unresolved position was learned from"


def test_online_uses_each_observation_once(st, store):
    from pqv3.learning.online import OnlineLearner
    store.insert("decisions", [
        {"decision_id": "d1", "run_id": "r1", "market_id": "m",
         "action": "TRADE", "ts": 1}], source="test")
    store.insert("agent_outputs", [
        {"run_id": "r1", "agent": "WALLET_FORENSICS", "subject": "s",
         "stance": "FOR", "confidence": 0.9}], source="test")
    store.insert("positions", [
        {"position_id": "p1", "mode": "PAPER", "market_id": "m",
         "opened_ts": 1, "closed_ts": 2, "size_usdc": 5.0,
         "realized_pnl": 2.0, "resolution": 1.0, "status": "CLOSED"}],
        source="test")
    ol = OnlineLearner(st, store)
    first = ol.update(mode="PAPER")
    assert first.applied == 1
    assert first.after["wallet"] > first.before["wallet"]
    second = ol.update(mode="PAPER")
    assert second.applied == 0
    assert second.skipped_already_used == 1


def test_online_steps_are_bounded(st, store):
    from pqv3.learning.online import OnlineLearner
    ol = OnlineLearner(st, store, max_step=0.02)
    w = ol.weights()
    assert ol.lo < w.get("wallet") < ol.hi
    rep = ol.report(mode="PAPER")
    assert rep["bounds"]["max_step"] == 0.02
    assert "Strategies are NOT learned online" in rep["note"]
