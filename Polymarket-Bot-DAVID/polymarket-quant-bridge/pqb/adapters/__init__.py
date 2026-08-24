"""
Inbound and outbound Polymarket adapters — the only exchange-aware code.

Deliberately empty of imports: ``sizing`` is pure arithmetic and must stay
importable (and testable) without the upstream ploymarketbot checkout that the
other two modules pull in. Import the adapters from their own modules:

    from pqb.adapters.data_adapter import PolymarketDataAdapter
    from pqb.adapters.execution_adapter import PolymarketExecutionAdapter
"""
