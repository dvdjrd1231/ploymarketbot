"""V2 configuration.

Deliberately a dataclass tree with defaults rather than a YAML schema: every
number that can suppress a trade has to be greppable, and every one of them
carries the name of the layer that owns it (see `gates.py`). A threshold whose
owner is unclear is how Strategy A's filters end up silently gating Strategy B.

Nothing here writes to the V1 installation. `data_db` is opened read-only and
every artefact V2 produces goes under `work_dir`, which defaults to a V2-only
directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Where the original project lives. V2 READS this and never writes to it.
# ---------------------------------------------------------------------------

_HERE = Path(__file__).resolve().parent.parent          # polymarket_quant_v2/
_REPO = _HERE.parent                                     # repo root

DEFAULT_DATA_DB = _REPO / "Polymarket-Bot-DATA" / "state" / "intel.sqlite3"
DEFAULT_JOURNAL_DB = _REPO / "Polymarket-Bot-DATA" / "state" / "journal.sqlite3"
DEFAULT_LIBRARY_DB = _REPO / "Polymarket-Bot-DATA" / "state" / "library.sqlite3"
DEFAULT_WORK_DIR = _HERE / "var"


@dataclass
class Costs:
    """Execution costs. Owner: EXECUTION.

    Charged on every backtested fill. Being wrong here in the optimistic
    direction is the cheapest way to manufacture an edge that does not exist,
    so the defaults are deliberately pessimistic.
    """

    fee_bps: float = 0.0            # Polymarket charges no maker/taker fee today
    slippage_bps: float = 50.0      # 0.50% against you, on top of the tape price
    max_notional: float = 500.0     # you are not the whale you are copying
    min_price: float = 0.02
    max_price: float = 0.98

    def fill_price(self, price: float) -> float:
        """The price you actually pay, buying, after costs."""
        return price * (1.0 + self.slippage_bps / 10_000.0 + self.fee_bps / 10_000.0)


@dataclass
class StrategyBConfig:
    """Strategy B's OWN gates. Owner: STRATEGY_B.

    None of these are read by Strategy A, and Strategy A's gates are not read
    here. That separation is asserted by `tests/test_isolation.py`.
    """

    # Behaviour matching
    min_behavior_match: float = 0.55        # 0-1; how like the reference this is
    min_wallet_settled_n: int = 20          # point-in-time evidence before following

    # Entry
    min_price: float = 0.05
    max_price: float = 0.95
    min_notional: float = 25.0              # the wallet's own conviction floor
    delay_secs: int = 60                    # realistic reaction time

    # Validation floors before a strategy may trade paper
    min_oos_fills: int = 30
    min_oos_markets: int = 5
    max_concentration: float = 0.60
    min_walkforward_positive: float = 0.50

    # Discovery
    max_wallets: int = 60
    min_wallet_trades: int = 60


@dataclass
class RiskConfig:
    """Owner: GLOBAL_SAFETY / PORTFOLIO_RISK. Applies to BOTH strategies."""

    # GLOBAL_SAFETY — these are the only rules allowed to block both routes.
    max_fraction_per_trade: float = 0.02        # of current equity
    max_fraction_per_market: float = 0.06
    hard_stop_drawdown: float = 0.35            # halt everything

    # PORTFOLIO_RISK
    max_open_positions: int = 40
    max_strategy_share: float = 0.50            # one strategy's share of exposure
    max_wallet_share: float = 0.35              # one followed wallet's share
    max_category_share: float = 0.50
    max_correlated_share: float = 0.40


@dataclass
class SizingConfig:
    """Owner: STRATEGY_B / PORTFOLIO_RISK. The Win Expansion ladder lives here."""

    mode: str = "fixed_fractional"   # fixed | fixed_fractional | edge | confidence
    base_fraction: float = 0.01      # of equity per trade at 1.00x
    kelly_fraction: float = 0.25     # fractional Kelly only; never full
    expansion_ladder: tuple = (1.00, 1.10, 1.25, 1.50, 2.00)
    max_expansion: float = 2.00
    # Win Expansion may only lift size when ALL of these hold.
    expansion_min_sample: int = 60
    expansion_min_expectancy: float = 0.02
    expansion_max_drawdown: float = 0.25
    drawdown_derisk_at: float = 0.10   # start shrinking size from this drawdown


@dataclass
class Compounding:
    starting_capital: float = 10_000.0
    reinvest: bool = True
    reserve_fraction: float = 0.10     # never deployable


@dataclass
class AccelConfig:
    """Rust acceleration. Owner: none — it may never change a decision."""

    mode: str = "auto"                 # auto | enabled | disabled | shadow
    tolerance: float = 1e-9

    @classmethod
    def from_env(cls) -> "AccelConfig":
        return cls(mode=os.environ.get("PQV2_ACCEL", "auto").lower())


@dataclass
class Settings:
    data_db: Path = field(default_factory=lambda: Path(
        os.environ.get("PQV2_DATA_DB", str(DEFAULT_DATA_DB))))
    journal_db: Path = field(default_factory=lambda: Path(
        os.environ.get("PQV2_JOURNAL_DB", str(DEFAULT_JOURNAL_DB))))
    library_db: Path = field(default_factory=lambda: Path(
        os.environ.get("PQV2_LIBRARY_DB", str(DEFAULT_LIBRARY_DB))))
    work_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("PQV2_WORK_DIR", str(DEFAULT_WORK_DIR))))

    costs: Costs = field(default_factory=Costs)
    strategy_b: StrategyBConfig = field(default_factory=StrategyBConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    sizing: SizingConfig = field(default_factory=SizingConfig)
    compounding: Compounding = field(default_factory=Compounding)
    accel: AccelConfig = field(default_factory=AccelConfig.from_env)

    chunk_rows: int = 50_000
    seed: int = 20260824
    # Out-of-sample split: the newest fraction of time is never used to
    # discover anything.
    oos_fraction: float = 0.30
    walkforward_folds: int = 5

    def ensure_dirs(self) -> "Settings":
        self.work_dir.mkdir(parents=True, exist_ok=True)
        (self.work_dir / "research").mkdir(exist_ok=True)
        (self.work_dir / "reports").mkdir(exist_ok=True)
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("data_db", "journal_db", "library_db", "work_dir"):
            d[k] = str(d[k])
        return d
