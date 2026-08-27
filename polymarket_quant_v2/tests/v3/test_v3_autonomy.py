"""§2 / §6 / §27 — the agent that changes the project.

The hard part of testing this is that it needs a tool-calling model, and a real
one is neither available nor deterministic in CI. So these tests stand up a
LOCAL HTTP SERVER that speaks the OpenAI-compatible tool-calling protocol and
replays a scripted sequence of tool calls. That exercises the genuine article
end to end — real `urllib` over a real socket, real JSON on the wire, real
`tool_calls` parsing, real dispatch, real files written to disk, real test run
— with only the model's *choice* of next call scripted.

What that proves, and what it does not: it proves the loop, the transport, the
tool surface, the path containment, the audit trail and the rollback all work.
It cannot prove that any particular model chooses well. That is the model's job
and it is why every session reports what it changed and how to undo it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pqv3.agents.autonomy import Agent, status
from pqv3.agents.tools import ToolError, Toolbox
from pqv3.core.store import Store


# --------------------------------------------------------------- mock model
class MockModel:
    """An OpenAI-compatible /v1/chat/completions endpoint with a script."""

    def __init__(self, script: list) -> None:
        self.script = list(script)
        self.requests: list = []
        self.httpd = None
        self.port = 0

    def __enter__(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):                                # noqa: N802
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.requests.append(body)
                step = (outer.script.pop(0) if outer.script
                        else {"content": "done"})
                msg = {"role": "assistant",
                       "content": step.get("content", "")}
                if step.get("tool"):
                    msg["tool_calls"] = [{
                        "id": f"call_{len(outer.requests)}",
                        "type": "function",
                        "function": {"name": step["tool"],
                                     "arguments": json.dumps(
                                         step.get("args", {}))}}]
                out = json.dumps({"choices": [{"message": msg}]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"


@pytest.fixture
def project(tmp_path):
    """A miniature project the agent may freely rewrite."""
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "engine.py").write_text(
        "def score(x):\n    return x * 2\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def box(st, project) -> Toolbox:
    return Toolbox(st, Store(st), root=project)


def wired(st, mock: MockModel):
    st.agents.llm_provider = "mock"
    st.agents.llm_endpoint = mock.endpoint
    st.agents.llm_model = "mock-coder"
    return st


# ------------------------------------------------------------- path scoping
def test_the_project_is_reachable_and_nothing_else_is(box, project):
    assert "def score" in box.read_file("pkg/engine.py")
    for escape in ("../outside.txt", "/etc/passwd",
                   "..\\..\\windows\\system32\\drivers\\etc\\hosts"):
        with pytest.raises(ToolError) as e:
            box.read_file(escape)
        assert "escapes the project" in str(e.value) or "no such file" in \
            str(e.value)


def test_a_symlink_out_of_the_tree_is_refused(box, project, tmp_path):
    target = tmp_path.parent / "secret.txt"
    target.write_text("x", encoding="utf-8")
    link = project / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this machine")
    with pytest.raises(ToolError):
        box.read_file("link.txt")


# ------------------------------------------------------------- the tools
def test_write_create_edit_and_delete_all_really_happen(box, project):
    box.write_file("pkg/new_module.py", "VALUE = 41\n")
    assert (project / "pkg" / "new_module.py").read_text() == "VALUE = 41\n"

    box.edit_file("pkg/new_module.py", "41", "42")
    assert (project / "pkg" / "new_module.py").read_text() == "VALUE = 42\n"

    box.delete_file("pkg/new_module.py")
    assert not (project / "pkg" / "new_module.py").exists()
    assert "pkg/new_module.py" in box.files_changed


def test_edit_refuses_to_guess(box):
    with pytest.raises(ToolError) as e:
        box.edit_file("pkg/engine.py", "not in the file", "x")
    assert "not found" in str(e.value)


def test_edit_refuses_an_ambiguous_match(box, project):
    (project / "pkg" / "dup.py").write_text("a = 1\na = 1\n", encoding="utf-8")
    with pytest.raises(ToolError) as e:
        box.edit_file("pkg/dup.py", "a = 1", "a = 2")
    assert "appears 2 times" in str(e.value)
    # Explicit count makes it unambiguous, and then it proceeds.
    box.edit_file("pkg/dup.py", "a = 1", "a = 2", count=2)
    assert (project / "pkg" / "dup.py").read_text() == "a = 2\na = 2\n"


def test_dry_run_writes_nothing_but_shows_the_diff(st, project):
    b = Toolbox(st, Store(st), root=project, dry_run=True)
    out = b.write_file("pkg/engine.py", "def score(x):\n    return x * 3\n")
    assert "-" in out and "+" in out
    assert "x * 2" in (project / "pkg" / "engine.py").read_text(), (
        "a dry run that writes is not a dry run")


def test_search_finds_the_execution_path(box):
    assert "pkg/engine.py" in box.search(r"def score")
    assert "no match" in box.search(r"zzz_not_here")


def test_capital_and_mode_are_not_reachable_as_tools(box):
    """§32 — omitted from the surface, not intercepted inside it."""
    for cmd in ("authorize-live", "mode"):
        a = box.call("run_pqv3", {"subcommand": cmd})
        assert not a.ok and "§32" in a.error
    assert box.call("place_order", {}).ok is False


# -------------------------------------------------------------- audit trail
def test_every_call_is_recorded(st, project):
    store = Store(st)
    b = Toolbox(st, store, root=project)
    b.call("read_file", {"path": "pkg/engine.py"})
    b.call("write_file", {"path": "pkg/x.py", "content": "X = 1\n"})
    rows = store.query("SELECT * FROM agent_actions ORDER BY id")
    assert [r["tool"] for r in rows] == ["read_file", "write_file"]
    assert all(r["ok"] == 1 for r in rows)


def test_a_checkpoint_is_taken_before_the_first_write(st, project):
    """§31 — a rollback point that exists before it is needed."""
    store = Store(st)
    b = Toolbox(st, store, root=project)
    b.call("read_file", {"path": "pkg/engine.py"})
    assert b.checkpoint_id == "", "reading must not take a checkpoint"
    b.call("write_file", {"path": "pkg/y.py", "content": "Y = 1\n"})
    assert store.query("SELECT * FROM checkpoints"), (
        "the first mutation must leave something to go back to")
    assert "rollback" in b.summary()


# --------------------------------------------------------------- the loop
def test_the_agent_actually_changes_the_project(st, project):
    """The whole point: an objective in, an edited file out."""
    script = [
        {"tool": "search", "args": {"pattern": "def score",
                                    "glob": "**/*.py"}},
        {"tool": "read_file", "args": {"path": "pkg/engine.py"}},
        {"tool": "edit_file", "args": {"path": "pkg/engine.py",
                                       "old": "x * 2", "new": "x * 3"}},
        {"content": "Changed the multiplier in pkg/engine.py from 2 to 3."},
    ]
    with MockModel(script) as m:
        wired(st, m)
        agent = Agent(st, Store(st))
        agent_box_root = project
        # Point the toolbox at the miniature project rather than the real one.
        import pqv3.agents.autonomy as A
        real = A.Toolbox
        A.Toolbox = lambda *a, **k: real(*a, **{**k, "root": agent_box_root})
        try:
            s = agent.run("make score triple its input instead of doubling it")
        finally:
            A.Toolbox = real

    assert s.available and s.finished
    assert (project / "pkg" / "engine.py").read_text() == \
        "def score(x):\n    return x * 3\n"
    assert s.files_changed == ["pkg/engine.py"]
    assert "multiplier" in s.answer
    assert [x.tool for x in s.steps if x.kind == "tool"] == \
        ["search", "read_file", "edit_file"]


def test_the_charter_is_the_system_prompt(st, project):
    with MockModel([{"content": "nothing to do"}]) as m:
        wired(st, m)
        Agent(st, Store(st)).run("say hello")
        sent = m.requests[0]
    system = sent["messages"][0]["content"]
    assert sent["messages"][0]["role"] == "system"
    assert "NEVER FABRICATE" in system, "the charter must reach the model"
    assert "AUTONOMOUS ENGINEER" in system, "with the tool-loop rules"
    assert {t["function"]["name"] for t in sent["tools"]} >= {
        "read_file", "write_file", "edit_file", "run_tests"}


def test_a_tool_failure_is_returned_to_the_model_not_raised(st, project):
    """The model must be able to recover from its own mistake."""
    script = [
        {"tool": "read_file", "args": {"path": "does/not/exist.py"}},
        {"tool": "write_file", "args": {"path": "pkg/ok.py",
                                        "content": "OK = 1\n"}},
        {"content": "recovered"},
    ]
    with MockModel(script) as m:
        wired(st, m)
        import pqv3.agents.autonomy as A
        real = A.Toolbox
        A.Toolbox = lambda *a, **k: real(*a, **{**k, "root": project})
        try:
            s = Agent(st, Store(st)).run("create ok.py")
        finally:
            A.Toolbox = real
    assert s.finished
    assert s.steps[0].ok is False and "no such file" in s.steps[0].result
    # The failure was fed back as a tool result, and the model carried on.
    tool_msgs = [msg for req in m.requests for msg in req["messages"]
                 if msg.get("role") == "tool"]
    assert any("ERROR" in msg["content"] for msg in tool_msgs)
    assert (project / "pkg" / "ok.py").exists()


def test_the_step_budget_ends_an_unproductive_loop(st, project):
    script = [{"tool": "list_dir", "args": {"path": "."}}] * 50
    with MockModel(script) as m:
        wired(st, m)
        import pqv3.agents.autonomy as A
        real = A.Toolbox
        A.Toolbox = lambda *a, **k: real(*a, **{**k, "root": project})
        try:
            s = Agent(st, Store(st), max_steps=4).run("loop forever")
        finally:
            A.Toolbox = real
    assert not s.finished
    assert "budget" in s.reason
    assert "without the model reporting completion" in s.note


def test_changes_without_a_test_run_are_flagged(st, project):
    script = [
        {"tool": "write_file", "args": {"path": "pkg/z.py", "content": "Z=1\n"}},
        {"content": "done, did not test"},
    ]
    with MockModel(script) as m:
        wired(st, m)
        import pqv3.agents.autonomy as A
        real = A.Toolbox
        A.Toolbox = lambda *a, **k: real(*a, **{**k, "root": project})
        try:
            s = Agent(st, Store(st)).run("add z")
        finally:
            A.Toolbox = real
    assert s.files_changed == ["pkg/z.py"]
    assert "did not run the test suite" in s.note


def test_a_model_error_is_reported_not_swallowed(st):
    st.agents.llm_provider = "mock"
    st.agents.llm_endpoint = "http://127.0.0.1:1/v1"     # nothing listening
    st.agents.llm_model = "m"
    st.agents.llm_timeout_secs = 2.0
    s = Agent(st, None).run("do something")
    assert s.available and not s.finished
    assert "model error" in s.reason


def test_the_session_is_persisted(st, project):
    store = Store(st)
    with MockModel([{"content": "ok"}]) as m:
        wired(st, m)
        Agent(st, store).run("a stated objective")
    rows = store.query("SELECT * FROM agent_sessions")
    assert rows and rows[0]["objective"] == "a stated objective"


# ------------------------------------------------------------------ status
def test_an_engineering_instruction_in_chat_runs_the_agent(st, project):
    """§2 — the console must not merely tell you how to make the change."""
    from pqv3.agents.console import Console
    script = [
        {"tool": "write_file", "args": {"path": "pkg/added.py",
                                        "content": "ADDED = True\n"}},
        {"content": "Created pkg/added.py."},
    ]
    with MockModel(script) as m:
        wired(st, m)
        import pqv3.agents.autonomy as A
        real = A.Toolbox
        A.Toolbox = lambda *a, **k: real(*a, **{**k, "root": project})
        try:
            r = Console(st, Store(st)).ask("add a module called added.py",
                                           narrate=False)
        finally:
            A.Toolbox = real

    assert r["mode"] == "ENGINEERING"
    assert r["agent"]["available"] and r["agent"]["finished"]
    assert (project / "pkg" / "added.py").exists(), (
        "the console described the change instead of making it")
    assert any("Changed 1 file" in f for f in r["finding"])
    # The reply must survive the HTTP boundary the dashboard reads it over.
    json.dumps(r, default=str)


def test_the_dashboard_renders_the_agent_session(st):
    """A session the browser cannot show is a session the user cannot audit."""
    from pqv3.server.ui import JS
    for field in ("t.agent", "files_changed", "rollback", "steps"):
        assert field in JS, f"the CHAT page drops {field}"


def test_auto_execution_can_be_turned_off(st, project):
    from pqv3.agents.console import Console
    with MockModel([{"content": "x"}]) as m:
        wired(st, m)
        st.agents.agent_auto = False
        r = Console(st, Store(st)).ask("rewrite the scanner", narrate=False)
    assert not r["agent"], "PQV3_AGENT_AUTO=0 must return the plan only"
    assert any(p.get("phase") == "INSPECT" for p in r["plan"])


def test_status_says_exactly_what_is_missing(st):
    st.agents.llm_provider = st.agents.llm_endpoint = st.agents.llm_model = ""
    s = status(st)
    assert s["available"] is False and s["can_modify_source"] is False
    assert "PQV3_LLM_PROVIDER" in s["note"]
    assert "authorize-live" in s["not_available_as_tools"]


def test_status_reports_the_full_tool_surface(st):
    st.agents.llm_provider = "x"
    st.agents.llm_endpoint = "http://y/v1"
    st.agents.llm_model = "z"
    s = status(st)
    assert s["available"] and s["can_modify_source"]
    assert {"read_file", "write_file", "edit_file", "delete_file",
            "run_tests", "search"} <= set(s["tools"])


def test_the_doctrine_page_stops_claiming_it_cannot_edit(st):
    from pqv3.agents import doctrine
    st.agents.llm_provider = "x"
    st.agents.llm_endpoint = "http://y/v1"
    st.agents.llm_model = "z"
    caps = doctrine.capabilities(st, None)
    can = {c["capability"] for c in caps["can"]}
    cannot = {c["capability"] for c in caps["cannot"]}
    assert "source-file modification" in can
    assert "source-file modification" not in cannot
    assert "live order placement from chat" in cannot, "§32 still holds"
