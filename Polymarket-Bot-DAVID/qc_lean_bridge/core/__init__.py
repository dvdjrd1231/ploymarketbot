"""Core research framework for the QC LEAN Bridge.

Pipeline order:
    data_pipeline -> features -> strategy_discovery -> validators -> ranking
                                    (backtester + risk_management underpin these)
    lean_export produces QuantConnect-ready code for accepted strategies.
"""
