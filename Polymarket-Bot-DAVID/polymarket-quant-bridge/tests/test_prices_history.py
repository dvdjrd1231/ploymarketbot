"""CLOB price-history (§4): probability history, which neither reference project
had. Parsing is what matters and is tested here with a stubbed HTTP client."""

from __future__ import annotations

import asyncio

from pqb.adapters.data_adapter import PolymarketDataAdapter
from pqb.config import Config
from pqb.logs import Log


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _HTTP:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def get(self, url, params=None):
        self.calls.append((url, params))
        return _Resp(self._payload)


def _adapter(payload):
    http = _HTTP(payload)
    return PolymarketDataAdapter(Config(), Log(), http=http), http


def test_history_is_parsed_and_sorted():
    adapter, http = _adapter(
        {"history": [{"t": 300, "p": 0.6}, {"t": 100, "p": 0.5},
                     {"t": 200, "p": 0.55}]})
    series = asyncio.run(adapter.prices_history("tok1"))
    assert series == [(100, 0.5), (200, 0.55), (300, 0.6)]
    # Hit the CLOB prices-history endpoint with the token as `market`.
    url, params = http.calls[0]
    assert url.endswith("/prices-history")
    assert params["market"] == "tok1"


def test_bad_rows_are_skipped_not_fatal():
    adapter, _ = _adapter(
        {"history": [{"t": 100, "p": 0.5}, {"nope": 1}, {"t": "x", "p": "y"}]})
    series = asyncio.run(adapter.prices_history("tok1"))
    assert series == [(100, 0.5)]


def test_error_returns_empty_not_raises():
    class _Boom:
        async def get(self, url, params=None):
            raise RuntimeError("network down")

    adapter = PolymarketDataAdapter(Config(), Log(), http=_Boom())
    assert asyncio.run(adapter.prices_history("tok1")) == []
