"""§31 change control and §40's outbound channel.

Both are infrastructure whose failure is silent by nature: a checkpoint nobody
can restore from and a notifier nobody receives look exactly like a working
checkpoint and a quiet week. So the tests here are mostly about refusal — that
a rollback declines when it would destroy uncommitted work, that a webhook
stays off until configured, and that a delivery failure is reported rather than
swallowed.
"""

from __future__ import annotations

import json

import pytest

from pqv3.agents import notify
from pqv3.agents.surface import Discovery
from pqv3.core.checkpoint import Checkpoints, git_state
from pqv3.core.store import Store


@pytest.fixture
def cps(st) -> Checkpoints:
    return Checkpoints(st, Store(st))


# -------------------------------------------------------------- checkpoints
def test_a_checkpoint_joins_code_and_store_state(cps):
    cp = cps.create(label="t", objective="testing", tests="374 passed")
    assert cp.checkpoint_id.startswith("cp-")
    assert cp.objective == "testing"
    assert cp.mode and cp.store["schema_version"]
    # The store half is the half git cannot record.
    assert "strategies" in cp.store and "strategies_live" in cp.store
    assert cps.list(), "a checkpoint that is not persisted is not a checkpoint"


def test_a_dirty_tree_is_recorded_on_the_checkpoint_itself(cps):
    """A SHA taken over uncommitted work does not describe what was running."""
    cp = cps.create(label="t")
    g = git_state()
    if not g.get("available"):
        pytest.skip("not a git repository")
    if g.get("dirty"):
        assert "WARNING" in cp.rollback
        assert "not captured" in cp.rollback.lower() or "NOT captured" in \
            cp.rollback


def test_rollback_refuses_while_work_would_be_destroyed(cps):
    cp = cps.create(label="t")
    plan = cps.rollback_plan(cp.checkpoint_id)
    g = git_state()
    if g.get("available") and g.get("dirty"):
        assert not plan["safe"]
        assert any("uncommitted" in b for b in plan["blockers"])
    assert "does NOT restore the store" in plan["warning"], (
        "restoring code without the store is a new configuration, not a "
        "rollback, and the plan has to say so")


def test_rollback_of_an_unknown_checkpoint_is_an_error_not_a_guess(cps):
    assert "error" in cps.rollback_plan("cp-does-not-exist")


def test_diff_reports_what_moved_in_the_store(cps, st):
    cp = cps.create(label="before")
    cps.store.insert("alerts", [{"kind": "t", "message": "m"}], source="test")
    d = cps.diff(cp.checkpoint_id)
    assert d["store"] == {} or all(
        v["delta"] != 0 for v in d["store"].values())
    assert "counts, not content" in d["note"]


def test_git_absence_is_a_state_not_a_crash(monkeypatch):
    monkeypatch.setattr("pqv3.core.checkpoint._git",
                        lambda *a, **k: (False, "not a git repository"))
    g = git_state()
    assert g["available"] is False
    assert "note" in g


# ------------------------------------------------------------ notifications
def _d(priority: float, headline: str = "x") -> dict:
    return Discovery(key="k", kind="DATA", headline=headline, measured="m",
                     importance=1.0, impact=1.0, urgency=priority).to_dict()


def test_the_webhook_is_off_until_configured(monkeypatch):
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    ch = notify.channels()
    assert ch["webhook"]["enabled"] is False
    assert notify.WEBHOOK_ENV in ch["webhook"]["detail"]
    assert ch["console"]["enabled"] and ch["file"]["enabled"]


def test_the_configured_url_is_never_echoed(monkeypatch):
    """Webhook URLs routinely carry a token in the path."""
    secret = "https://hooks.example.com/T0000/B1111/abcdefghijklmnop"
    monkeypatch.setenv(notify.WEBHOOK_ENV, secret)
    blob = json.dumps(notify.channels())
    assert secret not in blob
    assert "abcdefghijklmnop" not in blob


def test_every_finding_reaches_the_file_channel(st, monkeypatch):
    monkeypatch.delenv(notify.WEBHOOK_ENV, raising=False)
    out = notify.send(st, [_d(0.9, "urgent"), _d(0.2, "minor")])
    assert out["file"] == 2
    lines = (st.work_dir / "notifications.jsonl").read_text(
        encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(x) for x in lines]
    assert {r["headline"] for r in rows} == {"urgent", "minor"}
    assert [r["urgent"] for r in rows] == [True, False]


def test_only_urgent_findings_would_leave_the_machine(st, monkeypatch):
    """A webhook that relays everything is a webhook someone mutes."""
    posted = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "_post",
                        lambda url, payload: (posted.append(payload), (True, "HTTP 200"))[1])
    out = notify.send(st, [_d(0.9, "urgent"), _d(0.2, "minor")])
    assert out["webhook"]["attempted"] == 1
    assert posted[0]["headline"] == "urgent"
    assert all(p["priority"] >= notify.URGENT for p in posted)


def test_a_delivery_failure_is_reported_not_raised(st, monkeypatch):
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "_post",
                        lambda url, payload: (False, "HTTP 500"))
    out = notify.send(st, [_d(0.9)])
    assert out["webhook"]["delivered"] == 0
    assert out["webhook"]["errors"] == ["HTTP 500"]
    assert "never retried in a loop" in out["note"]


def test_payloads_are_scrubbed(st, monkeypatch):
    posted = []
    monkeypatch.setenv(notify.WEBHOOK_ENV, "https://example.invalid/hook")
    monkeypatch.setattr(notify, "_post",
                        lambda url, payload: (posted.append(payload), (True, "ok"))[1])
    key = "0x" + "cd" * 32
    notify.send(st, [_d(0.9, f"leaked {key}")])
    assert key not in json.dumps(posted)


def test_the_surfacer_delivers_without_taking_the_loop_down(st, monkeypatch):
    from pqv3.agents.surface import Surfacer
    store = Store(st)
    store.record_health("news", "ERROR", error="connection refused")

    def boom(*a, **k):
        raise RuntimeError("notifier exploded")
    monkeypatch.setattr("pqv3.agents.notify.send", boom)

    s = Surfacer(st, store)
    assert s.run(), "a broken notifier must not swallow the discovery"
    assert "error" in s.last_delivery
