"""
Configuration loading.

One file drives the whole system (YAML by default; JSON is accepted so the
config can follow whichever convention the host bridge already uses).

Secrets never appear in it. Any string may contain ``${env:NAME}`` or
``${env:NAME:default}``, which is resolved from the process environment — and
from a ``.env`` file beside the config if one exists. ``redacted()`` returns the
config with every credential field masked, and that is the only form anything is
ever allowed to log.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Optional

from .analytics.anomalies import AnomalyConfig

_ENV_PATTERN = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)(?::([^}]*))?\}")

# Keys whose values are secret. Matched case-insensitively as substrings, so
# `private_key`, `api_secret` and `passphrase` are all covered.
_SECRET_HINTS = ("private_key", "secret", "passphrase", "api_key", "token_auth")

# Section 7: the minimum-trade-size progression. Kept here as the default so a
# config that omits it still behaves exactly as specified.
DEFAULT_PROGRESSION: list[float] = [
    0.19, 0.29, 0.41, 0.47, 0.59, 0.67, 0.73, 0.83, 0.97, 1.03,
    1.09, 1.27, 1.37, 1.49, 1.57, 1.67, 1.79, 1.91, 1.97, 2.11,
    2.27, 2.39, 2.51, 2.63, 2.71, 2.81, 2.93, 3.11, 3.17, 3.37,
    3.49, 3.59, 3.73, 3.83, 3.97, 4.09, 4.21, 4.33, 4.43, 4.57,
    4.67, 4.87, 5.03, 5.21, 5.41, 5.57, 5.69, 5.77, 5.93, 6.07,
]


# --- sections ---------------------------------------------------------------

@dataclass
class PolymarketConfig:
    clob_host: str = "https://clob.polymarket.com"
    gamma_host: str = "https://gamma-api.polymarket.com"
    data_api_host: str = "https://data-api.polymarket.com"
    clob_ws_host: str = "wss://ws-subscriptions-clob.polymarket.com/ws"
    chain_id: int = 137
    signature_type: int = 0
    funder_address: str = ""
    private_key: str = ""
    use_price_stream: bool = True
    rest_price_fallback_seconds: float = 4.0
    # Polygon JSON-RPC endpoint for on-chain CTF reconstruction (§6). Empty
    # disables it — a deliberate choice, not a default, because the endpoint is
    # the operator's to pick (public RPC or a keyed provider). The default is
    # empty here; config.yaml carries the ``${env:PQB_POLYGON_RPC_URL:}`` line
    # so a provider key is resolved from the environment, never written to file.
    polygon_rpc_url: str = ""
    # Block to start CTF scans from. Polymarket's ConditionalTokens has activity
    # only well into Polygon's history, so 0 works but a later start is faster.
    ctf_from_block: int = 0
    ctf_chunk_blocks: int = 100_000


@dataclass
class ModeConfig:
    """Two independent flags must both be set before a real order can be sent.

    ``dry_run: false`` alone is not enough — ``allow_live`` has to be turned on
    as well. One accidental edit therefore cannot arm live trading.
    """

    dry_run: bool = True
    allow_live: bool = False
    kill_switch_file: str = "state/KILL"
    # Asks the process to shut down cleanly, as opposed to KILL which stops
    # trading but leaves it running. A file rather than a signal because the
    # dashboard launches the bot with no console attached, and a console
    # control event has nothing to be delivered to in that case — it simply
    # never arrives. A file works from the GUI, from another shell, and after
    # the GUI itself has gone away.
    stop_file: str = "state/STOP"
    # Dry-run needs a bankroll to size against; it is simulated money, and the
    # simulated book is persisted so a multi-hour session survives a restart.
    paper_starting_balance: float = 100.0
    reset_paper_on_start: bool = False

    @property
    def live(self) -> bool:
        return (not self.dry_run) and self.allow_live


@dataclass
class MarketFilters:
    categories: list[str] = field(default_factory=list)
    min_liquidity: float = 5_000.0
    min_volume_24h: float = 0.0
    max_markets: int = 25
    min_seconds_to_resolution: int = 3_600
    max_days_to_resolution: int = 30


@dataclass
class MarketsConfig:
    track: list[str] = field(default_factory=list)   # explicit condition ids
    follow_wallet_markets: bool = True               # add markets wallets trade
    filters: MarketFilters = field(default_factory=MarketFilters)


@dataclass
class WalletConfig:
    """A wallet the operator wants followed by name.

    Configuring a wallet **seeds** the analytical layer; it does not restrict
    it. Ingestion observes every wallet that trades regardless of this list, and
    ranking is derived from measured performance — so a configured wallet with a
    poor record ranks poorly, and an unconfigured one with a strong record can
    outrank it. ``weight`` is a floor on the influence a named wallet carries
    while its own history is still too thin to have earned one.
    """

    address: str = ""
    label: str = ""
    weight: float = 1.0


@dataclass
class IntelConfig:
    """Broad ingestion, dynamic ranking and anomaly detection.

    The defaults deliberately ingest far more than the decision model uses: raw
    coverage is cheap and irreversible if skipped, whereas deciding later that a
    wallet mattered is impossible if it was never recorded.
    """

    enabled: bool = True
    db: str = "state/intel.sqlite3"
    # Per-market trade history pulled each cycle. The deep sweep.
    trades_per_market: int = 100
    # Exchange-wide sweep, so wallets outside the tracked universe are still
    # seen. The shallow-but-broad half of the coverage.
    global_sweep: bool = True
    global_sweep_limit: int = 500
    max_trades_per_cycle: int = 5_000
    # How much history features and ranking are computed over.
    lookback_days: float = 30.0
    # How long raw observations are kept. Hourly market rollups outlive this.
    retention_days: float = 90.0
    # Cadence of the heavy profile-and-rank rebuild (seconds).
    refresh_seconds: int = 300
    # Size of the top-ranked analytical cohort. A label on a wallet, never a
    # filter on the feed — everything observed still reaches the engine.
    cohort_size: int = 25
    anomalies: AnomalyConfig = field(default_factory=AnomalyConfig)


@dataclass
class ResearchConfig:
    """Capturing the feature series, and discovering strategies from it.

    Capture is the part that cannot be deferred: an order book that existed for
    four seconds last Tuesday is gone, so the series discovery will eventually
    be run over has to be written down as it happens.
    """

    enabled: bool = True
    # The account discovery SIZES AGAINST. Research that sizes to a notional
    # account does not describe the account that will actually place the
    # orders: a rule sized to $10,000 books P&L ~100x the real figure on a
    # $100 book, and every expectancy, drawdown and rejection recorded from
    # it is wrong by that factor.
    #
    # 0 = derive it, which is what it should almost always be left at:
    # the engine's last recorded portfolio value if there is one, otherwise
    # ``mode.paper_starting_balance``. Set a positive number only to research
    # a hypothetical account size deliberately.
    account_equity: float = 0.0
    # A floor under the derived figure. Below this the position ladder is one
    # share wide at any realistic price and discovery is searching a space it
    # cannot express, so research runs at the floor and says so rather than
    # silently producing a degenerate ladder.
    min_account_equity: float = 25.0
    # How often a feature row is written per tracked token. Every cycle would
    # be one row per token per 20s — mostly duplicated state, since books do
    # not move that fast in most prediction markets.
    capture_seconds: int = 60
    # Captured rows a token needs before it is worth researching at all.
    min_rows: int = 200
    # Token series researched per run, richest history first. Wide by
    # default: cross-market generalisation is the whole point.
    max_tokens: int = 48
    # Independent token series a rule must be accepted on to be kept. Two is
    # the minimum that can distinguish a rule from a curve fit.
    min_tokens: int = 2
    # The non-print engine (qc_lean_bridge) between the feed and the bridge —
    # the operator's core architecture. Its structural snapshot rides on every
    # captured row so discovery searches structure, not just raw columns. Time
    # constants are Polymarket-scale (slower markets than futures).
    nonprint_enabled: bool = True
    nonprint_print_lookback_s: float = 900.0
    nonprint_void_max_age_s: float = 3600.0
    # Price band a market is still genuinely in play in. Outside it the outcome
    # is effectively settled and the price only converges on 0 or 1, which is
    # arithmetic rather than an edge: research over that stretch produced rules
    # with a 97.9% win rate and a Sharpe of 46, all of which said no more than
    # "a token at 0.998 reaches 0.999". Series are trimmed to the band before
    # discovery ever sees them, and backfill collects from inside it.
    uncertain_min_price: float = 0.10
    uncertain_max_price: float = 0.90
    # Spread to charge on a series that cannot report its own, in dollars per
    # share. Historical series are rebuilt from the trade tape and there is no
    # historical order book, so their spread column is zero for every row -
    # and charging what the data says would mean charging nothing. Costing a
    # round trip at zero is how a whole set of "validated" rules once appeared,
    # all of them from exactly those series and none from the live-captured
    # ones that did pay. The default is what live capture actually measures:
    # $0.010 on a $0.51 share, 2% one way.
    assumed_spread: float = 0.010
    # Backfill volume bounds, passed to the Gamma API. The Data API refuses an
    # offset past 10,000, so the biggest markets' uncertain period is
    # unreachable — only their settled tail can be paged. 0 disables a bound.
    backfill_max_volume: float = 25_000_000.0
    backfill_min_volume: float = 50_000.0
    # The historical dataset builds ITSELF. A fresh install has no settled
    # history, so discovery has nothing to study and the Discovery tab sits
    # empty — a workflow step nobody should have to know exists. When the
    # store holds fewer settled markets than the floor, a background backfill
    # starts with the bot and the hourly discovery picks it up from there.
    auto_backfill: bool = True
    auto_backfill_min_resolutions: int = 100
    auto_backfill_markets: int = 300
    auto_backfill_trades: int = 1200
    # The dataset must also self-heal on an UPGRADED install, not just a fresh
    # one. The trigger above never fires when the store is full of legacy
    # resolutions - but legacy tapes were collected from the settled tail, so
    # after the uncertainty-band change research drops nearly all of them and
    # the board sits empty forever with backfill convinced there is nothing to
    # do. When a discovery pass exports fewer studiable series than this
    # floor, a re-collection sweep runs (bounded by the cooldown).
    auto_backfill_min_series: int = 8
    auto_backfill_cooldown_seconds: int = 21_600   # at most one sweep per 6h
    # --- sequential / event-chain discovery (operator's spec) ---------------
    # A second research question alongside single-event discovery, never a
    # replacement: does the ORDER and TIMING of events carry information the
    # events do not carry alone? Chains walk the same library and frozen-OOS
    # ladder as every other candidate, and cannot vote in the live engine
    # until an execution path is deliberately enabled after forward
    # validation.
    seq_enabled: bool = True
    seq_max_len: int = 4              # 2..4 events; never longer
    seq_gap_bars: int = 15            # max bars between consecutive events
    seq_hold_bars: int = 15           # measurement/holding window
    seq_min_occurrences: int = 8      # per extra link, the floor multiplies
    seq_register_top: int = 6         # chains registered per pass, at most
    # --- sharp-move / crash-recovery research (operator's spec) -------------
    # Dislocation as a hypothesis: adaptive per-series detection (no
    # imported thresholds), response measured against the pre-move anchor,
    # candidates only where a condition cell beats costs AND the no-event
    # drift control. Same library, same frozen-OOS ladder, same execution
    # bar as sequences.
    sharp_enabled: bool = True
    sharp_hold_bars: int = 15
    sharp_min_events: int = 12
    sharp_register_top: int = 4
    # --- military-attack longshot calibration (operator's spec) -------------
    # The reported "<=35% + $2,500 -> 52%" result is re-derived from the
    # settled tapes with entry-time information only, against same-priced
    # non-military controls, clustered by geopolitical event. Candidates
    # walk the same library/OOS ladder and cannot vote in the live engine.
    longshot_enabled: bool = True
    longshot_min_events: int = 12     # independent event clusters per cell
    longshot_register_top: int = 2
    # --- wallet behavioral states (the operator's RN1 model) ----------------
    # A tracked wallet's buy is not always a prediction: the candidate
    # signal is a high first entry with checkpoint-frozen one-sided
    # commitment (quiet at 3m, persistent through 30m). Studied per wallet
    # over settled tapes; every cell must beat blindly copying the wallet.
    # Targets = pinned wallets + top-ranked + these explicit addresses.
    wallet_state_enabled: bool = True
    wallet_state_targets: list = field(default_factory=list)
    wallet_state_min_markets: int = 12
    wallet_state_register_top: int = 2
    wallet_state_max_premium: float = 0.03   # our entry vs the wallet's
    # --- wallet behavioral discovery (operator's spec) ----------------------
    # The third wallet question: WHAT repeating behavior explains a ranked
    # wallet's results, extracted as an explicit wallet-free market rule
    # (price band, preceding move, holding class) and replayed where the
    # source wallet never traded. Source markets join the candidate's
    # permanent exclusions; the wallet is never the proof. Research only —
    # wallet-pattern rules cannot vote in the live engine.
    wallet_behavior_enabled: bool = True
    wallet_behavior_min_trades: int = 10      # source observations per cell
    wallet_behavior_min_markets: int = 4      # independent source markets
    wallet_behavior_min_repeat: int = 3       # repeats within one wallet
    wallet_behavior_min_wallet_trades: int = 8   # wallet eligibility floor
    wallet_behavior_max_wallets: int = 12     # ranked+pinned wallets studied
    wallet_behavior_register_top: int = 3     # hypotheses registered per pass
    # --- TRUE out-of-sample validation (the operator's spec) ----------------
    # The newest fraction of markets (by end date) is held out entirely:
    # never searched, never used for thresholds or ranking. Candidates are
    # FROZEN and replayed against those untouched markets; only what survives
    # with enough independent evidence is VALIDATED and allowed to trade.
    oos_fraction: float = 0.3
    oos_min_trades: int = 30            # below this: still "OOS testing"
    oos_min_markets: int = 3            # independent markets it must trade in
    oos_min_expectancy: float = 0.0     # mean $ per trade after costs
    oos_hc_trades: int = 100            # high-confidence tier
    oos_hc_markets: int = 5
    # Market-independence gate (the operator's concentration test): a
    # strategy cannot be promoted to tradable while one market carries more
    # than this share of its positive OOS P&L — however good the headline
    # expectancy, one market's story is not a repeatable edge.
    oos_max_concentration: float = 0.7
    # Rejections are verdicts on the evidence that existed when they were
    # made, and the holdout pool grows every pass. This many rejected
    # candidates are rotated back through the replay each pass, oldest
    # verdict first, so "rejected" stops meaning "deleted" without letting
    # a large dead pile crowd out the live candidates. The promotion
    # standard is untouched: next_status still needs the whole cumulative
    # record to turn positive before any of them re-enters validation.
    rejected_recheck_per_pass: int = 8
    # Hard ceiling on how many times one verdict may be re-opened, ever.
    # The principled guard is that a re-open must be justified by evidence
    # the candidate does not have yet; this sits underneath it so no future
    # rule can put a strategy into a cycle.
    max_verdict_reopens: int = 3
    # Duplicate control: new registrations per strategy FAMILY per pass.
    # A family that unseen data keeps refusing shrinks to one slot.
    family_register_cap: int = 2
    # OOS allocation budget: frozen replays per pass across all candidates
    # and pool markets. Bounds compute; priority decides who spends it.
    oos_eval_budget: int = 1500
    # ADAPTIVE ALLOCATION (§13). Candidates given a slate each pass, the
    # per-family cap on that slate, and the share of it reserved for
    # candidates that have never been evaluated and for near misses.
    #
    # The reserves exist because priority is computed from evidence a
    # candidate already holds: without a floor, a candidate with none can
    # never earn any, and the audit found 153 of 231 candidates in exactly
    # that state. They are floors on attention, not holds on compute — an
    # unfilled reserve flows straight to exploitation.
    oos_candidates_per_pass: int = 40
    oos_family_slots: int = 6
    oos_explore_fraction: float = 0.40
    oos_near_miss_fraction: float = 0.15
    # The multi-source hypothesis layer: convergence, inversion, failure
    # states and synthesis. Additive and advisory — it proposes, ranks and
    # connects, and cannot promote, validate or trade. Off switches the
    # analysis off and changes nothing else.
    hypothesis_layer_enabled: bool = True
    # --- the autonomous research layer -------------------------------------
    # Four switches over one idea: the system should get better at deciding
    # what to research next, and must not get better at convincing itself
    # that weak strategies are good. Every one of these steers ALLOCATION.
    # None of them can move a candidate toward a gate, and turning them all
    # off leaves the validation ladder doing exactly what it does today.
    #
    # ADVERSARIAL: run the declared battery against each candidate's own
    # persisted evidence. Costs no replays and consumes no markets — it is
    # arithmetic over frozen validation rows — so the per-pass cap exists to
    # bound wall-clock on a large library, not to ration anything scarce.
    adversarial_enabled: bool = True
    adversarial_max_per_pass: int = 200
    # EXPERIMENT MEMORY: classify every outcome against the failure taxonomy
    # and remember it, so the pass stops re-registering the fifth spelling of
    # a refuted idea. Suppression is a THROTTLE (fewer family slots), never a
    # ban, and any success in a family clears it.
    experiment_memory_enabled: bool = True
    dead_end_family_slots: int = 1
    # THE RESEARCH REWARD: what deserves the next unit of compute. Multiplies
    # into research priority alongside the meta-structure weight. Off falls
    # back to `library.research_priority` exactly as before.
    research_reward_enabled: bool = True
    # COMPOSITION (§10): combine independently discovered signals from one
    # convergence group into new candidates. Each is an ordinary candidate
    # with zero inherited evidence and BOTH parents' markets excluded.
    compose_enabled: bool = True
    compose_per_pass: int = 4
    # The persistent OOS pool: settled series are immutable, so each is
    # built ONCE, cached, and reused forever as validation evidence —
    # breadth is then limited by the dataset, not by the per-pass export
    # cap. Built incrementally, richest tapes first.
    oos_pool_enabled: bool = True
    oos_pool_build_per_pass: int = 150
    oos_pool_max_series: int = 400
    # Pool ELIGIBILITY floor, separate from the discovery floor on purpose:
    # discovery needs long series to SEARCH, but replaying a frozen rule on
    # an unseen market is meaningful on far fewer rows. A lower floor here
    # widens evidence supply without touching any validation threshold —
    # the trade and market floors still gate every promotion.
    oos_pool_min_rows: int = 80
    # Bidirectional / hold discovery (operator's spec): direction and
    # holding period as evidence-driven DISCOVERY VARIABLES. A decisively
    # losing candidate spawns its inverse-side variant; a promising one
    # spawns half/double hold variants — each a fresh candidate with zero
    # inherited validation. Bounded per pass.
    variants_enabled: bool = True
    variants_per_pass: int = 6
    # FAMILY & MOTIF INTELLIGENCE: recurring STRUCTURE across candidates, with
    # evidence provenance so two strategies replayed on the same markets are
    # never counted as two confirmations. It multiplies a bounded weight into
    # research priority and proposes controlled nearby mutations out of the
    # SAME per-pass variant budget above — it does not get its own. Off falls
    # back to exactly the previous behaviour.
    motif_enabled: bool = True
    # Mutations proposed per pass by the motif layer. Deliberately smaller
    # than `variants_per_pass`: the failure-directed and evidence-driven
    # variants already have first claim on that budget, and a motif mutation
    # is the most speculative of the three.
    motif_mutations_per_pass: int = 3
    # Below this Family Research Score a motif proposes nothing. A motif that
    # has not earned standing has no business spending replay budget.
    motif_min_score: float = 0.25
    # Rows written to strategies.json (the board/engine view). A DISPLAY
    # bound only — the library database is unbounded and permanent. The
    # old hard 200 saturated the operator's board at exactly 200 rows.
    library_view_cap: int = 2000
    # Run discovery automatically from inside the bot, so someone who only ever
    # double-clicks the dashboard still gets discovered strategies without ever
    # touching a command line. Discovery is heavy, so it runs on its own timer
    # in a worker thread and never blocks a trading cycle.
    auto_discover: bool = True
    # How often auto-discovery runs, once enough history exists. Hourly: rules
    # do not change minute to minute, and each pass walk-forwards and
    # Monte-Carlos dozens of token series.
    discover_seconds: int = 3600


@dataclass
class EntryConfig:
    enabled: bool = True
    # Learning mode (the operator's request, verbatim: "stop trading until it
    # officially has strategies"). While no validated strategy exists, the
    # LEAN engine parks capital: no entries, exits unaffected, capture and
    # discovery keep running. The simulated bankroll cannot bleed away on the
    # market-quality fallback before discovery has produced anything.
    require_strategies: bool = True
    min_score: float = 0.55
    min_price: float = 0.05
    max_price: float = 0.95
    max_spread: float = 0.06
    wallet_signal_weight: float = 0.6
    require_wallet_signal: bool = False
    signal_ttl_seconds: int = 1_800
    # --- the expected-value gate (EVDecisionEngine) -------------------------
    # Minimum edge per dollar staked, AFTER slippage and fees, before a trade
    # is worth taking. Below this the estimate's own error dominates the edge.
    min_expected_value: float = 0.02
    # How much evidence must stand behind an estimate before it may act. A
    # handful of trades from one wallet cannot clear this, deliberately.
    min_confidence: float = 0.15
    # Assumed adverse move when taking liquidity, as a fraction of price.
    slippage: float = 0.01


@dataclass
class ExitConfig:
    take_profit_pct: float = 0.35
    reduce_at_pct: float = 0.20
    reduce_fraction: float = 0.5
    stop_loss_pct: float = 0.25
    trailing_drawdown_pct: float = 0.15
    trailing_arm_pct: float = 0.15       # only trail once this much in profit
    near_resolution_seconds: int = 3_600
    near_resolution_action: str = "hold"  # "hold" | "exit"
    follow_wallet_exit: bool = True
    wallet_exit_override_score: float = 0.65
    # Exit once holding is worse than this per dollar. Laxer than the entry
    # bar on purpose: getting out costs a spread and a fee, so a position is
    # not churned in and out around zero.
    exit_expected_value: float = -0.03
    max_hold_seconds: int = 0            # 0 = no time cap

    # --- the stagnation exit (a toggle, OFF by default) ---------------------
    # The operator's matrix: a position that has shown no momentum within a
    # window scaled to its market's horizon is dead capital and gets pruned so
    # the balance can rotate into live setups. Windows are per category,
    # decided by time-to-resolution:
    #     resolves within the hour  -> fast window   (his "ultra-short")
    #     resolves within the day   -> intraday window
    #     longer / unknown          -> long window   (multi-day / event)
    # "No momentum" means the position's return is still below
    # stagnation_min_move_pct when the window closes. Stops, trailing and
    # resolution exits all rank above this; it only prunes the going-nowhere.
    stagnation_enabled: bool = False
    stagnation_fast_seconds: int = 240        # 3-5 min band -> 4 min
    stagnation_intraday_seconds: int = 1500   # 15-30 min band -> 25 min
    stagnation_long_seconds: int = 5400       # 1-2 h band -> 1.5 h
    stagnation_min_move_pct: float = 0.02     # under +2% counts as "not moving"

    # --- the edge exit: the PRIMARY exit (master spec) ----------------------
    # A position is held because the measured edge said to hold it. When the
    # live conviction for continuing falls below this floor, the reason for
    # being in the trade is gone and it exits — before any fixed take-profit,
    # which (with the stop) is demoted to a backstop, not the mechanism.
    # 0 disables. The grace period stops a cold first cycle (empty books, no
    # signals yet) from reading as "edge gone" and churning a fresh entry.
    edge_exit_floor: float = 0.30
    edge_exit_grace_seconds: int = 180


@dataclass
class PortfolioConfig:
    # How many positions may be open at once. 0 means NO LIMIT — the Quant
    # Bridge decides how many to hold, bounded only by available cash. This is
    # not a strategy knob for a person to set; the brain owns it.
    max_open_positions: int = 0
    # Of portfolio value, per position — so position size grows with the
    # account. This is what makes profits compound: every gain raises the
    # portfolio value that the next position is sized against.
    max_position_fraction: float = 0.25
    min_order_usdc: float = 0.16
    # 0 disables the drawdown pause: the decision engine manages risk itself
    # rather than a hard imposed rule stopping it. Off by default; the
    # dashboard offers it as an ON/OFF switch.
    max_drawdown_pct: float = 0.0
    # Cash the engine never spends on positions. ON by default at 5%, with the
    # same 0-means-off contract as the drawdown pause.
    reserve_cash_fraction: float = 0.05
    # How much better a new opportunity must be, in EV per dollar, before an
    # existing position is sold to make room. A swap pays a spread and a fee
    # both ways, so churning between near-identical edges loses money.
    replace_margin: float = 0.05

    # --- the performance-deviation throttle (master spec) -------------------
    # When live results deviate materially from what was validated, the honest
    # response is smaller, not hopeful: entries scale down by throttle_factor
    # once the rolling realised return is this negative over at least
    # min_sample closed trades (and by factor squared at twice the deviation).
    # It never scales UP on a winning streak — that is how martingales start.
    throttle_min_sample: int = 20
    throttle_at_return: float = -0.05
    throttle_factor: float = 0.5

    @property
    def position_cap(self) -> int:
        """``max_open_positions`` with 0 (or less) meaning "no limit"."""
        return self.max_open_positions if self.max_open_positions > 0 else 1_000_000

    # Cost of a single fill, in USDC. Charged on the way in AND the way out, so
    # a round trip costs twice this. Polymarket publishes no per-fill fee on the
    # order response today, so it cannot be read back from the exchange — it is
    # stated here and applied to simulated fills and to sizing.
    fee_per_trade_usdc: float = 0.01
    # Money set aside purely for fees. Fees draw it down, and when it reaches
    # 10% of this starting amount it refills automatically (the operator's
    # replenish rule) — fee money never silently runs out mid-session. 0 = off.
    fee_fund_usdc: float = 2.0
    # Refuse an entry whose expected round-trip fee exceeds this share of the
    # stake. Without it the progression's first step ($0.19) would pay ~10.5% in
    # fees, and no realistic edge survives that — the engine would trade itself
    # broke while every individual decision looked reasonable.
    max_fee_fraction: float = 0.05


@dataclass
class HighConfidenceConfig:
    """The high-confidence trade filter (his standalone module, as briefed).

    Extremely selective by design: a candidate that survives the engine's own
    scoring must ALSO pass every check here before capital moves. Everything is
    configurable so thresholds can be optimised from recorded data, and the
    empirical gates carry his anti-overfitting rule: a setup's history is
    split chronologically and the recent, unseen part must not contradict the
    part the confidence came from.

    The one deliberate softening, stated plainly: in PAPER mode a setup with no
    accumulated history logs as "provisional" instead of being blocked —
    enforcing the sample gate on day one would mean no trades ever happen and
    no history can accumulate. In LIVE mode the empirical gates are strict.
    """

    enabled: bool = True
    # -- empirical gates -----------------------------------------------------
    min_setup_sample: int = 12          # closed comparable setups needed
    min_empirical_win: float = 0.62     # historical win share of the setup
    # The chronological hold-out: the newest fraction of the setup's history,
    # which must not fall more than the tolerated drop below the older part.
    validation_fraction: float = 0.3
    max_validation_drop: float = 0.15
    empirical_strict_in_paper: bool = False
    # -- structure gates -----------------------------------------------------
    min_expected_value: float = 0.02    # after spread, slippage and fees
    max_exhaustion: float = 55.0        # ms_exhaustion at or above this rejects
    # Entry is preferred while a move is being BORN, not after it happened:
    # allowed ms_ states for entry (developing / impulse / continuation).
    allowed_states: tuple = (1.0, 2.0, 3.0)
    require_state: bool = True
    min_depth_x_stake: float = 3.0      # visible depth must be >= 3x the stake
    max_spread: float = 0.08
    # -- contradictory evidence ----------------------------------------------
    # Each piece of contradicting structure counts 1 (reversal state, book
    # draining, imbalance against direction, abnormal spread, wallet flow
    # against). At or above the tolerance, the trade is rejected.
    contradiction_tolerance: int = 2
    # -- portfolio -----------------------------------------------------------
    max_category_share: float = 0.5     # of open exposure in one category


@dataclass
class EngineConfig:
    cycle_seconds: int = 20
    # "module.path:ClassName" of the decision engine. Point this at the Prop
    # Firm Quant Bridge's LEAN analysis layer to replace the bundled reference
    # engine; nothing else in this project needs to change. See bridge/ports.py.
    implementation: str = "pqb.bridge.baseline_engine:BaselineDecisionEngine"
    entry: EntryConfig = field(default_factory=EntryConfig)
    filter: HighConfidenceConfig = field(default_factory=HighConfidenceConfig)
    exits: ExitConfig = field(default_factory=ExitConfig)
    portfolio: PortfolioConfig = field(default_factory=PortfolioConfig)


@dataclass
class DoublingConfig:
    enabled: bool = True
    multiple: float = 2.0
    progression: list[float] = field(
        default_factory=lambda: list(DEFAULT_PROGRESSION))


@dataclass
class ReconciliationConfig:
    on_startup: bool = True
    # The brief requires reconciling every loop and halting on mismatch rather
    # than trading on stale state, so this defaults on and the interval below
    # only applies when it is turned off.
    every_loop: bool = True
    interval_seconds: int = 300
    halt_on_mismatch: bool = True
    halt_file: str = "state/HALT"
    size_tolerance: float = 0.01
    balance_tolerance: float = 0.01
    # A fill is visible to the CLOB before the Data API reports the position.
    # Without this grace period, reconciling immediately after an entry would
    # report the position "missing" and halt on every single trade.
    settle_grace_seconds: int = 120


@dataclass
class MarketStateConfig:
    """The Market-State layer between the event stream and LEAN (his spec).

    Deliberately simple: rolling windows, arithmetic, and thresholds that live
    HERE so they can be optimised from data later rather than exhumed from
    code. The layer classifies each market DORMANT -> DEVELOPING -> IMPULSE ->
    CONTINUATION -> EXHAUSTION -> REVERSAL and scores impulse / exhaustion /
    anomaly 0-100. It makes no trading decisions — LEAN consumes its output.
    """

    enabled: bool = True
    # A market with fewer prints than this across the long window is DORMANT.
    dormant_max_trades: int = 2
    # DEVELOPING needs activity at least this multiple of the market's own
    # baseline trade rate.
    developing_activity_x: float = 1.5
    # IMPULSE requires the impulse score at or above this.
    impulse_score_min: float = 55.0
    # CONTINUATION: impulse holds while the move keeps extending.
    continuation_score_min: float = 45.0
    # EXHAUSTION requires the exhaustion score at or above this while a real
    # move exists to exhaust.
    exhaustion_score_min: float = 55.0
    # REVERSAL: the short-window move runs against the long-window move by at
    # least this fraction of it.
    reversal_fraction: float = 0.4
    # |price change| over the long window that counts as a "real move" at all.
    min_move_pct: float = 0.02


@dataclass
class CapitalPreservationConfig:
    """Bounded caps that survive a bad sequence without silencing the edge.

    Separate from :class:`RiskConfig` on purpose. Those are absolute dollar
    limits an operator sets once; these are FRACTIONS of the account, so they
    keep meaning the same thing as the balance moves — which is exactly what a
    $100 account that is now $41.86 needs, and exactly what a fixed dollar cap
    stops doing the moment the account halves.

    Every value shrinks or blocks. None of them can increase a stake, and none
    of them can block an EXIT: `Runner._entry_block` applies the verdict to BUY
    decisions only. Defaults are deliberately loose enough not to change the
    behaviour of a healthy account and tight enough to stop a spiral.
    """

    enabled: bool = True
    # Never spend the last of the account. Below this share of equity in cash,
    # new entries stop; exits continue and refill it.
    min_cash_reserve_fraction: float = 0.10
    # Total mark-to-market of open positions, as a share of equity.
    max_total_exposure_fraction: float = 0.60
    # ...and the share any ONE cluster may hold, where a cluster is positions
    # sharing a market, a wallet thesis or a category. Four positions in one
    # category are one bet held four times, and counting them as four
    # independent risks is how a diversified-looking book takes one loss four
    # times over.
    max_cluster_fraction: float = 0.25
    # Below peak equity by this much: NEW ENTRIES stop entirely. Existing
    # positions are still managed and still exit. 0 disables.
    halt_entries_drawdown_pct: float = 0.45
    # The sizing ramp. Between these two drawdown levels the stake multiplier
    # falls linearly from 1.0 to `min_size_scale`, and it never rises: a loss
    # cannot increase the next stake, which is martingale behaviour made
    # structurally impossible rather than merely discouraged.
    shrink_from_drawdown_pct: float = 0.15
    shrink_to_drawdown_pct: float = 0.40
    min_size_scale: float = 0.40


@dataclass
class WalletStateResearchConfig:
    """The Wallet State Transition Research module — OFF by default.

    An isolated research subsystem studying what a wallet does after it first
    buys the OPPOSITE outcome in a market it was already in. It is not the
    same thing as `intel.wallet_state_*` above, which studies a different RN1
    hypothesis (first-buy price band + silence) and is unaffected by any value
    here.

    Two flags, not one, before any of it can reach a decision: `enabled` turns
    the RESEARCH on, and `integration_enabled` is separately required before
    `wallet_state_research.get_signal` returns anything but NO_SIGNAL. The same
    two-flag discipline that guards live trading, for the same reason — one
    careless edit must not be enough.

    With `enabled: false` (the default) the existing engine is bit-identical
    to the build before this module existed: nothing imports it on any hot
    path, and the one entry point returns a constant.
    """

    enabled: bool = False
    # research_only | backtest | walk_forward | paper | observe | influence.
    # The ladder in `wallet_state_research.signal.STAGES`. Only a person
    # editing this file can advance it.
    mode: str = "ResearchOnly"
    stage: str = "research_only"
    # RN1 STRATEGY MODEL V1 — the authoritative frozen model (entry-time,
    # three states). The +3m post-opposite rule below it is retained as a
    # SUPPORTING benchmark and is not the primary model any more.
    frozen_v1_enabled: bool = True
    # The clean prospective boundary: 2026-08-20 12:10 ET / 16:10 UTC. Do not
    # move it. Moving it invalidates every forward number that came before,
    # and the whole point of a frozen boundary is that it does not move.
    prospective_boundary_utc: str = "2026-08-20T16:10:00+00:00"
    prediction_freshness_minutes: float = 15.0
    # The frozen V1 thresholds. Present so they are auditable in one place,
    # NOT so they can be tuned: `strategy_v1` holds the authoritative
    # constants and a test pins them.
    initial_aggressive_price_threshold: float = 0.20
    initial_aggressive_capital_minimum: float = 5.00
    initial_directional_price_threshold: float = 0.80
    label_opposite_ratio_threshold: float = 1.40
    transitions_enabled: bool = True
    structure_discovery_enabled: bool = False
    paper_trading_enabled: bool = False
    live_execution_enabled: bool = False
    frozen_rn1_enabled: bool = True
    cross_wallet_enabled: bool = True
    cross_market_enabled: bool = True
    discovery_enabled: bool = False
    integration_enabled: bool = False
    minimum_wallet_samples: int = 12
    minimum_market_samples: int = 12
    signal_horizon_minutes: float = 3.0
    # Horizons compared against the frozen +3m. Reported, never used to
    # re-pick the frozen setting.
    extra_horizons: list = field(default_factory=lambda: [1.0, 5.0, 10.0])
    stakes: list = field(default_factory=lambda: [5.0, 10.0, 25.0])
    # Days of tape that must pass after an episode's last activity before its
    # lifecycle label is trusted. Below this the label says "we stopped
    # watching", not "the wallet stopped trading".
    label_quiet_days: float = 2.0
    walk_forward_folds: int = 4
    # 0 = every wallet. A cap is for a fast pass on a large store; RN1 is
    # always included regardless.
    max_wallets: int = 0
    min_wallet_trades: int = 20
    output_dir: str = "state/research/wallet_state"


@dataclass
class RiskConfig:
    """Hard pre-trade limits. A breach blocks NEW ENTRIES; it never blocks an
    exit — getting out of a position is how you *reduce* risk, so a risk halt
    that also trapped you in a loser would defeat its own purpose.

    All limits are in USDC and default to ``0.0`` meaning *disabled*, so an
    existing install is unchanged until the operator sets them deliberately.
    They are enforced in code, not documentation: the single-order cap rejects
    in the execution layer, and exposure / daily-loss block entries in the run
    loop (see ``Runner._entry_block``).
    """

    # Largest a single order may be. An order over this is rejected outright,
    # not silently shrunk — a size that large is a signal something upstream is
    # wrong, and quietly trimming it would hide that.
    max_single_order_usdc: float = 0.0
    # Total mark-to-market value of open positions. At or above this, no new
    # entry is opened until exits bring it back down.
    max_open_exposure_usdc: float = 0.0
    # Loss since the start of the UTC day (portfolio value at day-start minus
    # now). At or beyond this, entries stop for the rest of the day; exits still
    # run. Resets at the next UTC midnight.
    max_daily_loss_usdc: float = 0.0
    # A client-generated order id de-duplicates an identical order re-emitted
    # within this window, so a transient-retry or a repeated cycle cannot double
    # a position. Keyed on (token, side, price, size).
    idempotency_ttl_seconds: int = 120
    # Fraction-of-equity caps that keep meaning the same thing as the balance
    # moves. See the class docstring for why these are not merged into the
    # dollar limits above.
    preservation: CapitalPreservationConfig = field(
        default_factory=CapitalPreservationConfig)


@dataclass
class LiveGateConfig:
    """The code-enforced gate between paper and live (§3).

    Live trading is not merely a flag: it is blocked until the paper-mode record
    actually demonstrates an edge. This is checked in ``Runner.start`` on every
    live start, and a failure downgrades the session to paper rather than
    trading — a guideline that only lived in a document would be no gate at all.
    """

    require: bool = True
    # Settled predictions needed before the gate can even be judged. Below this
    # the sample is too small to prove anything, so live stays blocked.
    min_settled: int = 50
    # Realised P&L over closed trades must be at least this (default: positive).
    min_total_pnl_usdc: float = 0.0
    # Worst peak-to-trough on the realised sequence must be shallower than this.
    max_drawdown_usdc: float = 25.0
    # Our Brier score must beat the market's — the evidence the edge is real and
    # not luck. Without this a model that merely got lucky could arm itself.
    require_beat_market: bool = True


@dataclass
class StorageConfig:
    data_dir: str = "state"
    journal_db: str = "state/journal.sqlite3"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str = "state/pqb.log"
    max_bytes: int = 2_000_000
    backups: int = 5


def _cascade_default():
    from .analytics.cascade import CascadeConfig
    return CascadeConfig()


@dataclass
class Config:
    polymarket: PolymarketConfig = field(default_factory=PolymarketConfig)
    mode: ModeConfig = field(default_factory=ModeConfig)
    markets: MarketsConfig = field(default_factory=MarketsConfig)
    wallets: list[WalletConfig] = field(default_factory=list)
    intel: IntelConfig = field(default_factory=IntelConfig)
    research: ResearchConfig = field(default_factory=ResearchConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    doubling: DoublingConfig = field(default_factory=DoublingConfig)
    reconciliation: ReconciliationConfig = field(
        default_factory=ReconciliationConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    wallet_state_research: WalletStateResearchConfig = field(
        default_factory=WalletStateResearchConfig)
    market_state: MarketStateConfig = field(default_factory=MarketStateConfig)
    live_gate: LiveGateConfig = field(default_factory=LiveGateConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    # The liquidation-cascade hypothesis (operator's candidate): capture and
    # measurement only — see pqb.analytics.cascade. Lives here so its
    # thresholds are visibly starting points, never hidden constants.
    cascade: "CascadeConfig" = field(
        default_factory=lambda: _cascade_default())

    # Filled in by :func:`load`; everything relative resolves against it.
    root: Path = field(default_factory=Path.cwd)
    source_path: Optional[Path] = None

    # -- derived paths -------------------------------------------------------

    def build_version(self) -> str:
        """The shipped build stamp, or 'dev' when running from source.

        Written by the packaging step as VERSION.txt beside the project.
        Shown in the dashboard title and the boot log so 'which build are
        you actually running?' is answerable from a screenshot — the
        question every 'nothing changed' report turns on.
        """
        for candidate in (self.root / "VERSION.txt",
                          self.root.parent / "VERSION.txt"):
            try:
                text = candidate.read_text(encoding="utf-8").strip()
                if text:
                    return text.splitlines()[0][:60]
            except OSError:
                continue
        return "dev"

    def nested_install(self) -> Optional[Path]:
        """Detect the Windows Extract-All trap: the update extracted INTO
        the old install, leaving a fresh copy of the whole bundle nested
        one level down — `...\\Polymarket-Bot-DAVID\\Polymarket-Bot-DAVID`.
        The operator then launches the OLD code and reports 'nothing
        changed'. The signature: a folder inside the install carrying the
        install's own name and containing the bridge project."""
        install = self.root.parent
        nested = install / install.name / "polymarket-quant-bridge"
        if nested.is_dir() and nested.resolve() != self.root.resolve():
            return nested.parent
        return None

    def path(self, relative: str) -> Path:
        p = Path(relative).expanduser()
        out = p if p.is_absolute() else (self.root / p)
        # Resolve ".." components (the externalized-state paths are full of
        # them). Unresolved chains through the install directory made every
        # nested research path LONGER, which is how deep installs hit
        # Windows' 260-character limit (WinError 3) building export dirs.
        try:
            return out.resolve()
        except OSError:
            return out

    @property
    def data_dir(self) -> Path:
        return self.path(self.storage.data_dir)

    @property
    def journal_path(self) -> Path:
        return self.path(self.storage.journal_db)

    @property
    def intel_path(self) -> Path:
        return self.path(self.intel.db)

    @property
    def log_path(self) -> Path:
        return self.path(self.logging.file)

    @property
    def kill_switch_path(self) -> Path:
        return self.path(self.mode.kill_switch_file)

    @property
    def stop_path(self) -> Path:
        return self.path(self.mode.stop_file)

    @property
    def halt_path(self) -> Path:
        return self.path(self.reconciliation.halt_file)

    # -- validation ----------------------------------------------------------

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.mode.live and not self.polymarket.private_key:
            problems.append(
                "Live trading is enabled but no private key resolved. Set the "
                "environment variable referenced by polymarket.private_key.")
        if self.engine.cycle_seconds < 1:
            problems.append("engine.cycle_seconds must be at least 1.")
        if not self.doubling.progression:
            problems.append("doubling.progression must not be empty.")
        if self.doubling.multiple <= 1.0:
            problems.append("doubling.multiple must be greater than 1.")
        for w in self.wallets:
            addr = (w.address or "").strip()
            if not addr.startswith("0x") or len(addr) != 42:
                problems.append(f"Target wallet '{addr}' is not a 0x… address.")
        if not (0 < self.engine.portfolio.max_position_fraction <= 1):
            problems.append(
                "engine.portfolio.max_position_fraction must be in (0, 1].")
        if not (0.0 <= self.research.uncertain_min_price
                < self.research.uncertain_max_price <= 1.0):
            problems.append(
                "research.uncertain_min_price must be below "
                "research.uncertain_max_price, both within 0..1.")
        if self.research.assumed_spread < 0:
            problems.append("research.assumed_spread must not be negative.")
        if self.engine.exits.reduce_fraction <= 0 or \
                self.engine.exits.reduce_fraction >= 1:
            problems.append(
                "engine.exits.reduce_fraction must be between 0 and 1.")
        if self.intel.enabled:
            if self.intel.cohort_size < 0:
                problems.append("intel.cohort_size must not be negative.")
            if self.intel.lookback_days <= 0:
                problems.append("intel.lookback_days must be greater than 0.")
            if 0 < self.intel.retention_days < self.intel.lookback_days:
                # Retention shorter than the lookback silently shortens every
                # wallet's history: ranking would ask for 30 days and receive
                # whatever survived pruning, with nothing reporting the gap.
                problems.append(
                    f"intel.retention_days ({self.intel.retention_days}) is "
                    f"shorter than intel.lookback_days "
                    f"({self.intel.lookback_days}); ranking would silently see "
                    "less history than it asks for.")
        return problems

    def redacted(self) -> dict:
        """The only representation of the config that may be logged."""
        return _redact(_to_plain(self))


# --- loading ----------------------------------------------------------------

def load_env_file(path: Path) -> dict[str, str]:
    """Minimal ``.env`` reader (``KEY=value``, ``#`` comments, optional quotes).

    Deliberately not python-dotenv: one small function beats another dependency
    for a file format this simple, and it keeps the loader auditable — this code
    handles secrets.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def _resolve_env(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match) -> str:
        name, default = match.group(1), match.group(2)
        found = env.get(name, os.environ.get(name))
        if found is None or found == "":
            return default if default is not None else ""
        return found
    return _ENV_PATTERN.sub(replace, value)


def _walk_resolve(node: Any, env: dict[str, str]) -> Any:
    if isinstance(node, str):
        return _resolve_env(node, env)
    if isinstance(node, dict):
        return {k: _walk_resolve(v, env) for k, v in node.items()}
    if isinstance(node, list):
        return [_walk_resolve(v, env) for v in node]
    return node


def _read_structured(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise RuntimeError(
                "PyYAML is required to read a .yaml config. Either "
                "`pip install pyyaml` or use a .json config file."
            ) from exc
        return yaml.safe_load(text) or {}
    return json.loads(text or "{}")


def _build(cls, data: Any):
    """Instantiate one flat dataclass from plain config data.

    Unknown keys are ignored rather than fatal: a config written for a newer
    build should still start, not crash on an option this version predates.
    Nested sections are assembled by the ``_build_*`` helpers below — with
    ``from __future__ import annotations`` in effect, field types are strings
    here, so nesting cannot be detected reflectively.
    """
    if not is_dataclass(cls) or not isinstance(data, dict):
        return data
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


def load(path: str | Path) -> Config:
    """Read, env-resolve and validate a config file."""
    config_path = Path(path).expanduser().resolve()
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    root = config_path.parent
    # A .env beside the config, or one directory up (the usual project root).
    env: dict[str, str] = {}
    for candidate in (root.parent / ".env", root / ".env"):
        env.update(load_env_file(candidate))

    raw = _walk_resolve(_read_structured(config_path), env)
    if not isinstance(raw, dict):
        raise ValueError("The config file must contain a mapping at the top level.")

    cfg = Config(
        polymarket=_build(PolymarketConfig, raw.get("polymarket", {})),
        mode=_build(ModeConfig, raw.get("mode", {})),
        markets=_build_markets(raw.get("markets", {})),
        wallets=[_build(WalletConfig, w) for w in raw.get("wallets", []) or []],
        intel=_build_intel(raw.get("intel", {})),
        research=_build(ResearchConfig, raw.get("research", {})),
        engine=_build_engine(raw.get("engine", {})),
        doubling=_build(DoublingConfig, raw.get("doubling", {})),
        reconciliation=_build(ReconciliationConfig,
                              raw.get("reconciliation", {})),
        risk=_build_risk(raw.get("risk", {})),
        wallet_state_research=_build(
            WalletStateResearchConfig,
            raw.get("wallet_state_research", {})),
        market_state=_build(MarketStateConfig, raw.get("market_state", {})),
        live_gate=_build(LiveGateConfig, raw.get("live_gate", {})),
        storage=_build(StorageConfig, raw.get("storage", {})),
        logging=_build(LoggingConfig, raw.get("logging", {})),
        cascade=_build(_cascade_default().__class__,
                       raw.get("cascade", {})),
    )
    # Project root is the config's parent directory, or its grandparent when the
    # config lives in the conventional `config/` subfolder.
    cfg.root = root.parent if root.name == "config" else root
    cfg.source_path = config_path
    _adopt_external_state(cfg)
    return cfg


def _adopt_external_state(cfg: Config) -> None:
    """One-time move of install-local state to an EXTERNAL data folder.

    The shipped config points every state path outside the install folder
    (../../Polymarket-Bot-DATA/state), so an update — even extracting the
    zip to a brand-new folder — can never orphan the strategy library, the
    captured history, or the journal. An existing install's state/ folder
    is adopted by MOVING it there on the first start of an external-state
    build; nothing is copied, nothing duplicated, nothing left behind to
    go stale. Runs on every config load and is a no-op ever after.
    """
    try:
        target = cfg.data_dir
        legacy = (cfg.root / "state").resolve()
        if target.resolve() == legacy:
            return                      # in-install state configured (dev)
        if target.exists() and any(target.iterdir()):
            return                      # external state already in use
        if not legacy.exists() or not any(legacy.iterdir()):
            return                      # fresh install: nothing to adopt
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.rmdir()              # empty placeholder only
        shutil.move(str(legacy), str(target))
        (target / "MIGRATED-FROM.txt").write_text(
            f"State moved here from {legacy} when the external-state build "
            "first started.\nUpdates replace only the program folder; this "
            "folder is never touched by an update.\n", encoding="utf-8")
    except Exception:                                    # noqa: BLE001
        # A failed move must never cost the operator their library: fall
        # back to the in-install state for this run instead of silently
        # starting an empty one next to a full one.
        cfg.storage.data_dir = "state"
        cfg.storage.journal_db = "state/journal.sqlite3"
        cfg.intel.db = "state/intel.sqlite3"
        cfg.mode.kill_switch_file = "state/KILL"
        cfg.logging.file = "state/pqb.log"


def _build_markets(data: dict) -> MarketsConfig:
    markets = _build(MarketsConfig, data)
    if isinstance(data, dict) and isinstance(data.get("filters"), dict):
        markets.filters = _build(MarketFilters, data["filters"])
    return markets


def _build_intel(data: dict) -> IntelConfig:
    intel = _build(IntelConfig, data)
    if isinstance(data, dict) and isinstance(data.get("anomalies"), dict):
        intel.anomalies = _build(AnomalyConfig, data["anomalies"])
    return intel


def _build_risk(data: dict) -> RiskConfig:
    """Risk, with its nested capital-preservation block.

    Same reason as `_build_engine`: `from __future__ import annotations` makes
    every field type a string here, so a nested dataclass cannot be detected
    reflectively and has to be assembled explicitly. Without this the
    preservation block arrives as a plain dict and every attribute read on it
    fails at the first cycle — silently, in a thread, on the path that is
    supposed to protect the account.
    """
    risk = _build(RiskConfig, data)
    if isinstance(data, dict) and isinstance(data.get("preservation"), dict):
        risk.preservation = _build(CapitalPreservationConfig,
                                   data["preservation"])
    return risk


def _build_engine(data: dict) -> EngineConfig:
    engine = _build(EngineConfig, data)
    if isinstance(data, dict):
        if isinstance(data.get("entry"), dict):
            engine.entry = _build(EntryConfig, data["entry"])
        if isinstance(data.get("exits"), dict):
            engine.exits = _build(ExitConfig, data["exits"])
        if isinstance(data.get("portfolio"), dict):
            engine.portfolio = _build(PortfolioConfig, data["portfolio"])
        if isinstance(data.get("filter"), dict):
            engine.filter = _build(HighConfidenceConfig, data["filter"])
            allowed = engine.filter.allowed_states
            if isinstance(allowed, list):        # YAML lists arrive as lists
                engine.filter.allowed_states = tuple(float(s) for s in allowed)
    return engine


# --- redaction --------------------------------------------------------------

def _to_plain(node: Any) -> Any:
    if is_dataclass(node) and not isinstance(node, type):
        return {f.name: _to_plain(getattr(node, f.name)) for f in fields(node)}
    if isinstance(node, dict):
        return {k: _to_plain(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_to_plain(v) for v in node]
    if isinstance(node, Path):
        return str(node)
    return node


def _is_secret(key: str) -> bool:
    lowered = key.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _redact(node: Any) -> Any:
    if isinstance(node, dict):
        return {
            k: ("***set***" if _is_secret(k) and node[k] else
                ("" if _is_secret(k) else _redact(v)))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_redact(v) for v in node]
    return node
