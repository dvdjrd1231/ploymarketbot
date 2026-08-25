"""V3 configuration.

Same discipline as V2: a dataclass tree, not a YAML schema, so that every
number capable of suppressing a trade is greppable and carries the name of the
layer that owns it. New in V3 are the capital model (which defaults to $100,
not $10,000) and the collector/agent/server settings.

Two rules this file exists to make structural rather than cultural:

  1. No credential is ever a field here. `secrets.py` reads them from the
     environment or the OS store and hands out presence booleans. A private key
     that is not in the config tree cannot be serialised into a report, a log
     line, an agent prompt or a dashboard payload by accident.

  2. `mode` starts at RESEARCH and `live_authorized` starts False. Promoting to
     LIVE is a human action recorded in the store, never a config default and
     never a consequence of good backtest numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent       # polymarket_quant_v3/
_REPO = _HERE.parent

DEFAULT_DATA_DB = _REPO / "Polymarket-Bot-DATA" / "state" / "intel.sqlite3"
DEFAULT_WORK_DIR = _HERE / "var"

SCHEMA_VERSION = 3
DATA_VERSION = 1


class Mode(str, Enum):
    """Operating mode. The ladder is one-directional without human action."""

    RESEARCH = "RESEARCH"
    BACKTEST = "BACKTEST"
    WALK_FORWARD = "WALK_FORWARD"
    SHADOW = "SHADOW"
    PAPER = "PAPER"
    LIVE = "LIVE"

    @property
    def risks_capital(self) -> bool:
        return self is Mode.LIVE


# ---------------------------------------------------------------------------
# Capital
# ---------------------------------------------------------------------------

@dataclass
class CapitalConfig:
    """Owner: GLOBAL_SAFETY. The $100 model.

    Every fraction here is a fraction of *equity*, so the same numbers behave
    correctly at $100 and at $100,000. What does NOT scale is `min_order_usdc`:
    an exchange minimum is an absolute floor, and at $100 of equity a 2%
    per-trade cap is $2.00, which is below many venue minimums. That collision
    is the whole reason `CAPITAL_INFEASIBLE` exists as a first-class outcome
    rather than a rounding step — see portfolio/capital.py.
    """

    # The bankroll is CONFIGURED, never assumed. It is read from the
    # environment so that no code path anywhere can hard-code a wallet size,
    # and so the number can be changed without editing source:
    #
    #     set PQV3_STARTING_CAPITAL=250
    #     python -m pqv3 dashboard
    #
    # or per-run: `python -m pqv3 --capital 250 scan`.
    #
    # $100 is the default because it was specified, not because the system
    # believes anything about it. Every fraction below is a fraction of
    # *equity*, so the model behaves identically at $100 and at $100,000 —
    # what does NOT scale is `min_order_usdc`, and that collision is the whole
    # reason CAPITAL_INFEASIBLE exists. `pqv3 capital` prints it at whatever
    # bankroll is configured.
    starting_capital: float = float(
        os.environ.get("PQV3_STARTING_CAPITAL", "100.00") or 100.00)
    reserve_fraction: float = 0.10        # never deployable, not even by Kelly

    # Absolute venue constraints. Not fractions — they do not scale with equity.
    min_order_usdc: float = 1.00
    min_shares: float = 5.0
    price_tick: float = 0.01

    # Per-trade / per-market caps, as fractions of current equity.
    max_fraction_per_trade: float = 0.05      # $5.00 at $100
    max_fraction_per_market: float = 0.10
    max_fraction_correlated: float = 0.25
    max_fraction_one_wallet_copy: float = 0.20
    max_open_positions: int = 12              # 12 x $5 would already be 60%

    # Kelly is always fractional and always capped by the fraction above.
    kelly_fraction: float = 0.25

    hard_stop_drawdown: float = 0.25          # halt everything, human to resume

    def deployable(self, equity: float) -> float:
        return max(0.0, equity * (1.0 - self.reserve_fraction))


@dataclass
class CostConfig:
    """Owner: EXECUTION. Pessimistic on purpose.

    Being wrong here in the optimistic direction is the cheapest way to
    manufacture an edge that does not exist.
    """

    fee_bps: float = 0.0              # Polymarket charges no maker/taker fee today
    slippage_bps: float = 50.0        # 0.50% against you, on top of tape price
    latency_ms: int = 1500            # signal -> order on a home connection
    min_price: float = 0.02
    max_price: float = 0.98
    # Fraction of visible size you may assume you get. Never 1.0.
    fill_ratio_assumption: float = 0.50


# ---------------------------------------------------------------------------
# Collectors
# ---------------------------------------------------------------------------

@dataclass
class CollectorConfig:
    """Owner: DATA. Live capture for the layers that have no history.

    Every one of these starts with zero rows and stays honest about it. The
    dashboard reports `history_days` from the store, so a freshly installed
    system shows 0.0 rather than a plausible-looking number.
    """

    enabled: bool = False                    # opt-in; nothing dials out by default
    orderbook_interval_secs: int = 30
    orderbook_top_levels: int = 10
    news_interval_secs: int = 120
    chain_interval_secs: int = 60
    market_sync_interval_secs: int = 300
    http_timeout_secs: float = 10.0
    max_inflight: int = 4                    # bounded concurrency, always

    gamma_base: str = "https://gamma-api.polymarket.com"
    clob_base: str = "https://clob.polymarket.com"
    data_base: str = "https://data-api.polymarket.com"
    # Chain + news endpoints are deliberately unset. A URL that is not
    # configured is reported as NOT_CONFIGURED rather than silently skipped.
    chain_rpc: str = ""
    news_feeds: tuple = ()
    # Public/social streams as (url, source_name). Items from these enter at
    # the BOTTOM of the confirmation ladder and can only be promoted by
    # corroboration from a different source — see ingest/social.py.
    social_feeds: tuple = ()

    # Minimum history a collector-backed feature needs before any strategy may
    # depend on it. Below this the gate returns INSUFFICIENT_HISTORY.
    min_history_days: float = 14.0


# ---------------------------------------------------------------------------
# Research / validation
# ---------------------------------------------------------------------------

@dataclass
class ResearchConfig:
    """Owner: RESEARCH."""

    oos_fraction: float = 0.30
    walkforward_folds: int = 5
    min_oos_fills: int = 30
    min_oos_markets: int = 5
    max_concentration: float = 0.60
    min_walkforward_positive: float = 0.50
    bh_alpha: float = 0.10                 # Benjamini-Hochberg FDR level
    bootstrap_draws: int = 2000
    seed: int = 20260825

    # Holding period assumed by the $100 capital test when the settlement
    # clock is unusable. An ASSUMPTION, named and configurable, never a
    # silent default buried in the simulator. Results computed with it are
    # labelled MODELLED and are refused as validation evidence.
    modelled_hold_secs: int = 3 * 86_400

    # An "apparently perfect" strategy triggers MORE validation, not less.
    perfect_winrate_threshold: float = 0.98
    perfect_extra_oos_multiple: float = 3.0


@dataclass
class AgentConfig:
    """Owner: RESEARCH. Agents advise; they never move capital."""

    max_parallel: int = 8
    red_team_must_pass: bool = True
    min_consensus: float = 0.55            # weighted agreement to advance
    max_model_disagreement: float = 0.35   # above this, confidence is cut

    # Local LLM. Optional by construction: the quantitative engine runs whole
    # without it, and an LLM is never permitted to produce a number.
    llm_provider: str = os.environ.get("PQV3_LLM_PROVIDER", "")     # "" = off
    llm_endpoint: str = os.environ.get("PQV3_LLM_ENDPOINT", "")
    llm_model: str = os.environ.get("PQV3_LLM_MODEL", "")
    llm_context_limit: int = 8192
    llm_temperature: float = 0.2
    llm_timeout_secs: float = 60.0


@dataclass
class ServerConfig:
    """Owner: UI. Local only."""

    host: str = "127.0.0.1"                # never 0.0.0.0 by default
    port: int = 8787
    open_browser: bool = True

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"


@dataclass
class FreshnessConfig:
    """Owner: GLOBAL_SAFETY. Stale data must stop a trade, not slow one down."""

    max_market_age_secs: int = 300
    max_book_age_secs: int = 120
    max_news_age_secs: int = 3600
    max_chain_age_secs: int = 900


@dataclass
class Settings:
    data_db: Path = field(default_factory=lambda: Path(
        os.environ.get("PQV3_DATA_DB", str(DEFAULT_DATA_DB))))
    work_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("PQV3_WORK_DIR", str(DEFAULT_WORK_DIR))))

    mode: Mode = Mode.RESEARCH
    live_authorized: bool = False           # only a human, only at runtime

    capital: CapitalConfig = field(default_factory=CapitalConfig)
    costs: CostConfig = field(default_factory=CostConfig)
    collectors: CollectorConfig = field(default_factory=CollectorConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    agents: AgentConfig = field(default_factory=AgentConfig)
    server: ServerConfig = field(default_factory=ServerConfig)
    freshness: FreshnessConfig = field(default_factory=FreshnessConfig)

    schema_version: int = SCHEMA_VERSION
    data_version: int = DATA_VERSION

    @property
    def store_db(self) -> Path:
        return self.work_dir / "pqv3.sqlite3"

    def ensure_dirs(self) -> "Settings":
        self.work_dir.mkdir(parents=True, exist_ok=True)
        for sub in ("reports", "research", "cache", "logs"):
            (self.work_dir / sub).mkdir(exist_ok=True)
        return self

    def to_dict(self) -> dict:
        d = asdict(self)
        d["data_db"] = str(self.data_db)
        d["work_dir"] = str(self.work_dir)
        d["mode"] = self.mode.value
        return d


def load(**overrides) -> Settings:
    st = Settings()
    for k, v in overrides.items():
        if hasattr(st, k) and v is not None:
            setattr(st, k, v)
    return st.ensure_dirs()
