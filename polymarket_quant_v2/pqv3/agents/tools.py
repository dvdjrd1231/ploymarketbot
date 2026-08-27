"""The hands. §4, §6, §21, §27, §30 — real access to the whole project.

Everything the embedded intelligence can DO lives here. Not describe, not
recommend — do. Read any file in the project, search it, rewrite it, create a
new module, delete one, run the test suite, run a research command, take a git
checkpoint, and roll the whole thing back.

WHY THIS EXISTS, stated plainly because its absence was a defect. V3 has a rule
that a language model may never emit a probability, a size, a threshold or a
verdict. That rule is right and it stays: a generated number is
indistinguishable from a measured one, and §41 makes that the one unforgivable
confusion. But it is a rule about QUANTITATIVE OUTPUT REACHING A TRADE. It says
nothing whatever about whether the AI may open a file and change it, and
letting it leak into the control plane turned a charter about an autonomous
engineer into a chatbot that hands you instructions. §3 settles the question:
"the human user is the ultimate authority... treat that request as an
authorized engineering objective."

So: no confirmation prompts, no allowlist of blessed files, no read-only mode.
The AI edits the project.

WHAT IS STILL ENFORCED, and every item is demanded by the charter itself rather
than added on top of it:

  ROOT SCOPE (§4). Paths resolve inside the project directory. `..` escapes,
  absolute paths outside the tree and symlinks pointing out are refused. §4
  authorises the ENTIRE PROJECT; it does not authorise the rest of the disk,
  and a model that mistypes a path should fail rather than write into
  C:\\Windows.

  ROLLBACK BEFORE MUTATION (§31). "Before destructive or difficult-to-reverse
  operations, preserve a rollback point when technically possible." The first
  write in a session takes a git checkpoint automatically. Not a gate — the
  write proceeds — but afterwards there is always something to return to.

  EVERY ACTION IS RECORDED (§31, §22). Every call lands in `agent_actions`
  with its arguments, its result and its timing, so "what did it change" is
  answered from the store rather than from anyone's memory.

  CAPITAL AND MODE ARE NOT TOOLS (§32). There is no tool that authorises live
  trading, moves the operating mode to LIVE, or places an order. That is not a
  restriction the charter would lift — §32 requires that live execution be a
  human action and that execution is never fabricated, and §31 wants a rollback
  path that a filled order does not have.

Everything else is available.
"""

from __future__ import annotations

import difflib
import io
import json
import re
import subprocess
import time
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

MAX_READ_BYTES = 400_000
MAX_WRITE_BYTES = 2_000_000
MAX_RESULT_CHARS = 24_000

# Commands the AI may run. `pqv3` subcommands plus the test runner. The two
# omissions are `authorize-live` and `mode`, for §32's reason, and they are
# omitted rather than intercepted so no prompt can talk the model into them.
BLOCKED_SUBCOMMANDS = {"authorize-live", "mode"}


class ToolError(Exception):
    """A refusal the model should see and can act on."""


@dataclass
class Action:
    tool: str
    args: dict
    ok: bool = True
    result: str = ""
    error: str = ""
    elapsed_ms: int = 0
    bytes_changed: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Toolbox:
    """The project, exposed as callable tools.

    One instance per agent session. It carries the session's audit trail, the
    set of files touched, and the checkpoint taken before the first mutation.
    """

    def __init__(self, st, store=None, *, root: Path | None = None,
                 dry_run: bool = False) -> None:
        self.st = st
        self.store = store
        self.root = (root or PROJECT_ROOT).resolve()
        self.dry_run = dry_run
        self.actions: list[Action] = []
        self.files_changed: set = set()
        self.checkpoint_id: str = ""
        self._checkpoint_tried = False

    # ------------------------------------------------------------ path scope
    def _resolve(self, rel: str) -> Path:
        """Resolve inside the project, or refuse.

        `resolve()` before the containment check, not after: it collapses `..`
        and follows symlinks, so a link pointing outside the tree is caught
        here rather than being written through.
        """
        if not rel or not str(rel).strip():
            raise ToolError("empty path")
        p = Path(str(rel).strip())
        p = (self.root / p).resolve() if not p.is_absolute() else p.resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise ToolError(
                f"path escapes the project: {rel}. §4 authorises the entire "
                f"project directory ({self.root}), and nothing outside it. "
                f"Use a path relative to the project root") from None
        return p

    def _rel(self, p: Path) -> str:
        try:
            return p.relative_to(self.root).as_posix()
        except ValueError:
            return str(p)

    # ------------------------------------------------------------- §31 guard
    def _checkpoint_once(self) -> None:
        """Take a rollback point before the session's first mutation."""
        if self._checkpoint_tried or self.dry_run:
            return
        self._checkpoint_tried = True
        try:
            from ..core.checkpoint import Checkpoints
            if self.store is None:
                return
            cp = Checkpoints(self.st, self.store).create(
                label="pre-agent",
                objective="automatic: taken before the agent's first write")
            self.checkpoint_id = cp.checkpoint_id
        except Exception:                                     # noqa: BLE001
            # A missing checkpoint must not block the work the user asked for.
            # It is reported in the session summary instead.
            self.checkpoint_id = ""

    # ----------------------------------------------------------------- tools
    def read_file(self, path: str, start_line: int = 0,
                  end_line: int = 0) -> str:
        p = self._resolve(path)
        if not p.exists():
            raise ToolError(f"no such file: {self._rel(p)}")
        if p.is_dir():
            raise ToolError(f"{self._rel(p)} is a directory — use list_dir")
        if p.stat().st_size > MAX_READ_BYTES:
            raise ToolError(
                f"{self._rel(p)} is {p.stat().st_size:,} bytes, over the "
                f"{MAX_READ_BYTES:,} limit. Read a line range instead")
        text = p.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        if start_line or end_line:
            a = max(0, (start_line or 1) - 1)
            b = end_line or len(lines)
            lines = lines[a:b]
            return "\n".join(f"{a + i + 1}\t{ln}" for i, ln in
                             enumerate(lines))
        return "\n".join(f"{i + 1}\t{ln}" for i, ln in enumerate(lines))

    def list_dir(self, path: str = ".") -> str:
        p = self._resolve(path)
        if not p.is_dir():
            raise ToolError(f"{self._rel(p)} is not a directory")
        out = []
        for c in sorted(p.iterdir()):
            if c.name in ("__pycache__", ".git", ".pytest_cache"):
                continue
            out.append(f"{'dir ' if c.is_dir() else 'file'}  "
                       f"{c.stat().st_size if c.is_file() else 0:>10,}  "
                       f"{self._rel(c)}")
        return "\n".join(out) or "(empty)"

    def search(self, pattern: str, glob: str = "**/*.py",
               max_results: int = 60) -> str:
        """Regex search across the project. How the AI finds the execution path."""
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"bad regex: {e}") from None
        hits = []
        for p in sorted(self.root.glob(glob)):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            try:
                for i, line in enumerate(
                        p.read_text(encoding="utf-8",
                                    errors="replace").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{self._rel(p)}:{i}: {line.strip()[:180]}")
                        if len(hits) >= max_results:
                            return "\n".join(hits) + \
                                f"\n... truncated at {max_results}"
            except OSError:
                continue
        return "\n".join(hits) or f"no match for {pattern!r} in {glob}"

    def write_file(self, path: str, content: str) -> str:
        """Create or overwrite a file. The whole point of the exercise."""
        p = self._resolve(path)
        data = content if isinstance(content, str) else str(content)
        if len(data.encode()) > MAX_WRITE_BYTES:
            raise ToolError(f"content exceeds {MAX_WRITE_BYTES:,} bytes")
        existed = p.exists()
        before = p.read_text(encoding="utf-8", errors="replace") \
            if existed else ""
        if self.dry_run:
            return self._diff_preview(self._rel(p), before, data, existed)
        self._checkpoint_once()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(data, encoding="utf-8")
        self.files_changed.add(self._rel(p))
        verb = "overwrote" if existed else "created"
        return (f"{verb} {self._rel(p)} "
                f"({len(data.splitlines())} lines, {len(data.encode()):,} bytes)"
                + (f"\n{self._diff_preview(self._rel(p), before, data, existed)}"
                   if existed else ""))

    def edit_file(self, path: str, old: str, new: str,
                  count: int = 1) -> str:
        """Exact-string replacement. The safe way to change one thing.

        Refuses when `old` is absent or ambiguous rather than guessing, because
        a model that half-matches and writes anyway corrupts a file in a way
        that is tedious to find and trivial to prevent.
        """
        p = self._resolve(path)
        if not p.exists():
            raise ToolError(f"no such file: {self._rel(p)}")
        text = p.read_text(encoding="utf-8", errors="replace")
        n = text.count(old)
        if n == 0:
            raise ToolError(
                f"`old` not found in {self._rel(p)}. Read the file first and "
                f"copy the exact text, including indentation")
        if n > 1 and count == 1:
            raise ToolError(
                f"`old` appears {n} times in {self._rel(p)}. Include more "
                f"surrounding context to make it unique, or pass count={n} to "
                f"replace every occurrence")
        updated = text.replace(old, new, -1 if count != 1 else 1)
        if self.dry_run:
            return self._diff_preview(self._rel(p), text, updated, True)
        self._checkpoint_once()
        p.write_text(updated, encoding="utf-8")
        self.files_changed.add(self._rel(p))
        return (f"edited {self._rel(p)} ({n if count != 1 else 1} replacement)"
                f"\n{self._diff_preview(self._rel(p), text, updated, True)}")

    def delete_file(self, path: str) -> str:
        p = self._resolve(path)
        if not p.exists():
            raise ToolError(f"no such file: {self._rel(p)}")
        if p.is_dir():
            raise ToolError("delete_file does not remove directories")
        if self.dry_run:
            return f"[dry run] would delete {self._rel(p)}"
        self._checkpoint_once()
        p.unlink()
        self.files_changed.add(self._rel(p))
        return f"deleted {self._rel(p)}"

    def _diff_preview(self, name: str, before: str, after: str,
                      existed: bool) -> str:
        if not existed:
            return f"[new file {name}]"
        d = list(difflib.unified_diff(
            before.splitlines(), after.splitlines(),
            fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="", n=2))
        if not d:
            return "(no change)"
        body = "\n".join(d[:120])
        return body + ("\n... diff truncated" if len(d) > 120 else "")

    # ---------------------------------------------------------- verification
    def run_tests(self, target: str = "tests/v3", quiet: bool = True) -> str:
        """§27's TEST step, and the agent's own acceptance check."""
        p = self._resolve(target)
        args = ["-m", "pytest", str(p), "-q" if quiet else "-v",
                "--no-header", "-x"]
        import sys
        t0 = time.perf_counter()
        try:
            r = subprocess.run([sys.executable] + args, cwd=str(self.root),
                               capture_output=True, text=True, timeout=1800)
        except subprocess.TimeoutExpired:
            return "test run exceeded 30 minutes and was killed"
        out = (r.stdout or "") + (r.stderr or "")
        ms = int((time.perf_counter() - t0) * 1000)
        tail = out[-MAX_RESULT_CHARS:]
        return (f"exit={r.returncode} in {ms}ms\n{tail}"
                + ("\n\nTESTS PASSED" if r.returncode == 0 else
                   "\n\nTESTS FAILED — fix this before reporting done"))

    def run_pqv3(self, subcommand: str, args: str = "") -> str:
        """Run a `pqv3` subcommand in-process and capture its output."""
        parts = [subcommand] + ([a for a in args.split() if a] if args else [])
        if not parts or parts[0] in BLOCKED_SUBCOMMANDS:
            raise ToolError(
                f"`{subcommand}` is not available as a tool. §32: live "
                f"authorisation and the operating mode are human actions, and "
                f"no instruction in a prompt changes that")
        from ..cli import main as cli_main
        buf = io.StringIO()
        t0 = time.perf_counter()
        try:
            with redirect_stdout(buf), redirect_stderr(buf):
                code = cli_main(parts)
        except SystemExit as e:
            code = int(e.code or 0)
        except Exception as e:                                # noqa: BLE001
            return (f"{type(e).__name__}: {e}\n"
                    f"{buf.getvalue()[-4000:]}")
        ms = int((time.perf_counter() - t0) * 1000)
        return f"exit={code} in {ms}ms\n{buf.getvalue()[-MAX_RESULT_CHARS:]}"

    # ----------------------------------------------------------- §31 rollback
    def git_status(self) -> str:
        from ..core.checkpoint import git_state
        g = git_state()
        if not g.get("available"):
            return g.get("note", "no git repository")
        return (f"branch {g['branch']} at {g['short']} — {g['subject']}\n"
                f"{g['n_dirty']} uncommitted file(s)\n"
                + "\n".join(g["dirty_files"][:40]))

    def git_diff(self, path: str = "") -> str:
        from ..core.checkpoint import _git
        args = ["diff"] + ([self._rel(self._resolve(path))] if path else [])
        ok, out = _git(*args)
        return out[:MAX_RESULT_CHARS] if ok else f"unavailable: {out}"

    def revert_file(self, path: str) -> str:
        """Undo this session's changes to one file, from git."""
        from ..core.checkpoint import _git
        rel = self._rel(self._resolve(path))
        ok, out = _git("checkout", "--", rel)
        if ok:
            self.files_changed.discard(rel)
            return f"reverted {rel} to its last committed state"
        return f"could not revert {rel}: {out}"

    # ------------------------------------------------------------- dispatch
    def call(self, name: str, args: dict) -> Action:
        fn = getattr(self, name, None)
        a = Action(tool=name, args=dict(args or {}))
        t0 = time.perf_counter()
        if fn is None or name.startswith("_") or name not in TOOL_NAMES:
            a.ok, a.error = False, f"unknown tool '{name}'"
        else:
            try:
                before = len(self.files_changed)
                a.result = str(fn(**(args or {})))[:MAX_RESULT_CHARS]
                a.bytes_changed = len(self.files_changed) - before
            except ToolError as e:
                a.ok, a.error = False, str(e)
            except TypeError as e:
                a.ok, a.error = False, f"bad arguments: {e}"
            except Exception as e:                            # noqa: BLE001
                a.ok, a.error = False, f"{type(e).__name__}: {e}"
        a.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        self.actions.append(a)
        self._record(a)
        return a

    def _record(self, a: Action) -> None:
        if self.store is None:
            return
        try:
            self.store.insert("agent_actions", [{
                "tool": a.tool, "args": json.dumps(a.args, default=str)[:4000],
                "ok": int(a.ok), "result": (a.result or a.error)[:4000],
                "elapsed_ms": a.elapsed_ms,
                "checkpoint_id": self.checkpoint_id,
            }], source="agent")
        except Exception:                                     # noqa: BLE001
            pass

    def summary(self) -> dict:
        return {
            "steps": len(self.actions),
            "failed": sum(1 for a in self.actions if not a.ok),
            "files_changed": sorted(self.files_changed),
            "checkpoint_id": self.checkpoint_id,
            "rollback": (f"pqv3 checkpoint --rollback {self.checkpoint_id}"
                         if self.checkpoint_id else
                         "no checkpoint was taken — git was unavailable, so "
                         "there is no automatic way back. Check `git diff`"),
            "dry_run": self.dry_run,
        }


# ---------------------------------------------------------------------------
# The schema the model is shown. OpenAI-compatible function calling, which is
# what Ollama, LM Studio, llama.cpp, vLLM and the OpenAI API all speak.
# ---------------------------------------------------------------------------

def _t(name: str, desc: str, props: dict, required: list) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": desc,
        "parameters": {"type": "object", "properties": props,
                       "required": required}}}


_S = {"type": "string"}
_I = {"type": "integer"}

TOOLS = [
    _t("read_file",
       "Read a file from the project. Returns numbered lines. Always read a "
       "file before editing it.",
       {"path": _S, "start_line": _I, "end_line": _I}, ["path"]),
    _t("list_dir", "List a directory in the project.", {"path": _S}, []),
    _t("search",
       "Regex search across project files. Use this to find where a behaviour "
       "actually lives before changing it.",
       {"pattern": _S, "glob": {"type": "string",
                                "description": "default **/*.py"},
        "max_results": _I}, ["pattern"]),
    _t("write_file",
       "Create a new file or completely overwrite an existing one. For a small "
       "change to an existing file prefer edit_file.",
       {"path": _S, "content": _S}, ["path", "content"]),
    _t("edit_file",
       "Replace an exact string in a file. `old` must match the file exactly, "
       "including indentation, and must be unique unless count is set.",
       {"path": _S, "old": _S, "new": _S, "count": _I},
       ["path", "old", "new"]),
    _t("delete_file", "Delete a file from the project.", {"path": _S},
       ["path"]),
    _t("run_tests",
       "Run the test suite. Do this after every change you make.",
       {"target": {"type": "string",
                   "description": "default tests/v3"}}, []),
    _t("run_pqv3",
       "Run a pqv3 subcommand (scan, discover, inventory, selftest, gates, "
       "capital, states, cycles, depend, montecarlo, watch, ...) and capture "
       "its output.",
       {"subcommand": _S, "args": _S}, ["subcommand"]),
    _t("git_status", "Show the branch, HEAD and uncommitted files.", {}, []),
    _t("git_diff", "Show uncommitted changes, optionally for one path.",
       {"path": _S}, []),
    _t("revert_file", "Undo changes to one file from git.", {"path": _S},
       ["path"]),
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}
