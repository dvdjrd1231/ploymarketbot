"""WALLET STATE TRANSITION RESEARCH — an isolated research subsystem.

    Does the RN1 post-opposite-buy state transition contain a generalisable and
    EXECUTABLE predictive edge across Polymarket wallets and markets, and can
    any robust component of it improve the existing engine without degrading
    what it already does?

That is the question. This package answers it and does nothing else. It is
switched off by default, it is reached through exactly one function that
returns `NO_SIGNAL` when disabled, and with `enabled: false` the existing
engine's behaviour is bit-identical to the build before this package existed.

**This is not the same thing as `analytics/wallet_states.py`.** That module
studies a different RN1 hypothesis — a first-buy price band followed by
silence — and it is untouched. This one studies what happens AFTER the wallet
first buys the OPPOSITE outcome. They share a wallet and nothing else.

## The two questions, kept apart

The single most common way this research goes wrong is answering the first
question and reporting it as the second:

* **Question A — behaviour.** Given the wallet's state three minutes after it
  first bought the other side, will it push to a genuinely aggressive
  opposite position, or is it rebalancing to protect what it has?
* **Question B — money.** If we buy the opposite outcome on that prediction,
  at a price we could actually have been filled at, after spread, slippage,
  fees and settlement, do we make money?

74% accuracy on A says nothing about B. Every report in this package answers
them in separate sections and never averages them into one number.

## What the available data can and cannot support

Measured against the real store rather than assumed (see `events.audit`):

* 713k observed trades, 70.5k wallets, 16.2k markets, explicit BUY/SELL and
  explicit share size — so inventory reconstruction is exact, not inferred
  from cash/price.
* ~262 settled tokens. Terminal settlement is therefore available for a small
  minority of episodes, and P&L that depends on it is reported on its own
  sample with its own count rather than blended into a headline.
* Order-book history (`research_rows`) covers ~2.5k tokens over a few days.
  For everything else the executable price is reconstructed from the trade
  tape, which is a print, not a quote. That distinction is carried on every
  fill as `price_source` and every execution report states its mix.

Where a field the brief asks for cannot be reconstructed, it is marked
UNAVAILABLE with the reason and the experiment continues on the strongest
valid version. Nothing is fabricated to fill a column.

## Layout (and the brief's suggested component names)

| brief                         | here                                    |
|-------------------------------|-----------------------------------------|
| WalletStateTransitionEngine   | `runner.run` / `episodes`               |
| WalletStateFeatureBuilder     | `features.build`                        |
| WalletBehaviorClassifier      | `classifier.FrozenRN1` (+ registry)     |
| WalletBehaviorModelRegistry   | `classifier.REGISTRY`                   |
| WalletSignalGenerator         | `signal.generate`                       |
| WalletBacktestEvaluator       | `backtest.simulate`                     |
| WalletWalkForwardValidator    | `validation.walk_forward`               |
| WalletCrossSectionAnalyzer    | `validation.cross_wallet` / `cross_market` |
| WalletStrategyAdapter         | `signal.get_signal` (the one entry point) |
"""

from __future__ import annotations

# The frozen benchmark's identity. Never overwritten, never retuned; an
# optimised model is always a NEW version id beside it.
RN1_WALLET = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"
RN1_MODEL_VERSION = "RN1_FROZEN_V1"

from .classifier import (AGGRESSIVE, PROTECT, DIRECTIONAL,  # noqa: E402
                         FrozenRN1, REGISTRY)
from .signal import NO_SIGNAL, WalletStateSignalResult, get_signal  # noqa: E402

__all__ = [
    "RN1_WALLET", "RN1_MODEL_VERSION",
    "AGGRESSIVE", "PROTECT", "DIRECTIONAL", "FrozenRN1", "REGISTRY",
    "NO_SIGNAL", "WalletStateSignalResult", "get_signal",
]
