"""Wallet connection: the safety properties, not the happy path.

The happy path needs a funded key and a network, so it is exercised manually.
What is asserted here is everything that must hold when the operator makes a
mistake -- because the mistakes are what cost money, and two of these were real
bugs found by running the command rather than by reading it.
"""

from __future__ import annotations

import pytest

from pqb import wallet_setup as ws

VALID = "0x" + "a1" * 32                      # 64 hex chars, well-formed
SEED = ("witch collapse practice feed shame open despair creek "
        "road again ice least")


# --------------------------------------------------------------- redaction
def test_redact_never_echoes_a_seed_phrase():
    """Regression: an early version printed `witch ...east` to the terminal.

    Fingerprinting unvalidated input leaked the first and last words of a
    recovery phrase onto a screen that may be shared, scrolled back, or logged.
    """
    out = ws.redact(SEED)
    assert "witch" not in out
    assert "least" not in out
    assert "east" not in out
    assert "withheld" in out


def test_redact_shows_only_the_ends_of_a_valid_key():
    out = ws.redact(VALID)
    assert out == "0xa1a1...a1a1"
    assert VALID not in out
    # The fingerprint must be far shorter than the key it describes.
    assert len(out) < 20


@pytest.mark.parametrize("bad", ["", "0xdeadbeef", "not-a-key", "0x" + "zz" * 32])
def test_redact_withholds_anything_malformed(bad):
    assert "withheld" in ws.redact(bad)


# --------------------------------------------------------------- validation
def test_seed_phrase_is_rejected_with_a_useful_message():
    with pytest.raises(ws.WalletSetupError) as exc:
        ws.validate_key(SEED)
    msg = str(exc.value).upper()
    assert "SEED" in msg or "RECOVERY" in msg


def test_malformed_key_is_rejected():
    with pytest.raises(ws.WalletSetupError):
        ws.validate_key("0xdeadbeef")


def test_valid_key_normalises_to_0x_prefixed():
    assert ws.validate_key("a1" * 32) == VALID
    assert ws.validate_key(VALID) == VALID


# ------------------------------------------------------------ key sourcing
def test_key_is_never_read_from_argv():
    """argv is world-readable in the process list and lands in shell history.

    Asserted structurally: the CLI parser must expose no option that takes a
    key as a value.
    """
    from pqb.cli import build_parser

    parser = build_parser()
    text = parser.format_help()
    for sub in ("wallet-connect", "wallet-check"):
        assert sub in text
    # No option anywhere may be named --key / --private-key.
    assert "--private-key" not in text
    assert "--key " not in text


def test_missing_key_fails_closed_rather_than_prompting(monkeypatch):
    monkeypatch.delenv("PQB_PRIVATE_KEY", raising=False)
    with pytest.raises(ws.WalletSetupError) as exc:
        ws.read_private_key(allow_prompt=False)
    assert "PQB_PRIVATE_KEY" in str(exc.value)


def test_key_file_is_read_and_stripped(tmp_path, monkeypatch):
    monkeypatch.delenv("PQB_PRIVATE_KEY", raising=False)
    f = tmp_path / "k.txt"
    f.write_text(f"  {VALID}\n", encoding="utf-8")
    assert ws.read_private_key(key_file=str(f)) == VALID


def test_empty_key_file_is_an_error(tmp_path):
    f = tmp_path / "empty.txt"
    f.write_text("   \n", encoding="utf-8")
    with pytest.raises(ws.WalletSetupError):
        ws.read_private_key(key_file=str(f))


def test_env_var_is_used_when_no_file_given(monkeypatch):
    monkeypatch.setenv("PQB_PRIVATE_KEY", VALID)
    assert ws.read_private_key() == VALID


# ------------------------------------------------------------- persistence
CONFIG = """\
polymarket:
  signature_type: 0
  funder_address: "${env:PQB_FUNDER_ADDRESS:}"
  private_key: "${env:PQB_PRIVATE_KEY:}"
mode:
  dry_run: true
"""


def _report(sig=2, funder="0x" + "b" * 40):
    return ws.WalletReport(
        signer_address="0x" + "c" * 40, signature_type=sig,
        funder_address=funder, balance=12.5, reachable=True)


def test_apply_writes_only_the_non_secret_fields(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")

    changed = ws.apply_to_config(p, _report())
    text = p.read_text(encoding="utf-8")

    assert "signature_type: 2" in text
    assert '"0x' + "b" * 40 + '"' in text
    assert len(changed) == 2


def test_apply_never_writes_a_private_key(tmp_path):
    """The config must stay safe to copy, share or commit."""
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    ws.apply_to_config(p, _report())
    text = p.read_text(encoding="utf-8")

    assert VALID not in text
    assert "a1a1a1" not in text
    # The env indirection for the secret must survive untouched.
    assert 'private_key: "${env:PQB_PRIVATE_KEY:}"' in text


def test_apply_does_not_enable_live_trading(tmp_path):
    """Connecting a wallet is not the same act as arming it."""
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    ws.apply_to_config(p, _report())
    assert "dry_run: true" in p.read_text(encoding="utf-8")


def test_apply_refuses_an_unidentified_account(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(CONFIG, encoding="utf-8")
    unknown = ws.WalletReport(signer_address="0x" + "c" * 40)  # signature_type -1
    with pytest.raises(ws.WalletSetupError):
        ws.apply_to_config(p, unknown)
    assert p.read_text(encoding="utf-8") == CONFIG


def test_report_dict_carries_no_secret():
    d = _report().as_dict()
    assert "privateKey" not in d
    assert VALID not in repr(d)
