"""Secrets, live-trading authorisation, and not writing to the V1 install.

Three things that must be true regardless of how the rest of the system
behaves, so they are asserted rather than documented.
"""

from __future__ import annotations

import json
import os
import sqlite3

import pytest

from pqv3 import secrets
from pqv3.config import Mode


PLANTED_KEY = "0x" + "ab" * 32
PLANTED_SEED = ("abandon ability able about above absent absorb abstract "
                "absurd abuse access accident")


# ------------------------------------------------------------------ secrets
def test_redact_catches_a_planted_private_key():
    text = f"connecting with key {PLANTED_KEY} now"
    out = secrets.redact(text)
    assert PLANTED_KEY not in out
    assert secrets.REDACTED in out


def test_redact_catches_a_seed_phrase():
    out = secrets.redact(f"recovery: {PLANTED_SEED}")
    assert "abandon ability able" not in out


def test_scrub_drops_secret_named_keys_entirely():
    """A redacted field still tells an attacker the field exists."""
    payload = {"ok": 1, "private_key": PLANTED_KEY,
               "nested": {"api_secret": "s3cret", "fine": "yes"}}
    out = secrets.scrub(payload)
    assert "private_key" not in out
    assert "api_secret" not in out["nested"]
    assert out["nested"]["fine"] == "yes"
    assert out["ok"] == 1


def test_env_secret_is_redacted_by_value(monkeypatch):
    monkeypatch.setenv(secrets.ENV_PRIVATE_KEY, "supersecretvalue123")
    assert "supersecretvalue123" not in secrets.redact(
        "log line supersecretvalue123 end")


def test_status_never_exposes_length_or_prefix(monkeypatch):
    monkeypatch.setenv(secrets.ENV_PRIVATE_KEY, PLANTED_KEY)
    for s in secrets.status():
        blob = json.dumps(s.__dict__)
        assert PLANTED_KEY not in blob
        assert "ab" * 8 not in blob
        assert set(s.__dict__) == {"name", "present", "source"}


def test_no_module_exposes_a_key_getter():
    """The signing boundary must have no legitimate 'give me the key' call.

    Signing is implemented now, so the invariant is no longer "it refuses
    everything" — it is that the key never travels upward. There is no getter,
    the resolver is private, and `sign` returns a signature rather than the
    material that made it.
    """
    assert not hasattr(secrets, "get_private_key")
    assert not hasattr(secrets.SigningBoundary, "private_key")
    assert not hasattr(secrets.SigningBoundary, "key")
    # The only resolver is private, so every call site is greppable.
    public = [n for n in dir(secrets.SigningBoundary) if not n.startswith("_")]
    assert set(public) == {"available", "sign", "address"}, public


def test_signing_refuses_a_payload_that_is_not_a_digest(monkeypatch):
    """Signing raw bytes would skip the EIP-712 domain binding entirely."""
    monkeypatch.delenv(secrets.ENV_PRIVATE_KEY, raising=False)
    with pytest.raises(ValueError, match="32-byte digest"):
        secrets.SigningBoundary().sign(b"payload")


def test_signing_refuses_when_no_wallet_is_configured(monkeypatch):
    monkeypatch.delenv(secrets.ENV_PRIVATE_KEY, raising=False)
    monkeypatch.setattr(secrets, "_keyring_get", lambda name: None)
    with pytest.raises(NotImplementedError, match="no wallet credential"):
        secrets.SigningBoundary().sign(b"\x00" * 32)


def test_a_signature_does_not_contain_the_key(monkeypatch):
    """The point of the boundary, asserted directly."""
    monkeypatch.setenv(secrets.ENV_PRIVATE_KEY, PLANTED_KEY)
    sig = secrets.SigningBoundary().sign(b"\x11" * 32)
    assert len(sig) == 65
    planted = bytes.fromhex(PLANTED_KEY.replace("0x", ""))
    assert planted not in sig
    assert PLANTED_KEY.replace("0x", "") not in sig.hex()


def test_api_payloads_are_scrubbed(monkeypatch, st, store):
    monkeypatch.setenv(secrets.ENV_PRIVATE_KEY, PLANTED_KEY)
    from pqv3.server.api import Api
    api = Api(st, store, None)
    for section in Api.ROUTES:
        blob = json.dumps(api.get(section), default=str)
        assert PLANTED_KEY not in blob, f"section {section} leaked the key"


def test_rendered_dashboard_contains_no_secret(monkeypatch, st):
    monkeypatch.setenv(secrets.ENV_PRIVATE_KEY, PLANTED_KEY)
    from pqv3.server.ui import page
    html = page(mode="RESEARCH", wallet=secrets.wallet_banner(),
                live_authorized=False, starting_capital=100.0,
                url="http://127.0.0.1:8787/")
    assert PLANTED_KEY not in html
    assert "WALLET CONNECTED" in html or "WALLET NOT CONFIGURED" in html


# ------------------------------------------------------------- live trading
def test_live_is_disabled_by_default(st):
    assert st.mode is Mode.RESEARCH
    assert st.live_authorized is False


def test_live_mode_without_authorization_is_forced_back(tape):
    from pqv3.runtime import Engine
    tape.mode = Mode.LIVE
    tape.live_authorized = False
    eng = Engine(tape)
    eng.start(build_dna=False)
    assert eng.st.mode is Mode.PAPER, "LIVE was entered without authorization"
    alerts = eng.store.query("SELECT * FROM alerts WHERE kind='safety'")
    assert alerts, "forcing out of LIVE was not alerted"


def test_authorization_records_the_state_at_consent(tape):
    from pqv3.runtime import Engine
    eng = Engine(tape)
    eng.start(build_dna=False)
    out = eng.authorize_live(granted=True, actor="tester", note="unit test")
    assert out["live_authorized"] is True
    rows = eng.store.query("SELECT * FROM authorizations")
    assert len(rows) == 1
    snap = json.loads(rows[0]["snapshot"])
    assert "requirements" in snap and snap["requirements"], (
        "authorization was recorded without the evidence it was granted "
        "against")
    assert out["unmet_requirements"], (
        "a fresh install cannot meet the live requirements; the record must "
        "say which were unmet")


def test_declining_authorization_returns_to_paper(tape):
    from pqv3.runtime import Engine
    eng = Engine(tape)
    eng.start(build_dna=False)
    eng.authorize_live(granted=True, actor="t")
    out = eng.authorize_live(granted=False, actor="t")
    assert out["live_authorized"] is False
    assert out["mode"] == "PAPER"


# ------------------------------------------------------ V1/V2 not written to
def test_v1_database_is_opened_read_only(tape):
    from pqv3.core.source import connect_ro
    conn = connect_ro(tape.data_db)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE should_not_exist(x INTEGER)")
    conn.close()


def test_v3_writes_only_under_its_own_work_dir(tape):
    from pqv3.runtime import Engine
    before = os.path.getmtime(tape.data_db)
    eng = Engine(tape)
    eng.start(build_dna=False)
    eng.store.insert("alerts", [{"kind": "t", "message": "m"}], source="test")
    assert os.path.getmtime(tape.data_db) == before, (
        "the V1 database was modified")
    assert eng.store.path.is_relative_to(tape.work_dir)


def test_collectors_do_not_dial_out_unless_enabled(st, store):
    from pqv3.ingest.collectors import MarketCollector
    st.collectors.enabled = False
    run = MarketCollector(st, store).run()
    assert run.status == "DISABLED"
    assert run.rows == 0
