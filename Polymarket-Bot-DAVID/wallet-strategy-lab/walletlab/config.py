"""Configuration, paths and the cost model.

Everything tunable lives here so an experiment's inputs can be hashed (§36).
No module reads an environment variable directly except through `settings()`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict, field
from pathlib import Path

# Algorithm version. Bump when a change alters computed numbers; it is part of
# every cache key and every experiment record, so old results never silently
# mix with new ones (§25, §36).
ENGINE_VERSION = "walletlab/0.1.0"


@dataclass(frozen=True)
class CostModel:
    """Realistic execution costs (§29).

    Polymarket charges no explicit maker/taker fee on most markets, so the real
    cost of copying is spread + slippage + the fact that you are, by
    construction, arriving after the wallet you are copying. Defaults are
    deliberately pessimistic: a strategy that only survives at zero cost is not
    a strategy.
    """

    slippage_bps: float = 100.0      # 1.00% of price, paid on entry
    fee_bps: float = 0.0             # explicit fee, if the venue charges one
    min_price: float = 0.02          # refuse to model fills outside this band
    max_price: float = 0.98
    max_notional: float = 5_000.0    # per-trade cap; you are not the whale

    def fill_price(self, price: float) -> float:
        """Price actually paid when copying a BUY at `price`."""
        return price * (1.0 + (self.slippage_bps + self.fee_bps) / 10_000.0)


@dataclass(frozen=True)
class Settings:
    data_db: Path
    work_dir: Path
    costs: CostModel = field(default_factory=CostModel)

    # Memory discipline (§22). Nothing in the engine may load an unbounded
    # result set; every query path is chunked at this size.
    chunk_rows: int = 250_000
    max_events_in_memory: int = 2_000_000

    def registry_path(self) -> Path:
        return self.work_dir / "experiments.sqlite3"

    def cache_dir(self) -> Path:
        return self.work_dir / "cache"

    def fingerprint(self) -> dict:
        d = asdict(self)
        d["data_db"] = str(self.data_db)
        d["work_dir"] = str(self.work_dir)
        d["engine_version"] = ENGINE_VERSION
        return d


_DEFAULT_DATA = Path(
    os.environ.get(
        "WALLETLAB_DATA_DB",
        r"D:\git_olaf\ploymarketbot\Polymarket-Bot-DATA\state\intel.sqlite3",
    )
)
_DEFAULT_WORK = Path(
    os.environ.get(
        "WALLETLAB_WORK_DIR",
        r"D:\git_olaf\ploymarketbot\Polymarket-Bot-DATA\state\walletlab",
    )
)


def settings(data_db: Path | None = None, work_dir: Path | None = None) -> Settings:
    s = Settings(
        data_db=Path(data_db or _DEFAULT_DATA),
        work_dir=Path(work_dir or _DEFAULT_WORK),
    )
    s.work_dir.mkdir(parents=True, exist_ok=True)
    s.cache_dir().mkdir(parents=True, exist_ok=True)
    return s
