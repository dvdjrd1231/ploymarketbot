"""Connect a MetaMask (or any EOA) wallet, and prove it before trusting it.

The question this answers is the one the operator actually asks:

    "I have a MetaMask wallet. How do I trade through it?"

The short answer is that a headless bot cannot use the MetaMask *extension* --
there is no browser and nobody to click Approve. It needs the private key
exported from MetaMask, which it then uses to sign directly. Same wallet, same
address, same funds; the extension is simply not in the path.

The long answer is the part that goes wrong, and it is the reason this module
exists rather than a paragraph of documentation:

  * **Funds are often not where the key is.** If the account was funded through
    the Polymarket website, the USDC usually sits in a Polymarket *proxy
    wallet* that the MetaMask key controls -- not at the MetaMask address. The
    signer address shows a zero balance, and the natural conclusion ("the key
    is wrong") is incorrect. `autodetect_account` tries EOA first, then the
    proxy configurations, and keeps whichever one actually reports USDC.

  * **A seed phrase is not a private key.** Pasting the twelve words exposes
    every account in the wallet rather than one. Rejected explicitly, with a
    message that says which is wanted.

  * **Keys leak through the boring channels.** Never accepted as a command-line
    argument: argv is visible in the process list and lands in shell history.
    Read from a file, an environment variable, or an interactive prompt that
    does not echo -- in that order of preference.

Nothing here places an order and nothing here enables live trading. It writes
only the two *non-secret* fields to config (`signature_type`, `funder_address`),
and it leaves `dry_run` exactly as it found it. Turning on live trading stays a
separate, deliberate act.
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# The signature types py-clob-client understands, in the words an operator uses.
ACCOUNT_KINDS = {
    0: "EOA -- MetaMask / Trust Wallet / raw key (the signer holds the USDC)",
    1: "Polymarket email (Magic) wallet -- funds held by a proxy",
    2: "Polymarket browser wallet -- funds held by a proxy",
}


class WalletSetupError(RuntimeError):
    """Anything that should stop setup with a message a person can act on."""


@dataclass
class WalletReport:
    """What was discovered. Deliberately carries no private key."""

    signer_address: str
    signature_type: int = -1
    funder_address: str = ""
    balance: float = 0.0
    account_label: str = ""
    reachable: bool = False
    note: str = ""

    @property
    def configured(self) -> bool:
        return self.signature_type >= 0

    def as_dict(self) -> dict:
        return {
            "signerAddress": self.signer_address,
            "signatureType": self.signature_type,
            "funderAddress": self.funder_address,
            "balance": round(self.balance, 6),
            "accountLabel": self.account_label,
            "reachable": self.reachable,
            "note": self.note,
        }


def redact(key: str) -> str:
    """A fingerprint of a VALIDATED key, safe to print or log.

    Only ever call this after `validate_key` has accepted the input. An early
    version printed the fingerprint first, which meant that pasting a seed
    phrase by mistake echoed `witch ...east` to the terminal -- the first and
    last words of a recovery phrase, to a screen that may be shared or logged.
    Refusing to fingerprint anything that is not a well-formed key removes that
    whole class of accident rather than relying on call order.
    """
    k = (key or "").strip()
    body = k[2:] if k[:2].lower() == "0x" else k
    # Length alone is not enough: a 66-character non-hex string would slip
    # through and be echoed. The check must be the same shape as the one that
    # decides a key is valid at all.
    if len(body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in body):
        return "(not a valid key -- withheld)"
    return f"0x{body[:4]}...{body[-4:]}"


# ---------------------------------------------------------------------------
# Getting the key, without letting it leak
# ---------------------------------------------------------------------------

def read_private_key(key_file: Optional[str] = None,
                     env_var: str = "PQB_PRIVATE_KEY",
                     allow_prompt: bool = True) -> str:
    """Obtain the key from a file, the environment, or an unechoed prompt.

    Deliberately NOT from argv. `ps` shows another user's command line on most
    systems, and every shell writes it to history.
    """
    if key_file:
        path = Path(key_file).expanduser()
        if not path.is_file():
            raise WalletSetupError(f"Key file not found: {path}")
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            raise WalletSetupError(f"Key file is empty: {path}")
        return raw

    env = (os.environ.get(env_var) or "").strip()
    if env:
        return env

    if not allow_prompt:
        raise WalletSetupError(
            f"No key supplied. Set {env_var}, or pass --key-file, or run "
            "interactively so it can be entered at a prompt."
        )

    import getpass
    try:
        entered = getpass.getpass(
            "Paste your MetaMask PRIVATE KEY (64 hex chars, input hidden): "
        ).strip()
    except (EOFError, KeyboardInterrupt):
        raise WalletSetupError("Cancelled -- no key entered.") from None
    if not entered:
        raise WalletSetupError("Cancelled -- no key entered.")
    return entered


def validate_key(raw: str) -> str:
    """Normalise, with the two common mistakes named explicitly.

    Delegates to the upstream validator so there is one definition of a valid
    key, and re-raises as WalletSetupError so callers handle one exception type.
    """
    from .upstream import PolymarketError, normalize_private_key

    try:
        return normalize_private_key(raw)
    except PolymarketError as exc:
        raise WalletSetupError(str(exc)) from exc


def signer_address(private_key: str) -> str:
    """Derive the 0x address locally. No network, no key transmitted."""
    from .upstream import PolymarketError, derive_signer_address

    try:
        return derive_signer_address(private_key)
    except PolymarketError as exc:
        raise WalletSetupError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def discover(private_key: str, *, host: str, chain_id: int,
             funder_hint: str = "", offline: bool = False) -> WalletReport:
    """Work out how this key is configured, and whether it holds USDC.

    `offline=True` stops after local derivation -- useful for checking a key is
    well-formed without contacting the venue.
    """
    key = validate_key(private_key)
    addr = signer_address(key)
    report = WalletReport(signer_address=addr)

    if offline:
        report.note = "offline: key is valid and the address derives; balance not checked"
        return report

    from .upstream import PolymarketError, autodetect_account, clob_available

    # clob_available() returns (ok, error) -- a bare truthiness test on the
    # tuple is always True and would sail straight past a missing client.
    ok, why = clob_available()
    if not ok:
        report.note = (f"py-clob-client is unavailable ({why}), so the venue "
                       "cannot be contacted. Install it with: "
                       "pip install py-clob-client")
        return report

    async def _go():
        client, detected = await autodetect_account(
            host=host, chain_id=chain_id, private_key=key,
            funder_hint=funder_hint)
        return client, detected

    try:
        client, detected = asyncio.run(_go())
    except PolymarketError as exc:
        raise WalletSetupError(
            f"{exc}\n\n"
            "Common causes, in the order they actually happen:\n"
            "  1. Your USDC is in a Polymarket proxy wallet. Copy the address\n"
            "     shown in the Polymarket app and re-run with --funder 0x...\n"
            "  2. The funds are on a different chain. Polymarket uses Polygon.\n"
            "  3. The key is for a different account than you expect."
        ) from exc

    report.reachable = True
    report.signature_type = int(detected.signature_type)
    report.funder_address = str(detected.funder_address or "")
    report.balance = float(detected.balance or 0.0)
    report.account_label = str(getattr(detected, "account_label", "") or
                               ACCOUNT_KINDS.get(report.signature_type, ""))
    if report.balance <= 0:
        report.note = (
            "Authenticated, but no USDC was found in any configuration. The key "
            "works; the funds are somewhere this search did not look. If you "
            "hold funds in the Polymarket app, pass its address with --funder."
        )
    return report


# ---------------------------------------------------------------------------
# Persisting the NON-SECRET half
# ---------------------------------------------------------------------------

_SIG_RE = re.compile(r'^(?P<indent>\s*)signature_type:\s*\d+', re.MULTILINE)
_FUNDER_RE = re.compile(
    r'^(?P<indent>\s*)funder_address:\s*(?P<q>["\']?)(?P<val>[^"\'\n#]*)(?P=q)',
    re.MULTILINE)


def apply_to_config(config_path: Path, report: WalletReport) -> list[str]:
    """Write `signature_type` and `funder_address` into config.yaml.

    The private key is never written here. config.yaml resolves it from the
    environment (`${env:PQB_PRIVATE_KEY}`) precisely so that a config file can
    be copied, shared or committed without carrying a credential, and that
    property is not weakened to save the operator one step.
    """
    if not report.configured:
        raise WalletSetupError("Nothing to write: the account was not identified.")
    path = Path(config_path)
    if not path.is_file():
        raise WalletSetupError(f"Config not found: {path}")

    text = path.read_text(encoding="utf-8")
    changed: list[str] = []

    new_text, n = _SIG_RE.subn(
        lambda m: f"{m.group('indent')}signature_type: {report.signature_type}",
        text, count=1)
    if n:
        text = new_text
        changed.append(f"signature_type: {report.signature_type}")

    funder = report.funder_address or ""
    new_text, n = _FUNDER_RE.subn(
        lambda m: f'{m.group("indent")}funder_address: "{funder}"',
        text, count=1)
    if n:
        text = new_text
        changed.append(f'funder_address: "{funder}"')

    if changed:
        path.write_text(text, encoding="utf-8")
    return changed
