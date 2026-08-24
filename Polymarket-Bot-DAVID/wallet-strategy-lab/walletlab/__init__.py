"""walletlab — wallet-specific Polymarket strategy discovery.

See docs/AUDIT.md for why this exists alongside pqb rather than replacing it.
"""

from .config import ENGINE_VERSION, settings

__all__ = ["ENGINE_VERSION", "settings"]
__version__ = "0.1.0"
