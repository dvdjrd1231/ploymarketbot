"""Test fixtures. Adds the project root to sys.path so `pqb` imports work."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class MemoryStore:
    """A ``StateStore`` that keeps everything in a dict.

    The doubling rule's persistence contract is "write it, read it back after a
    restart", and a dict models that exactly — so its tests need no SQLite and
    no temp files.
    """

    def __init__(self, initial: dict | None = None):
        self.data = dict(initial or {})
        self.writes = 0

    def get_state(self, key, default=None):
        return self.data.get(key, default)

    def set_state(self, key, value) -> None:
        self.data[key] = value
        self.writes += 1


@pytest.fixture
def store() -> MemoryStore:
    return MemoryStore()


@pytest.fixture
def journal(tmp_path):
    from pqb.journal import Journal
    j = Journal(tmp_path / "journal.sqlite3", mode="test")
    yield j
    j.close()


@pytest.fixture
def intel_store(tmp_path):
    from pqb.analytics.store import IntelStore
    s = IntelStore(tmp_path / "intel.sqlite3")
    yield s
    s.close()


def trade(wallet: str, ts: int, token: str = "T1", side: str = "BUY",
          price: float = 0.40, usdc: float = 100.0, market: str = "M1",
          outcome: str = "Yes"):
    """A normalised observation, with size derived so usdc is the dial.

    Size is what the API publishes, but every test here reasons in notional —
    so the helper takes the notional and back-solves the shares.
    """
    from pqb.models import WalletTrade
    return WalletTrade(
        wallet=wallet, ts=ts, market_id=market, token_id=token,
        outcome=outcome, side=side, price=price,
        size=(usdc / price if price else 0.0), usdc=usdc, source="test")
