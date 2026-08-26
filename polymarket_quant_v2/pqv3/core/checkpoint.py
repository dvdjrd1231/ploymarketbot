"""§31 — change control. A rollback point that exists before it is needed.

    "Before destructive or difficult-to-reverse operations, preserve a rollback
     point when technically possible. Every major modification should have:
     timestamp, objective, files changed, reason, expected improvement, test
     result, validation result, rollback path."

The temptation is to build a versioning system. That would be the wrong answer:
this project sits in a git repository, git already stores every one of those
fields better than a bespoke table would, and a second source of truth about
what the code was is worse than none. What git does NOT capture is the other
half of the state — which schema the store was on, how many rows each table
held, which strategies were live, what the operating mode was. Restoring the
code to last Tuesday while the store carries this Friday's strategies is not a
rollback, it is a new and undocumented configuration.

So a checkpoint here is a JOIN: the git commit, plus the store state, plus the
human's stated objective, recorded together at one instant with an exact
rollback command attached.

ROLLBACK IS NOT AUTOMATIC and the asymmetry is deliberate. Creating a
checkpoint is free and cannot lose anything, so it happens on request and
before promotions. Restoring one discards work, so it refuses to run against a
dirty tree, requires an explicit `--yes`, and prints exactly what it is about
to do first. §31 asks for a rollback path, not for a system that takes it by
itself.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent

TRACKED_TABLES = ("strategies", "hypotheses", "research_passes", "decisions",
                  "fills", "positions", "markets", "book_snapshots",
                  "news_items", "chain_events", "discoveries")


def _git(*args, cwd: Path = _REPO) -> tuple[bool, str]:
    """Run a git command. Never raises — git being absent is a state, not a fault."""
    try:
        p = subprocess.run(("git",) + args, cwd=str(cwd), capture_output=True,
                           text=True, timeout=20)
        return p.returncode == 0, (p.stdout or p.stderr).strip()
    except Exception as e:                                    # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"


def git_state() -> dict:
    ok, sha = _git("rev-parse", "HEAD")
    if not ok:
        return {"available": False,
                "note": f"not a git repository, or git is unavailable: {sha}. "
                        f"Checkpoints still record store state; the code half "
                        f"of the rollback path is unavailable"}
    _, branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    _, status = _git("status", "--porcelain")
    dirty = [ln for ln in status.splitlines() if ln.strip()]
    _, subject = _git("log", "-1", "--pretty=%s")
    return {"available": True, "sha": sha, "short": sha[:12], "branch": branch,
            "subject": subject, "dirty": bool(dirty),
            "dirty_files": [ln[3:] for ln in dirty[:40]],
            "n_dirty": len(dirty)}


@dataclass
class Checkpoint:
    checkpoint_id: str = ""
    label: str = ""
    objective: str = ""
    git: dict = field(default_factory=dict)
    store: dict = field(default_factory=dict)
    mode: str = ""
    live_authorized: bool = False
    tests: str = ""
    rollback: str = ""
    ts: int = 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class Checkpoints:
    def __init__(self, st, store) -> None:
        self.st = st
        self.store = store

    def _store_state(self) -> dict:
        out = {"schema_version": self.store.get_meta("schema_version"),
               "path": str(self.store.path)}
        for t in TRACKED_TABLES:
            try:
                out[t] = self.store.count(t)
            except Exception:                                 # noqa: BLE001
                out[t] = None
        try:
            out["strategies_live"] = self.store.count(
                "strategies", "status IN ('APPROVED','LIVE')")
        except Exception:                                     # noqa: BLE001
            out["strategies_live"] = None
        return out

    def create(self, *, label: str = "", objective: str = "",
               tests: str = "") -> Checkpoint:
        g = git_state()
        now = int(time.time())
        cp = Checkpoint(
            checkpoint_id=f"cp-{now}",
            label=label or time.strftime("%Y-%m-%d %H:%M", time.gmtime(now)),
            objective=objective, git=g, store=self._store_state(),
            mode=self.st.mode.value,
            live_authorized=bool(self.st.live_authorized),
            tests=tests, ts=now)
        cp.rollback = (
            f"git checkout {g['sha']}" if g.get("available") else
            "unavailable — no git repository was found at checkpoint time")
        if g.get("available") and g.get("dirty"):
            cp.rollback += (f"   # WARNING: {g['n_dirty']} file(s) were "
                            f"uncommitted when this was taken and are NOT "
                            f"captured by the SHA. Commit or stash before "
                            f"relying on this")
        try:
            self.store.insert("checkpoints", [{
                "checkpoint_id": cp.checkpoint_id, "label": cp.label,
                "objective": cp.objective, "git_sha": g.get("sha", ""),
                "git_branch": g.get("branch", ""),
                "git_dirty": int(bool(g.get("dirty"))),
                "detail": cp.to_dict(), "mode": cp.mode,
                "live_authorized": int(cp.live_authorized),
                "tests": tests, "rollback": cp.rollback,
            }], source="checkpoint")
        except Exception:                                     # noqa: BLE001
            pass
        return cp

    def list(self, limit: int = 25) -> list:
        try:
            return self.store.query(
                "SELECT id, checkpoint_id, label, objective, git_sha, "
                "       git_branch, git_dirty, mode, tests, rollback, ts "
                "  FROM checkpoints ORDER BY id DESC LIMIT ?", (limit,))
        except Exception:                                     # noqa: BLE001
            return []

    def get(self, checkpoint_id: str) -> dict | None:
        rows = self.store.query(
            "SELECT * FROM checkpoints WHERE checkpoint_id=? LIMIT 1",
            (checkpoint_id,))
        return rows[0] if rows else None

    def diff(self, checkpoint_id: str) -> dict:
        """What has changed since that checkpoint — code AND store."""
        row = self.get(checkpoint_id)
        if not row:
            return {"error": f"no checkpoint '{checkpoint_id}'"}
        old = json.loads(row["detail"] or "{}")
        now_store = self._store_state()
        old_store = old.get("store", {})
        table_delta = {}
        for k, v in now_store.items():
            if isinstance(v, int) and isinstance(old_store.get(k), int):
                if v != old_store[k]:
                    table_delta[k] = {"was": old_store[k], "now": v,
                                      "delta": v - old_store[k]}
        g = git_state()
        code = {}
        if g.get("available") and row["git_sha"]:
            ok, out = _git("diff", "--stat", row["git_sha"], "HEAD")
            code["stat"] = out if ok else f"unavailable: {out}"
            ok2, names = _git("diff", "--name-only", row["git_sha"], "HEAD")
            code["files"] = names.splitlines() if ok2 else []
            code["same_commit"] = (g.get("sha") == row["git_sha"])
        return {"checkpoint": dict(row), "code": code, "store": table_delta,
                "git_now": g,
                "note": ("store deltas are counts, not content. A table with "
                         "the same count can still hold different rows — this "
                         "answers 'what moved', not 'what is identical'")}

    def rollback_plan(self, checkpoint_id: str) -> dict:
        """What restoring would do, and whether it is currently safe.

        Returns a plan. Never restores. `cli.cmd_checkpoint` requires an
        explicit `--yes` on top of this, and refuses on a dirty tree, because
        §31's rollback path is for a human to take deliberately.
        """
        d = self.diff(checkpoint_id)
        if "error" in d:
            return d
        g = d["git_now"]
        blockers = []
        if not g.get("available"):
            blockers.append("no git repository — the code cannot be restored")
        if g.get("dirty"):
            blockers.append(
                f"{g['n_dirty']} uncommitted file(s). Restoring would discard "
                f"them with no record. Commit or stash first")
        if d["checkpoint"].get("git_dirty"):
            blockers.append(
                "the tree was already dirty when this checkpoint was taken, "
                "so its SHA does not describe the code that was running")
        return {
            "checkpoint_id": checkpoint_id,
            "command": d["checkpoint"].get("rollback", ""),
            "code_changes": d["code"],
            "store_changes": d["store"],
            "blockers": blockers,
            "safe": not blockers,
            "warning": (
                "restoring the code does NOT restore the store. Rows written "
                "since this checkpoint stay written, and strategies discovered "
                "since it stay discovered. Check `store_changes` above and "
                "decide whether that combination is one you want — §31 wants a "
                "rollback point, not the illusion of a time machine")}
