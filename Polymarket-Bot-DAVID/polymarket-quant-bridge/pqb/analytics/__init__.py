"""
The analytical layer: broad ingestion -> normalisation -> features -> ranking
and anomaly detection.

This package deliberately knows nothing about Polymarket's API and nothing
about placing orders. It consumes normalised :class:`~pqb.models.WalletTrade`
records and produces :class:`~pqb.models.WalletIntel`,
:class:`~pqb.models.MarketIntel` and :class:`~pqb.models.AnomalySignal` — which
the decision engine may use, ignore, or weigh however its own analysis says is
warranted.

The ordering the brief asks for maps onto these modules directly:

    broad ingestion   adapters/data_adapter.observe_trades()
    normalisation     models.WalletTrade
    feature generation analytics/features.py
    ranking / anomaly  analytics/ranking.py, analytics/anomalies.py
    decision           bridge/*
    outcome feedback   analytics/feedback.py
"""

from .store import IntelStore

__all__ = ["IntelStore"]
