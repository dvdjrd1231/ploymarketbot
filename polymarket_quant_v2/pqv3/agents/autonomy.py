"""The autonomous engineer. §2, §6, §27, §28, §30, §39.

    "Do NOT merely respond with instructions telling the user how to make the
     change. When the necessary tools and permissions exist: inspect the
     system, locate the relevant files, understand dependencies, determine the
     correct modification, MAKE THE MODIFICATION, run appropriate tests, verify
     functionality, report what changed."

This is that loop. The user states an objective in plain English; the model is
handed the charter and the toolbox and works until the objective is met or the
step budget is gone. It reads files, greps for the execution path, edits
source, creates modules, deletes them, runs the test suite, and iterates on its
own failures.

WHAT CHANGED AND WHY IT HAD TO. The first build of this system read V3's rule
that a language model may never emit a probability, a size, a threshold or a
verdict, and generalised it into "the model may not act". Those are different
claims. The first is about a generated number reaching a trading decision,
where it would be indistinguishable from a measurement — it is right, it is
§41, and `LocalLLM.ask` still enforces it for every narrative role. The second
does not follow from it and is not in the charter; §3 says the opposite in as
many words. So the numeric restriction stays exactly where it belongs, on
commentary about measured evidence, and the control plane is unrestricted.

THE STEP BUDGET IS NOT A LIMIT ON CAPABILITY. It stops a model that has got
into a loop from spending a night rewriting the same file, and it is
configurable up to whatever the user wants. Running out of budget is reported
as an unfinished job, never as a finished one.

THE SESSION ALWAYS ENDS WITH A ROLLBACK PATH. §31 requires it: the first write
of a session takes a git checkpoint automatically, and the summary carries the
command that undoes everything. That is not a gate on the work — the work
already happened — it is the thing that makes doing the work safe to authorise.

NO MODEL, NO AGENT. With `PQV3_LLM_*` unset there is nothing to drive the loop,
and this says so plainly instead of pretending. The quantitative engine is
unaffected either way: it never needed a model and still does not.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import doctrine
from .llm import LocalLLM
from .tools import TOOLS, Toolbox

DEFAULT_MAX_STEPS = 40
DEFAULT_MAX_TOKENS = 4096

# Appended to the charter for this role. The charter says what to be; this says
# how to operate a tool loop, which the charter cannot know about.
OPERATING_RULES = """

YOU ARE RUNNING AS AN AUTONOMOUS ENGINEER INSIDE THE PROJECT, WITH TOOLS.

You have real access: read_file, list_dir, search, write_file, edit_file,
delete_file, run_tests, run_pqv3, git_status, git_diff, revert_file. Use them.
Do not describe a change you could make — make it.

How to work:

1. ORIENT FIRST. search and read_file before you edit anything. §26: follow the
   execution path to the layer that is actually broken rather than patching the
   layer where the symptom shows.
2. SMALLEST EFFECTIVE CHANGE (§6). Prefer edit_file over write_file on an
   existing file. Do not reformat code you are not changing.
3. PRESERVE WHAT WORKS (§6). Do not remove behaviour the user did not ask you
   to remove.
4. MATCH THE SURROUNDING CODE. This codebase comments the WHY and the
   non-obvious tradeoff, never the obvious what. Match that.
5. TEST WHAT YOU CHANGED (§27). Call run_tests after your edits. If tests fail,
   read the failure and fix it — a failing suite is not a finished job.
6. REPORT HONESTLY (§41). When you are done, reply with plain text: what you
   changed, file by file; what you verified and how; what you did NOT do and
   why; anything you are unsure about. Never claim a test passed that you did
   not run.

If the objective is ambiguous, make the reasonable interpretation and say which
one you took. Do not stop to ask unless proceeding either way would be unsafe.

When the work is complete, reply with text and NO tool call. That ends the
session.
"""


@dataclass
class Step:
    n: int
    kind: str                       # "tool" | "text"
    tool: str = ""
    args: dict = field(default_factory=dict)
    ok: bool = True
    result: str = ""
    text: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class Session:
    objective: str
    available: bool = False
    finished: bool = False
    reason: str = ""
    answer: str = ""
    steps: list = field(default_factory=list)
    files_changed: list = field(default_factory=list)
    checkpoint_id: str = ""
    rollback: str = ""
    tests: dict = field(default_factory=dict)
    model: str = ""
    charter_in_force: str = ""
    elapsed_ms: int = 0
    note: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Agent:
    """Drives a tool-using model against the project until the job is done."""

    def __init__(self, st, store=None, *, max_steps: int = 0,
                 dry_run: bool = False, on_step=None) -> None:
        self.st = st
        self.store = store
        self.llm = LocalLLM(st)
        self.max_steps = max_steps or int(
            getattr(st.agents, "agent_max_steps", DEFAULT_MAX_STEPS))
        self.dry_run = dry_run
        self.on_step = on_step          # callback for live progress

    # ------------------------------------------------------------------ run
    def run(self, objective: str, *, context: str = "") -> Session:
        t0 = time.perf_counter()
        s = Session(objective=objective, model=self.llm.cfg.llm_model)

        if not self.llm.configured:
            s.reason = "no model configured"
            s.note = (
                "The agent needs a tool-capable model to drive it. Set "
                "PQV3_LLM_PROVIDER, PQV3_LLM_ENDPOINT and PQV3_LLM_MODEL — any "
                "OpenAI-compatible endpoint works, including Ollama or LM "
                "Studio on this machine. Nothing else about the system depends "
                "on it: every figure on every page is computed without a model "
                "and still is.")
            return s

        s.available = True
        charter, which = doctrine.system_prompt(
            "narrative", context_limit=self.llm.cfg.llm_context_limit,
            force="full" if self.llm.cfg.llm_context_limit >= 32_000 else "")
        s.charter_in_force = which

        box = Toolbox(self.st, self.store, dry_run=self.dry_run)
        messages = [
            {"role": "system", "content": charter + OPERATING_RULES},
            {"role": "user", "content": (
                f"{objective}\n\n"
                + (f"Context already gathered for you:\n{context}\n\n"
                   if context else "")
                + f"The project root is the working directory. "
                  f"Operating mode is {self.st.mode.value}.")},
        ]

        for n in range(1, self.max_steps + 1):
            msg = self.llm.chat(messages, tools=TOOLS,
                                max_tokens=DEFAULT_MAX_TOKENS)
            if msg.get("error"):
                s.reason = f"model error: {msg['error']}"
                s.note = msg["error"]
                break

            calls = msg.get("tool_calls") or []
            if not calls:
                # No tool call means the model considers the job done.
                s.finished = True
                s.answer = (msg.get("content") or "").strip()
                s.reason = "the model reported the objective complete"
                s.steps.append(Step(n=n, kind="text", text=s.answer,
                                    elapsed_ms=msg.get("elapsed_ms", 0)))
                self._emit(s.steps[-1])
                break

            # Keep the assistant turn verbatim: the tool_call ids must survive
            # so each result can be matched to the call that asked for it.
            messages.append({k: v for k, v in msg.items()
                             if k in ("role", "content", "tool_calls")}
                            | {"role": "assistant"})

            for call in calls:
                fn = (call.get("function") or {})
                name = fn.get("name", "")
                raw = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                    result = ("arguments were not valid JSON; resend them as a "
                              "JSON object")
                    messages.append({"role": "tool",
                                     "tool_call_id": call.get("id", ""),
                                     "content": result})
                    s.steps.append(Step(n=n, kind="tool", tool=name, ok=False,
                                        result=result))
                    self._emit(s.steps[-1])
                    continue

                a = box.call(name, args)
                step = Step(n=n, kind="tool", tool=name, args=args, ok=a.ok,
                            result=(a.result or a.error),
                            elapsed_ms=a.elapsed_ms)
                s.steps.append(step)
                self._emit(step)
                messages.append({
                    "role": "tool", "tool_call_id": call.get("id", ""),
                    "content": (a.result if a.ok
                                else f"ERROR: {a.error}")[:24_000]})
                if name == "run_tests":
                    s.tests = {"ran": True, "passed": "TESTS PASSED" in a.result,
                               "output_tail": a.result[-1500:]}
        else:
            s.reason = (f"step budget of {self.max_steps} exhausted before the "
                        f"model reported completion")

        summary = box.summary()
        s.files_changed = summary["files_changed"]
        s.checkpoint_id = summary["checkpoint_id"]
        s.rollback = summary["rollback"]
        s.elapsed_ms = int((time.perf_counter() - t0) * 1000)

        if s.files_changed and not s.tests.get("ran"):
            s.note = ("files were changed and the model did not run the test "
                      "suite. Run `python -m pytest tests/v3 -q` before "
                      "trusting this, or ask the agent to verify its own "
                      "work.")
        elif s.tests.get("ran") and not s.tests.get("passed"):
            s.note = ("THE TEST SUITE DID NOT PASS after these changes. The "
                      f"rollback path is: {s.rollback}")
        elif not s.finished and not s.note:
            s.note = (f"the session ended without the model reporting "
                      f"completion ({s.reason}). Anything it changed is listed "
                      f"above and the rollback path still applies.")
        self._persist(s)
        return s

    def _emit(self, step: Step) -> None:
        if self.on_step:
            try:
                self.on_step(step)
            except Exception:                                 # noqa: BLE001
                pass

    def _persist(self, s: Session) -> None:
        if self.store is None:
            return
        try:
            self.store.insert("agent_sessions", [{
                "objective": s.objective[:2000], "finished": int(s.finished),
                "reason": s.reason[:500], "answer": s.answer[:8000],
                "steps": len(s.steps),
                "files_changed": json.dumps(s.files_changed),
                "checkpoint_id": s.checkpoint_id,
                "tests_passed": int(bool(s.tests.get("passed"))),
                "model": s.model, "elapsed_ms": s.elapsed_ms,
            }], source="agent")
        except Exception:                                     # noqa: BLE001
            pass


def status(st) -> dict:
    """Is the autonomous path live, and if not, exactly what is missing?"""
    llm = LocalLLM(st)
    cfg = st.agents
    return {
        "available": llm.configured,
        "model": cfg.llm_model or None,
        "endpoint": cfg.llm_endpoint or None,
        "provider": cfg.llm_provider or None,
        "max_steps": int(getattr(cfg, "agent_max_steps", DEFAULT_MAX_STEPS)),
        "tools": sorted(t["function"]["name"] for t in TOOLS),
        "can_modify_source": llm.configured,
        "note": (
            "the agent can read, search, write, create and delete any file in "
            "the project, run the test suite and run pqv3 commands. It takes a "
            "git checkpoint before its first write and every session reports a "
            "rollback command."
            if llm.configured else
            "NOT CONFIGURED. Set PQV3_LLM_PROVIDER, PQV3_LLM_ENDPOINT and "
            "PQV3_LLM_MODEL to any OpenAI-compatible endpoint — Ollama and LM "
            "Studio both work locally — and the agent becomes available with "
            "no other change. The quantitative engine does not use it either "
            "way."),
        "not_available_as_tools": {
            "authorize-live": "§32 — live execution is a human action",
            "mode": "§32 — the operating-mode ladder is a human action"},
    }
