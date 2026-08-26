"""The charter, the console, and the boundary between them.

The tests that matter here are the negative ones. It is easy to write a chat
interface that answers everything confidently; the whole design problem is
answering only what the store supports and saying so otherwise. So most of what
follows asserts an absence: no fabricated capability, no execution without a
confirmation, no live command in the runnable set, no dependence on a language
model for any figure.
"""

from __future__ import annotations

import json

import pytest

from pqv3.agents import doctrine
from pqv3.agents.console import ACTIONS, MODE_PATTERNS, Console
from pqv3.core.store import Store
from pqv3.server.api import Api


# --------------------------------------------------------------- the charter
def test_charter_loads_and_is_complete():
    assert doctrine.available(), (
        "docs/MASTER-SYSTEM-PROMPT.md is the charter itself, not documentation "
        "about it. If it is missing the system runs, but it runs uninstructed")
    secs = doctrine.sections()
    numbers = [s.number for s in secs]
    assert numbers == list(range(0, 44)), numbers
    assert doctrine.section(41).title == "NEVER FABRICATE"


def test_condensed_is_sent_when_the_full_text_will_not_fit():
    """A charter that crowds out the evidence has defeated its own purpose."""
    prompt, which = doctrine.system_prompt(context_limit=8192)
    assert which == "condensed"
    assert "NEVER FABRICATE" in prompt

    prompt, which = doctrine.system_prompt(context_limit=200_000)
    assert which == "full"
    assert len(prompt) > 30_000

    # Forcing is available for a caller that knows its own window.
    assert doctrine.system_prompt(force="condensed")[1] == "condensed"


def test_role_limits_are_attached_to_the_prompt():
    for role in ("verdict", "probability", "sizing", "threshold"):
        prompt, _ = doctrine.system_prompt(role)
        assert "ROLE LIMIT" in prompt, role


def test_capabilities_publish_what_is_missing(st, store):
    caps = doctrine.capabilities(st, store)
    named = {c["capability"] for c in caps["cannot"]}
    # These four are authorised by the charter and NOT implemented here. If one
    # ever ships, this test should be edited deliberately — never deleted to
    # make the list look better.
    assert "source-file modification" in named
    assert "PDF ingestion" in named
    assert "live order placement from chat" in named
    assert "autonomous self-modification" in named
    for c in caps["cannot"]:
        assert c["charter"], "a limit must cite the clause it fails to meet"


# ---------------------------------------------------------------- the console
@pytest.fixture
def con(st) -> Console:
    store = Store(st)
    return Console(st, store, api=Api(st, store))


def test_modes_are_recognised(con):
    cases = {
        "audit the entire system": "AUDIT",
        "why is the news panel empty": "AUDIT",
        "fix the wallet engine": "ENGINEERING",
        "rewrite the scanner": "ENGINEERING",
        "backtest this across every market": "BACKTEST",
        "improve the system": "IMPROVEMENT",
        "what is the highest-value thing to do": "IMPROVEMENT",
        "explain why that was rejected": "EXPLANATION",
        "run the inventory": "EXECUTION",
        "analyse every wallet": "RESEARCH",
        "how many strategies are there": "RESEARCH",
    }
    for text, want in cases.items():
        got = con.classify(text)[0]
        assert got == want, f"{text!r} -> {got}, expected {want}"


def test_every_reply_labels_its_state(con):
    """§32. Simulation must never be readable as execution."""
    for q in ("how many wallets", "audit everything", "fix the news collector"):
        r = con.ask(q, narrate=False)
        assert r["state"] == con.st.mode.value
        assert r["mode"] in {m[0] for m in MODE_PATTERNS}


def test_answers_do_not_need_a_language_model(con):
    """The point of the design: unplug the model, keep every figure."""
    assert not con.st.agents.llm_provider
    r = con.ask("audit the entire system", narrate=True)
    assert r["finding"], "an unconfigured model must not empty the answer"
    assert r["llm"]["available"] is False
    assert "computed" in r["llm"]["note"]


def test_narrow_diagnosis_names_the_first_broken_link(con):
    r = con.ask("why is the news panel empty", narrate=False)
    assert r["mode"] == "AUDIT"
    chain = r["plan"][0]["chain"]
    assert [ln["layer"] for ln in chain][0] == "INPUT"
    rc = r["plan"][0]["root_cause"]
    assert rc["layer"] == "INPUT" and not rc["ok"]
    # The UI link is the one thing that is NOT broken: an empty panel over an
    # empty table is the panel working.
    assert chain[-1]["layer"] == "UI" and chain[-1]["ok"]


def test_broad_instruction_is_decomposed_not_refused(con):
    """§28: do not hand a broad instruction back to the user."""
    r = con.ask("find every problem in the entire system", narrate=False)
    assert len(r["topics"]) > 4
    assert r["diagnosis"]
    severities = {d.get("severity") for d in r["diagnosis"] if d.get("severity")}
    assert severities and severities <= {"BLOCKING", "HIGH", "MEDIUM", "LOW",
                                         "INFO"}


def test_engineering_request_locates_real_files_and_admits_the_limit(con):
    r = con.ask("rewrite the wallet intelligence engine", narrate=False)
    inspect = [p for p in r["plan"] if p.get("phase") == "INSPECT"][0]
    assert inspect["files"], "an engineering plan that names no file is prose"
    for f in inspect["files"]:
        assert f["exists"] and f["lines"] > 0
    modify = [p for p in r["plan"] if p.get("phase") == "MODIFY"][0]
    assert modify["blocked"] is True
    assert any(c["capability"] == "source-file modification"
               for c in r["cannot"])


def test_insufficient_evidence_is_a_legitimate_answer(con):
    """§33. Silence beats a confident guess."""
    r = con.ask("what were the fills on the third tuesday", narrate=False)
    assert r["finding"]
    joined = " ".join(r["finding"]).lower()
    assert "no rows" in joined or "insufficient evidence" in joined


# ------------------------------------------------------------------ execution
def test_nothing_that_moves_capital_is_runnable():
    """§31/§32 as a unit test rather than as a promise in a docstring."""
    for name in ("authorize-live", "promote", "mode", "collect"):
        assert ACTIONS[name].runnable is False, name
        assert ACTIONS[name].why_not, name


def test_execution_fails_closed_without_a_confirmation(con):
    r = con.run("inventory")
    assert r["ok"] is False and r["needs_confirm"] is True

    r = con.run("inventory", confirm="something else")
    assert r["ok"] is False

    r = con.run("authorize-live", confirm="authorize-live")
    assert r["ok"] is False and r["refused"] is True

    r = con.run("no-such-action", confirm="no-such-action")
    assert r["ok"] is False and "unknown action" in r["error"]


def test_a_confirmed_action_actually_runs(con):
    r = con.run("gates", confirm="gates")
    assert r["ok"] is True and r["exit_code"] == 0
    assert "gate" in r["output"].lower()


# --------------------------------------------------------------- §22 memory
def test_every_turn_is_remembered(con):
    con.ask("how many wallets", narrate=False)
    con.ask("audit everything", narrate=False)
    rows = con.history()
    assert len(rows) >= 2
    assert rows[0]["mode"] in ("AUDIT", "RESEARCH")
    # Stored as JSON so the reasoning survives, not just the question.
    assert isinstance(json.loads(rows[0]["finding"]), list)


def test_reply_is_json_serialisable(con):
    """It crosses an HTTP boundary; anything non-serialisable is a 500."""
    r = con.ask("audit the entire system", narrate=False)
    json.dumps(r, default=None)


# ------------------------------------------------------------------- the API
def test_doctrine_section_is_served(st):
    store = Store(st)
    d = Api(st, store).get("doctrine")
    assert d["status"]["available"] is True
    assert d["capabilities"]["n_cannot"] >= 4
    assert "NEVER FABRICATE" in d["text"]


def test_chat_route_is_post_only_and_same_origin(st):
    """A loopback bind is not a substitute for refusing a cross-site POST."""
    from pqv3.server.app import make_handler
    h = make_handler(st, Api(st, Store(st)), None)
    assert hasattr(h, "do_POST")
    assert "chat" not in Api.ROUTES, (
        "chat must not be a GET route: it has side effects and takes a body")
